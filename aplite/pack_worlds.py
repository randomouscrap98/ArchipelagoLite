"""
Pack core world folders into .apworld files, as upstream's setup.py does for official builds (build_dist.sh runs this
on the build's app/ copy). Zipped worlds go through aplite's per-apworld bytecode cache: a few dozen cache files
instead of thousands of .py/.pyc files, which are slow to open on Windows.

Differences from setup.py: it ships source (portable; aplite compiles once per machine), and it can't import the
worlds (the build has no dependencies), so the game name comes from the world's archipelago.json or, failing that, a
`game = "..."` string in a class in its __init__.py. A world where neither finds exactly one name stays a folder.
Usage: python aplite/pack_worlds.py APP_DIR UPSTREAM_VERSION
"""
import ast
import json
import os
import shutil
import sys
import zipfile


def folder_only_games(app: str) -> set[str]:
    """setup.py's non_apworlds: worlds that need to stay folders."""
    with open(os.path.join(app, "setup.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in tree.body:
        target = node.target if isinstance(node, ast.AnnAssign) else \
            node.targets[0] if isinstance(node, ast.Assign) else None
        if getattr(target, "id", None) == "non_apworlds":
            return set(ast.literal_eval(node.value))
    raise SystemExit("pack_worlds: no non_apworlds in setup.py")


def container_version(app: str) -> int:
    with open(os.path.join(app, "worlds", "Files.py"), encoding="utf-8") as f:
        for node in ast.parse(f.read()).body:
            if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "container_version":
                return ast.literal_eval(node.value)
    raise SystemExit("pack_worlds: no container_version in worlds/Files.py")


def game_name(folder: str, manifest: dict) -> str | None:
    if manifest.get("game"):
        return manifest["game"]
    with open(os.path.join(folder, "__init__.py"), encoding="utf-8-sig") as f:
        tree = ast.parse(f.read())
    games = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for statement in node.body:
                if isinstance(statement, ast.Assign) and any(getattr(t, "id", None) == "game"
                                                             for t in statement.targets):
                    value = statement.value
                elif isinstance(statement, ast.AnnAssign) and getattr(statement.target, "id", None) == "game":
                    value = statement.value
                else:
                    continue
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    games.add(value.value)
    return games.pop() if len(games) == 1 else None


def main() -> None:
    app, version = sys.argv[1:]
    worlds = os.path.join(app, "worlds")
    skip = folder_only_games(app)
    simple_version = ".".join(version.split(".")[:3])
    packed, kept = 0, []
    for name in sorted(os.listdir(worlds)):
        folder = os.path.join(worlds, name)
        if name.startswith(("_", ".")) or not os.path.isfile(os.path.join(folder, "__init__.py")):
            continue  # helper packages (_bizhawk, ...) and files: as upstream
        manifest_file = os.path.join(folder, "archipelago.json")
        manifest = {}
        if os.path.isfile(manifest_file):
            with open(manifest_file, encoding="utf-8") as f:
                manifest = json.load(f)
        game = game_name(folder, manifest)
        if game in skip:
            continue
        if game is None:
            kept.append(name)
            continue
        manifest.update({"game": game, "compatible_version": 7, "version": container_version(app),
                         "minimum_ap_version": simple_version, "maximum_ap_version": simple_version})
        with zipfile.ZipFile(folder + ".apworld", "x", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for root, dirs, files in os.walk(folder):
                dirs[:] = sorted(d for d in dirs if d != "__pycache__")
                for file in sorted(files):
                    path = os.path.join(root, file)
                    relative = os.path.relpath(path, worlds).replace(os.sep, "/")
                    if file != "archipelago.json" or root != folder:
                        z.write(path, relative)
            z.writestr(f"{name}/archipelago.json", json.dumps(manifest))
        shutil.rmtree(folder)
        packed += 1
    print(f"pack_worlds: {packed} worlds packed" + (f"; kept as folders (no game name found): {', '.join(kept)}"
                                                    if kept else ""))


if __name__ == "__main__":
    main()
