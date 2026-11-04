"""
The self-host server process (see aplite/selfhost/__init__.py):
    python -m aplite.selfhost.server serve        upstream's WebHost, cut down to one room's pages (in selfhost/current;
                                                  also usable headless, e.g. under systemd: SIGTERM stops it)
    python -m aplite.selfhost.server load FILE    import a generated game into cwd and create its room

Differences from WebHost.py: config from selfhost/config.yaml; no generation, options pages, tutorials, sprite
download or /stats (bokeh); worlds without web tutorials are kept (WebHost.py drops them for its tutorial and options
pages; their games can still be in a seed); only the room's pages are served (ALLOWED), other site pages redirect to
archipelago.gg; room logs in cwd/logs (upstream writes them to user_path but reads them from cwd); optional game
server bind address.
Top level stays light: room processes are spawned (multiprocessing) and re-import this module.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import signal
import sys
import time
import types
import typing
import uuid

from aplite import selfhost

if typing.TYPE_CHECKING:
    from flask import Flask

ALLOWED = {
    "static", "favicon", "host_room", "host_room_command", "display_log", "view_seed",
    "get_multiworld_tracker", "get_multiworld_sphere_tracker", "get_player_tracker", "get_generic_game_tracker",
    "download_patch", "download_spoiler", "download_slot_file", "show_session", "set_session",
    "api.room_info", "api.tracker_data", "api.static_tracker_data", "api.tracker_slot_data",
    "api.get_datapackage", "api.get_datapackage_by_checksum", "api.get_datapackage_checksums",
}
"""Endpoints served. The api ones are what external trackers read; names checked against the app at startup."""


def webhost_config(config: dict[str, typing.Any]) -> dict[str, typing.Any]:
    return {
        "PORT": config["port"],
        "SECRET_KEY": bytes.fromhex(config["secret_key"]),
        "HOST_ADDRESS": config["public_address"],
        "GAME_PORTS": [config["game_port"], 0],  # 0: any free port if it's taken (the room page shows the real one)
        "SELFLAUNCHCERT": config["cert"] or None,
        "SELFLAUNCHKEY": config["key"] or None,
        "SELFGEN": False,
        "GENERATORS": 0,
        "HOSTERS": 1,
        "PONY": {"provider": "sqlite", "filename": os.path.abspath(selfhost.DB_FILE), "create_db": True},
    }


def create_app(config: dict[str, typing.Any], serving: bool) -> Flask:
    import settings
    settings.no_gui = True
    sys.modules["WebHostLib.stats"] = types.ModuleType("WebHostLib.stats")  # the /stats page needs bokeh
    from WebHostLib import app, cache, register
    from WebHostLib.models import db
    app.config.update(webhost_config(config))
    if serving and not app.config["HOST_ADDRESS"]:
        import Utils
        logging.info("Getting public IP, as public_address is empty.")
        app.config["HOST_ADDRESS"] = Utils.get_public_ipv4()
    register()
    cache.init_app(app)
    db.bind(**app.config["PONY"])
    db.generate_mapping(create_tables=True)
    return app


def restrict(app: Flask) -> None:
    from flask import abort, redirect, request
    missing = ALLOWED - set(app.view_functions)
    if missing:
        logging.warning(f"Allowed endpoints that don't exist (renamed upstream?): {sorted(missing)}")

    @app.before_request
    def only_the_room():
        if request.path == "/":
            info = selfhost.room_info()
            return redirect(f"/room/{selfhost.suuid(info['room'])}") if info else ("No game is hosted here.", 404)
        if request.endpoint in ALLOWED:
            return None
        if request.endpoint is None or request.method != "GET":
            abort(404)
        if any(isinstance(value, uuid.UUID) for value in (request.view_args or {}).values()):
            abort(404)  # pages about other seeds/rooms (or new rooms) don't exist here
        # general site pages (header links: games, tutorials, ...) live on the real site
        return redirect(f"https://archipelago.gg{request.full_path.rstrip('?')}")

    # first, before upstream's session handler
    app.before_request_funcs[None].insert(0, app.before_request_funcs[None].pop())


def room_process(bind: str, *args: typing.Any) -> None:
    """Hoster process target: upstream's run_server_process with room logs in cwd/logs and the bind address."""
    import WebHostLib.customserver as customserver
    original_init = customserver.WebHostContext.__init__

    def init(self, *init_args, **kwargs):
        original_init(self, *init_args, **kwargs)
        self.host = bind

    def set_up_logging(room_id) -> logging.Logger:
        logger = logging.getLogger(f"RoomLogger {room_id}")
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()
        os.makedirs("logs", exist_ok=True)
        handler = logging.FileHandler(os.path.join("logs", f"{room_id}.txt"), "a", encoding="utf-8-sig")
        handler.setFormatter(logging.Formatter("[%(asctime)s]: %(message)s"))
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        return logger

    if bind:
        customserver.WebHostContext.__init__ = init
    customserver.set_up_logging = set_up_logging
    customserver.run_server_process(*args)


def write_status(**status: typing.Any) -> None:
    import psutil
    process = psutil.Process()
    status = {"pid": process.pid, "created": process.create_time(), **status}
    path = selfhost.root("server.json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(status, f)
    os.replace(path + ".tmp", path)


def stop_rooms() -> None:
    """Room processes save on KeyboardInterrupt (SIGINT); terminate whatever is left after that."""
    import multiprocessing
    children = multiprocessing.active_children()
    for child in children:
        if sys.platform != "win32" and child.pid:
            os.kill(child.pid, signal.SIGINT)
    for child in children:
        child.join(15)
        if child.is_alive():
            child.terminate()


def serve() -> None:
    import multiprocessing
    multiprocessing.set_start_method("spawn")
    config = selfhost.load_config()
    if selfhost.server_status():
        sys.exit("A self-host server is already running.")
    os.makedirs(selfhost.current(), exist_ok=True)
    os.chdir(selfhost.current())  # WebHostLib's relative paths (logs, file_locks); also when run by hand
    write_status(serving=False)

    def terminate(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)

    try:
        app = create_app(config, serving=True)
        restrict(app)
        from WebHostLib import autolauncher
        autolauncher.run_server_process = functools.partial(room_process, config["game_bind"])
        autolauncher.autohost(app.config)
        from waitress import create_server
        server = create_server(app, host=config["bind"], port=config["port"], threads=10)
        write_status(serving=True, host_address=app.config["HOST_ADDRESS"], bind=config["bind"],
                     port=config["port"])
        logging.info(f"Serving on {config['bind']}:{config['port']}")
        server.run()
    finally:
        from WebHostLib.autolauncher import stop
        stop()
        stop_rooms()
        try:
            os.remove(selfhost.root("server.json"))
        except OSError:
            pass
        logging.info("Server stopped.")


def load(path: str) -> None:
    """Imports a generated game (.zip / .archipelago) into cwd's DB and creates its room with a fresh owner."""
    import zipfile
    from flask import get_flashed_messages
    from pony.orm import commit, db_session
    config = selfhost.load_config()
    app = create_app(config, serving=False)
    from WebHostLib.models import Room, Seed
    from WebHostLib.upload import process_multidata, upload_zip_to_db
    owner = uuid.uuid4()
    with app.test_request_context(), db_session:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zfile:
                seed = upload_zip_to_db(zfile, owner=owner)
        else:
            with open(path, "rb") as f:
                slots, multidata = process_multidata(f.read())
            seed = Seed(multidata=multidata, slots=slots, owner=owner)
        if isinstance(seed, str):
            raise ValueError(seed)
        if not seed:
            raise ValueError("; ".join(map(str, get_flashed_messages())) or "Could not load the game.")
        room = Room(seed=seed, owner=owner, tracker=uuid.uuid4(), timeout=int(config["room_timeout"]))
        commit()
        players = sorted(((slot.player_id, slot.player_name, slot.game) for slot in seed.slots))
        info = {
            "room": str(room.id), "seed": str(seed.id), "tracker": str(room.tracker), "owner": str(owner),
            "name": os.path.splitext(os.path.basename(path))[0], "source": os.path.abspath(path),
            "loaded": time.strftime("%Y-%m-%d %H:%M:%S"), "players": [list(p) for p in players],
        }
    with open(selfhost.ROOM_FILE, "w", encoding="utf-8") as f:
        json.dump(info, f, indent=1)
    print(f"Loaded {info['name']}: {len(players)} players")


def main() -> None:
    logging.basicConfig(format="[%(asctime)s] %(message)s", level=logging.INFO, stream=sys.stdout)
    command, *args = sys.argv[1:] or ["serve"]
    if command == "serve":
        serve()
    elif command == "load" and len(args) == 1:
        try:
            load(args[0])
        except Exception as e:
            logging.exception(e)
            print(f"{selfhost.LOAD_ERROR}{e}", file=sys.stderr)
            sys.exit(1)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
