#!/usr/bin/env python3
"""app.py - Textual front-end for the home backup / restore tool.

All file-system work lives in backend.py; this module only renders widgets,
collects choices and runs the backend in worker threads.

    python app.py                      # start the dashboard
    python app.py --dir /mnt/backups   # prefill the backup directory
"""

from __future__ import annotations

import argparse
import os
import subprocess
import threading
import traceback
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Optional

try:
    from textual.theme import Theme
except ImportError:
    Theme = None

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DirectoryTree,
    Footer,
    Header,
    Input,
    Label,
    OptionList,
    ProgressBar,
    RadioButton,
    RadioSet,
    RichLog,
    SelectionList,
    Static,
    Switch,
    TabbedContent,
    TabPane,
)
from textual.widgets.option_list import Option
from textual.widgets.selection_list import Selection

import backend
from backend import BackupError, RestorePolicy

LOG_STYLES = {
    "info": ("•", "cyan"),
    "ok": ("✔", "bold green"),
    "warn": ("▲", "bold yellow"),
    "error": ("✖", "bold red"),
    "dim": (" ", "dim"),
}

POLICIES = [
    RestorePolicy.SKIP,
    RestorePolicy.OVERWRITE,
    RestorePolicy.BACKUP_THEN_OVERWRITE,
]

CUSTOM_THEMES = [
    Theme(
        name="cyberpunk",
        primary="#00ff9f",
        secondary="#ff007f",
        accent="#00e5ff",
        warning="#fcee0a",
        error="#ff003c",
        success="#00ff9f",
        background="#080812",
        surface="#121324",
        panel="#1a1b35",
        dark=True,
    ),
    Theme(
        name="everforest",
        primary="#a7c080",
        secondary="#7fbbb3",
        accent="#dbbc7f",
        warning="#dbbc7f",
        error="#e67e80",
        success="#a7c080",
        background="#2d353b",
        surface="#343f44",
        panel="#3d484d",
        dark=True,
    ),
    Theme(
        name="kanagawa",
        primary="#7e9cd8",
        secondary="#957fb8",
        accent="#7aa89f",
        warning="#e6c384",
        error="#c34043",
        success="#98bb6c",
        background="#1f1f28",
        surface="#2a2a37",
        panel="#363646",
        dark=True,
    ),
] if Theme else []

CONFIG_DIR = Path.home() / ".config" / "home-backup-tui"
THEME_FILE = CONFIG_DIR / "theme.txt"


def load_saved_theme() -> Optional[str]:
    try:
        if THEME_FILE.is_file():
            content = THEME_FILE.read_text().strip()
            if content:
                return content
    except Exception:
        pass
    return None


def save_theme(theme_name: str) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        THEME_FILE.write_text(theme_name.strip() + "\n")
    except Exception:
        pass


def is_portal_supported() -> bool:
    """Check if native XDG Desktop Portal is available."""
    try:
        import portal
        return portal.is_portal_available()
    except Exception:
        pass
    script = Path(__file__).resolve().parent / "portal.py"
    if script.is_file():
        try:
            res = subprocess.run(["python3", str(script), "--check"], timeout=3)
            return res.returncode == 0
        except Exception:
            pass
    return False


def choose_directory_portal(current_folder: Optional[str] = None) -> Optional[str]:
    """Invoke native XDG Desktop Portal FileChooser in folder mode."""
    try:
        import portal
        res = portal.pick_directory_portal(current_folder=current_folder)
        if res:
            return res
    except Exception:
        pass
    script = Path(__file__).resolve().parent / "portal.py"
    if script.is_file():
        try:
            cmd = ["python3", str(script)]
            if current_folder:
                cmd.append(str(current_folder))
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=180)
            if proc.returncode == 0:
                out = proc.stdout.strip()
                if out:
                    return out
        except Exception:
            pass
    return None


class DirectoryPickerModal(ModalScreen[Optional[str]]):
    """In-TUI fallback directory picker when XDG Desktop Portal is unavailable."""

    AUTO_FOCUS = "#picker-tree"
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    DirectoryPickerModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.75);
    }
    #picker-dialog {
        width: 80;
        height: 24;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    #picker-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    #picker-tree {
        height: 1fr;
        border: round $primary;
        background: transparent;
    }
    #picker-actions {
        height: 3;
        margin-top: 1;
        align-horizontal: right;
    }
    #picker-actions Button {
        margin-left: 2;
    }
    """

    def __init__(self, initial_dir: Optional[str] = None) -> None:
        super().__init__()
        self.initial_dir = initial_dir or str(Path.home())

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-dialog"):
            yield Label("📁 Select Backup Directory", id="picker-title")
            yield DirectoryTree(self.initial_dir, id="picker-tree")
            with Horizontal(id="picker-actions"):
                yield Button("Cancel", id="picker-cancel")
                yield Button("Select", id="picker-select", variant="primary")

    @on(Button.Pressed, "#picker-select")
    def _select(self) -> None:
        tree = self.query_one("#picker-tree", DirectoryTree)
        if tree.cursor_node and tree.cursor_node.data:
            node_path = tree.cursor_node.data.path
            self.dismiss(str(node_path) if node_path.is_dir() else str(node_path.parent))
        else:
            self.dismiss(self.initial_dir)

    @on(Button.Pressed, "#picker-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    """Small yes/no dialog used before a real (non dry-run) restore."""

    AUTO_FOCUS = "#no"
    BINDINGS = [Binding("escape", "answer_no", "Cancel")]

    def __init__(self, title: str, lines: list[str], confirm_label: str) -> None:
        super().__init__()
        self._title = title
        self._lines = lines
        self._confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, id="dialog-title")
            for line in self._lines:
                yield Label(Text(line), classes="dialog-line")
            with Horizontal(id="dialog-buttons"):
                yield Button("Cancel", id="no")
                yield Button(self._confirm_label, id="yes", variant="error")

    @on(Button.Pressed, "#yes")
    def _yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def _no(self) -> None:
        self.dismiss(False)

    def action_answer_no(self) -> None:
        self.dismiss(False)


class BackupApp(App[None]):
    TITLE = "Home Backup TUI"
    AUTO_FOCUS = "#essentials"

    CSS = """
    /* Below roughly 100x30 the content scrolls instead of overlapping. */
    #main { height: 1fr; padding: 0 1; overflow-y: auto; }

    #path-row { height: 3; }
    #path-row Label { width: auto; padding: 1 2 0 0; text-style: bold; color: $accent; }
    #backup-path { width: 1fr; }
    #btn-choose-dir {
        height: 3;
        width: auto;
        min-width: 14;
        margin-left: 1;
        background: $panel;
        color: $accent;
        border: none;
    }
    #btn-choose-dir:hover {
        background: $accent;
        color: $background;
    }
    #btn-theme-switcher {
        height: 3;
        width: auto;
        min-width: 20;
        margin-left: 1;
        padding: 0 1;
        background: $panel;
        color: $primary;
        border: none;
    }
    #btn-theme-switcher:hover {
        background: $primary;
        color: $background;
    }

    TabbedContent { height: 1fr; min-height: 19; }
    TabPane { padding: 0; }
    .panes { height: 1fr; }
    .pane {
        width: 1fr; height: 1fr;
        border: round $primary; padding: 0 1; margin-right: 1;
    }
    .pane:focus-within { border: round $accent; }
    .pane SelectionList, .pane OptionList {
        height: 1fr; border: none; padding: 0; background: transparent;
    }
    .add-row { height: 3; }
    .add-row Input { width: 1fr; }
    .add-row Button { margin-left: 1; }
    .refresh-row { height: 3; }

    #policy { width: 1fr; height: auto; border: round $primary; background: transparent; margin-right: 1; }

    .actions { height: auto; min-height: 3; align-vertical: middle; }
    .actions Label { padding: 0 1 0 0; }
    .actions Switch { margin-right: 3; }
    .actions Button { margin-right: 2; }

    #activity {
        height: 30%; min-height: 10; max-height: 22;
        border: round $secondary; padding: 0 1;
    }
    #progress-row { height: 1; }
    #progress { width: 1fr; }
    #cancel { height: 1; width: 10; min-width: 10; border: none; margin-left: 1; }
    #status { height: 1; color: $text-muted; }
    #log { height: 1fr; background: transparent; }

    ConfirmScreen { align: center middle; }
    #dialog {
        width: 72; height: auto; padding: 1 2;
        border: thick $error; background: $surface;
    }
    #dialog-title { text-style: bold; color: $error; margin-bottom: 1; }
    .dialog-line { width: 100%; }
    #dialog-buttons { height: 3; margin-top: 1; align-horizontal: right; }
    #dialog-buttons Button { margin-left: 2; }
    """

    BINDINGS = [
        Binding("f1", "show_mode('backup-tab')", "Backup mode"),
        Binding("f2", "show_mode('restore-tab')", "Restore mode"),
        Binding("b", "show_mode('backup-tab')", "Backup", show=False),
        Binding("r", "show_mode('restore-tab')", "Restore", show=False),
        Binding("ctrl+o", "choose_directory", "Browse dir"),
        Binding("t", "change_theme", "Theme"),
        Binding("ctrl+t", "change_theme", "Theme", show=False),
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("q", "quit", "Quit", priority=False),
    ]

    def __init__(
        self,
        initial_dir: str = "",
        home: Optional[Path] = None,
        engine: str = "auto",
        theme: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.home = Path(home).expanduser() if home else Path.home()
        self.engine = engine
        self._initial_dir = initial_dir
        self._cancel = threading.Event()
        self._busy = False
        self._snapshots: dict[str, backend.Snapshot] = {}
        self._current_snapshot: Optional[backend.Snapshot] = None
        self._essential_values: set[str] = set()
        self._extra_values: set[str] = set()
        self._expanded_dirs: set[str] = set()
        self._custom_extras: set[str] = set()

        for t in CUSTOM_THEMES:
            try:
                self.register_theme(t)
            except Exception:
                pass

        chosen_theme = theme or load_saved_theme() or "catppuccin-mocha"
        try:
            self.theme = chosen_theme
        except Exception:
            try:
                self.theme = "tokyo-night"
            except Exception:
                pass

    def _watch_theme(self, theme_name: str) -> None:
        super()._watch_theme(theme_name)
        try:
            self.stylesheet.set_variables(self.get_css_variables())
            self.stylesheet.reparse()
            for s in self.screen_stack:
                self.stylesheet.update(s)
                s.refresh(layout=True)
        except Exception:
            pass
        try:
            btn = self.query_one("#btn-theme-switcher", Button)
            btn.label = f"󰔎 {theme_name}"
        except Exception:
            pass
        save_theme(theme_name)

    @on(Button.Pressed, "#btn-theme-switcher")
    def _on_theme_pressed(self) -> None:
        self.action_change_theme()

    @on(Button.Pressed, "#btn-choose-dir")
    def _on_choose_dir_pressed(self) -> None:
        self.action_choose_directory()

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="main"):
            with Horizontal(id="path-row"):
                yield Label("Backup directory")
                yield Input(
                    value=self._initial_dir,
                    placeholder="Parent folder for snapshots, e.g. /run/media/you/DISK/backups",
                    id="backup-path",
                )
                yield Button("📁 Choose...", id="btn-choose-dir", tooltip="Select location via XDG Desktop Portal [Ctrl+O]")
                yield Button(f"󰔎 {self.theme}", id="btn-theme-switcher", tooltip="Theme Picker [t]")
            with TabbedContent(id="modes", initial="backup-tab"):
                with TabPane("Backup  [F1]", id="backup-tab"):
                    with Horizontal(classes="panes"):
                        with Vertical(classes="pane", id="essentials-pane"):
                            yield SelectionList[str](id="essentials")
                        with Vertical(classes="pane", id="extras-pane"):
                            yield SelectionList[str](id="extras")
                            with Horizontal(classes="add-row"):
                                yield Input(
                                    placeholder="Add a path or folder, e.g. .config/nvim",
                                    id="extra-input",
                                )
                                yield Button("Add", id="add-extra")
                                yield Button("Expand [e]", id="expand-extra")
                    with Horizontal(classes="actions"):
                        yield Label("Dry run")
                        yield Switch(value=False, id="backup-dry")
                        yield Button("▶ Start backup", id="start-backup", variant="success")
                with TabPane("Restore  [F2]", id="restore-tab"):
                    with Horizontal(classes="panes"):
                        with Vertical(classes="pane", id="snapshots-pane"):
                            yield OptionList(id="snapshots")
                            with Horizontal(classes="refresh-row"):
                                yield Button("Refresh", id="refresh-snaps")
                        with Vertical(classes="pane", id="snap-items-pane"):
                            yield SelectionList[str](id="snap-items")
                    with Horizontal(classes="actions"):
                        with RadioSet(id="policy"):
                            yield RadioButton(RestorePolicy.SKIP.label, value=True)
                            yield RadioButton(RestorePolicy.OVERWRITE.label)
                            yield RadioButton(RestorePolicy.BACKUP_THEN_OVERWRITE.label)
                        yield Label("Dry run")
                        yield Switch(value=False, id="restore-dry")
                        yield Button("↩ Restore", id="start-restore", variant="warning")
            with Vertical(id="activity"):
                with Horizontal(id="progress-row"):
                    yield ProgressBar(total=100, show_eta=False, id="progress")
                    yield Button("Cancel", id="cancel", variant="error", disabled=True)
                yield Static("Idle", id="status")
                yield RichLog(id="log", wrap=True, markup=False, highlight=False, max_lines=5000)
        yield Footer()

    def on_mount(self) -> None:
        try:
            btn = self.query_one("#btn-theme-switcher", Button)
            btn.label = f"󰔎 {self.theme}"
        except Exception:
            pass
        self.sub_title = f"home: {self.home}"
        self.query_one("#essentials-pane").border_title = "Essentials · auto-detected"
        self.query_one("#extras-pane").border_title = "Additional items from ~ · [e] expand/collapse"
        self.query_one("#snapshots-pane").border_title = "Snapshots (newest first)"
        self.query_one("#snap-items-pane").border_title = "Items to restore"
        self.query_one("#policy").border_title = "If a file already exists"
        self.query_one("#activity").border_title = "Activity"

        engine = "rsync" if backend.rsync_available() else "built-in Python copier (rsync not found)"
        if self.engine == "python":
            engine = "built-in Python copier"
        self._write_log("info", f"Home: {self.home}   Engine: {engine}")
        self._populate_backup_lists()
        if self._initial_dir:
            self._refresh_snapshots(quiet=True)

    # ----------------------------------------------------------- backup lists

    def _populate_backup_lists(self) -> None:
        essentials_list = self.query_one("#essentials", SelectionList)
        found = backend.detect_essentials(self.home)
        self._essential_values = {item.rel for item in found}
        essentials_list.add_options(
            [
                Selection(
                    Text.assemble((item.rel, "bold"), (f"   {item.group}", "dim")),
                    item.rel,
                    True,
                )
                for item in found
            ]
        )
        missing = [g for g in backend.ESSENTIAL_GROUPS if g not in {i.group for i in found}]
        if missing:
            self._write_log("dim", f"Not found under home: {', '.join(missing)}")
        if not found:
            self._write_log("warn", "None of the essential items exist under this home directory.")

        self._refresh_extras_list()

    def _refresh_extras_list(self, keep_cursor_rel: Optional[str] = None) -> None:
        """Render the extras list hierarchically with expandable folders."""
        extras_list = self.query_one("#extras", SelectionList)
        cur_selected = set(extras_list.selected) if hasattr(extras_list, "selected") else set()

        if keep_cursor_rel is None:
            idx = extras_list.highlighted
            if idx is not None and 0 <= idx < extras_list.option_count:
                try:
                    keep_cursor_rel = extras_list.get_option_at_index(idx).value
                except Exception:
                    pass

        try:
            entries = backend.list_home_entries(self.home)
        except BackupError as exc:
            self._write_log("error", str(exc))
            return

        options: list[Selection[str]] = []
        self._extra_values.clear()

        def add_sub_entries(parent_rel: str, depth: int) -> None:
            indent = "  " * depth
            try:
                sub_entries = backend.list_dir_entries(parent_rel, self.home)
            except BackupError:
                return
            for sub in sub_entries:
                self._extra_values.add(sub.rel)
                sub_name = sub.rel.rsplit("/", 1)[-1]
                is_sub_dir = sub.is_dir
                is_sub_expanded = sub.rel in self._expanded_dirs
                is_sel = sub.rel in cur_selected
                if is_sub_dir:
                    icon = "▾ " if is_sub_expanded else "▸ "
                    color = "bold cyan" if is_sub_expanded else "bold yellow"
                    label = Text.assemble((indent + icon, color), (sub_name + "/", "bold"))
                    options.append(Selection(label, sub.rel, is_sel))
                    if is_sub_expanded:
                        add_sub_entries(sub.rel, depth + 1)
                else:
                    label = Text.assemble((indent + "  ", "dim"), (sub_name, "italic"))
                    options.append(Selection(label, sub.rel, is_sel))

        for entry in entries:
            if entry.rel in self._essential_values:
                continue
            self._extra_values.add(entry.rel)
            is_expanded = entry.rel in self._expanded_dirs
            is_sel = entry.rel in cur_selected
            if entry.is_dir:
                icon = "▾ " if is_expanded else "▸ "
                color = "bold cyan" if is_expanded else "bold yellow"
                label = Text.assemble((icon, color), (entry.rel + "/", "bold"))
                options.append(Selection(label, entry.rel, is_sel))
                if is_expanded:
                    add_sub_entries(entry.rel, 1)
            else:
                label = Text(entry.rel, style="italic")
                options.append(Selection(label, entry.rel, is_sel))

        # Include custom added items if not already shown
        for custom_rel in sorted(self._custom_extras):
            if custom_rel not in self._extra_values and custom_rel not in self._essential_values:
                self._extra_values.add(custom_rel)
                is_sel = custom_rel in cur_selected
                is_dir = (self.home / custom_rel).is_dir()
                label = Text(custom_rel + ("/" if is_dir else ""), style="bold")
                options.append(Selection(label, custom_rel, is_sel))

        extras_list.clear_options()
        extras_list.add_options(options)

        if keep_cursor_rel is not None:
            for i in range(extras_list.option_count):
                if extras_list.get_option_at_index(i).value == keep_cursor_rel:
                    extras_list.highlighted = i
                    break

        self._update_expand_button_label()

    def _update_expand_button_label(self) -> None:
        try:
            btn = self.query_one("#expand-extra", Button)
            extras_list = self.query_one("#extras", SelectionList)
            idx = extras_list.highlighted
            if idx is not None and 0 <= idx < extras_list.option_count:
                rel = extras_list.get_option_at_index(idx).value
                if (self.home / rel).is_dir():
                    btn.label = "Collapse [e]" if rel in self._expanded_dirs else "Expand [e]"
                    return
            btn.label = "Expand [e]"
        except Exception:
            pass

    def _toggle_expand_dir(self, rel: str) -> None:
        target = self.home / rel
        if not target.is_dir():
            self.notify(f"'{rel}' is not a directory.", severity="information")
            return
        if rel in self._expanded_dirs:
            # Collapse directory and any subdirectories
            self._expanded_dirs = {d for d in self._expanded_dirs if d != rel and not d.startswith(rel + "/")}
            self._refresh_extras_list(keep_cursor_rel=rel)
            self.notify(f"Collapsed {rel}/")
        else:
            # Expand directory (and its parent ancestors if any)
            parts = rel.split("/")
            for i in range(1, len(parts)):
                self._expanded_dirs.add("/".join(parts[:i]))
            self._expanded_dirs.add(rel)
            self._refresh_extras_list(keep_cursor_rel=rel)
            self.notify(f"Expanded {rel}/")

    def on_key(self, event: Key) -> None:
        if self.focused and self.focused.id == "extras":
            if event.key in ("e", "enter", "right", "left"):
                extras_list = self.query_one("#extras", SelectionList)
                idx = extras_list.highlighted
                if idx is not None and 0 <= idx < extras_list.option_count:
                    rel = extras_list.get_option_at_index(idx).value
                    is_dir = (self.home / rel).is_dir()
                    if event.key == "left":
                        if rel in self._expanded_dirs:
                            event.prevent_default()
                            event.stop()
                            self._toggle_expand_dir(rel)
                        elif "/" in rel:
                            parent_rel = rel.rsplit("/", 1)[0]
                            for i in range(extras_list.option_count):
                                if extras_list.get_option_at_index(i).value == parent_rel:
                                    event.prevent_default()
                                    event.stop()
                                    extras_list.highlighted = i
                                    break
                    elif event.key == "right":
                        if is_dir and rel not in self._expanded_dirs:
                            event.prevent_default()
                            event.stop()
                            self._toggle_expand_dir(rel)
                    else:  # "e" or "enter"
                        if is_dir:
                            event.prevent_default()
                            event.stop()
                            self._toggle_expand_dir(rel)

    @on(Button.Pressed, "#expand-extra")
    def _on_expand_extra_pressed(self) -> None:
        extras_list = self.query_one("#extras", SelectionList)
        field = self.query_one("#extra-input", Input)
        val = field.value.strip()
        if val:
            try:
                rel = backend.normalize_rel(val, self.home)
                self._toggle_expand_dir(rel)
                field.value = ""
                extras_list.focus()
                return
            except BackupError as exc:
                self.notify(str(exc), severity="error")
                return
        idx = extras_list.highlighted
        if idx is not None and 0 <= idx < extras_list.option_count:
            rel = extras_list.get_option_at_index(idx).value
            self._toggle_expand_dir(rel)
        extras_list.focus()

    @on(SelectionList.SelectionHighlighted, "#extras")
    def _on_extras_highlighted(self, event: SelectionList.SelectionHighlighted) -> None:
        self._update_expand_button_label()

    @on(Button.Pressed, "#add-extra")
    @on(Input.Submitted, "#extra-input")
    def _add_extra(self) -> None:
        field = self.query_one("#extra-input", Input)
        try:
            rel = backend.normalize_rel(field.value, self.home)
        except BackupError as exc:
            self.notify(str(exc), severity="error")
            return
        if not os.path.lexists(self.home / rel):
            self.notify(f"{rel} does not exist.", severity="warning")
            return
        extras_list = self.query_one("#extras", SelectionList)
        if rel in self._essential_values:
            self.notify(f"{rel} is already included under Essentials.")
        else:
            is_dir = (self.home / rel).is_dir()
            self._custom_extras.add(rel)
            if is_dir:
                self._expanded_dirs.add(rel)
            self._refresh_extras_list(keep_cursor_rel=rel)
            extras_list.select(rel)
            self.notify(f"Added and selected {rel}.")
        field.value = ""

    # ----------------------------------------------------------------- running

    def _begin(self, label: str) -> None:
        self._busy = True
        self._cancel = threading.Event()
        for selector in ("#start-backup", "#start-restore", "#refresh-snaps", "#add-extra", "#expand-extra", "#btn-choose-dir"):
            self.query_one(selector, Button).disabled = True
        self.query_one("#cancel", Button).disabled = False
        self.query_one("#progress", ProgressBar).update(total=100, progress=0)
        self._set_status(label)

    def _end(self) -> None:
        self._busy = False
        for selector in ("#start-backup", "#start-restore", "#refresh-snaps", "#add-extra", "#expand-extra", "#btn-choose-dir"):
            self.query_one(selector, Button).disabled = False
        self.query_one("#cancel", Button).disabled = True

    @on(Button.Pressed, "#cancel")
    def _request_cancel(self) -> None:
        if self._busy and not self._cancel.is_set():
            self._cancel.set()
            self._write_log("warn", "Cancelling ...")
            self._set_status("Cancelling ...")

    def _set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(Text(text))

    def _set_progress(self, fraction: float, text: str) -> None:
        percent = max(0, min(100, round(fraction * 100)))
        self.query_one("#progress", ProgressBar).update(total=100, progress=percent)
        self._set_status(f"{percent:3d}%  {text}")

    def _write_log(self, level: str, message: str) -> None:
        icon, style = LOG_STYLES.get(level, LOG_STYLES["info"])
        stamp = datetime.now().strftime("%H:%M:%S")
        line = Text.assemble(
            (stamp + " ", "dim"),
            (icon + " ", style),
            (message, "dim" if level == "dim" else ""),
        )
        self.query_one("#log", RichLog).write(line)

    # Callbacks handed to the backend; they are invoked from worker threads.
    def _thread_log(self, level: str, message: str) -> None:
        self.call_from_thread(self._write_log, level, message)

    def _thread_progress(self, fraction: float, text: str) -> None:
        self.call_from_thread(self._set_progress, fraction, text)

    def _operation_finished(self, result: Optional[backend.OperationResult]) -> None:
        self._end()
        if result is None:
            self._set_status("Failed - see the log")
            self.notify("The operation failed - see the activity log.", severity="error")
            return
        if result.cancelled:
            level, severity = "warn", "warning"
        elif result.ok:
            level, severity = "ok", "information"
        else:
            level, severity = "error", "error"
        if result.ok:
            self.query_one("#progress", ProgressBar).update(total=100, progress=100)
        self._write_log(level, result.summary)
        if result.safety_backup:
            self._write_log("info", f"Replaced files were saved in {result.safety_backup}")
        self._set_status(result.summary)
        self.notify(result.summary, severity=severity, timeout=8)
        if result.kind == "backup" and result.ok and not result.dry_run:
            self._refresh_snapshots(quiet=True)

    # ------------------------------------------------------------------ backup

    @on(Button.Pressed, "#start-backup")
    def _start_backup(self) -> None:
        if self._busy:
            return
        parent = self.query_one("#backup-path", Input).value
        items = list(self.query_one("#essentials", SelectionList).selected)
        items += list(self.query_one("#extras", SelectionList).selected)
        if not items:
            self.notify("Select at least one item to back up.", severity="warning")
            return
        dry = self.query_one("#backup-dry", Switch).value
        self._begin("Preparing backup (dry run) ..." if dry else "Preparing backup ...")
        self._backup_worker(parent, items, dry, self._cancel)

    @work(thread=True, group="operation")
    def _backup_worker(self, parent: str, items: list[str], dry: bool, cancel: threading.Event) -> None:
        result = None
        try:
            result = backend.run_backup(
                parent,
                items,
                home=self.home,
                dry_run=dry,
                engine=self.engine,
                on_progress=self._thread_progress,
                on_log=self._thread_log,
                cancel=cancel,
            )
        except BackupError as exc:
            self._thread_log("error", str(exc))
        except Exception:  # last-resort guard: keep the UI alive and show why
            self._thread_log("error", "Unexpected error:\n" + traceback.format_exc())
        self.call_from_thread(self._operation_finished, result)

    # ----------------------------------------------------------------- restore

    @on(TabbedContent.TabActivated)
    def _tab_changed(self, event: TabbedContent.TabActivated) -> None:
        if event.pane.id == "restore-tab" and not self._busy:
            self._refresh_snapshots(quiet=True)

    @on(Input.Submitted, "#backup-path")
    def _path_submitted(self) -> None:
        self._refresh_snapshots(quiet=False)

    @on(Button.Pressed, "#refresh-snaps")
    def _refresh_pressed(self) -> None:
        self._refresh_snapshots(quiet=False)

    def _refresh_snapshots(self, quiet: bool) -> None:
        parent = self.query_one("#backup-path", Input).value
        if not parent.strip():
            if not quiet:
                self.notify("Enter the backup directory path first.", severity="warning")
            return
        self._snapshot_worker(parent, quiet)

    @work(thread=True, group="snapshots", exclusive=True)
    def _snapshot_worker(self, parent: str, quiet: bool) -> None:
        try:
            snapshots = backend.list_snapshots(parent)
        except BackupError as exc:
            self.call_from_thread(self._snapshots_failed, str(exc), quiet)
            return
        except Exception:
            self.call_from_thread(self._snapshots_failed, traceback.format_exc(), quiet)
            return
        self.call_from_thread(self._snapshots_loaded, snapshots, quiet)

    def _snapshots_failed(self, message: str, quiet: bool) -> None:
        self._clear_snapshots()
        # A missing/unmounted folder is expected while the user is still typing, so
        # only shout about it when they explicitly asked (Enter / Refresh).
        if not quiet:
            self._write_log("error", message)
            self.notify(message, severity="error", timeout=8)

    def _clear_snapshots(self) -> None:
        self._snapshots = {}
        self._current_snapshot = None
        self.query_one("#snapshots", OptionList).clear_options()
        self.query_one("#snap-items", SelectionList).clear_options()

    def _snapshots_loaded(self, snapshots: list[backend.Snapshot], quiet: bool) -> None:
        self._clear_snapshots()
        usable = [s for s in snapshots if s.complete]
        incomplete = len(snapshots) - len(usable)
        self._snapshots = {s.name: s for s in usable}
        listing = self.query_one("#snapshots", OptionList)
        listing.add_options(
            [
                Option(
                    Text.assemble(
                        (s.name, "bold"),
                        (f"   {len(s.items)} item(s)", "dim"),
                    ),
                    id=s.name,
                )
                for s in usable
            ]
        )
        if not quiet:
            self._write_log("info", f"Found {len(usable)} snapshot(s).")
        if incomplete:
            self._write_log("warn", f"Ignored {incomplete} incomplete snapshot folder(s) without a {backend.MANIFEST_NAME}.")
        if usable:
            listing.highlighted = 0

    @on(OptionList.OptionHighlighted, "#snapshots")
    def _snapshot_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        snapshot = self._snapshots.get(event.option.id or "")
        if snapshot is None:
            return
        self._current_snapshot = snapshot
        items_list = self.query_one("#snap-items", SelectionList)
        items_list.clear_options()
        items_list.add_options([Selection(Text(rel), rel, True) for rel in snapshot.items])
        self.query_one("#snap-items-pane").border_title = (
            f"Items to restore · {len(snapshot.items)} in {snapshot.created:%Y-%m-%d %H:%M}"
        )

    @on(Button.Pressed, "#start-restore")
    def _start_restore(self) -> None:
        if self._busy:
            return
        snapshot = self._current_snapshot
        if snapshot is None:
            self.notify("Pick a snapshot first.", severity="warning")
            return
        items = list(self.query_one("#snap-items", SelectionList).selected)
        if not items:
            self.notify("Select at least one item to restore.", severity="warning")
            return
        index = self.query_one("#policy", RadioSet).pressed_index
        policy = POLICIES[index if index >= 0 else 0]
        dry = self.query_one("#restore-dry", Switch).value
        if dry:
            self._run_restore(snapshot, items, policy, True)
            return
        lines = [
            f"Restore {len(items)} item(s) from {snapshot.name}",
            f"into {self.home}",
            "",
            f"If a file already exists: {policy.label.lower()}.",
        ]
        if policy is RestorePolicy.OVERWRITE:
            lines.append("Overwritten files cannot be recovered.")
        if any(i == ".ssh" or i.startswith(".ssh/") for i in items):
            lines.append("SSH permissions will be reset (700 / 600 / 644).")
        self.push_screen(
            ConfirmScreen("Confirm restore", lines, "Restore"),
            partial(self._restore_confirmed, snapshot, items, policy),
        )

    def _restore_confirmed(
        self,
        snapshot: backend.Snapshot,
        items: list[str],
        policy: RestorePolicy,
        confirmed: Optional[bool],
    ) -> None:
        if confirmed:
            self._run_restore(snapshot, items, policy, False)
        else:
            self._write_log("dim", "Restore cancelled.")

    def _run_restore(
        self, snapshot: backend.Snapshot, items: list[str], policy: RestorePolicy, dry: bool
    ) -> None:
        self._begin("Preparing restore (dry run) ..." if dry else "Preparing restore ...")
        self._restore_worker(snapshot, items, policy, dry, self._cancel)

    @work(thread=True, group="operation")
    def _restore_worker(
        self,
        snapshot: backend.Snapshot,
        items: list[str],
        policy: RestorePolicy,
        dry: bool,
        cancel: threading.Event,
    ) -> None:
        result = None
        try:
            result = backend.run_restore(
                snapshot,
                items,
                policy,
                home=self.home,
                dry_run=dry,
                engine=self.engine,
                on_progress=self._thread_progress,
                on_log=self._thread_log,
                cancel=cancel,
            )
        except BackupError as exc:
            self._thread_log("error", str(exc))
        except Exception:
            self._thread_log("error", "Unexpected error:\n" + traceback.format_exc())
        self.call_from_thread(self._operation_finished, result)

    # ----------------------------------------------------------------- actions

    def action_show_mode(self, tab_id: str) -> None:
        tabs = self.query_one(TabbedContent)
        if tabs.active == tab_id:
            return
        # Hiding a pane while one of its widgets has focus makes Textual hand focus to
        # the next widget in the (still backup-tab) DOM, and focusing a widget inside a
        # pane re-activates that pane - so F2 would silently bounce back to Backup.
        # Park focus on the path field (outside every pane) before switching.
        self.query_one("#backup-path", Input).focus()
        tabs.active = tab_id
        target = "#snapshots" if tab_id == "restore-tab" else "#essentials"
        self.call_after_refresh(lambda: self.query_one(target).focus())

    def action_choose_directory(self) -> None:
        """Browse and select backup directory using native portal or fallback modal."""
        if self._busy:
            return
        current_val = self.query_one("#backup-path", Input).value.strip()
        initial = current_val or str(self.home)

        if is_portal_supported():
            self._set_status("Opening desktop file chooser portal ...")
            self._portal_choose_worker(initial)
        else:
            self._open_in_tui_picker(initial)

    @work(thread=True, group="portal_picker", exclusive=True)
    def _portal_choose_worker(self, initial: str) -> None:
        chosen = choose_directory_portal(current_folder=initial)
        self.call_from_thread(self._on_portal_result, chosen)

    def _on_portal_result(self, chosen: Optional[str]) -> None:
        self._set_status("Idle")
        if chosen:
            self._apply_chosen_dir(chosen)
        else:
            self.notify("Directory selection cancelled.", severity="information")

    def _open_in_tui_picker(self, initial: str) -> None:
        def _on_modal_dismiss(chosen: Optional[str]) -> None:
            if chosen:
                self._apply_chosen_dir(chosen)
            else:
                self.notify("Directory selection cancelled.", severity="information")

        self.push_screen(DirectoryPickerModal(initial_dir=initial), _on_modal_dismiss)

    def _apply_chosen_dir(self, path_str: str) -> None:
        inp = self.query_one("#backup-path", Input)
        inp.value = path_str
        self._write_log("ok", f"Selected backup directory: {path_str}")
        self.notify(f"Selected: {path_str}")
        self._refresh_snapshots(quiet=True)

    def action_change_theme(self) -> None:
        """Open theme selection command palette."""
        self.search_themes()

    async def action_quit(self) -> None:
        self._cancel.set()  # stop any running rsync before leaving
        self.exit()


def main() -> None:
    parser = argparse.ArgumentParser(description="TUI for backing up and restoring files in your home directory.")
    parser.add_argument("--dir", default="", help="prefill the backup parent directory")
    parser.add_argument(
        "--home",
        default=None,
        help="treat this directory as the home directory (handy for trying the app safely)",
    )
    parser.add_argument(
        "--engine",
        choices=("auto", "rsync", "python"),
        default="auto",
        help="copy engine (default: rsync if installed, otherwise the built-in copier)",
    )
    parser.add_argument(
        "--theme",
        default=None,
        help="initial theme (e.g. catppuccin-mocha, tokyo-night, nord, dracula, gruvbox, cyberpunk)",
    )
    args = parser.parse_args()
    BackupApp(
        initial_dir=args.dir,
        home=Path(args.home) if args.home else None,
        engine=args.engine,
        theme=args.theme,
    ).run()


if __name__ == "__main__":
    main()
