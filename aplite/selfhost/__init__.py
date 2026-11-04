"""
Self-host: upstream's WebHost (WebHostLib) serving one room (room page, trackers, sphere tracker, downloads), run as a
detached server process (aplite/selfhost/server.py) that the TUI (aplite/selfhost/app.py) starts, attaches to and
controls through files: the pid file, the room's sqlite DB (commands go in as Command rows, exactly like the web
console's) and the room log.

Layout, in Utils.user_path("selfhost"):
    config.yaml     settings (DEFAULTS), shared by every room
    server.json     running server: pid, process create time, effective address/port
    server.log      server output (appended)
    incoming/       a game being loaded (moved to current/ once it loaded)
    current/        the hosted room: ap.db3 (everything: multidata, patches, save), logs/<room id>.txt, room.json
    archive/<name>/ earlier rooms, moved here whole when a new game is loaded (archive_dir overrides the location)

Kept light (no worlds, no WebHostLib): the server's room processes are spawned and import this package.
"""
from __future__ import annotations

import datetime
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
import typing
import uuid

import yaml

import Utils

DEFAULTS: dict[str, typing.Any] = {
    "bind": "0.0.0.0",  # address the website listens on (127.0.0.1: this machine / a reverse proxy only)
    "port": 8080,  # website port
    "public_address": "",  # host name/IP players connect to (shown on the room page); empty: detect public IP
    "public_url": "",  # website URL for links, e.g. behind a reverse proxy; empty: http://public_address:port
    "game_port": 38281,  # the room's game server port
    "game_bind": "",  # address the game server listens on; empty: all (127.0.0.1 for a TLS reverse proxy)
    "cert": "",  # TLS certificate + key for the game server (wss), e.g. certbot's fullchain.pem / privkey.pem
    "key": "",
    "room_timeout": 7200,  # seconds without activity before the room sleeps (opening the room page wakes it)
    "archive_dir": "",  # where replaced rooms go; empty: selfhost/archive
    "secret_key": "",  # signs the website's session cookies; generated
}
DESCRIPTIONS: dict[str, str] = {
    "bind": "Website listen address (0.0.0.0 = everywhere, 127.0.0.1 = this machine / reverse proxy only)",
    "port": "Website port",
    "public_address": "Host name or IP players connect to (empty = detect the public IP)",
    "public_url": "Website URL for links, e.g. behind a reverse proxy (empty = http://address:port)",
    "game_port": "Game server port (forward this one and the website port)",
    "game_bind": "Game server listen address (empty = everywhere, 127.0.0.1 for a TLS reverse proxy)",
    "cert": "TLS certificate file for the game server (wss), empty = plain ws",
    "key": "TLS key file for the game server",
    "room_timeout": "Seconds without activity before the room sleeps (the room page wakes it)",
    "archive_dir": "Folder for replaced rooms (empty = selfhost/archive)",
}
SERVER_MODULE = "aplite.selfhost.server"
ROOM_FILE = "room.json"
DB_FILE = "ap.db3"
LOAD_ERROR = "aplite-load-error: "


def root(*path: str) -> str:
    return os.path.join(os.environ.get("APLITE_SELFHOST_DIR") or Utils.user_path("selfhost"), *path)


def current(*path: str) -> str:
    return root("current", *path)


def load_config() -> dict[str, typing.Any]:
    config = dict(DEFAULTS)
    try:
        with open(root("config.yaml"), encoding="utf-8") as f:
            config.update(yaml.safe_load(f) or {})
    except FileNotFoundError:
        pass
    if not config["secret_key"]:
        config["secret_key"] = secrets.token_hex(32)
        save_config(config)
    return config


def save_config(config: dict[str, typing.Any]) -> None:
    os.makedirs(root(), exist_ok=True)
    with open(root("config.yaml"), "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, sort_keys=False)


def archive_root(config: dict[str, typing.Any]) -> str:
    return os.path.abspath(os.path.expanduser(config["archive_dir"])) if config["archive_dir"] else root("archive")


# --- server process ---

def server_status() -> dict[str, typing.Any] | None:
    """server.json of the running server, or None when it isn't running (stale files are ignored)."""
    import psutil
    try:
        with open(root("server.json"), encoding="utf-8") as f:
            status = json.load(f)
        process = psutil.Process(status["pid"])
        if abs(process.create_time() - status["created"]) > 1:
            return None  # pid reused
    except (OSError, ValueError, KeyError, psutil.Error):
        return None
    return status


def start_server() -> None:
    os.makedirs(current(), exist_ok=True)
    from aplite import child_env
    log = open(root("server.log"), "ab")
    log.write(f"\n--- starting {datetime.datetime.now():%Y-%m-%d %H:%M:%S} ---\n".encode())
    log.flush()
    kwargs: dict[str, typing.Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True  # survives the TUI and its terminal
    with log:
        subprocess.Popen([sys.executable, "-m", SERVER_MODULE, "serve"], cwd=current(),
                         env={**child_env(), "PYTHONUNBUFFERED": "1"},
                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, **kwargs)


def wait_for_server(timeout: float = 60) -> dict[str, typing.Any] | None:
    """The server writes server.json at once and marks it serving after loading worlds (a few seconds); None if it
    exits first (see server.log) or takes longer than `timeout`."""
    end = time.monotonic() + timeout
    seen = False
    while time.monotonic() < end:
        status = server_status()
        if status and status.get("serving"):
            return status
        if status:
            seen = True
        elif seen or time.monotonic() > end - timeout + 15:
            return None
        time.sleep(0.25)
    return None


def stop_server(timeout: float = 20) -> bool:
    """SIGTERM: the server stops its room processes (which save) and exits. Killed after `timeout`; room processes
    still left are killed too (on Windows terminate() is a hard kill, so that's the only way they end)."""
    import psutil
    status = server_status()
    if not status:
        return True
    try:
        process = psutil.Process(status["pid"])
        children = process.children(recursive=True)
        process.terminate()
        try:
            process.wait(timeout)
        except psutil.TimeoutExpired:
            process.kill()
        for child in children:
            if child.is_running():
                child.kill()
    except psutil.Error:
        pass
    return server_status() is None


def base_url(config: dict[str, typing.Any], status: dict[str, typing.Any] | None = None) -> str:
    if config["public_url"]:
        return config["public_url"].rstrip("/")
    address = config["public_address"] or (status or {}).get("host_address") or "localhost"
    return f"http://{address}:{config['port']}"


# --- the room ---

def room_info(folder: str | None = None) -> dict[str, typing.Any] | None:
    """room.json written when the game was loaded: room/seed/tracker ids, owner, players, source file."""
    try:
        with open(os.path.join(folder or current(), ROOM_FILE), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def suuid(value: str) -> str:
    """The website's short UUID form used in URLs."""
    import base64
    return base64.urlsafe_b64encode(uuid.UUID(value).bytes).rstrip(b"=").decode("ascii")


def links(info: dict[str, typing.Any], base: str) -> dict[str, str]:
    return {
        "Room": f"{base}/room/{suuid(info['room'])}",
        "Multiworld tracker": f"{base}/tracker/{suuid(info['tracker'])}",
        "Sphere tracker": f"{base}/sphere_tracker/{suuid(info['tracker'])}",
        "Owner link (open once in your browser for the room console)": f"{base}/session/{info['owner']}",
    }


def _db(folder: str | None = None) -> sqlite3.Connection:
    return sqlite3.connect(os.path.join(folder or current(), DB_FILE), timeout=10)


def _room_key(info: dict[str, typing.Any]) -> bytes:
    return uuid.UUID(info["room"]).bytes  # pony stores UUIDs as 16-byte blobs in sqlite (datetimes as str())


def room_state(info: dict[str, typing.Any], folder: str | None = None) -> dict[str, typing.Any] | None:
    """last_activity (UTC), last_port (-1 = failed), timeout, and whether it should be running (awake)."""
    try:
        with _db(folder) as db:
            row = db.execute("SELECT last_activity, last_port, timeout FROM Room WHERE id = ?",
                             (_room_key(info),)).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    last_activity = datetime.datetime.fromisoformat(row[0])
    awake = Utils.utcnow() - last_activity < datetime.timedelta(seconds=row[2])
    return {"last_activity": last_activity, "last_port": row[1], "timeout": row[2], "awake": awake}


def wake(info: dict[str, typing.Any]) -> None:
    """What opening the room page does: fresh activity makes the server start the room."""
    with _db() as db:
        db.execute("UPDATE Room SET last_activity = ? WHERE id = ?", (str(Utils.utcnow()), _room_key(info)))


def set_timeout(info: dict[str, typing.Any], seconds: int) -> None:
    with _db() as db:
        db.execute("UPDATE Room SET timeout = ? WHERE id = ?", (seconds, _room_key(info)))


def send_command(info: dict[str, typing.Any], text: str) -> None:
    """A server console command, as the room page's console sends it (the room process polls these)."""
    with _db() as db:
        db.execute("INSERT INTO Command (room, commandtext) VALUES (?, ?)", (_room_key(info), text))


def log_path(info: dict[str, typing.Any], folder: str | None = None) -> str:
    return os.path.join(folder or current(), "logs", f"{info['room']}.txt")


# --- loading and archiving ---

class LoadError(Exception):
    pass


def archive_current(config: dict[str, typing.Any]) -> str | None:
    """Moves current/ into the archive (server must be stopped); returns the archive folder."""
    if not os.path.isdir(current()) or not os.listdir(current()):
        return None
    info = room_info() or {}
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    name = Utils.get_file_safe_name(f"{stamp}_{info.get('name', 'room')}")
    target = os.path.join(archive_root(config), name)
    os.makedirs(archive_root(config), exist_ok=True)
    shutil.move(current(), target)
    return target


def load_game(path: str) -> str:
    """Imports a generated game into a fresh folder and creates its room, in a child process (needs the worlds and
    WebHostLib; the server can keep running meanwhile). Returns the folder, for replace_current."""
    from aplite import child_env
    folder = root("incoming")
    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(folder)
    result = subprocess.run([sys.executable, "-m", SERVER_MODULE, "load", os.path.abspath(path)], cwd=folder,
                            env=child_env(), stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if result.returncode != 0 or not room_info(folder):
        errors = [line[len(LOAD_ERROR):] for line in result.stderr.splitlines() if line.startswith(LOAD_ERROR)]
        shutil.rmtree(folder, ignore_errors=True)
        raise LoadError(errors[-1] if errors else f"loading failed (exit code {result.returncode})")
    return folder


def replace_current(config: dict[str, typing.Any], folder: str) -> str | None:
    """Archives current/ and puts `folder` in its place (server must be stopped). Returns the archive folder."""
    archived = archive_current(config)
    shutil.rmtree(current(), ignore_errors=True)  # an empty leftover
    shutil.move(folder, current())
    return archived


def archives(config: dict[str, typing.Any]) -> list[tuple[str, dict[str, typing.Any] | None]]:
    folder = archive_root(config)
    if not os.path.isdir(folder):
        return []
    return [(os.path.join(folder, name), room_info(os.path.join(folder, name)))
            for name in sorted(os.listdir(folder), reverse=True) if os.path.isdir(os.path.join(folder, name))]
