"""
Keep in background: with the "background_spares" pref above 0, a manager process keeps that many spare aplite
processes with the worlds already loaded, and each new aplite open hands its terminal to one of them. Only the first
open since boot (or since worlds/host.yaml/aplite changed) waits for the world load.

- Thin client: the `python -m aplite` the user started. try_handoff() sends its pid, cwd, argv and environment (on
  Linux also its terminal fds), waits for the spare to exit and exits with its code. RELAUNCH (back to the launcher)
  asks for another spare.
- Manager (`python -m aplite.background`): no worlds, stdlib only. Starts spares one at a time (loads in parallel
  would slow each other down), gives an open to the oldest, reports exit codes. Quits when the pref goes back to 0,
  once the opens it handed out have ended.
- Spare (`python -m aplite --spare`): loads worlds in a thread and takes the first open, even mid-load (the launcher
  shows its cached list meanwhile): attaches to the opener's console (Windows) or takes its fds (Linux), adopts its
  cwd, environment and argv, then runs it like a normal start.

Windows and Linux only. Stdlib only: the thin client runs this before anything heavy is imported.
"""
from __future__ import annotations

import getpass
import hashlib
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import typing

if typing.TYPE_CHECKING:
    from multiprocessing.connection import Connection, Listener

PREF = "background_spares"
MAX_SPARES = 3
RELAUNCH = 75
"""Exit code for "start again, fresh" (coming back to the launcher); aplite.cmd uses the same one."""
SUPPORTED = sys.platform in ("win32", "linux")
FAMILY = "AF_PIPE" if sys.platform == "win32" else "AF_UNIX"
SPARE_ARG = "--spare"

in_spare = False
"""True in a spare process (set by serve_spare)."""

APPIMAGE = os.environ.get("APPIMAGE") if os.path.isfile(os.environ.get("APPIMAGE") or "") else None
"""The AppImage file when running from one (its runtime sets $APPIMAGE). Each run mounts it at a new path that goes
away when that run ends, so the manager's address and the update checks go by this file, and the manager and spares
start through it (each with a mount of its own, outliving the opener's)."""

logger = logging.getLogger("aplite.background")


def address() -> str | None:
    """The manager's pipe/socket: one per user, machine (containers share /tmp with their host) and install. None when
    no private folder for the socket can be had (Linux)."""
    from aplite import REPO
    key = hashlib.sha1(f"{getpass.getuser()}|{socket.gethostname()}|{APPIMAGE or REPO}".encode()).hexdigest()[:16]
    if sys.platform == "win32":
        return rf"\\.\pipe\aplite-{key}"
    folder = os.environ.get("XDG_RUNTIME_DIR") or os.path.join(tempfile.gettempdir(), f"aplite-{os.getuid()}")
    try:
        os.makedirs(folder, mode=0o700, exist_ok=True)
        info = os.lstat(folder)
    except OSError:
        return None
    # someone else's folder (made first in a shared /tmp) could hold a fake manager that gets our terminal and env
    if info.st_uid != os.getuid() or info.st_mode & 0o077 or not os.path.isdir(folder) or os.path.islink(folder):
        return None
    return os.path.join(folder, f"aplite-{key}.sock")


def connect() -> Connection:
    """A connection to the running manager; OSError when there is none."""
    path = address()
    if path is None:
        raise OSError("no private folder for the background socket")
    from multiprocessing.connection import Client  # here: every aplite start imports this module
    return Client(path, family=FAMILY)


def request(message: dict) -> typing.Any:
    """One request/reply with the manager; OSError/EOFError when it isn't running."""
    with connect() as conn:
        conn.send(message)
        return conn.recv()


def running() -> bool:
    try:
        return request({"op": "status"}) is not None
    except (OSError, EOFError):
        return False


def python_command(*args: str) -> list[str]:
    """Command line for a Python process that may outlive this one: through the AppImage (see APPIMAGE; its launcher
    takes --python), else this interpreter."""
    return [APPIMAGE, "--python", *args] if APPIMAGE else [sys.executable, *args]


def child_env() -> dict[str, str]:
    from aplite import child_env as env
    return env()


def relaunch() -> typing.NoReturn:
    """Start aplite again, fresh, in this terminal (back to the launcher without leftovers from the last component)."""
    sys.stdout.flush()
    if sys.platform == "win32":  # no real exec there: the new process would race the shell for the console
        if os.environ.get("APLITE_RELAUNCH"):  # aplite.cmd (Windows dist) starts us again on this exit code
            sys.exit(int(os.environ["APLITE_RELAUNCH"]))
        sys.exit(subprocess.call([sys.executable, "-m", "aplite"], env=child_env()))
    os.execve(sys.executable, [sys.executable, "-m", "aplite"], child_env())


# --- thin client ---------------------------------------------------------------------------------------------------

def try_handoff(argv: list[str]) -> None:
    """First thing `python -m aplite` does: when a manager is running, a spare runs this open and this process exits
    with its exit code. Returns without doing anything otherwise."""
    if not SUPPORTED or os.environ.get("APLITE_NO_BACKGROUND"):
        return
    if argv[:1] and argv[0] in (SPARE_ARG, "-h", "--help", "--list", "--version"):
        return
    try:  # a spare needs a console/terminal to take over (not pipes or files)
        if not (os.isatty(0) and os.isatty(1)):
            return
    except OSError:
        return
    try:
        conn = connect()
    except OSError:
        return
    code = _open_in_spare(conn, argv)
    while code == RELAUNCH:  # back to the launcher: the next spare
        try:
            conn = connect()
        except OSError:
            relaunch()
        code = _open_in_spare(conn, [])
        if code is None:
            relaunch()
    if code is not None:
        sys.exit(code)


def _open_in_spare(conn: Connection, argv: list[str]) -> int | None:
    """Run this open in a spare and return its exit code; None when the manager declined (open normally)."""
    with conn:
        try:
            conn.send({"op": "open", "pid": os.getpid(), "cwd": os.getcwd(), "argv": argv, "env": dict(os.environ)})
            if sys.platform != "win32":
                send_fds(conn, [0, 1, 2])
            reply = conn.recv()
        except (OSError, EOFError):
            return None
        if "pid" not in reply:
            return None
        spare = reply["pid"]
        _leave_terminal_to(spare)
        try:
            return conn.recv()["exit"]  # the manager sends it when the spare exits
        except (OSError, EOFError):  # the manager went away: never give the shell the terminal while the spare has it
            _wait_for_exit(spare)
            return 1


def _leave_terminal_to(pid: int) -> None:
    """Stop reacting to the terminal: the spare owns it now. Linux: pass on what only the foreground process (us)
    gets from the terminal: resizes, Ctrl+C outside raw mode, hangups."""
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetConsoleCtrlHandler(None, True)  # Ctrl+C reaches the spare (same console) directly
        return
    import signal

    def forward(signum, _frame) -> None:
        try:
            os.kill(pid, signum)
        except OSError:
            pass
    for sig in (signal.SIGWINCH, signal.SIGINT, signal.SIGQUIT, signal.SIGHUP, signal.SIGTERM, signal.SIGCONT):
        signal.signal(sig, forward)
    for sig in (signal.SIGTSTP, signal.SIGTTIN, signal.SIGTTOU):  # a stopped thin client would hand the shell the tty
        signal.signal(sig, signal.SIG_IGN)


def _wait_for_exit(pid: int) -> None:
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if handle:
            kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0xFFFFFFFF)
            kernel32.CloseHandle(ctypes.c_void_p(handle))
        return
    while os.path.exists(f"/proc/{pid}"):  # not our child, so no waitpid (and no os.pidfd_open on musl builds)
        time.sleep(0.2)


def send_fds(conn: Connection, fds: list[int]) -> None:
    """Pass fds along a Connection's unix socket (Connection reads exact message sizes, so this byte stays separate)."""
    with socket.socket(fileno=os.dup(conn.fileno())) as sock:
        socket.send_fds(sock, [b"F"], fds)


def recv_fds(conn: Connection, count: int) -> list[int]:
    with socket.socket(fileno=os.dup(conn.fileno())) as sock:
        _, fds, _, _ = socket.recv_fds(sock, 1, count)
    return fds


# --- spare ---------------------------------------------------------------------------------------------------------

def spare_start_load(load: typing.Callable[[], object]) -> None:
    """In a spare: load worlds in a thread, then tell the manager (it starts the next spare only then)."""
    def run() -> None:
        try:
            load()
        except BaseException:
            logger.exception("Loading worlds failed in a background spare")
        try:
            request({"op": "loaded", "pid": os.getpid()})
        except (OSError, EOFError):
            pass
    threading.Thread(target=run, name="spare-load", daemon=True).start()


def serve_spare(fingerprint: typing.Callable[[], object]) -> None:
    """In a spare: wait for an open, take over its terminal and adopt its cwd, environment and argv, then return (the
    caller runs it as a normal start). Exits when the manager goes away, or when `fingerprint()` (worlds, settings,
    aplite itself) changed since this spare started (the manager starts a fresh one)."""
    global in_spare
    in_spare = True
    started_with = fingerprint()
    try:
        conn = connect()
        conn.send({"op": "spare", "pid": os.getpid()})
        message = conn.recv()
        fds = recv_fds(conn, 3) if sys.platform != "win32" else []
        if fingerprint() != started_with:
            conn.send({"stale": True})
            sys.exit(0)
        conn.send({"ok": True})
        conn.close()
    except (OSError, EOFError):
        sys.exit(0)
    if sys.platform == "win32":
        _attach_console(message["pid"])
    else:
        _adopt_fds(fds)
    os.chdir(message["cwd"])
    os.environ.clear()
    os.environ.update(message["env"])
    sys.argv[1:] = message["argv"]


def _attach_console(pid: int) -> None:
    """Move to the opener's console. The world-loading thread may be printing meanwhile: fds 0-2 never sit free (an
    import could take one) and are never closed while they hold the old console's handles (closing those after
    FreeConsole crashed Python in Windows Terminal, 0xC0000409)."""
    import ctypes
    import msvcrt
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    devnull = os.open(os.devnull, os.O_RDWR)
    sys.stdout = sys.stderr = open(os.devnull, "w")
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)  # closes the old console's handles while they're still valid
    os.close(devnull)
    kernel32.FreeConsole()
    if not kernel32.AttachConsole(pid):
        raise OSError(ctypes.get_last_error(), "AttachConsole failed")
    conin = os.open("CONIN$", os.O_RDWR)
    conout = os.open("CONOUT$", os.O_RDWR)
    for new, fds in ((conin, (0,)), (conout, (1, 2))):
        for fd in fds:
            os.dup2(new, fd)
        os.close(new)
    kernel32.SetStdHandle.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    for std, fd in ((-10, 0), (-11, 1), (-12, 2)):  # Textual's Windows driver uses GetStdHandle
        kernel32.SetStdHandle(std & 0xFFFFFFFF, msvcrt.get_osfhandle(fd))
    _reopen_std_streams("utf-8")


def _adopt_fds(fds: list[int]) -> None:
    encoding = sys.__stdout__.encoding if sys.__stdout__ else "utf-8"
    for fd, target in zip(fds, (0, 1, 2)):
        os.dup2(fd, target)  # atomic: the old /dev/null is closed in the same step
        os.close(fd)
    _reopen_std_streams(encoding)


def _reopen_std_streams(encoding: str) -> None:
    """New sys.std* for fds 0-2 (the old ones were made for a non-terminal: block-buffered, no console I/O), and the
    log handlers that wrote to the old stdout moved over."""
    old = {id(sys.stdout), id(sys.__stdout__), id(sys.stderr), id(sys.__stderr__)}
    sys.stdin = sys.__stdin__ = open(0, "r", encoding=encoding, closefd=False)
    sys.stdout = sys.__stdout__ = open(1, "w", encoding=encoding, errors="replace", buffering=1, closefd=False)
    sys.stderr = sys.__stderr__ = open(2, "w", encoding=encoding, errors="backslashreplace", buffering=1,
                                       closefd=False)
    for logger_ in (logging.getLogger(), *logging.Logger.manager.loggerDict.values()):
        for handler in getattr(logger_, "handlers", ()):
            if type(handler) is logging.StreamHandler and id(handler.stream) in old:
                handler.setStream(sys.stdout)


def tree_fingerprint(dirs: list[str], files: list[str]) -> list:
    """What a spare's loaded state depends on: the entries of `dirs` and the `files` (names, sizes, mtimes)."""
    result = []
    for folder in dirs:
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.name == "__pycache__" or entry.name.endswith((".pyc", ".tmp")):
                        continue  # written by imports (and by spares themselves)
                    info = entry.stat()
                    result.append((folder, entry.name, info.st_size, info.st_mtime_ns))
        except OSError:
            result.append((folder, None))
    for path in files:
        try:
            info = os.stat(path)
            result.append((path, info.st_size, info.st_mtime_ns))
        except OSError:
            result.append((path, None))
    return sorted(result, key=repr)


# --- settings side (any aplite process) ----------------------------------------------------------------------------

def configure(count: int, log_path: str) -> None:
    """Apply a new spare count: start the manager, change its count, or (0) tell it to quit."""
    try:
        request({"op": "config", "count": count})
        return
    except (OSError, EOFError):
        if count <= 0:
            return
    start_manager(count, log_path)


def start_manager(count: int, log_path: str) -> None:
    from aplite import REPO
    args = python_command("-m", "aplite.background", str(count), log_path)
    cwd = os.path.expanduser("~") if APPIMAGE else REPO  # not in this run's mount: it would keep it busy
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    # its output to the log too: a manager that dies before it logs (a launcher or AppImage runtime error, a
    # traceback) leaves a reason there
    with open(log_path, "ab") as output:
        kwargs: dict[str, typing.Any] = dict(stdin=subprocess.DEVNULL, stdout=output, stderr=output, env=child_env(),
                                             cwd=cwd, close_fds=True)
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            try:  # out of the terminal's job, if it has one that allows it, so closing the window doesn't take it along
                subprocess.Popen(args, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kwargs)
                return
            except OSError:
                subprocess.Popen(args, creationflags=flags, **kwargs)
        else:
            subprocess.Popen(args, start_new_session=True, **kwargs)


def memory_bytes() -> int | None:
    """This process's resident memory (a spare holds about as much as an aplite with its worlds loaded)."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                    (name, ctypes.c_size_t) for name in (
                        "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                        "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            kernel32 = ctypes.windll.kernel32
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            if not kernel32.K32GetProcessMemoryInfo(ctypes.c_void_p(kernel32.GetCurrentProcess()),
                                                    ctypes.byref(counters), counters.cb):
                return None
            return counters.WorkingSetSize
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, AttributeError):
        pass
    return None


# --- manager -------------------------------------------------------------------------------------------------------

def code_stamp() -> list:
    """This file and the dist's _version.txt (sizes, mtimes), and the AppImage file: any aplite update rewrites them,
    and a manager whose code was replaced under it steps down rather than talk to a newer thin client with old code."""
    folder = os.path.dirname(os.path.abspath(__file__))
    return tree_fingerprint([], [os.path.abspath(__file__), os.path.join(folder, "_version.txt"), *install_files()])


def install_files() -> list[str]:
    """Files an update replaces outside the loaded code: the AppImage (its mounts keep showing the old contents)."""
    return [APPIMAGE] if APPIMAGE else []


class Spare:
    def __init__(self, process: subprocess.Popen) -> None:
        self.process = process
        self.conn: Connection | None = None
        """Set when the spare is up and waiting for an open."""
        self.loaded = False


class Manager:
    FAILURE_LIMIT = 3
    """Spares in a row that died before taking an open: stop starting them (opens load normally)."""

    def __init__(self, count: int) -> None:
        self.count = count
        self.spares: list[Spare] = []
        """Not yet handed out, oldest first."""
        self.active = 0
        """Opens handed to a spare that haven't ended."""
        self.loading: set[int] = set()
        """Spares still loading worlds, handed out or not: the next one starts after (loads compete for the CPU)."""
        self.stopping = False
        self.failures = 0
        self.cond = threading.Condition()
        self.path = ""
        self.stamp = code_stamp()

    def serve(self, path: str) -> None:
        self.path = path
        listener = self._listen(path)
        if listener is None:
            return
        logger.info(f"background manager {os.getpid()} up, {self.count} spare(s)")
        with self.cond:
            self._top_up()
        while True:
            try:
                conn = listener.accept()
            except OSError:  # a client that gave up while connecting
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _listen(self, path: str) -> Listener | None:
        from multiprocessing.connection import Listener
        if sys.platform == "win32":
            try:  # multiprocessing makes the pipe with FILE_FLAG_FIRST_PIPE_INSTANCE: a second manager can't
                return Listener(path, family=FAMILY)
            except OSError:
                logger.info("another background manager is running")
                return None
        import fcntl
        self._lock = open(path + ".lock", "w")
        try:  # held for life; the socket file of a dead manager can go then
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            logger.info("another background manager is running")
            return None
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return Listener(path, family=FAMILY)

    def _top_up(self) -> None:
        """Start a spare when there are fewer than `count` and none is loading (one load at a time). Holds cond."""
        if self.stopping or self.failures >= self.FAILURE_LIMIT:
            return
        if len(self.spares) < self.count and not self.loading:
            self._start_spare()

    def _start_spare(self) -> Spare:
        args = python_command("-m", "aplite", SPARE_ARG)
        kwargs: dict[str, typing.Any] = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL, env=child_env())
        if sys.platform == "win32":
            # its own hidden console to detach from; not a new process group (that would turn Ctrl+C off)
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        spare = Spare(subprocess.Popen(args, **kwargs))
        self.spares.append(spare)
        self.loading.add(spare.process.pid)
        threading.Thread(target=self._watch, args=(spare,), daemon=True).start()
        logger.info(f"started spare {spare.process.pid}")
        return spare

    def _watch(self, spare: Spare) -> None:
        """A spare that exits before taking an open (crashed, killed, stale) is replaced."""
        spare.process.wait()
        with self.cond:
            self.loading.discard(spare.process.pid)
            if spare in self.spares:
                self.spares.remove(spare)
                self.failures += 1
                logger.warning(f"spare {spare.process.pid} exited ({spare.process.returncode}) before an open")
            self._top_up()
            self.cond.notify_all()

    def _handle(self, conn: Connection) -> None:
        try:
            message = conn.recv()
            op = message.get("op")
            if op == "spare":
                self._register(conn, message["pid"])
                return  # conn stays open: it carries the open later
            if op == "open":
                fds = recv_fds(conn, 3) if sys.platform != "win32" else []
                if code_stamp() != self.stamp:
                    for fd in fds:
                        os.close(fd)
                    self._step_down(conn)
                    return
                try:
                    self._open(conn, message, fds)
                finally:
                    for fd in fds:
                        os.close(fd)
                return
            if op == "loaded":
                with self.cond:
                    self.loading.discard(message["pid"])
                    for spare in self.spares:
                        if spare.process.pid == message["pid"]:
                            spare.loaded = True
                            self.failures = 0
                    self._top_up()
                    self.cond.notify_all()
                conn.send(True)
            elif op == "config":
                self._configure(int(message["count"]))
                conn.send(True)
            elif op == "status":
                with self.cond:
                    conn.send({"pid": os.getpid(), "count": self.count, "active": self.active,
                               "spares": [(s.process.pid, s.loaded, s.conn is not None) for s in self.spares]})
            conn.close()
        except (OSError, EOFError):
            conn.close()
        except Exception:
            logger.exception("background manager request failed")
            conn.close()

    def _register(self, conn: Connection, pid: int) -> None:
        with self.cond:
            spare = next((s for s in self.spares if s.process.pid == pid), None)
            if spare is None:
                conn.close()
                return
            spare.conn = conn
            logger.info(f"spare {pid} waiting for an open")
            self.cond.notify_all()

    def _open(self, conn: Connection, message: dict, fds: list[int]) -> None:
        for _attempt in range(3):
            spare = self._take_spare()
            if spare is None:
                conn.send({"declined": True})
                conn.close()
                return
            try:
                spare.conn.send(message)
                if fds:
                    send_fds(spare.conn, fds)
                reply = spare.conn.recv()
            except (OSError, EOFError):
                reply = {}
            spare.conn.close()
            if reply.get("ok"):
                break
            if reply.get("stale"):  # worlds/settings/aplite changed: the other spares are stale too
                logger.info("spares are out of date; starting fresh ones")
                with self.cond:
                    for other in self.spares:
                        other.process.kill()
                    self.spares.clear()
            self._end(spare)
        else:
            conn.send({"declined": True})
            conn.close()
            return
        conn.send({"pid": spare.process.pid})
        logger.info(f"spare {spare.process.pid} took an open for {message['pid']}")
        code = spare.process.wait()
        logger.info(f"spare {spare.process.pid} ran an open for {message['pid']}, exit {code}")
        try:
            conn.send({"exit": code})
        except OSError:
            pass
        conn.close()
        self._end(spare, ended_open=True)

    def _take_spare(self) -> Spare | None:
        """The oldest waiting spare (loaded ones first), starting one if none is coming. None when stopping."""
        with self.cond:
            if self.stopping or self.failures >= self.FAILURE_LIMIT:
                return None
            if not self.spares:
                self._start_spare()
            deadline = time.monotonic() + 120
            while True:
                if self.stopping:
                    return None
                ready = [s for s in self.spares if s.conn is not None]
                if ready:
                    spare = next((s for s in ready if s.loaded), ready[0])
                    self.spares.remove(spare)
                    self.active += 1
                    self._top_up()
                    return spare
                if not self.spares:  # the one we waited for died
                    if self.failures >= self.FAILURE_LIMIT:
                        return None
                    self._start_spare()
                if not self.cond.wait(timeout=max(0.0, deadline - time.monotonic())):
                    return None

    def _end(self, spare: Spare, ended_open: bool = False) -> None:
        with self.cond:
            self.active -= 1
            if not ended_open and spare.process.poll() is None:
                spare.process.kill()
            self._top_up()
            if self.stopping and self.active == 0:
                self._exit()

    def _configure(self, count: int) -> None:
        with self.cond:
            self.count = max(0, min(MAX_SPARES, count))
            logger.info(f"spares: {self.count}")
            # drop the extra waiting spares, newest (least loaded) first
            while len(self.spares) > self.count:
                self.spares.pop().process.kill()
            if self.count == 0:
                self.stopping = True
                self.cond.notify_all()
                if self.active == 0:
                    self._exit()
            else:
                self.failures = 0
                self._top_up()


    def _step_down(self, conn: Connection) -> None:
        """aplite was updated: this open loads normally (and starts a new manager after its load, once this one is
        gone); quit when the opens already handed out end."""
        logger.info("aplite was updated; stopping (a new manager starts with a later open)")
        conn.send({"declined": True})
        conn.close()
        with self.cond:
            self.stopping = True
            for spare in self.spares:
                spare.process.kill()
            self.spares.clear()
            self.cond.notify_all()
            if self.active == 0:
                self._exit()

    def _exit(self) -> typing.NoReturn:
        """Now, from whichever thread: the main one sits in accept()."""
        logger.info("background manager stopped")
        if sys.platform != "win32":
            try:  # os._exit skips the Listener's own cleanup
                os.unlink(self.path)
            except OSError:
                pass
        os._exit(0)


def main() -> None:
    count, log_path = int(sys.argv[1]), sys.argv[2]
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    logging.basicConfig(filename=log_path, level=logging.INFO, format="%(asctime)s %(message)s")
    path = address()
    if path is None:
        logger.warning("no private folder for the background socket")
        return
    Manager(max(1, min(MAX_SPARES, count))).serve(path)


if __name__ == "__main__":
    main()
