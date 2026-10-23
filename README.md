# CommanderTUI

Dual-pane terminal file manager: compare two folders, sync them, or move
files/folders between them (and to USB/SMB/any mounted path) — with a
phosphor-green 80s-terminal look and a short modem-style boot animation.

Status: **v1.2.0** — 108/108 tests passing. MIT licensed.

---

## Why this exists

A safe, simple alternative to heavier dual-pane file managers. It was
written after an earlier attempt of the same idea had to be scrapped for
running an unauthenticated local HTTP server. This project deliberately:

- **Never opens a network port.** Pure terminal UI (Textual), no HTTP server (the only outgoing request is the daily update check, which you can turn off),
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

**PyPI (Linux / macOS / WSL).** It is a standard package on [PyPI](https://pypi.org/project/commandertui/) (Python 3.11+). Recommended via `pipx`:

```bash
pipx install commandertui
```

Or inside an activated virtual environment:

```bash
pip install commandertui
```

**macOS.** Same Python package (`pipx install commandertui`), plus
[kitty](https://sw.kovidgoyal.net/kitty/) for the standalone window. Places
lists the volumes under `/Volumes` (system volumes such as Recovery and
Preboot are hidden) and FUSE mounts inside your home folder. To get a
double-clickable app with the CommanderTUI icon:

```bash
packaging/macos/build_app.sh            # builds packaging/macos/CommanderTUI.app
packaging/macos/build_app.sh --install  # also copies it to /Applications
```

The app opens kitty at 1174x637 px and runs `commandertui`. It looks for the
command in `~/.local/bin` (pipx), Homebrew, `/usr/local/bin`, your `PATH`,
and a source checkout's `.venv`. Tested by the author on macOS with a Time
Machine volume, Google Drive and OneDrive mounts.

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
| `F2` | Rename the item under the cursor (never overwrites an existing name) |
| `F5` | Copy marked (or item under cursor) to the other pane |
| `F6` | Move marked to the other pane |
| `F7` | Create a folder in the active pane |
| `F8` / `Delete` | Delete marked (to trash) |
| `/` | Filter the active pane by name (contains, ignores case) as you type; `Enter` keeps it, `Esc` clears it |
| `+` / `-` / `*` | Mark / unmark by pattern (`*.iso`, or plain text = contains) / invert marks, over the rows shown |
| `u` | Update: shows the pending new version (see *Updates*) |
| `i` | Size: file/folder count and total bytes of marked items (background, `Esc` stops it) |
| `h` | Show/hide hidden (dot) files in both panes |
| `p` | Places: jump to a mounted drive (USB/SMB/rclone), saved bookmark or saved network location. Inside: `a` add a network location, `s` save the active pane's folder as a bookmark, `d` delete a saved one |
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

**Network locations (Linux, needs `gio`/GVFS):** in Places press `a`, pick
SMB, SFTP or FTP and type the server's IP or name. For SMB the app lists the
shares on the server and you pick one; SFTP/FTP connect straight in. The
location is saved in `~/.config/commandertui/network.json` (protocol, host
and share only -- never a password) and reappears in Places, mounting itself
when picked. If the server asks for a login, `gio` asks for it in the
terminal and can remember it in the system keyring, exactly like Nautilus.

Every key also has a clickable box on the bottom bar: one row on wide windows
(168+ columns), two rows when narrower (110 columns is the practical minimum).
The active pane has the thick border and the inverted path bar; each path bar
shows the free space of that disk, and the marked count and total size.

## Updates

Once a day, in the background and only after the start-up animation, the app
asks PyPI for the latest version (a plain GET, nothing about you is sent). If
a newer one exists you get a dialog -- **Update now / Later / Skip this
version** -- and the header keeps saying `update X available (press u)`.
It never installs on its own. **Update now** hands the terminal over and runs
the command that matches how it was installed:

| Installed as | Update runs |
|---|---|
| Arch package (`makepkg`) | rebuilds from the release's PKGBUILD with `makepkg -si` (your sudo password is typed in the terminal, never read by the app) |
| pipx | `pipx upgrade commandertui` |
| pip | `pip install --upgrade commandertui` |
| git checkout | no button: the dialog tells you to `git pull` |

Turn the check off with `commandertui --no-update-check` or by setting
`COMMANDERTUI_NO_UPDATE_CHECK=1`. `COMMANDERTUI_FAKE_LATEST=9.9.9` shows the
dialog without a real release, to see how it looks. The logic lives in one
self-contained file, `commandertui/updater.py`, written to be copied into
other apps.

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
├── netlocations.py           # SMB/SFTP/FTP locations via gio (list shares, mount, save)
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
