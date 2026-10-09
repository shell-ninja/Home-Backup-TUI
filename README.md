# Home Backup TUI

A Textual dashboard for backing up and restoring the important parts of your home
directory. File-system logic lives in `backend.py` (no UI imports); `app.py` is only the
interface.

## Run

```bash
# Easy launcher (automatically manages virtualenv and requirements):
./run.sh

# Or with options:
./run.sh --dir /run/media/you/DISK/backups
./run.sh --theme dracula
```

Alternatively, run manually:
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Needs Python 3.9+ and, preferably, `rsync` (Arch: `sudo pacman -S rsync`). Without rsync the
app falls back to a built-in Python copier. Use a terminal of at least 100x30; smaller
windows scroll.

Options:
- `--dir PATH`: prefills the backup directory
- `--engine auto|rsync|python`: copy engine
- `--home PATH`: treats another directory as home (safe for trying the app out)
- `--theme THEME`: initial theme (e.g. `catppuccin-mocha`, `tokyo-night`, `nord`, `dracula`, `gruvbox`, `cyberpunk`, `everforest`, `kanagawa`)

## Using it

| Key | Action |
| :--- | :--- |
| `F1` or `b` | Switch to **Backup mode** |
| `F2` or `r` | Switch to **Restore mode** |
| `Ctrl+O` | Open **Directory Chooser** (native XDG Desktop Portal or fallback picker) |
| Click **📁 Choose...** button | Open **Directory Chooser** |
| `e` / `Enter` / `Right` | Expand directory in **Additional items** pane |
| `e` / `Left` | Collapse expanded directory (or navigate up to parent folder) |
| Click **Expand [e]** | Toggle expand/collapse of highlighted directory or typed path |
| `Space` | Toggle item checkbox in selection list |
| `t` or `Ctrl+T` | Open **Theme Picker** command palette (persists across launches) |
| Click `󰔎 <theme>` button | Open **Theme Picker** |
| `Enter` in path field | List snapshots in that directory |
| `Ctrl+Q` or `q` | Quit application (cleanly terminates any active backup) |

**Backup mode**
1. Select the parent backup directory: click **📁 Choose...** (or press `Ctrl+O`) to open your desktop's native file chooser (XDG Desktop Portal), or type/paste the path manually.
2. Essentials are detected and pre-ticked: `.gitconfig`, `.ssh`, VSCodium (`.config/VSCodium`,
   `.var/app/com.vscodium.codium`, `.vscode-oss`, ...), Brave (`.config/BraveSoftware` and the
   Flatpak path) and anything named *antigravity* (`antigravity`, `.antigravity`,
   `.config/Antigravity`, ...). Only paths that exist are listed.
3. Under **Additional items from ~**, expand any directory (`▸`) to inspect subfolders and files.
   Select any specific file or subfolder to back up, or tick the parent directory.
   You can also type a path or folder (e.g. `.config/nvim`) and press **Add** or **Expand**.
4. Optionally enable **Dry run**, then press **Start backup**.

The snapshot is written to `<parent>/<username>-<DD-MM-YYYY_HH-MM-SS>/` (mode 700), copied with
`rsync -a --info=progress2`. A `.manifest` file lists the top-level items in the snapshot; it is
written last, so an interrupted backup has no manifest and is ignored by Restore.

**Restore mode**
1. Pick a snapshot (newest first) and untick any items you do not want.
2. Choose what happens when a file already exists:
   skip, overwrite, or back up the existing file first (into `~/.restore-backup-<timestamp>/`) and then overwrite.
3. Optionally enable **Dry run**; a real restore asks for confirmation first.
4. If `.ssh` was restored, permissions are re-applied: directories 700, `*.pub` 644, all other files 600.

## Error handling

- Missing or unmounted backup drive: `/run/media/...` and `/media/...` paths must be real
  mount points, and the destination directory is never auto-created, so a backup cannot
  silently fill your system disk.
- Permission denied, read-only media, no space left (checked up front) and vanished
  files are reported in the activity log; one failing item does not stop the others.
- A warning is shown for FAT/exFAT/NTFS destinations, which cannot keep Unix permissions.
- Backups into a folder that is inside a selected item are refused.

Tip: close Brave and VSCodium before backing up so the profiles are copied in a consistent state.

## Tests

```bash
python -m unittest discover -s tests -v     # or: pip install pytest && pytest
```
