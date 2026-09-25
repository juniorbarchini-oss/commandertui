# CommanderTUI

Dual-pane terminal file manager: compare two folders, sync them, or move
files/folders between them (and to USB/SMB/any mounted path) — with a
phosphor-green 80s-terminal look and a short modem-style boot animation.

Status: **v1.0.0** — 40/40 tests passing. MIT licensed.

---

## Why this exists

A safe, simple alternative to heavier dual-pane file managers. It was
written after an earlier attempt of the same idea had to be scrapped for
running an unauthenticated local HTTP server. This project deliberately:

- **Never opens a network port.** Pure terminal UI (Textual), no HTTP server,
  no webview.
- **Never does a raw `rm -rf`.** Deletes always go through the desktop trash
  (`send2trash` → `gio trash` → manual move into `~/.local/share/Trash`, in
  that order).
- **Never overwrites in place.** Copies stream into a hidden `.tmp` file next
  to the destination and are committed with `os.replace` — a crash or
  Ctrl+C never leaves a half-written file.
- **Never guesses on a conflict.** Two-way sync copies each side's exclusive
  files automatically, but a file changed on *both* sides is always shown to
  you, never auto-resolved.

## Install

**Arch Linux / Omarchy.** An AUR package is on the way (`yay -S commandertui`).
Until then, build it from the `PKGBUILD` in this repo. `makepkg` pulls in
everything it needs:

```bash
git clone https://github.com/juniorbarchini-oss/commandertui.git
cd commandertui/packaging/arch
makepkg -si
```

Remove it with `sudo pacman -R commandertui`.

**Other distros.** It is a regular Python package (Python 3.11+):

```bash
pipx install git+https://github.com/juniorbarchini-oss/commandertui.git
```

**From a source checkout** (development):

```bash
python3 -m venv .venv && .venv/bin/pip install -e . -r requirements-dev.txt
bin/commandertui [left-dir] [right-dir]
```

## Run

| Command | What it does |
|---|---|
| `cmdrtui` | Opens it in its own terminal window. This is also what the **CommanderTUI** app-launcher entry runs |
| `commandertui` | Runs it inside the terminal you are in |
| `commandertui DIR1 DIR2` | Opens with those two folders side by side |

`cmdrtui` uses your desktop's default terminal (`xdg-terminal-exec`). If that
isn't available it tries foot, alacritty, kitty and ghostty. The window
always gets the app-id/class `commandertui`, so a window-manager rule can
float it. It needs about 1174x637 px to fit every key hint. On Hyprland:

```ini
# hyprland.conf
windowrulev2 = float, class:^(commandertui)$
windowrulev2 = size 1174 637, class:^(commandertui)$
windowrulev2 = center, class:^(commandertui)$
```

```lua
-- Omarchy Lua config (~/.config/hypr/hyprland.lua)
o.window("^commandertui$", { float = true, center = true, size = { 1174, 637 } })
```

## Keys

| Key | Action |
|---|---|
| `Tab` | Switch active pane |
| `↑`/`↓`, `Enter` | Navigate, open directory |
| `Backspace` | Go to parent directory |
| `Space` | Mark/unmark item under cursor |
| `F5` | Copy marked (or item under cursor) to the other pane |
| `F6` | Move marked to the other pane |
| `F7` | Create a folder in the active pane |
| `F8` / `Delete` | Delete marked (to trash) |
| `i` | Size: file/folder count and total bytes of marked items (background, `Esc` stops it) |
| `h` | Show/hide hidden (dot) files in both panes |
| `p` | Places: jump to a mounted drive (USB/SMB/rclone) or saved bookmark |
| `c` | Compare: full recursive diff between the two panes' current folders |
| `r` | Refresh both panes |
| `q` | Quit |

While a copy/move runs, the progress window shows two bars (current file
and whole job, by bytes) plus speed in Mb/s and time left. `Esc` asks to
cancel: finished files stay, the file in progress is discarded (never left
half-written), and a move never removes a source it didn't fully copy.

**Places** lists only what is mounted right now: USB drives
(`/run/media`, `/media`, `/mnt` — checked with `ismount`, so an empty
on-demand mount point doesn't show), GVFS/Nautilus network shares, FUSE
mounts inside `$HOME` (e.g. `rclone mount`), and saved bookmarks.

Inside **Compare**:

| Key | Action |
|---|---|
| `m` | Mirror: make right identical to left |
| `u` | Update: copy new/changed left → right, never deletes |
| `b` | Two-way: propagate each side's exclusive changes; conflicts (changed on both sides) are listed, never guessed |
| `Space` | Mark a row for manual handling |
| `c` | Copy marked rows in whichever direction they diverge |
| `Esc` | Back to the panes |

Every bulk or destructive action shows a plain-language summary (count of
operations, total bytes) and requires `y` to confirm.

## Architecture

```
commandertui/
├── models.py          # FileEntry, DiffEntry, OperationQueue, QueuedOp
├── scanner.py          # os.scandir recursive tree walk
├── diff.py             # pure comparison: left-only / right-only / modified / identical
├── plan.py              # diff -> OperationQueue (mirror / update / two-way)
├── sync.py               # atomic copy/move (streamed, tmp+replace), checksum verify
├── trash.py               # 3-layer safe delete (send2trash / gio trash / manual)
├── executor.py             # runs an OperationQueue against the filesystem
├── bookmarks.py             # detected mounts (USB/SMB/rclone) + saved places
├── theme.py                  # green/amber phosphor palettes
├── boot.py                    # startup "modem" reveal animation
├── widgets.py                  # FilePanel: one pane's directory listing
├── modals.py                    # Confirm / Places / Input / Size / Message screens
├── progress_screen.py            # two-bar transfer progress + cancel
├── run_queue.py                   # confirm -> run queue in a worker thread
├── compare_screen.py              # full diff view + bulk/manual sync actions
├── app.py                          # CommanderApp: wires panes + keybindings
└── cli.py                           # argument parsing, entrypoint
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest
black commandertui tests   # formatter, line length set in pyproject.toml
```

Engine (scanner/diff/plan/sync/executor) is fully unit tested against a real
filesystem (`tmp_path`). The UI has smoke tests via Textual's `Pilot`
(boot sequence dismisses, pane switching, mark→copy→confirm end to end).

## Not in scope

No file preview, no deduplication engine, no desktop GUI wrapper — those are
different problems.

## Credits

Designed and tested by Humberto Barchini. The code was written together
with [Claude](https://www.anthropic.com/claude) (Anthropic) through
[Claude Code](https://claude.com/claude-code): **Claude Sonnet 5** built the
first version, **Claude Opus 5.5** did most of v0.2 and the v1.0 packaging.

## License

MIT — see [LICENSE](LICENSE).
