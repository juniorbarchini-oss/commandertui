# CommanderTUI

Dual-pane terminal file manager: compare two folders, sync them, or move
files/folders between them (and to USB/SMB/any mounted path) — with a
phosphor-green 80s-terminal look and a short modem-style boot animation.

Status: **v0.2.0** — 40/40 tests passing. No
packaging yet.

---

## Why this exists

Successor to two prior attempts (`FolderWorks` desktop apps, `folder-works-tui`)
that were retired for cause — see the vault Ficha for `FolderWorks Linux`
(cancelled: ran an unauthenticated local HTTP server) and the fact that
`folder-works-tui` never worked and was deleted outright. This project
deliberately:

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

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py [left-dir] [right-dir]
```

Or system-wide:

```bash
sudo ./install.sh      # -> /opt/commandertui + /usr/local/bin/commandertui
commandertui           # run inside the current terminal
commandertui-window    # open in its own foot window (app-id "commandertui")
sudo ./uninstall.sh    # add --purge to also remove ~/.config/commandertui
```

On Hyprland, a window rule keyed on that app-id makes it float at a fixed
size, e.g. in `~/.config/hypr/hyprland.lua`:

```lua
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
├── bookmarks.py             # detected mounts (USB/SMB) + saved places
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
different problems; see `FolderWorks Linux`/`FolderWorks Mac` in the vault if
you need a dedup finder specifically.
