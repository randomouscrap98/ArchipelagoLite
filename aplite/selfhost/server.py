"""
The self-host server process (see aplite/selfhost/__init__.py):
    python -m aplite.selfhost.server serve        upstream's WebHost, cut down to our games' pages (in selfhost/data;
                                                  also usable headless, e.g. under systemd: SIGTERM stops it)
    python -m aplite.selfhost.server load FILE    import a generated game into cwd's DB and create its room

Differences from WebHost.py: config from selfhost/config.yaml; no generation, options pages, tutorials, sprite
download or /stats (bokeh); worlds without web tutorials are kept (WebHost.py drops them for its tutorial and options
pages; their games can still be in a seed); only room pages are served (ALLOWED), other site pages redirect to
archipelago.gg; only active games are hosted (selfhost.active(); inactive ones get a notice instead of the room page);
our /owner/<id> instead of /session; room logs in cwd/logs (upstream writes them to user_path but reads them from cwd);
optional game server bind address.
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

from aplite import selfhost, site_pages, write_atomic

if typing.TYPE_CHECKING:
    from flask import Flask

ALLOWED = {
    "static", "favicon", "host_room", "host_room_command", "display_log", "view_seed",
    "get_multiworld_tracker", "get_multiworld_sphere_tracker", "get_player_tracker", "get_generic_game_tracker",
    "download_patch", "download_spoiler", "download_slot_file",
    "api.room_info", "api.tracker_data", "api.static_tracker_data", "api.tracker_slot_data",
    "api.get_datapackage", "api.get_datapackage_by_checksum", "api.get_datapackage_checksums",
}
"""Endpoints served. The api ones are what external trackers read; names checked against the app at startup."""
BLOCKED = {"show_session", "set_session"}
"""404 rather than redirected to archipelago.gg (that would hand it the owner id): /owner/<id> replaces them."""


def webhost_config(config: dict[str, typing.Any]) -> dict[str, typing.Any]:
    return {
        "PORT": config["port"],
        "SECRET_KEY": bytes.fromhex(config["secret_key"]),
        "HOST_ADDRESS": config["public_address"],
        "GAME_PORTS": selfhost.game_ports(config),  # no 0 (any free port): it wouldn't be forwarded
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
    from flask import abort, redirect, render_template_string, request, session, url_for
    missing = (ALLOWED | BLOCKED) - set(app.view_functions)
    if missing:
        logging.warning(f"Allowed/blocked endpoints that don't exist (renamed upstream?): {sorted(missing)}")

    @app.before_request
    def only_the_room():
        if request.path == "/":
            return index()
        if request.path.startswith("/owner/"):
            # the owner link: this browser's session becomes the room's owner (console, log), then the room page
            info = next((info for info in selfhost.games() if request.path == f"/owner/{info['owner']}"), None)
            if not info:
                abort(404)
            session["_id"] = uuid.UUID(info["owner"])
            return redirect(url_for("host_room", room=uuid.UUID(info["room"])))
        if request.endpoint == "host_room" and str(request.view_args["room"]) not in selfhost.active():
            return not_hosted(request.view_args["room"])
        if request.endpoint in ALLOWED:
            return None
        if request.endpoint is None or request.endpoint in BLOCKED or request.method != "GET":
            abort(404)
        if any(isinstance(value, uuid.UUID) for value in (request.view_args or {}).values()):
            abort(404)  # pages about other seeds/rooms (or new rooms) don't exist here
        return site_pages.to_official_site()  # general site pages (header links: games, tutorials, ...)

    def index():
        hosted = [info for room in selfhost.active() if (info := selfhost.game(room))]
        if len(hosted) == 1:
            return redirect(url_for("host_room", room=uuid.UUID(hosted[0]["room"])))
        return render_template_string(INDEX_PAGE, games=hosted, UUID=uuid.UUID)

    def not_hosted(room: uuid.UUID):
        """An inactive game: its tracker and download pages still work, its room page would wait for a port."""
        from pony.orm import db_session
        from WebHostLib.models import Room
        with db_session:
            found = Room.get(id=room)
            if not found:
                abort(404)
            info = selfhost.game(str(room)) or {"name": "This game"}
            slots = sorted((slot.player_id, slot.player_name, slot.game, slot.data is not None)
                           for slot in found.seed.slots)
            return render_template_string(NOT_HOSTED_PAGE, name=info["name"], room=found, slots=slots)

    # first, before upstream's session handler
    app.before_request_funcs[None].insert(0, app.before_request_funcs[None].pop())


def mount_at_url_path(app: Flask, config: dict[str, typing.Any]) -> None:
    """Behind a reverse proxy on a sub-path (public_url https://host/archipelago): links get that prefix
    (SCRIPT_NAME, which url_for uses), and requests work whether the proxy strips the prefix or passes it on. Taken
    from our own config, not a request header, so clients can't change the links (pages are cached)."""
    from urllib.parse import urlparse
    prefix = urlparse(config["public_url"]).path.rstrip("/")
    if not prefix:
        return
    wsgi_app = app.wsgi_app

    def mounted(environ, start_response):
        path = environ.get("PATH_INFO", "")
        if path == prefix or path.startswith(prefix + "/"):
            environ["PATH_INFO"] = path[len(prefix):] or "/"
        environ["SCRIPT_NAME"] = prefix
        return wsgi_app(environ, start_response)

    app.wsgi_app = mounted
    # upstream's page header (header/baseHeader.html) has hardcoded root links (logo, games, faq...): prefix those
    import re
    root_link = re.compile(r'(\s(?:href|src|action)=")/(?!/|' + re.escape(prefix[1:]) + r'(?:/|"))')

    @app.after_request
    def prefix_root_links(response):  # runs before Flask-Compress's (registered earlier)
        if response.mimetype == "text/html" and not response.direct_passthrough:
            response.set_data(root_link.sub(rf"\1{prefix}/", response.get_data(as_text=True)))
        return response

    logging.info(f"Links use the URL path {prefix} (from public_url)")


def only_active(autolauncher: types.ModuleType) -> None:
    """Upstream starts every room whose page was visited lately; here only active games are hosted."""
    original = autolauncher.MultiworldInstance.start_room
    cache: dict[str, typing.Any] = {"stamp": None, "active": set()}

    def start_room(self, room_id) -> None:
        try:
            stamp = os.stat(selfhost.active_file()).st_mtime_ns
        except OSError:
            stamp = None
        if stamp != cache["stamp"]:  # called ~10 times a second per room: reread only when it changed
            cache["stamp"], cache["active"] = stamp, set(selfhost.active())
        if str(room_id) in cache["active"]:
            original(self, room_id)

    autolauncher.MultiworldInstance.start_room = start_room


INDEX_PAGE = site_pages.page("Games", """
        <h1>Games hosted here</h1>
        {% if games %}
        <ul>
            {% for game in games %}
            <li><a href="{{ url_for('host_room', room=UUID(game.room)) }}">{{ game.name }}</a>
                ({{ game.players|length }} players)</li>
            {% endfor %}
        </ul>
        {% else %}
        <p>No game is hosted here right now.</p>
        {% endif %}
""")

NOT_HOSTED_PAGE = site_pages.page("{{ name }} (not hosted)", """
        <h1>{{ name }}</h1>
        <p>This game isn't hosted right now, so you can't connect to it. Its trackers and downloads still work:
           <a href="{{ url_for('get_multiworld_tracker', tracker=room.tracker) }}">Multiworld Tracker</a>,
           <a href="{{ url_for('get_multiworld_sphere_tracker', tracker=room.tracker) }}">Sphere Tracker</a>.</p>
        <table>
            <tr><th>Id</th><th>Name</th><th>Game</th><th>Download</th><th>Tracker</th></tr>
            {% for player_id, player_name, game, has_file in slots %}
            <tr>
                <td>{{ player_id }}</td><td>{{ player_name }}</td><td>{{ game }}</td>
                <td>{% if has_file %}
                    <a href="{{ url_for('download_slot_file', room_id=room.id, player_id=player_id) }}">Download</a>
                {% endif %}</td>
                <td><a href="{{ url_for('get_player_tracker', tracker=room.tracker, tracked_team=0,
                                        tracked_player=player_id) }}">Tracker</a></td>
            </tr>
            {% endfor %}
        </table>
""")


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
    write_atomic(selfhost.root("server.json"), json.dumps(status))


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
    os.makedirs(selfhost.data(), exist_ok=True)
    os.chdir(selfhost.data())  # WebHostLib's relative paths (logs, file_locks); also when run by hand
    write_status(serving=False)

    def terminate(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)

    try:
        app = create_app(config, serving=True)
        restrict(app)
        from WebHostLib import autolauncher
        autolauncher.run_server_process = functools.partial(room_process, config["game_bind"])
        only_active(autolauncher)
        autolauncher.autohost(app.config)
        from waitress import create_server
        mount_at_url_path(app, config)
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
        info: selfhost.Info = {
            "room": str(room.id), "seed": str(seed.id), "tracker": str(room.tracker), "owner": str(owner),
            "name": os.path.splitext(os.path.basename(path))[0], "source": os.path.abspath(path),
            "loaded": time.strftime("%Y-%m-%d %H:%M:%S"), "players": [list(p) for p in players],
        }
    selfhost.save_game(info)
    print(f"{selfhost.LOADED_ROOM}{info['room']}")


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
