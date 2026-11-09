# Archipelago Lite

Archipelago Lite is Archipelago in your terminal. It runs the same clients, generator and server as the official
Archipelago, but everything opens as a text interface instead of desktop windows. It works over SSH, on a Steam Deck
in desktop mode, or anywhere you'd rather not deal with the full GUI.

It's based on Archipelago 0.6.8, so games, YAMLs, apworlds and seeds are all compatible with the official version.

## Setup

Grab the download for your system from the
[releases page](https://github.com/randomouscrap98/ArchipelagoLite/releases).

Lite keeps its data (host.yaml, your Players folder, custom_worlds, output) in the same places the official
Archipelago does. If you already have Archipelago installed, set Lite up next to it and the two will share
everything: your YAMLs, your apworlds and your settings. You can keep using both.

### Windows

1. Download the Windows zip.
2. Right-click it, choose **Extract All**, and change the destination to your existing Archipelago folder, so
   `aplite.exe` ends up next to `ArchipelagoLauncher.exe`. If you used the official installer, that's usually
   `C:\ProgramData\Archipelago`. Don't have Archipelago? Keep the suggested folder, or pick any folder you like.
3. Double-click `aplite.exe`.

It opens in Windows Terminal if you have it, which looks best. If your antivirus blocks `aplite.exe`, use
`aplite_fallback.cmd` instead.

### Linux: tar.gz

1. Download the tar.gz.
2. Extract it into your existing Archipelago folder, the one with `ArchipelagoLauncher` in it:
   `tar -xzf ArchipelagoLite_*.tar.gz --strip-components=1 -C path/to/Archipelago`.
   No Archipelago yet? Just extract it anywhere.
3. Run `./aplite`, or double-click it.

### Linux: AppImage

Download the AppImage, make it executable, and run it. It uses `~/.local/share/Archipelago`, the same folder as the
official Archipelago AppImage, so there's nothing to set up.

### Updating

Extract the new download over your current one, into the same folder (for the AppImage, just replace the file).
Your files are kept.

### Moving over from an older Lite

Older versions of Lite kept your files inside the `app` folder. The first time you start a newer version, it moves
them out next to the launcher and tells you what it moved.

## The launcher

Starting Lite with nothing else opens the launcher: a list of everything you can run. Type to search, then press
Enter to open it. Clients, tools and settings are all here.

A few keys work everywhere:

| Key    | What it does                                        |
|--------|-----------------------------------------------------|
| Ctrl+Q | Quit                                                |
| Ctrl+L | Back to the launcher                                |
| Ctrl+P | Options: theme, colors, glyphs and more             |

The bottom bar of each screen shows the keys for that screen.

### Skipping the launcher

You can also open anything directly by name, or by giving it a patch file:

```
aplite "Text Client"
aplite Generate
aplite mygame.apbp
```

On Windows that's `aplite.exe`, and from source it's `./aplite.sh`. Clients take the same options as the official
ones, so `aplite "Text Client" --connect archipelago.gg:38281 --name Me` connects right away. `aplite --list` shows
every name you can use, and `aplite <name> --help` shows its options.

## Playing

Pick your game's client in the launcher, or open a patch file with **Open Patch**. Type the server address into the
connect bar (for example `archipelago.gg:38281`), then your slot name when asked.

Once you're connected, the client has tabs for the log, your items, hints and (with Universal Tracker installed) a
tracker. Ctrl+F searches the current tab. Commands like `/hint` work just like the official client, and Tab
completes them.

Manual games are supported too: Lite has its own Manual client, so any manual apworld works.

## Making a game

1. **Options Creator** makes a player YAML for any installed game. It saves to your Players folder.
2. **Generate Seed** builds the multiworld. Pick the YAMLs you want from the file list on the left (Enter adds one,
   Ctrl+O adds everything in your Players folder), then press Ctrl+G. It checks your YAMLs for problems before it
   starts.
3. When it's done, you can upload the game to archipelago.gg or host it yourself, right from the same screen.

**Self-host** runs the game on your own computer, room page included, so your friends can connect and download
their patches just like on the website.

## Apworlds

**APWorld Manager** lets you browse the apworld repositories, install new games and update the ones you have. You
can also pick a specific version if the newest one gives you trouble. Apworlds go in your `custom_worlds` folder,
which is shared with the official Archipelago if you set Lite up next to it.

## Settings

**Open host.yaml** edits Archipelago's settings (ROM paths, server options and so on) without needing a text editor.

The Ctrl+P options menu has settings for Lite itself:

- **Theme** and **Client Colors** change how things look.
- **Glyphs: switch to simple** is for terminals that show boxes or question marks where the lines and blocks
  should be.
- **Background spares** (Windows and Linux) keeps a copy of Lite loaded in the background, so it opens almost
  instantly next time. It's off by default. On Windows, turn it off before updating.

## Running from source (macOS and Linux)

There's no Mac download, but Lite runs fine from source. You need `git` and [uv](https://docs.astral.sh/uv/), which
downloads the right Python and everything else for you.

1. Install uv. On a Mac with Homebrew that's `brew install uv`; otherwise follow the
   [uv install guide](https://docs.astral.sh/uv/getting-started/installation/).
2. Get the code:
   `git clone https://github.com/randomouscrap98/ArchipelagoLite.git`
3. Start it:
   `cd ArchipelagoLite` and then `./aplite.sh`

The first start takes a while because it installs everything. After that it starts right up.

To update, run `git pull` in the same folder. The next start installs anything new on its own.

When running from source, your files (host.yaml, Players, custom_worlds, output) live in the ArchipelagoLite folder
itself.

## Help and bug reports

Found a bug or got stuck? Open an issue on
[GitHub](https://github.com/randomouscrap98/ArchipelagoLite/issues). If something crashed, the log files in the
`logs` folder are very helpful to include.
