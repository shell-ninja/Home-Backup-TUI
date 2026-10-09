"""Backend tests. Run from the project folder:  python -m unittest discover -s tests -v

Every copy test runs against both engines (rsync is skipped when not installed).
"""

import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backend  # noqa: E402
from backend import BackupError, RestorePolicy  # noqa: E402

NOW = datetime(2026, 10, 8, 13, 45, 0)
ENGINES = ["python"] + (["rsync"] if backend.rsync_available() else [])


def mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def make_home(root: Path) -> Path:
    home = root / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("PRIVATE\n")
    (home / ".ssh" / "id_ed25519.pub").write_text("ssh-ed25519 AAAA me@host\n")
    (home / ".ssh" / "config").write_text("Host *\n  ForwardAgent no\n")
    (home / ".gitconfig").write_text("[user]\n  name = Me\n")
    (home / ".config" / "BraveSoftware" / "Brave-Browser" / "Default").mkdir(parents=True)
    (home / ".config" / "BraveSoftware" / "Brave-Browser" / "Default" / "Preferences").write_text("{}")
    (home / ".var" / "app" / "com.brave.Browser" / "config" / "BraveSoftware").mkdir(parents=True)
    (home / ".var" / "app" / "com.brave.Browser" / "config" / "BraveSoftware" / "Local State").write_text("{}")
    (home / ".config" / "VSCodium" / "User").mkdir(parents=True)
    (home / ".config" / "VSCodium" / "User" / "settings.json").write_text("{}")
    (home / ".vscode-oss" / "extensions").mkdir(parents=True)
    (home / ".vscode-oss" / "extensions" / "ext.txt").write_text("x")
    (home / "antigravity").mkdir()
    (home / "antigravity" / "notes.md").write_text("hi")
    (home / ".config" / "Antigravity").mkdir(parents=True)
    (home / ".config" / "Antigravity" / "state.json").write_text("{}")
    (home / "Documents").mkdir()
    (home / "Documents" / "a.txt").write_text("doc")
    # a symlink inside an item, to make sure links survive as links
    os.symlink("id_ed25519.pub", home / ".ssh" / "link.pub")
    os.chmod(home / ".ssh", 0o755)
    os.chmod(home / ".ssh" / "id_ed25519", 0o644)
    return home


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = make_home(self.root)
        self.backups = self.root / "backups"
        self.backups.mkdir()
        self.logs: list[tuple[str, str]] = []

    def tearDown(self):
        self._tmp.cleanup()

    def reset(self):
        """Fresh home/backups for the next engine inside a subTest loop."""
        self.tearDown()
        self.setUp()

    def log(self, level, message):
        self.logs.append((level, message))

    def backup(self, items, engine="python", **kw):
        return backend.run_backup(
            str(self.backups), items, home=self.home, engine=engine, now=NOW, on_log=self.log, **kw
        )

    def snapshot(self):
        snaps = backend.list_snapshots(str(self.backups))
        self.assertEqual(len(snaps), 1)
        return snaps[0]


class DetectionTests(Base):
    def test_finds_all_essentials_that_exist(self):
        rels = {i.rel for i in backend.detect_essentials(self.home)}
        self.assertEqual(
            rels,
            {
                ".gitconfig",
                ".ssh",
                ".config/VSCodium",
                ".vscode-oss",
                ".config/BraveSoftware",
                ".var/app/com.brave.Browser/config/BraveSoftware",
                "antigravity",
                ".config/Antigravity",
            },
        )

    def test_antigravity_scan_is_case_insensitive_and_finds_unlisted_names(self):
        (self.home / ".local" / "share" / "Antigravity-Data").mkdir(parents=True)
        rels = {i.rel for i in backend.detect_essentials(self.home)}
        self.assertIn(".local/share/Antigravity-Data", rels)

    def test_absent_items_are_not_listed(self):
        shutil.rmtree(self.home / ".ssh")
        rels = {i.rel for i in backend.detect_essentials(self.home)}
        self.assertNotIn(".ssh", rels)

    def test_home_entries_dirs_first(self):
        entries = backend.list_home_entries(self.home)
        flags = [e.is_dir for e in entries]
        self.assertEqual(flags, sorted(flags, reverse=True))


class PathTests(Base):
    def test_normalize_variants(self):
        self.assertEqual(backend.normalize_rel("~/Documents", self.home), "Documents")
        self.assertEqual(backend.normalize_rel(str(self.home / "Documents/a.txt"), self.home), "Documents/a.txt")
        self.assertEqual(backend.normalize_rel(".config//nvim/", self.home), ".config/nvim")

    def test_normalize_rejects_outside_and_whole_home(self):
        for bad in ("/etc/passwd", "../outside", "", "~", "."):
            with self.assertRaises(BackupError, msg=bad):
                backend.normalize_rel(bad, self.home)

    def test_missing_parent_mentions_drive(self):
        with self.assertRaises(BackupError) as ctx:
            backend.validate_parent(str(self.root / "nope"), write=True)
        self.assertIn("mounted", str(ctx.exception))

    def test_unmounted_removable_media_is_an_error(self):
        with self.assertRaises(BackupError) as ctx:
            backend.validate_parent("/run/media/someone/NOT-PLUGGED-IN/backups", write=True)
        self.assertIn("not a mounted drive", str(ctx.exception))

    def test_empty_parent(self):
        with self.assertRaises(BackupError):
            backend.validate_parent("   ", write=True)

    def test_snapshot_name_format(self):
        self.assertEqual(backend.snapshot_name(NOW, "alice"), "alice-08-10-2026_13-45-00")


class BackupTests(Base):
    def test_backup_each_engine(self):
        items = [".gitconfig", ".ssh", ".config/BraveSoftware", "Documents"]
        for engine in ENGINES:
            with self.subTest(engine=engine):
                for child in self.backups.iterdir():
                    shutil.rmtree(child)
                result = self.backup(items, engine)
                self.assertTrue(result.ok, result.summary)
                snap = self.snapshot()
                self.assertRegex(snap.name, r"^.+-08-10-2026_13-45-00$")
                self.assertEqual(set(snap.items), set(items))
                self.assertEqual((snap.path / ".gitconfig").read_text(), "[user]\n  name = Me\n")
                self.assertTrue((snap.path / ".config/BraveSoftware/Brave-Browser/Default/Preferences").exists())
                self.assertTrue(os.path.islink(snap.path / ".ssh" / "link.pub"))
                self.assertEqual(mode(snap.path), 0o700, "snapshot root must stay private")
                text =(snap.path / ".manifest").read_text()
                self.assertTrue(text.startswith("# home-backup manifest v1"))
                self.assertEqual(
                    [ln for ln in text.splitlines() if not ln.startswith("# ")], items
                )

    def test_dry_run_writes_nothing(self):
        for engine in ENGINES:
            with self.subTest(engine=engine):
                result = self.backup([".ssh", "Documents"], engine, dry_run=True)
                self.assertTrue(result.ok, result.summary)
                self.assertEqual(list(self.backups.iterdir()), [])

    def test_nested_selection_is_collapsed(self):
        result = self.backup([".config", ".config/BraveSoftware", ".config/BraveSoftware"])
        self.assertEqual(result.completed, [".config"])

    def test_missing_item_is_reported_not_fatal(self):
        result = self.backup([".gitconfig", ".does-not-exist"])
        self.assertTrue(result.ok)
        self.assertEqual(result.missing, [".does-not-exist"])
        self.assertEqual(self.snapshot().items, (".gitconfig",))

    def test_all_missing_leaves_no_empty_snapshot(self):
        result = self.backup([".nope"])
        self.assertFalse(result.ok)
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_destination_inside_selected_item_is_refused(self):
        inside = self.home / "Documents" / "bk"
        inside.mkdir()
        with self.assertRaises(BackupError):
            backend.run_backup(str(inside), ["Documents"], home=self.home, now=NOW)

    def test_nothing_selected(self):
        with self.assertRaises(BackupError):
            self.backup([])

    def test_cancel_before_start_creates_no_manifest(self):
        cancel = threading.Event()
        cancel.set()
        result = self.backup([".ssh"], cancel=cancel)
        self.assertTrue(result.cancelled)
        self.assertFalse(result.ok)
        self.assertEqual(list(self.backups.glob("*/.manifest")), [])

    def test_incomplete_snapshot_is_flagged(self):
        (self.backups / "bob-01-01-2026_00-00-00").mkdir()
        (self.backups / "not-a-snapshot").mkdir()
        snaps = backend.list_snapshots(str(self.backups))
        self.assertEqual([(s.name, s.complete) for s in snaps], [("bob-01-01-2026_00-00-00", False)])


class RestoreTests(Base):
    ITEMS = [".gitconfig", ".ssh"]

    def prepare(self, engine):
        self.assertTrue(self.backup(self.ITEMS, engine).ok)
        snap = self.snapshot()
        # drift: edit one file, delete another, break permissions
        (self.home / ".gitconfig").write_text("[user]\n  name = Changed Locally\n")
        (self.home / ".ssh" / "config").unlink()
        os.chmod(self.home / ".ssh", 0o755)
        os.chmod(self.home / ".ssh" / "id_ed25519", 0o644)
        os.chmod(self.home / ".ssh" / "id_ed25519.pub", 0o600)
        return snap

    def restore(self, snap, policy, engine, **kw):
        return backend.run_restore(
            snap, self.ITEMS, policy, home=self.home, engine=engine, now=NOW, on_log=self.log, **kw
        )

    def test_skip_keeps_existing_but_fills_gaps(self):
        for engine in ENGINES:
            with self.subTest(engine=engine):
                self.reset()
                snap = self.prepare(engine)
                result = self.restore(snap, RestorePolicy.SKIP, engine)
                self.assertTrue(result.ok, result.summary)
                self.assertIn("Changed Locally", (self.home / ".gitconfig").read_text())
                self.assertTrue((self.home / ".ssh" / "config").exists())

    def test_overwrite_replaces_existing(self):
        for engine in ENGINES:
            with self.subTest(engine=engine):
                self.reset()
                snap = self.prepare(engine)
                result = self.restore(snap, RestorePolicy.OVERWRITE, engine)
                self.assertTrue(result.ok, result.summary)
                self.assertIn("name = Me", (self.home / ".gitconfig").read_text())
                self.assertIsNone(result.safety_backup)

    def test_backup_then_overwrite_keeps_old_copy(self):
        for engine in ENGINES:
            with self.subTest(engine=engine):
                self.reset()
                snap = self.prepare(engine)
                result = self.restore(snap, RestorePolicy.BACKUP_THEN_OVERWRITE, engine)
                self.assertTrue(result.ok, result.summary)
                self.assertIn("name = Me", (self.home / ".gitconfig").read_text())
                self.assertIsNotNone(result.safety_backup)
                kept = result.safety_backup / ".gitconfig"
                self.assertIn("Changed Locally", kept.read_text())

    def test_ssh_permissions_are_reapplied(self):
        for engine in ENGINES:
            with self.subTest(engine=engine):
                self.reset()
                snap = self.prepare(engine)
                result = self.restore(snap, RestorePolicy.SKIP, engine)
                self.assertGreater(result.ssh_changes, 0)
                self.assertEqual(mode(self.home / ".ssh"), 0o700)
                self.assertEqual(mode(self.home / ".ssh" / "id_ed25519"), 0o600)
                self.assertEqual(mode(self.home / ".ssh" / "config"), 0o600)
                self.assertEqual(mode(self.home / ".ssh" / "id_ed25519.pub"), 0o644)

    def test_restore_dry_run_changes_nothing(self):
        for engine in ENGINES:
            with self.subTest(engine=engine):
                self.reset()
                snap = self.prepare(engine)
                result = self.restore(snap, RestorePolicy.BACKUP_THEN_OVERWRITE, engine, dry_run=True)
                self.assertTrue(result.ok, result.summary)
                self.assertIn("Changed Locally", (self.home / ".gitconfig").read_text())
                self.assertFalse((self.home / ".ssh" / "config").exists())
                self.assertEqual(mode(self.home / ".ssh"), 0o755)
                self.assertEqual(list(self.home.glob(".restore-backup-*")), [])

    def test_home_directory_mode_is_never_touched(self):
        # Regression: rsync with a bare "./item" source copies the "." entry's mode onto
        # the destination root, which would chmod $HOME to the snapshot's mode.
        for engine in ENGINES:
            with self.subTest(engine=engine):
                self.reset()
                os.chmod(self.home, 0o751)
                snap = self.prepare(engine)
                before = mode(self.home)
                for policy in RestorePolicy:
                    self.assertTrue(self.restore(snap, policy, engine).ok)
                    self.assertEqual(mode(self.home), before, policy)

    def test_vanished_snapshot_gives_clear_error(self):
        snap = self.prepare("python")
        shutil.rmtree(snap.path)
        with self.assertRaises(BackupError) as ctx:
            self.restore(snap, RestorePolicy.OVERWRITE, "python")
        self.assertIn("drive", str(ctx.exception))

    def test_items_not_in_manifest_are_ignored(self):
        snap = self.prepare("python")
        with self.assertRaises(BackupError):
            backend.run_restore(snap, ["Documents"], RestorePolicy.OVERWRITE, home=self.home, engine="python")


class SshPermissionTests(Base):
    def test_modes(self):
        ssh = self.home / ".ssh"
        (ssh / "keys").mkdir()
        (ssh / "keys" / "deploy").write_text("k")
        os.chmod(ssh / "keys" / "deploy", 0o664)
        changed = backend.fix_ssh_permissions(ssh)
        self.assertGreater(changed, 0)
        self.assertEqual(mode(ssh), 0o700)
        self.assertEqual(mode(ssh / "keys"), 0o700)
        self.assertEqual(mode(ssh / "keys" / "deploy"), 0o600)
        self.assertEqual(mode(ssh / "id_ed25519.pub"), 0o644)
        # idempotent
        self.assertEqual(backend.fix_ssh_permissions(ssh), 0)


if __name__ == "__main__":
    unittest.main()
