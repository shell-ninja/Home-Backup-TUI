"""Headless UI tests using Textual's pilot. Run:  python -m unittest discover -s tests -v"""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from unittest.mock import MagicMock, patch
from textual.widgets import Button, Input, OptionList, RadioButton, RadioSet, RichLog, SelectionList, Switch  # noqa: E402

from app import BackupApp, DirectoryPickerModal  # noqa: E402
from test_backend import make_home  # noqa: E402


def log_text(app: BackupApp) -> str:
    return "\n".join(strip.text for strip in app.query_one("#log", RichLog).lines)


class AppFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = make_home(self.root)
        self.backups = self.root / "backups"
        self.backups.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def make_app(self, initial=True) -> BackupApp:
        return BackupApp(initial_dir=str(self.backups) if initial else "", home=self.home, engine="python")

    async def settle(self, pilot, app):
        await app.workers.wait_for_complete()
        await pilot.pause()

    async def test_lists_are_populated_and_essentials_preselected(self):
        app = self.make_app()
        async with app.run_test(size=(150, 50)) as pilot:
            await pilot.pause()
            essentials = app.query_one("#essentials", SelectionList)
            self.assertEqual(essentials.option_count, 8)
            self.assertEqual(len(essentials.selected), 8)
            extras = app.query_one("#extras", SelectionList)
            self.assertIn("Documents", [extras.get_option_at_index(i).value for i in range(extras.option_count)])
            self.assertEqual(extras.selected, [])

    async def test_mode_switching_with_function_keys(self):
        app = self.make_app()
        async with app.run_test(size=(150, 50)) as pilot:
            await pilot.pause()
            await pilot.press("f2")
            await pilot.pause()
            self.assertEqual(app.query_one("TabbedContent").active, "restore-tab")
            await pilot.press("f1")
            await pilot.pause()
            self.assertEqual(app.query_one("TabbedContent").active, "backup-tab")

    async def test_mode_switch_works_whatever_widget_has_focus(self):
        # Regression: hiding a pane while one of its widgets was focused made Textual
        # re-activate that pane, so F2 (or F1) silently bounced back.
        app = self.make_app()
        async with app.run_test(size=(150, 50)) as pilot:
            await pilot.pause()
            tabs = app.query_one("TabbedContent")
            for selector in ("#extra-input", "#backup-dry", "#add-extra", "#start-backup", "#essentials", "#backup-path"):
                tabs.active = "backup-tab"
                await pilot.pause()
                app.query_one(selector).focus()
                await pilot.pause()
                await pilot.press("f2")
                await pilot.pause(0.2)
                self.assertEqual(tabs.active, "restore-tab", f"F2 failed with {selector} focused")
            for selector in ("#restore-dry", "#start-restore", "#refresh-snaps", "#policy"):
                tabs.active = "restore-tab"
                await pilot.pause()
                app.query_one(selector).focus()
                await pilot.pause()
                await pilot.press("f1")
                await pilot.pause(0.2)
                self.assertEqual(tabs.active, "backup-tab", f"F1 failed with {selector} focused")

    async def test_layout_has_no_overlap_at_common_sizes(self):
        # Regression: the 5-row policy box used to spill out of its 3-row action row.
        for size in ((150, 50), (110, 36), (100, 30)):
            app = self.make_app()
            async with app.run_test(size=size) as pilot:
                await pilot.pause()
                activity = app.query_one("#activity").region
                for tab, buttons in (("backup-tab", ("#start-backup",)), ("restore-tab", ("#start-restore", "#policy"))):
                    app.query_one("TabbedContent").active = tab
                    await pilot.pause()
                    for selector in buttons:
                        region = app.query_one(selector).region
                        self.assertGreater(region.width, 0, f"{selector} not laid out at {size}")
                        self.assertLessEqual(region.bottom, activity.y, f"{selector} overlaps Activity at {size}")

    async def test_backup_then_restore_roundtrip(self):
        app = self.make_app()
        async with app.run_test(size=(150, 50)) as pilot:
            await pilot.pause()

            # add an extra, nested path through the input row
            app.query_one("#extra-input", Input).value = "Documents/a.txt"
            await pilot.click("#add-extra")
            await pilot.pause()
            self.assertIn("Documents/a.txt", app.query_one("#extras", SelectionList).selected)

            # dry run first: nothing may be written
            app.query_one("#backup-dry", Switch).value = True
            await pilot.click("#start-backup")
            await self.settle(pilot, app)
            self.assertEqual(list(self.backups.iterdir()), [])
            self.assertIn("Dry run", log_text(app))

            # real backup
            app.query_one("#backup-dry", Switch).value = False
            await pilot.click("#start-backup")
            await self.settle(pilot, app)
            snapshots = list(self.backups.iterdir())
            self.assertEqual(len(snapshots), 1)
            manifest = (snapshots[0] / ".manifest").read_text()
            self.assertIn("Documents/a.txt", manifest)
            self.assertFalse(app.query_one("#start-backup", Button).disabled)

            # damage things, then restore from the Restore tab
            (self.home / ".gitconfig").write_text("broken")
            os.chmod(self.home / ".ssh", 0o777)
            await pilot.press("f2")
            await self.settle(pilot, app)
            listing = app.query_one("#snapshots", OptionList)
            self.assertEqual(listing.option_count, 1)
            self.assertEqual(len(app.query_one("#snap-items", SelectionList).selected), 9)

            overwrite = list(app.query_one("#policy", RadioSet).query(RadioButton))[1]
            overwrite.value = True
            await pilot.pause()
            await pilot.click("#start-restore")
            await pilot.pause()
            await pilot.click("#yes")  # confirmation dialog
            await self.settle(pilot, app)

            self.assertIn("name = Me", (self.home / ".gitconfig").read_text())
            self.assertEqual(stat.S_IMODE(os.stat(self.home / ".ssh").st_mode), 0o700)
            self.assertIn("SSH permissions", log_text(app))

    async def test_declining_the_confirmation_changes_nothing(self):
        app = self.make_app()
        async with app.run_test(size=(150, 50)) as pilot:
            await pilot.pause()
            await pilot.click("#start-backup")
            await self.settle(pilot, app)
            (self.home / ".gitconfig").write_text("keep me")
            await pilot.press("f2")
            await self.settle(pilot, app)
            overwrite = list(app.query_one("#policy", RadioSet).query(RadioButton))[1]
            overwrite.value = True
            await pilot.pause()
            await pilot.click("#start-restore")
            await pilot.pause()
            await pilot.click("#no")
            await self.settle(pilot, app)
            self.assertEqual((self.home / ".gitconfig").read_text(), "keep me")

    async def test_missing_backup_drive_is_reported_without_crashing(self):
        app = self.make_app(initial=False)
        async with app.run_test(size=(150, 50)) as pilot:
            await pilot.pause()
            app.query_one("#backup-path", Input).value = str(self.root / "unplugged-drive")
            await pilot.click("#start-backup")
            await self.settle(pilot, app)
            self.assertIn("does not exist", log_text(app))
            self.assertFalse(app.query_one("#start-backup", Button).disabled)
            self.assertTrue(app.query_one("#cancel", Button).disabled)

    async def test_start_with_nothing_selected_only_warns(self):
        app = self.make_app()
        async with app.run_test(size=(150, 50)) as pilot:
            await pilot.pause()
            app.query_one("#essentials", SelectionList).deselect_all()
            await pilot.click("#start-backup")
            await self.settle(pilot, app)
            self.assertEqual(list(self.backups.iterdir()), [])
            self.assertFalse(app._busy)

    async def test_theme_switcher_and_persistence(self):
        from textual.command import CommandPalette
        app = BackupApp(initial_dir="", home=self.home, engine="python", theme="nord")
        async with app.run_test(size=(150, 50)) as pilot:
            await pilot.pause()
            btn = app.query_one("#btn-theme-switcher", Button)
            self.assertIn("nord", str(btn.label))

            # Test opening theme palette with 't'
            await pilot.press("t")
            await pilot.pause()
            self.assertIsInstance(app.screen, CommandPalette)
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, CommandPalette)

            # Test opening theme palette via theme button
            await pilot.click("#btn-theme-switcher")
            await pilot.pause()
            self.assertIsInstance(app.screen, CommandPalette)
            await pilot.press("escape")
            await pilot.pause()

            # Test dynamic theme switching to custom theme
            app.theme = "cyberpunk"
            await pilot.pause()
            self.assertIn("cyberpunk", str(btn.label))

    async def test_expand_directory_select_backup_and_restore(self):
        # Create nested folders and files in home
        (self.home / "Documents" / "Work").mkdir(parents=True, exist_ok=True)
        (self.home / "Documents" / "Work" / "report.txt").write_text("secret report")
        (self.home / "Documents" / "todo.txt").write_text("buy milk")

        app = self.make_app()
        async with app.run_test(size=(150, 50)) as pilot:
            await pilot.pause()
            extras = app.query_one("#extras", SelectionList)

            # Locate Documents in extras list
            doc_idx = [extras.get_option_at_index(i).value for i in range(extras.option_count)].index("Documents")
            extras.focus()
            extras.highlighted = doc_idx
            await pilot.pause()

            # Press 'e' to expand Documents
            await pilot.press("e")
            await pilot.pause()

            opts1 = [extras.get_option_at_index(i).value for i in range(extras.option_count)]
            self.assertIn("Documents/todo.txt", opts1)
            self.assertIn("Documents/Work", opts1)

            # Highlight and select Documents/todo.txt
            extras.highlighted = opts1.index("Documents/todo.txt")
            await pilot.press("space")
            await pilot.pause()
            self.assertIn("Documents/todo.txt", extras.selected)

            # Expand Documents/Work using Expand button
            extras.highlighted = [extras.get_option_at_index(i).value for i in range(extras.option_count)].index("Documents/Work")
            await pilot.click("#expand-extra")
            await pilot.pause()

            opts2 = [extras.get_option_at_index(i).value for i in range(extras.option_count)]
            self.assertIn("Documents/Work/report.txt", opts2)

            # Select report.txt
            extras.focus()
            extras.highlighted = opts2.index("Documents/Work/report.txt")
            await pilot.press("space")
            await pilot.pause()
            self.assertIn("Documents/Work/report.txt", extras.selected)

            # Deselect essentials so only our expanded extras are backed up
            app.query_one("#essentials", SelectionList).deselect_all()
            await pilot.click("#start-backup")
            await self.settle(pilot, app)

            # Verify backup snapshot was created with exact nested paths
            snaps = list(self.backups.iterdir())
            self.assertEqual(len(snaps), 1)
            snap_dir = snaps[0]
            self.assertTrue((snap_dir / "Documents" / "Work" / "report.txt").exists())
            self.assertTrue((snap_dir / "Documents" / "todo.txt").exists())

            # Delete the backed-up files from home
            (self.home / "Documents" / "Work" / "report.txt").unlink()
            (self.home / "Documents" / "todo.txt").unlink()
            self.assertFalse((self.home / "Documents" / "Work" / "report.txt").exists())

            # Switch to restore mode
            await pilot.press("f2")
            await pilot.pause()
            await pilot.click("#refresh-snaps")
            await self.settle(pilot, app)

            # Start restore and confirm
            await pilot.click("#start-restore")
            await pilot.pause()
            await pilot.click("#yes")
            await self.settle(pilot, app)

            # Verify both files are restored to the exact expanded directories
            self.assertTrue((self.home / "Documents" / "Work" / "report.txt").exists())
            self.assertEqual((self.home / "Documents" / "Work" / "report.txt").read_text(), "secret report")
            self.assertTrue((self.home / "Documents" / "todo.txt").exists())
            self.assertEqual((self.home / "Documents" / "todo.txt").read_text(), "buy milk")

    async def test_choose_directory_button_and_portal(self):
        app = self.make_app(initial=False)
        target_dir = self.root / "external_backup"
        target_dir.mkdir()

        async with app.run_test(size=(150, 50)) as pilot:
            await pilot.pause()
            btn = app.query_one("#btn-choose-dir", Button)
            self.assertIn("Choose", str(btn.label))

            # Test portal success flow
            with patch("app.is_portal_supported", return_value=True), \
                 patch("app.choose_directory_portal", return_value=str(target_dir)):
                await pilot.click("#btn-choose-dir")
                await self.settle(pilot, app)

            inp = app.query_one("#backup-path", Input)
            self.assertEqual(inp.value, str(target_dir))
            self.assertIn(str(target_dir), log_text(app))

            # Test shortcut ctrl+o with fallback modal when portal is not available
            with patch("app.is_portal_supported", return_value=False):
                await pilot.press("ctrl+o")
                await pilot.pause()
                self.assertIsInstance(app.screen, DirectoryPickerModal)
                await pilot.press("escape")
                await pilot.pause()
                self.assertNotIsInstance(app.screen, DirectoryPickerModal)

            # Test disabled state during operation
            app._begin("Running...")
            self.assertTrue(btn.disabled)
            app._end()
            self.assertFalse(btn.disabled)


if __name__ == "__main__":
    unittest.main()
