"""aplite's Manual Client. Every manual_*.apworld bundles its own copy of the ManualForArchipelago template client,
frozen at whatever version its author copied, and the launcher runs the highest one. Those are kivy-only (the game
comes from a kivy dropdown, some import kivy at module level, old ones crash in CommonContext.__init__ with
game = "not set"), so aplite replaces the component with this: the same protocol and behavior (one press sends a
check, out-of-logic presses are blocked once Universal Tracker has results, scout buttons, a goal row), on top of
UT's context when it's installed so the Tracker tab works too. Reference: the template's src/ManualClient.py."""
from __future__ import annotations

import asyncio
import functools
import logging
import re
import typing

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, Tree

from aplite import colors

if typing.TYPE_CHECKING:
    from CommonClient import CommonContext

logger = logging.getLogger("Client")

NO_CATEGORY = "(No Category)"
HINTED = "(Hinted)"
DEFAULT_GOAL = "__Manual Game Complete__"
REFRESH = 0.5
"""Seconds between checks for changes to show (the template's UI is told through ctx.ui, which aplite leaves None)."""


def natural_key(text: str) -> list:
    """The template's default sort: natural (key2 < key12), case-insensitive, leading articles ignored."""
    text = re.sub(r"^(the|a|an) ", "", text.lower())
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


def category_key(name: str) -> list:
    """Categories in natural order, the "(...)" ones (No Category, Hinted) first."""
    return [0 if name.lstrip().startswith("(") else 1, *natural_key(name)]


@functools.cache
def context_class() -> type[CommonContext]:
    """ManualContext on Universal Tracker's context when it's installed (so the Tracker tab and logic work), else on
    CommonContext. Built on first use: importing either loads a lot."""
    import Utils
    from CommonClient import ClientCommandProcessor, CommonContext
    from MultiServer import mark_raw
    from NetUtils import ClientStatus
    from worlds.AutoWorld import AutoWorldRegister
    try:
        from worlds.tracker.TrackerClient import TrackerCommandProcessor, TrackerGameContext
        base, base_processor, tracker = TrackerGameContext, TrackerCommandProcessor, True
    except ImportError:
        base, base_processor, tracker = CommonContext, ClientCommandProcessor, False

    class ManualCommandProcessor(base_processor):
        @mark_raw
        def _cmd_send(self, location_name: str) -> bool:
            """Send a check (also out of logic)."""
            name, usable, response = Utils.get_intended_text(location_name, self.ctx.location_names_to_id.keys())
            if not usable:
                self.output(response)
                return False
            self.ctx.send_check(self.ctx.location_names_to_id[name])
            return True

        @mark_raw
        def _cmd_death(self, cause: str = "") -> bool:
            """Send a death to Death Link (when the slot has it on)."""
            if "DeathLink" not in self.ctx.tags:
                self.output("Death Link isn't on for this slot.")
                return False
            self.ctx.start(self.ctx.send_death(cause))
            return True

    class ManualContext(base):
        command_processor = ManualCommandProcessor
        game = None
        items_handling = 0b111  # full remote
        tags = {"AP"}
        uses_tracker = tracker

        def __init__(self, server_address, password, config: dict) -> None:
            super().__init__(server_address, password)
            self.items_handling = 0b111  # UT's __init__ sets its own
            self.tags = {"AP"}
            self.locked_game = config.get("game")
            """From an .apmanual file: always this game."""
            self.game = self.locked_game
            self.username = config.get("player_name")
            self.item_table: dict = config.get("items", {})
            self.location_table: dict = config.get("locations", {})
            self.category_table: dict = config.get("categories", {})
            self.room_games: set[str] = set()
            self.location_names_to_id: dict[str, int] = {}
            self.item_names_to_id: dict[str, int] = {}
            self.goal_location: dict = {"name": DEFAULT_GOAL}
            self.victory_names = [DEFAULT_GOAL]
            self.goaled = False
            self.sent: set[int] = set()
            """Checks sent but not confirmed by the server yet: hidden right away, as the template does."""
            self.reachable: set[str] | None = None
            """UT's in-logic locations, None until it reports (then out-of-logic presses are blocked)."""
            self.glitched: set[str] = set()
            self.reachable_events: set[str] | None = None
            self.version = 0
            """Bumped on every change the Manual tab shows."""
            self.tasks: set[asyncio.Task] = set()
            if tracker:
                self.set_callback(self.on_tracker_updated)
                if hasattr(self, "set_events_callback"):
                    self.set_events_callback(self.on_tracker_events)
                if hasattr(self, "set_glitches_callback"):
                    self.set_glitches_callback(self.on_glitches)

        def start(self, coroutine) -> None:
            task = asyncio.get_running_loop().create_task(coroutine)
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

        # game and login

        async def prepare_data_package(self, relevant_games: set[str], remote_date_package_versions: dict) -> None:
            self.room_games = set(relevant_games)  # RoomInfo's games, before server_auth
            await super().prepare_data_package(relevant_games, remote_date_package_versions)

        def manual_games(self) -> list[str]:
            """The Manual games this slot could be: the room's (installed or not, names come from the server's data
            package), or every installed one when the server didn't say. The last one used first."""
            installed = {game for game in AutoWorldRegister.world_types if game.startswith("Manual_")}
            games = {game for game in self.room_games if game.startswith("Manual_")} or installed
            last = Utils.persistent_load().get("client", {}).get("last_manual_game")
            return sorted(games, key=lambda game: (game != last, game.casefold()))

        async def pick_game(self) -> str | None:
            if self.locked_game:
                return self.locked_game
            games = self.manual_games()
            if not games:
                logger.error("This room has no Manual games. Install the Manual's apworld, or connect to its room.")
                return None
            if len(games) == 1:
                return games[0]
            if self.game in games:  # reconnecting
                return self.game
            logger.info("Which Manual game is this slot? Type its number:")
            for number, game in enumerate(games, 1):
                logger.info(f"  {number}: {game}")
            while True:
                answer = await self.console_input()
                if answer is None:
                    return None
                answer = answer.strip()
                if answer.isdigit() and 1 <= int(answer) <= len(games):
                    return games[int(answer) - 1]
                if answer in games:
                    return answer
                logger.info(f"Type a number from 1 to {len(games)}.")

        async def server_auth(self, password_requested: bool = False):
            if password_requested and not self.password:
                await CommonContext.server_auth(self, password_requested)
            game = await self.pick_game()
            if not game:
                await self.disconnect()
                return
            self.game = game
            self.goaled = False
            self.sent.clear()
            from worlds import network_data_package
            if game in network_data_package["games"]:
                self.update_ids(network_data_package["games"][game])
            world = self.world()
            if world is not None and getattr(world, "victory_names", None):
                self.victory_names = list(world.victory_names)
            else:
                self.victory_names = [DEFAULT_GOAL]
            self.goal_location = self.get_location_by_name(self.victory_names[0])
            await self.get_username()
            await self.send_connect()

        def update_ids(self, data_package: dict) -> None:
            self.location_names_to_id = data_package["location_name_to_id"]
            self.item_names_to_id = data_package["item_name_to_id"]

        def update_data_package(self, data_package: dict) -> None:
            super().update_data_package(data_package)
            if self.game in data_package["games"]:
                self.update_ids(data_package["games"][self.game])

        # the Manual's data (installed world, else the .apmanual tables, else just the name)

        def world(self):
            return AutoWorldRegister.world_types.get(self.game) if self.game else None

        def get_location_by_name(self, name: str) -> dict:
            location = self.location_table.get(name)
            if not location and (world := self.world()) is not None:
                location = getattr(world, "location_name_to_location", {}).get(name)
            return location or {"name": name}

        def get_item_by_name(self, name: str) -> dict:
            item = self.item_table.get(name)
            if not item and (world := self.world()) is not None:
                item = getattr(world, "item_name_to_item", {}).get(name)
            return item or {"name": name}

        def category_hidden(self, name: str) -> bool:
            settings = self.category_table.get(name)
            if settings is None and (world := self.world()) is not None:
                settings = getattr(world, "category_table", {}).get(name)
            return bool(settings and settings.get("hidden"))

        def sort_key(self, entry: dict) -> list:
            return natural_key(entry.get("sort-key") or entry["name"])

        # actions

        def press_allowed(self, name: str) -> bool:
            """The template blocks out-of-logic presses when UT is loaded (its default settings; glitched allowed).
            Here only once UT has reported, so a failed UT generation doesn't block everything."""
            return self.reachable is None or name in self.reachable or name in self.glitched

        def send_check(self, location_id: int) -> None:
            self.sent.add(location_id)
            self.version += 1
            self.start(self.check_locations([location_id]))

        def scout(self, location_id: int) -> None:
            self.start(self.send_msgs([{"cmd": "LocationScouts", "locations": [location_id], "create_as_hint": 2}]))

        def send_goal(self) -> None:
            self.goaled = True
            self.version += 1
            if not self.finished_game:
                self.finished_game = True
                self.start(self.send_msgs([{"cmd": "StatusUpdate", "status": ClientStatus.CLIENT_GOAL}]))

        def goal_allowed(self) -> bool:
            if self.reachable is None or self.reachable_events is None:  # UT hasn't reported (or failed to generate)
                return True
            return "__Victory__" in self.reachable_events

        # server and tracker updates

        def on_package(self, cmd: str, args: dict) -> None:
            super().on_package(cmd, args)
            if cmd == "Connected":
                Utils.persistent_store("client", "last_manual_game", self.game)
                slot_data = args.get("slot_data") or {}
                goal = slot_data.get("goal")
                if isinstance(goal, int) and 0 <= goal < len(self.victory_names):
                    self.goal_location = self.get_location_by_name(self.victory_names[goal])
                if slot_data.get("death_link"):
                    self.start(self.update_death_link(True))
            self.version += 1

        def on_tracker_updated(self, reachable_locations: list[str]) -> None:
            self.reachable = set(reachable_locations)
            self.version += 1

        def on_tracker_events(self, events: list[str]) -> None:
            self.reachable_events = set(events)
            self.version += 1

        def on_glitches(self, glitched_locations: list[str]) -> None:
            self.glitched = set(glitched_locations)
            self.version += 1

        def aplite_tabs(self) -> list[tuple[str, typing.Any]]:
            """Extra tabs for aplite's client screen."""
            return [("Manual", ManualPane(self))]

    return ManualContext


class ManualTree(Tree):
    BINDINGS = [Binding("s", "scout", "Scout"), Binding("escape", "app.focus_command", show=False)]

    def action_scout(self) -> None:
        node = self.cursor_node
        if node and node.data and node.data[0] == "location" and node.data[2]:
            self.app.notify(f"Scouting {node.data[3]}")
            self.screen.query_one(ManualPane).ctx.scout(node.data[1])


class ManualPane(Vertical):
    """The template's Manual tab: received items and remaining locations, both by category, with a search."""
    DEFAULT_CSS = """
    ManualPane { height: 1fr; }
    ManualPane #manual-search { height: 1; }
    ManualPane Horizontal { height: 1fr; }
    ManualPane Tree { width: 1fr; height: 1fr; }
    ManualPane #manual-items { border-right: vkey $panel; }
    """

    def __init__(self, ctx) -> None:
        super().__init__()
        self.ctx = ctx
        self.shown: tuple | None = None

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Search items and locations (Enter: to the locations)", id="manual-search",
                    classes="tab-search", compact=True)
        with Horizontal():
            yield ManualTree("Items Received", id="manual-items")
            yield ManualTree("Remaining Locations", id="manual-locations")

    def on_mount(self) -> None:
        for tree in self.query(ManualTree):
            tree.show_root = True
            tree.root.expand()
        self.set_interval(REFRESH, self.refresh_trees)
        self.refresh_trees()

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self.refresh_trees()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.query_one("#manual-locations", ManualTree).focus()

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        event.stop()
        data = event.node.data
        if not data:
            return
        ctx = self.ctx
        if data[0] == "location":
            if not ctx.press_allowed(data[3]):
                self.app.notify(f"{data[3]} is out of logic. /send {data[3]} sends it anyway.", severity="warning")
                return
            ctx.send_check(data[1])
        elif data[0] == "goal":
            if not ctx.goal_allowed():
                self.app.notify("The goal is out of logic.", severity="warning")
                return
            ctx.send_goal()
        self.refresh_trees()

    # building

    def refresh_trees(self) -> None:
        ctx = self.ctx
        query = self.query_one("#manual-search", Input).value.strip().casefold()
        hints = ctx.stored_data.get(f"_read_hints_{ctx.team}_{ctx.slot}", []) if ctx.slot else []
        key = (ctx.version, query, len(ctx.items_received), len(ctx.missing_locations), len(hints),
               ctx.slot is not None)
        if key == self.shown:
            return
        self.shown = key
        items = self.query_one("#manual-items", ManualTree)
        locations = self.query_one("#manual-locations", ManualTree)
        if not ctx.slot or not ctx.game:
            for tree in (items, locations):
                tree.clear()
                tree.root.add_leaf(Text("Waiting for connection...", style="dim"))
            return
        self.fill(items, *self.item_groups(), query)
        self.fill(locations, *self.location_groups(hints), query)

    def item_groups(self) -> tuple[str, dict[str, list[tuple[Text, tuple]]]]:
        """Received items by category, with counts, in the order they first arrived (the template's default)."""
        ctx = self.ctx
        counts: dict[str, int] = {}
        for network_item in ctx.items_received:
            name = ctx.item_names.lookup_in_game(network_item.item)
            counts[name] = counts.get(name, 0) + 1
        groups: dict[str, list] = {}
        for name, count in counts.items():
            item = ctx.get_item_by_name(name)
            categories = [c for c in item.get("category", []) if not ctx.category_hidden(c)] or \
                ([] if item.get("category") else [NO_CATEGORY])
            for category in categories:
                groups.setdefault(category, []).append((Text(f"{name} ({count})"), ("item", name)))
        return f"Items Received ({len(ctx.items_received)})", groups

    def location_groups(self, hints: list) -> tuple[str, dict[str, list[tuple[Text, tuple]]]]:
        ctx = self.ctx
        hinted = {hint["location"] for hint in hints if hint["finding_player"] == ctx.slot and not hint["found"]}
        missing = [i for i in ctx.missing_locations if i not in ctx.sent]
        entries = []
        for location_id in missing:
            name = ctx.location_names.lookup_in_game(location_id)
            entries.append((ctx.get_location_by_name(name), location_id, name))
        entries.sort(key=lambda entry: ctx.sort_key(entry[0]))
        groups: dict[str, list] = {}
        for location, location_id, name in entries:
            label = Text(name, style=self.logic_style(name))
            if location.get("hint_text"):
                label.append(f"  {location['hint_text']}", style="dim")
            elif location.get("scoutable"):
                label.append("  [s: scout]", style="dim")
            entry = (label, ("location", location_id, bool(location.get("scoutable")), name))
            categories = [c for c in location.get("category", []) if not ctx.category_hidden(c)] or \
                ([] if location.get("category") else [NO_CATEGORY])
            if location_id in hinted:
                categories = [HINTED, *categories]
            for category in categories:
                groups.setdefault(category, []).append(entry)
        if not ctx.goaled:
            goal = ctx.goal_location
            text = "VICTORY! (seed finished)" if goal["name"] == DEFAULT_GOAL else f"GOAL: {goal['name']}"
            label = Text(text, style="bold")
            for category in [c for c in goal.get("category", []) if not ctx.category_hidden(c)] or [NO_CATEGORY]:
                groups.setdefault(category, []).append((label, ("goal",)))
        return f"Remaining Locations ({len(missing) + (not ctx.goaled)})", groups

    def logic_style(self, name: str) -> str:
        ctx = self.ctx
        if ctx.reachable is None:
            return ""
        if name in ctx.reachable:
            return colors.style("tracker_in_logic")
        if name in ctx.glitched:
            return colors.style("tracker_glitched")
        return ""

    def fill(self, tree: ManualTree, title: str, groups: dict[str, list[tuple[Text, tuple]]], query: str) -> None:
        """Rebuild a tree, keeping which categories are open and where the cursor is. A search shows only matches
        (a matching category shows all of its entries) with their categories open."""
        expanded = {node.data[1] for node in tree.root.children if node.data and node.is_expanded}
        cursor = tree.cursor_node.data if tree.cursor_node else None
        line = tree.cursor_line
        tree.clear()
        tree.root.set_label(title)
        found = None
        for category in sorted(groups, key=category_key):
            entries = groups[category]
            if query and query not in category.casefold():
                entries = [entry for entry in entries if query in entry[0].plain.casefold()]
                if not entries:
                    continue
            label = Text(f"{category} ({len(entries)})")
            if any(entry[0].style for entry in entries):
                label.stylize(colors.style("tracker_in_logic"))
            node = tree.root.add(label, data=("category", category), expand=bool(query) or category in expanded)
            for text, data in entries:
                leaf = node.add_leaf(text, data=data)
                if data == cursor and found is None and node.is_expanded:
                    found = leaf
            if cursor == ("category", category):
                found = node
        tree.root.expand()
        if found is not None:
            tree.call_after_refresh(tree.move_cursor, found, animate=False)
        elif line >= 0:
            tree.call_after_refresh(tree.move_cursor_to_line, max(0, min(line, tree.last_line)))


def run(*args: str) -> None:
    """The "Manual Client" component: same arguments as the template's (an optional .apmanual file)."""
    from CommonClient import get_base_parser
    parser = get_base_parser(description="Manual Client, for operating a Manual game in Archipelago.")
    parser.add_argument("--name", default=None, help="Slot name to connect as.")
    parser.add_argument("apmanual_file", default="", type=str, nargs="?", help="Path to an APMANUAL file")
    parsed, _ = parser.parse_known_args([arg for arg in args if arg != "Manual Client"])
    asyncio.run(main(parsed))


def read_apmanual(path: str) -> dict:
    """An .apmanual file: a zip with a manifest (newer) or base64 JSON (older), as the template reads it."""
    import json
    import zipfile
    from base64 import b64decode
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as opened:
            name = next((n for n in opened.namelist() if n.endswith("archipelago.json")), None)
            if name:
                return json.loads(opened.read(name))
    with open(path) as f:
        return json.loads(b64decode(f.read()))


async def main(args) -> None:
    import os
    from CommonClient import server_loop
    config = {}
    if args.apmanual_file and os.path.exists(args.apmanual_file):
        config = read_apmanual(args.apmanual_file)
        if config.get("server") and not args.connect:
            args.connect = config["server"]
    cls = context_class()
    ctx = cls(args.connect, args.password, config)
    ctx.username = ctx.username or args.name
    ctx.server_task = asyncio.create_task(server_loop(ctx), name="server loop")
    if cls.uses_tracker:
        ctx.run_generator()
    ctx.run_cli()
    await ctx.exit_event.wait()
    ctx.server_address = None
    await ctx.shutdown()
