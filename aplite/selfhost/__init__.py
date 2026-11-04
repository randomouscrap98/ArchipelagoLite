"""
Self-host: upstream's WebHost (WebHostLib) serving our games' pages (room page, trackers, sphere tracker, downloads),
run as a detached server process (aplite/selfhost/server.py) that the TUI (aplite/selfhost/app.py) starts, attaches to
and controls through files: the pid file, the shared sqlite DB (commands go in as Command rows, exactly like the web
console's), the room logs and the active list.

Every game loaded stays in the DB (progress included) until deleted. Up to room_count of them are active: hosted,
each on a port of game_port .. game_port + room_count - 1. Inactive games keep their tracker and download pages;
their room page says they aren't hosted.

Layout, in Utils.user_path("selfhost"):
    config.yaml        settings (DEFAULTS)
    server.json        running server: pid, process create time, effective address/port
    server.log         server output (appended)
    active.json        room ids of the active games, in tab order
    games/<room>.json  one per loaded game: room/seed/tracker ids, owner, name, players, source file
    data/              the server's folder: ap.db3 (everything: multidata, patches, saves), logs/<room>.txt

Kept light (no worlds, no WebHostLib): the server's room processes are spawned and import this package.
"""
from __future__ import annotations

import datetime
import json
import os
import secrets
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
    "room_count": 1,  # games hosted at once, each on its own game port
    "game_port": 38281,  # first game port (game_port .. game_port + room_count - 1)
    "game_bind": "",  # address the game servers listen on; empty: all (127.0.0.1 for a TLS reverse proxy)
    "cert": "",  # TLS certificate + key for the game servers (wss), e.g. certbot's fullchain.pem / privkey.pem
    "key": "",
    "room_timeout": 7200,  # seconds without activity before a room sleeps (opening the room page wakes it)
    "secret_key": "",  # signs the website's session cookies; generated
}
DESCRIPTIONS: dict[str, str] = {
    "bind": "Website listen address (0.0.0.0 = everywhere, 127.0.0.1 = this machine / reverse proxy only)",
    "port": "Website port",
    "public_address": "Host name or IP players connect to (empty = detect the public IP)",
    "public_url": "Website URL for links, e.g. behind a reverse proxy (empty = http://address:port)",
    "room_count": "How many games can be active (hosted) at once; each needs its own game port",
    "game_port": "First game port: the games use game_port up to game_port + room_count - 1 (forward those and the "
                 "website port)",
    "game_bind": "Game server listen address (empty = everywhere, 127.0.0.1 for a TLS reverse proxy)",
    "cert": "TLS certificate file for the game servers (wss), empty = plain ws",
    "key": "TLS key file for the game servers",
    "room_timeout": "Seconds without activity before a room sleeps (the room page wakes it)",
}
SERVER_MODULE = "aplite.selfhost.server"
DB_FILE = "ap.db3"
LOAD_ERROR = "aplite-load-error: "

Info = dict[str, typing.Any]


def root(*path: str) -> str:
    return os.path.join(os.environ.get("APLITE_SELFHOST_DIR") or Utils.user_path("selfhost"), *path)


def data(*path: str) -> str:
    return root("data", *path)


def load_config() -> dict[str, typing.Any]:
    config = dict(DEFAULTS)
    try:
        with open(root("config.yaml"), encoding="utf-8") as f:
            config.update({k: v for k, v in (yaml.safe_load(f) or {}).items() if k in DEFAULTS})
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


def game_ports(config: dict[str, typing.Any]) -> list[int]:
    return list(range(config["game_port"], config["game_port"] + max(1, config["room_count"])))


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
    os.makedirs(data(), exist_ok=True)
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
        subprocess.Popen([sys.executable, "-m", SERVER_MODULE, "serve"], cwd=data(),
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


# --- games ---

def games() -> list[Info]:
    """Every loaded game's info (games/<room>.json), newest first."""
    folder = root("games")
    found = []
    for name in os.listdir(folder) if os.path.isdir(folder) else ():
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                found.append(json.load(f))
        except (OSError, ValueError):
            pass
    return sorted(found, key=lambda info: info.get("loaded", ""), reverse=True)


def game(room: str) -> Info | None:
    try:
        with open(root("games", f"{room}.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_game(info: Info) -> None:
    os.makedirs(root("games"), exist_ok=True)
    with open(root("games", f"{info['room']}.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, indent=1)


def active() -> list[str]:
    """Room ids of the active games (tab order); ids whose game is gone are dropped."""
    try:
        with open(root("active.json"), encoding="utf-8") as f:
            ids = json.load(f)
    except (OSError, ValueError):
        return []
    return [room for room in ids if os.path.exists(root("games", f"{room}.json"))]


def _save_active(ids: list[str]) -> None:
    os.makedirs(root(), exist_ok=True)
    with open(root("active.json") + ".tmp", "w", encoding="utf-8") as f:
        json.dump(ids, f)
    os.replace(root("active.json") + ".tmp", root("active.json"))


def activate(info: Info, config: dict[str, typing.Any]) -> str | None:
    """Makes a game active (hosted once the server runs; woken now). Returns why it can't be, if it can't."""
    ids = active()
    if info["room"] in ids:
        return None
    if len(ids) >= config["room_count"]:
        return (f"All {config['room_count']} room(s) are in use: deactivate a game first, or raise room_count in "
                "Settings (and forward the extra port).")
    _save_active(ids + [info["room"]])
    with _db() as db:  # an /exit left from deactivating it would stop it again at once
        db.execute("DELETE FROM Command WHERE room = ? AND commandtext = '/exit'", (_room_key(info),))
    wake(info)
    return None


def deactivate(info: Info, running: bool) -> None:
    """No longer hosted: a running room is told to /exit (it saves); the server won't start it again."""
    _save_active([room for room in active() if room != info["room"]])
    state = room_state(info)
    if running and state and state["awake"]:
        send_command(info, "/exit")


def delete_game(info: Info) -> None:
    """Removes an inactive game for good: its DB rows, log and info."""
    key = _room_key(info)
    with _db() as db:
        db.execute("DELETE FROM Command WHERE room = ?", (key,))
        db.execute("DELETE FROM Room WHERE id = ?", (key,))
        seed = uuid.UUID(info["seed"]).bytes
        if not db.execute("SELECT COUNT(*) FROM Room WHERE seed = ?", (seed,)).fetchone()[0]:
            db.execute("DELETE FROM Slot WHERE seed = ?", (seed,))
            db.execute("DELETE FROM Seed WHERE id = ?", (seed,))
    for path in (log_path(info), root("games", f"{info['room']}.json")):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def suuid(value: str) -> str:
    """The website's short UUID form used in URLs."""
    import base64
    return base64.urlsafe_b64encode(uuid.UUID(value).bytes).rstrip(b"=").decode("ascii")


def room_url(info: Info, base: str) -> str:
    return f"{base}/room/{suuid(info['room'])}"


def owner_link(info: Info, base: str) -> str:
    """Our /owner/<id> page: makes the browser the room's owner (console, log), then shows the room page."""
    return f"{base}/owner/{info['owner']}"


def _db() -> sqlite3.Connection:
    return sqlite3.connect(data(DB_FILE), timeout=10)


def _room_key(info: Info) -> bytes:
    return uuid.UUID(info["room"]).bytes  # pony stores UUIDs as 16-byte blobs in sqlite (datetimes as str())


def room_state(info: Info) -> dict[str, typing.Any] | None:
    """last_activity (UTC), last_port (-1 = failed), timeout, and whether it should be running (awake)."""
    try:
        with _db() as db:
            row = db.execute("SELECT last_activity, last_port, timeout FROM Room WHERE id = ?",
                             (_room_key(info),)).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    last_activity = datetime.datetime.fromisoformat(row[0])
    awake = Utils.utcnow() - last_activity < datetime.timedelta(seconds=row[2])
    return {"last_activity": last_activity, "last_port": row[1], "timeout": row[2], "awake": awake}


def wake(info: Info) -> None:
    """What opening the room page does: fresh activity makes the server start the room (if it's active)."""
    with _db() as db:
        db.execute("UPDATE Room SET last_activity = ? WHERE id = ?", (str(Utils.utcnow()), _room_key(info)))


def set_timeouts(seconds: int) -> None:
    """For every room (each room reads its timeout when it starts)."""
    try:
        with _db() as db:
            db.execute("UPDATE Room SET timeout = ?", (seconds,))
    except sqlite3.Error:
        pass  # no DB yet


def send_command(info: Info, text: str) -> None:
    """A server console command, as the room page's console sends it (the room process polls these)."""
    with _db() as db:
        db.execute("INSERT INTO Command (room, commandtext) VALUES (?, ?)", (_room_key(info), text))


def log_path(info: Info) -> str:
    return data("logs", f"{info['room']}.txt")


# --- loading ---

class LoadError(Exception):
    pass


def load_game(path: str) -> Info:
    """Imports a generated game into the DB and creates its room (inactive), in a child process (needs the worlds and
    WebHostLib); the server can keep running meanwhile. Returns its info."""
    from aplite import child_env
    os.makedirs(data(), exist_ok=True)
    result = subprocess.run([sys.executable, "-m", SERVER_MODULE, "load", os.path.abspath(path)], cwd=data(),
                            env=child_env(), stdin=subprocess.DEVNULL, capture_output=True, text=True)
    room = next((line[len("room: "):].strip() for line in result.stdout.splitlines() if line.startswith("room: ")),
                None)
    info = game(room) if room else None
    if result.returncode != 0 or not info:
        errors = [line[len(LOAD_ERROR):] for line in result.stderr.splitlines() if line.startswith(LOAD_ERROR)]
        raise LoadError(errors[-1] if errors else f"loading failed (exit code {result.returncode})")
    return info
