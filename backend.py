"""backend.py - file-system logic for the home backup / restore tool.

Nothing in this module imports Textual, so it can be unit-tested and reused
from a plain CLI. Long-running calls report through two optional callbacks and
accept a ``threading.Event`` so a UI can cancel them.

Snapshot layout::

    <parent>/<username>-<DD-MM-YYYY_HH-MM-SS>/
        .manifest              <- one relative path per line (+ "# " header lines)
        .gitconfig
        .ssh/...
        .config/BraveSoftware/...

Copying is done by ``rsync -a --info=progress2`` (one process per item, so a
failure in one item cannot sink the rest). If rsync is not installed the module
falls back to a pure-Python copier with the same semantics.
"""

from __future__ import annotations

import errno
import getpass
import os
import re
import select as _select
import shutil
import socket
import stat
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Optional

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

MANIFEST_NAME = ".manifest"
MANIFEST_HEADER = "# home-backup manifest v1"
TIMESTAMP_FORMAT = "%d-%m-%Y_%H-%M-%S"

_SNAPSHOT_RE = re.compile(
    r"^(?P<user>.+)-(?P<stamp>\d{2}-\d{2}-\d{4}_\d{2}-\d{2}-\d{2})$"
)
# rsync --info=progress2 line:  "  1,234,567  45%   12.34MB/s    0:00:03 (xfr#3, to-chk=0/10)"
_PROGRESS_RE = re.compile(r"^\s*[\d.,]+[KMGTP]?\s+(\d+)%")

# Candidate locations (relative to $HOME). Only the ones that exist are used.
ESSENTIAL_GROUPS: dict[str, tuple[str, ...]] = {
    "Git": (".gitconfig",),
    "SSH": (".ssh",),
    "VSCodium": (
        ".config/VSCodium",
        ".config/VSCodium - Insiders",
        ".var/app/com.vscodium.codium",
        ".vscode-oss",
        ".vscode-oss-insiders",
    ),
    "Brave": (
        ".config/BraveSoftware",
        ".var/app/com.brave.Browser/config/BraveSoftware",
    ),
    "Antigravity": (
        "antigravity",
        ".antigravity",
        ".config/Antigravity",
        ".gemini/antigravity",
    ),
}
# Directories scanned (one level deep) for anything with "antigravity" in its name.
_ANTIGRAVITY_SCAN_DIRS = (".", ".config", ".local/share", ".var/app")

# Filesystems that cannot faithfully store Unix permissions / symlinks.
_LOSSY_FILESYSTEMS = {"vfat", "exfat", "msdos", "ntfs", "ntfs3"}

ProgressCallback = Callable[[float, str], None]  # (fraction 0..1, current item)
LogCallback = Callable[[str, str], None]  # (level, message)
# levels: "info", "ok", "warn", "error", "dim"


def _ignore_progress(fraction: float, text: str) -> None:
    pass


def _ignore_log(level: str, message: str) -> None:
    pass


# --------------------------------------------------------------------------- #
# Types
# --------------------------------------------------------------------------- #


class BackupError(Exception):
    """An error whose message is safe to show to the user as-is."""


class RestorePolicy(str, Enum):
    SKIP = "skip"
    OVERWRITE = "overwrite"
    BACKUP_THEN_OVERWRITE = "backup_then_overwrite"

    @property
    def label(self) -> str:
        return {
            RestorePolicy.SKIP: "Skip existing files",
            RestorePolicy.OVERWRITE: "Overwrite existing files",
            RestorePolicy.BACKUP_THEN_OVERWRITE: "Back up existing files first, then overwrite",
        }[self]


@dataclass(frozen=True)
class EssentialItem:
    group: str
    rel: str  # POSIX path relative to home


@dataclass(frozen=True)
class HomeEntry:
    rel: str
    is_dir: bool


@dataclass(frozen=True)
class Snapshot:
    name: str
    path: Path
    user: str
    created: datetime
    items: tuple[str, ...]
    complete: bool  # False when the .manifest is missing (interrupted backup)


@dataclass
class OperationResult:
    kind: str  # "backup" | "restore"
    dry_run: bool = False
    ok: bool = False
    cancelled: bool = False
    destination: Optional[Path] = None
    completed: list[str] = field(default_factory=list)
    partial: list[str] = field(default_factory=list)  # copied, but with warnings
    missing: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    safety_backup: Optional[Path] = None  # restore policy 3: where old files went
    ssh_changes: Optional[int] = None  # permissions changed (or that would be)

    @property
    def summary(self) -> str:
        label = "Backup" if self.kind == "backup" else "Restore"
        if self.dry_run:
            label += " dry run"
        if self.cancelled:
            return f"{label} cancelled."
        done = len(self.completed) + len(self.partial)
        parts = [f"{done} item(s) {'would be processed' if self.dry_run else 'processed'}"]
        if self.partial:
            parts.append(f"{len(self.partial)} with warnings")
        if self.missing:
            parts.append(f"{len(self.missing)} not found")
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        text = f"{label}: " + ", ".join(parts) + "."
        if self.kind == "backup" and not self.dry_run and self.destination and done:
            text += f" Snapshot: {self.destination}"
        return text


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _home(home: Optional[Path]) -> Path:
    base = Path(home) if home is not None else Path.home()
    return Path(os.path.normpath(os.path.abspath(base)))


def _username() -> str:
    try:
        return getpass.getuser()
    except Exception:  # no passwd entry, odd containers, ...
        return os.environ.get("USER") or "user"


def rsync_available() -> bool:
    return shutil.which("rsync") is not None


def human_size(num: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(num) < 1024 or unit == "TiB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TiB"


def describe_oserror(exc: OSError) -> str:
    code = exc.errno
    if isinstance(exc, PermissionError) or code in (errno.EACCES, errno.EPERM):
        return "permission denied"
    if code == errno.ENOENT:
        return "no such file or directory (was the drive unmounted?)"
    if code == errno.ENOSPC:
        return "no space left on the destination"
    if code == errno.EROFS:
        return "the destination is read-only"
    if code in (errno.EIO, errno.ENODEV, errno.ENXIO):
        return "I/O error (was the drive disconnected?)"
    return exc.strerror or str(exc)


def snapshot_name(now: Optional[datetime] = None, user: Optional[str] = None) -> str:
    now = now or datetime.now()
    return f"{user or _username()}-{now.strftime(TIMESTAMP_FORMAT)}"


def path_size(path: Path, cancel: Optional[threading.Event] = None) -> int:
    """Apparent size in bytes of a file or tree (symlinks are not followed)."""
    try:
        st = os.lstat(path)
    except OSError:
        return 0
    if not stat.S_ISDIR(st.st_mode):
        return st.st_size
    total = 0
    for dirpath, _dirs, files in os.walk(path, onerror=lambda _e: None):
        if cancel is not None and cancel.is_set():
            break
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def _is_within(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def _prune_nested(rels: Iterable[str]) -> list[str]:
    """Drop duplicates and items already covered by a selected parent directory."""
    ordered = list(dict.fromkeys(rels))
    kept: list[str] = []
    for rel in sorted(ordered, key=lambda r: (r.count("/"), r)):
        if not any(rel == k or rel.startswith(k + "/") for k in kept):
            kept.append(rel)
    keep_set = set(kept)
    return [r for r in ordered if r in keep_set]


def _filesystem_type(path: Path) -> Optional[str]:
    best, fstype = "", None
    try:
        with open("/proc/self/mounts", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                fields = line.split()
                if len(fields) < 3:
                    continue
                mount = fields[1].replace("\\040", " ")
                prefix = mount.rstrip("/") + "/"
                if (str(path) == mount or str(path).startswith(prefix)) and len(mount) > len(best):
                    best, fstype = mount, fields[2]
    except OSError:
        return None
    return fstype


def _mount_root_guess(path: Path) -> Optional[tuple[Path, bool]]:
    """If *path* looks like it lives on removable media, return (mount root, strict).

    ``strict`` means desktop auto-mounters always create a real mount point there
    (/run/media/<user>/<label>, /media/<user>/<label>), so a missing mount means the
    drive is not plugged in. /mnt/<name> is only a hint - people also use it as a
    plain directory.
    """
    parts = path.parts
    if parts[:3] == ("/", "run", "media") and len(parts) >= 5:
        return Path(*parts[:5]), True
    if parts[:2] == ("/", "media") and len(parts) >= 4:
        return Path(*parts[:4]), True
    if parts[:2] == ("/", "mnt") and len(parts) >= 3:
        return Path(*parts[:3]), False
    return None


# --------------------------------------------------------------------------- #
# Detection & path handling
# --------------------------------------------------------------------------- #


def detect_essentials(home: Optional[Path] = None) -> list[EssentialItem]:
    """Find the always-backed-up assets that actually exist under *home*."""
    home = _home(home)
    found: dict[str, EssentialItem] = {}
    for group, candidates in ESSENTIAL_GROUPS.items():
        for rel in candidates:
            if os.path.lexists(home / rel):
                found.setdefault(rel, EssentialItem(group, rel))
    for base in _ANTIGRAVITY_SCAN_DIRS:
        folder = home if base == "." else home / base
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for name in names:
            if "antigravity" in name.lower():
                rel = name if base == "." else f"{base}/{name}"
                found.setdefault(rel, EssentialItem("Antigravity", rel))
    return list(found.values())


def list_home_entries(home: Optional[Path] = None) -> list[HomeEntry]:
    """Top-level entries of *home*: directories first, then files, case-insensitive."""
    home = _home(home)
    try:
        names = os.listdir(home)
    except OSError as exc:
        raise BackupError(f"Cannot read {home}: {describe_oserror(exc)}") from exc
    entries = [HomeEntry(name, (home / name).is_dir()) for name in names]
    entries.sort(key=lambda e: (not e.is_dir, e.rel.lstrip(".").lower()))
    return entries


def list_dir_entries(rel_dir: str, home: Optional[Path] = None) -> list[HomeEntry]:
    """Entries inside *home / rel_dir*: directories first, then files, sorted case-insensitive."""
    home = _home(home)
    target = home / rel_dir if rel_dir else home
    try:
        names = os.listdir(target)
    except OSError as exc:
        raise BackupError(f"Cannot read {target}: {describe_oserror(exc)}") from exc
    entries: list[HomeEntry] = []
    for name in names:
        item_path = target / name
        try:
            is_dir = item_path.is_dir()
        except OSError:
            is_dir = False
        child_rel = f"{rel_dir}/{name}" if rel_dir else name
        entries.append(HomeEntry(child_rel, is_dir))
    entries.sort(key=lambda e: (not e.is_dir, e.rel.rsplit("/", 1)[-1].lstrip(".").lower()))
    return entries


def normalize_rel(text: str, home: Optional[Path] = None) -> str:
    """Turn user input ('~/x', '/home/me/x', '.config/x') into a path relative to home."""
    home = _home(home)
    raw = text.strip()
    if not raw:
        raise BackupError("The path is empty.")
    if "\n" in raw or "\x00" in raw:
        raise BackupError("The path contains an invalid character.")
    if raw == "~" or raw.startswith("~/"):
        candidate = home / raw[2:]
    else:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = home / candidate
    candidate = Path(os.path.normpath(candidate))
    try:
        rel = candidate.relative_to(home)
    except ValueError:
        raise BackupError(f"'{text}' is not inside your home directory ({home}).") from None
    if str(rel) == ".":
        raise BackupError("Pick individual items rather than the whole home directory.")
    return rel.as_posix()


def validate_parent(text: str, *, write: bool) -> tuple[Path, list[str]]:
    """Check the backup parent directory. Returns (path, warnings) or raises BackupError.

    The directory is deliberately NOT created on demand: if an external drive is not
    mounted, its mount point (e.g. /run/media/me/DISK) is missing or empty, and
    creating it would silently fill the system disk instead of the backup drive.
    """
    raw = text.strip()
    if not raw:
        raise BackupError("Enter the backup directory path first.")
    path = Path(os.path.normpath(os.path.abspath(os.path.expanduser(raw))))
    warnings: list[str] = []

    guess = _mount_root_guess(path)
    if guess is not None and not os.path.ismount(guess[0]):
        root, strict = guess
        message = f"{root} is not a mounted drive - is the backup disk plugged in and mounted?"
        if strict:
            raise BackupError(message)
        warnings.append(message + " Files would be written to the system disk.")

    if not path.exists():
        raise BackupError(
            f"{path} does not exist. If it is on an external drive, check that the drive is mounted."
        )
    if not path.is_dir():
        raise BackupError(f"{path} is not a directory.")
    needed = (os.W_OK if write else os.R_OK) | os.X_OK
    if not os.access(path, needed):
        action = "write to" if write else "read"
        raise BackupError(f"No permission to {action} {path} (or the drive is mounted read-only).")

    fstype = _filesystem_type(path)
    if write and fstype in _LOSSY_FILESYSTEMS:
        warnings.append(
            f"{path} is on a {fstype} filesystem, which cannot store Unix permissions or symlinks "
            "faithfully (this matters for ~/.ssh). Prefer ext4/btrfs/xfs for backups."
        )
    return path, warnings


# --------------------------------------------------------------------------- #
# Manifest & snapshots
# --------------------------------------------------------------------------- #


def _write_manifest(
    dest: Path, items: list[str], partial: list[str], home: Path, engine: str, now: datetime
) -> None:
    lines = [
        MANIFEST_HEADER,
        f"# created: {now.isoformat(timespec='seconds')}",
        f"# user: {_username()}",
        f"# host: {socket.gethostname()}",
        f"# home: {home}",
        f"# engine: {engine}",
    ]
    lines += [f"# WARNING: partial transfer: {rel}" for rel in partial]
    lines += items
    (dest / MANIFEST_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_manifest(snapshot_dir: Path) -> list[str]:
    """Return the item list from a snapshot's manifest."""
    manifest = Path(snapshot_dir) / MANIFEST_NAME
    try:
        text = manifest.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise BackupError(f"{snapshot_dir} has no {MANIFEST_NAME} (the backup was interrupted?).") from None
    except OSError as exc:
        raise BackupError(f"Cannot read {manifest}: {describe_oserror(exc)}") from exc
    return [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("# ")]


def list_snapshots(parent: str) -> list[Snapshot]:
    """All ``<user>-<timestamp>`` folders in *parent*, newest first."""
    path, _warnings = validate_parent(parent, write=False)
    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name)
    except OSError as exc:
        raise BackupError(f"Cannot read {path}: {describe_oserror(exc)}") from exc
    snapshots: list[Snapshot] = []
    for entry in entries:
        match = _SNAPSHOT_RE.match(entry.name)
        try:
            if not match or not entry.is_dir():
                continue
            created = datetime.strptime(match["stamp"], TIMESTAMP_FORMAT)
        except (ValueError, OSError):
            continue
        try:
            items, complete = tuple(read_manifest(Path(entry.path))), True
        except BackupError:
            items, complete = (), False
        snapshots.append(
            Snapshot(entry.name, Path(entry.path), match["user"], created, items, complete)
        )
    snapshots.sort(key=lambda s: s.created, reverse=True)
    return snapshots


# --------------------------------------------------------------------------- #
# Copy engines
# --------------------------------------------------------------------------- #


@dataclass
class _Ctx:
    log: LogCallback
    progress: ProgressCallback
    cancel: threading.Event
    dry_run: bool
    use_rsync: bool


def _choose_engine(engine: str, log: LogCallback) -> bool:
    if engine not in ("auto", "rsync", "python"):
        raise ValueError(f"unknown engine {engine!r}")
    if engine == "python":
        return False
    if rsync_available():
        return True
    if engine == "rsync":
        raise BackupError("rsync was requested but is not installed (or not on PATH).")
    log("warn", "rsync not found - using the built-in Python copier instead.")
    return False


def _stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _rsync_item(
    src_root: Path,
    rel: str,
    dest_root: Path,
    ctx: _Ctx,
    extra_args: list[str],
    report: Callable[[float], None],
) -> tuple[str, str]:
    """Copy one item with rsync. Returns (status, detail); status is ok/partial/cancelled."""
    info = "progress2,name1" if ctx.dry_run else "progress2"
    # --relative recreates the path below dest_root (".config/BraveSoftware" ->
    # <dest>/.config/BraveSoftware). The "/./" anchor matters: everything before it is
    # NOT transferred. A bare "./item" source would make rsync also sync the "." entry,
    # copying the source root's mode/mtime onto the destination root - i.e. it would
    # chmod your home directory to the snapshot's mode on restore.
    cmd = ["rsync", "-a", f"--info={info}", "--no-inc-recursive", "--relative"]
    if ctx.dry_run:
        cmd.append("--dry-run")
    cmd += extra_args
    cmd += ["--", f"{src_root}/./{rel}", f"{dest_root}/"]
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(src_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
    except FileNotFoundError as exc:
        raise BackupError("rsync is not installed (or not on PATH).") from exc
    except OSError as exc:
        raise BackupError(f"Could not start rsync: {describe_oserror(exc)}") from exc

    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    pending = b""
    names: list[str] = []
    problems: list[str] = []
    vanished = False

    def handle(raw: bytes) -> None:
        nonlocal vanished
        text = raw.decode("utf-8", "replace").strip()
        if not text:
            return
        match = _PROGRESS_RE.match(text)
        if match:
            report(int(match.group(1)) / 100)
        elif text.startswith("rsync"):  # "rsync: ..." / "rsync error: ..."
            if "vanished" in text:
                vanished = True
            problems.append(text)
            ctx.log("warn", text)
        elif ctx.dry_run:
            names.append(text)

    try:
        while True:
            if ctx.cancel.is_set():
                _stop(proc)
                return "cancelled", ""
            ready, _, _ = _select.select([fd], [], [], 0.2)
            if not ready:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            pending += chunk
            *lines, pending = re.split(rb"[\r\n]", pending)
            for raw in lines:
                handle(raw)
        handle(pending)
        code = proc.wait()
    finally:
        if proc.poll() is None:
            _stop(proc)
        proc.stdout.close()

    if code == 0:
        status = "ok"
    elif code == 24:  # files vanished while copying - normal for a running browser
        status = "ok"
        if not vanished:
            ctx.log("warn", f"{rel}: some files vanished during the transfer")
    elif code == 23:  # partial transfer (e.g. unreadable files)
        status = "partial"
    else:
        reason = problems[-1] if problems else "no details"
        raise BackupError(f"rsync failed with exit code {code}: {reason}")

    detail = ""
    if ctx.dry_run:
        detail = f"would transfer {len(names)} entr{'y' if len(names) == 1 else 'ies'}"
        for name in names[:6]:
            ctx.log("dim", f"    {name}")
        if len(names) > 6:
            ctx.log("dim", f"    ... and {len(names) - 6} more")
    report(1.0)
    return status, detail


def _copy_node(src: Path, dst: Path) -> None:
    """Copy a file or symlink atomically (temp name + rename), never losing the old dst."""
    tmp = dst.with_name(dst.name + ".hb-tmp")
    try:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        if os.path.islink(src):
            os.symlink(os.readlink(src), tmp)
        else:
            shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    except BaseException:
        try:
            if os.path.lexists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        raise


def _copy_leaf(
    src: Path,
    dst: Path,
    rel: str,
    ctx: _Ctx,
    policy: "RestorePolicy",
    backup_dir: Optional[Path],
    stats: dict[str, int],
) -> None:
    mode = os.lstat(src).st_mode
    if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
        stats["skipped"] += 1  # sockets, fifos, device nodes
        return
    if os.path.lexists(dst):
        if policy is RestorePolicy.SKIP:
            stats["skipped"] += 1
            return
        if os.path.isdir(dst) and not os.path.islink(dst):
            raise OSError(errno.EISDIR, "a directory exists at the destination")
        if policy is RestorePolicy.BACKUP_THEN_OVERWRITE and backup_dir is not None and not ctx.dry_run:
            stash = backup_dir / rel
            stash.parent.mkdir(parents=True, exist_ok=True)
            _copy_node(dst, stash)
    if ctx.dry_run:
        stats["copied"] += 1
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    _copy_node(src, dst)
    stats["copied"] += 1


def _count_leaves(path: Path, cancel: threading.Event) -> int:
    if path.is_symlink() or not path.is_dir():
        return 1
    total = 0
    for dirpath, dirs, files in os.walk(path):
        total += len(files) + sum(1 for d in dirs if (Path(dirpath) / d).is_symlink())
        if cancel.is_set():
            break
    return total


def _python_item(
    src_root: Path,
    rel: str,
    dest_root: Path,
    ctx: _Ctx,
    policy: "RestorePolicy",
    backup_dir: Optional[Path],
    report: Callable[[float], None],
) -> tuple[str, str]:
    """Pure-Python equivalent of the rsync call (used when rsync is missing)."""
    src = src_root / rel
    stats = {"copied": 0, "skipped": 0, "problems": 0}
    total = max(_count_leaves(src, ctx.cancel), 1)
    seen = 0

    def tick() -> None:
        nonlocal seen
        seen += 1
        report(min(seen / total, 1.0))

    if src.is_symlink() or not src.is_dir():
        _copy_leaf(src, dest_root / rel, rel, ctx, policy, backup_dir, stats)
        tick()
    else:
        created_dirs: list[tuple[Path, Path]] = []
        for dirpath, dirnames, filenames in os.walk(src):
            if ctx.cancel.is_set():
                return "cancelled", ""
            here = Path(dirpath)
            rel_here = here.relative_to(src_root)
            dst_here = dest_root / rel_here
            if os.path.lexists(dst_here) and not dst_here.is_dir():
                ctx.log("warn", f"{rel_here}: a file is in the way at the destination - skipped")
                stats["problems"] += 1
                dirnames[:] = []
                continue
            if not dst_here.exists() and not ctx.dry_run:
                dst_here.mkdir(parents=True, exist_ok=True)
                created_dirs.append((here, dst_here))
            leaves = list(filenames)
            for name in list(dirnames):  # symlinks to directories are copied as links
                if (here / name).is_symlink():
                    dirnames.remove(name)
                    leaves.append(name)
            for name in leaves:
                if ctx.cancel.is_set():
                    return "cancelled", ""
                leaf_rel = (rel_here / name).as_posix()
                try:
                    _copy_leaf(here / name, dst_here / name, leaf_rel, ctx, policy, backup_dir, stats)
                except OSError as exc:
                    stats["problems"] += 1
                    ctx.log("warn", f"{leaf_rel}: {describe_oserror(exc)}")
                tick()
        for src_dir, dst_dir in reversed(created_dirs):
            try:
                shutil.copystat(src_dir, dst_dir)
            except OSError:
                pass
    status = "partial" if stats["problems"] else "ok"
    verb = "would copy" if ctx.dry_run else "copied"
    return status, f"{verb} {stats['copied']} file(s), skipped {stats['skipped']}"


def _run_items(
    src_root: Path,
    rels: list[str],
    dest_root: Path,
    ctx: _Ctx,
    result: OperationResult,
    *,
    policy: RestorePolicy = RestorePolicy.OVERWRITE,
    backup_dir: Optional[Path] = None,
) -> None:
    extra: list[str] = []
    if policy is RestorePolicy.SKIP:
        extra.append("--ignore-existing")
    elif policy is RestorePolicy.BACKUP_THEN_OVERWRITE and backup_dir is not None:
        extra += ["--backup", f"--backup-dir={backup_dir}"]

    total = len(rels)
    prefix = "[dry run] " if ctx.dry_run else ""
    for index, rel in enumerate(rels):
        if ctx.cancel.is_set():
            result.cancelled = True
            return

        def report(fraction: float, _i: int = index, _rel: str = rel) -> None:
            ctx.progress((_i + fraction) / total, _rel)

        report(0.0)
        if not os.path.lexists(src_root / rel):
            result.missing.append(rel)
            ctx.log("warn", f"{rel}: not found, skipped")
            report(1.0)
            continue
        ctx.log("info", f"{prefix}{rel}")
        try:
            if ctx.use_rsync:
                status, detail = _rsync_item(src_root, rel, dest_root, ctx, extra, report)
            else:
                status, detail = _python_item(src_root, rel, dest_root, ctx, policy, backup_dir, report)
        except BackupError as exc:
            result.failed.append((rel, str(exc)))
            ctx.log("error", f"{rel}: {exc}")
            continue
        except OSError as exc:
            message = describe_oserror(exc)
            result.failed.append((rel, message))
            ctx.log("error", f"{rel}: {message}")
            continue
        if status == "cancelled":
            result.cancelled = True
            return
        if status == "partial":
            result.partial.append(rel)
            ctx.log("warn", f"{rel}: copied with warnings" + (f" ({detail})" if detail else ""))
        else:
            result.completed.append(rel)
            ctx.log("ok", f"{rel}" + (f" - {detail}" if detail else ""))


# --------------------------------------------------------------------------- #
# Public operations
# --------------------------------------------------------------------------- #


def run_backup(
    parent: str,
    items: Iterable[str],
    *,
    home: Optional[Path] = None,
    dry_run: bool = False,
    engine: str = "auto",
    on_progress: Optional[ProgressCallback] = None,
    on_log: Optional[LogCallback] = None,
    cancel: Optional[threading.Event] = None,
    now: Optional[datetime] = None,
) -> OperationResult:
    """Create ``<parent>/<user>-<DD-MM-YYYY_HH-MM-SS>/`` with the chosen items.

    Raises BackupError for problems that stop the whole run (bad destination,
    not enough space, ...). Per-item problems are collected in the result.
    """
    log = on_log or _ignore_log
    progress = on_progress or _ignore_progress
    cancel = cancel or threading.Event()
    home = _home(home)
    now = now or datetime.now()
    result = OperationResult(kind="backup", dry_run=dry_run)

    parent_path, warnings = validate_parent(parent, write=True)
    for warning in warnings:
        result.warnings.append(warning)
        log("warn", warning)

    rels = _prune_nested(normalize_rel(item, home) for item in items)
    if not rels:
        raise BackupError("Nothing is selected to back up.")

    dest = parent_path / snapshot_name(now)
    for rel in rels:
        if _is_within(dest, home / rel):
            raise BackupError(
                f"The backup destination {dest} is inside '{rel}', which is being backed up. "
                "Choose a backup directory outside the selected items."
            )

    use_rsync = _choose_engine(engine, log)
    ctx = _Ctx(log, progress, cancel, dry_run, use_rsync)
    result.destination = dest

    log("info", "Calculating size ...")
    needed = sum(path_size(home / rel, cancel) for rel in rels if os.path.lexists(home / rel))
    if cancel.is_set():
        result.cancelled = True
        return result
    free = shutil.disk_usage(parent_path).free
    log("info", f"About {human_size(needed)} to copy; {human_size(free)} free at the destination.")
    if needed > free:
        raise BackupError(
            f"Not enough free space at {parent_path}: need about {human_size(needed)}, "
            f"only {human_size(free)} available."
        )

    if dry_run:
        log("info", "Dry run - nothing will be written.")
    else:
        try:
            dest.mkdir(mode=0o700)  # snapshots hold private keys and browser profiles
        except OSError as exc:
            raise BackupError(f"Could not create {dest}: {describe_oserror(exc)}") from exc
    log("info", f"Snapshot: {dest}")

    _run_items(home, rels, dest, ctx, result)

    done = set(result.completed) | set(result.partial)
    if result.cancelled:
        if not dry_run:
            log(
                "warn",
                f"Backup cancelled. The incomplete folder {dest} has no {MANIFEST_NAME}, "
                "so Restore will ignore it; delete it when convenient.",
            )
    elif not dry_run:
        if done:
            ordered = [rel for rel in rels if rel in done]
            try:
                _write_manifest(dest, ordered, result.partial, home, "rsync" if use_rsync else "python", now)
            except OSError as exc:
                raise BackupError(f"Could not write {MANIFEST_NAME}: {describe_oserror(exc)}") from exc
            log("ok", f"Wrote {MANIFEST_NAME} with {len(ordered)} item(s).")
        else:
            try:
                dest.rmdir()  # nothing was copied; do not leave an empty snapshot behind
            except OSError:
                pass
    result.ok = not result.cancelled and bool(done) and not result.failed
    progress(1.0, "done")
    return result


def run_restore(
    snapshot: Snapshot,
    items: Iterable[str],
    policy: RestorePolicy,
    *,
    home: Optional[Path] = None,
    dry_run: bool = False,
    engine: str = "auto",
    on_progress: Optional[ProgressCallback] = None,
    on_log: Optional[LogCallback] = None,
    cancel: Optional[threading.Event] = None,
    now: Optional[datetime] = None,
) -> OperationResult:
    """Restore *items* from *snapshot* into *home* using the given conflict policy."""
    log = on_log or _ignore_log
    progress = on_progress or _ignore_progress
    cancel = cancel or threading.Event()
    home = _home(home)
    now = now or datetime.now()
    result = OperationResult(kind="restore", dry_run=dry_run, destination=home)

    if not snapshot.path.is_dir():
        raise BackupError(
            f"{snapshot.path} is no longer available. Is the backup drive still connected?"
        )
    manifest_items = read_manifest(snapshot.path)
    wanted = set(items)
    rels = [rel for rel in manifest_items if rel in wanted]
    for unknown in sorted(wanted - set(manifest_items)):
        log("warn", f"{unknown}: not listed in the snapshot manifest, ignored")
    if not rels:
        raise BackupError("Nothing is selected to restore.")
    if not os.access(home, os.W_OK | os.X_OK):
        raise BackupError(f"No permission to write to {home}.")

    use_rsync = _choose_engine(engine, log)
    ctx = _Ctx(log, progress, cancel, dry_run, use_rsync)

    backup_dir: Optional[Path] = None
    if policy is RestorePolicy.BACKUP_THEN_OVERWRITE:
        base = home / f".restore-backup-{now.strftime(TIMESTAMP_FORMAT)}"
        backup_dir, counter = base, 1
        while backup_dir.exists():
            counter += 1
            backup_dir = Path(f"{base}-{counter}")
        log("info", f"Existing files that get replaced will be moved to {backup_dir}")

    log("info", f"Policy: {policy.label}")
    if dry_run:
        log("info", "Dry run - nothing will be written.")
    _run_items(snapshot.path, rels, home, ctx, result, policy=policy, backup_dir=backup_dir)

    if backup_dir is not None and backup_dir.exists():
        result.safety_backup = backup_dir

    touched_ssh = any(
        rel == ".ssh" or rel.startswith(".ssh/") for rel in result.completed + result.partial
    )
    if touched_ssh and not result.cancelled:
        log("info", "Re-applying SSH permissions ...")
        result.ssh_changes = fix_ssh_permissions(home / ".ssh", dry_run=dry_run, on_log=log)
        verb = "would change" if dry_run else "changed"
        log("ok", f"SSH permissions checked ({verb} {result.ssh_changes} path(s)).")

    done = result.completed + result.partial
    result.ok = not result.cancelled and bool(done) and not result.failed
    progress(1.0, "done")
    return result


def fix_ssh_permissions(
    ssh_dir: Path, *, dry_run: bool = False, on_log: Optional[LogCallback] = None
) -> int:
    """Apply OpenSSH-friendly modes: dirs 700, *.pub 644, every other regular file 600.

    Symlinks and special files inside ~/.ssh are left alone. Returns the number of
    paths whose mode was (or, in a dry run, would be) changed.
    """
    log = on_log or _ignore_log
    ssh_dir = Path(os.path.realpath(ssh_dir))
    if not ssh_dir.is_dir():
        return 0
    changes = 0

    def apply(path: Path, mode: int) -> None:
        nonlocal changes
        try:
            current = stat.S_IMODE(os.lstat(path).st_mode)
        except OSError as exc:
            log("warn", f"{path}: {describe_oserror(exc)}")
            return
        if current == mode:
            return
        if not dry_run:
            try:
                os.chmod(path, mode)
            except OSError as exc:
                log("warn", f"chmod {mode:o} {path}: {describe_oserror(exc)}")
                return
        changes += 1
        log("dim", f"  {'would set' if dry_run else 'set'} {mode:04o}  {path}")

    apply(ssh_dir, 0o700)
    for dirpath, dirnames, filenames in os.walk(ssh_dir):
        for name in dirnames:
            sub = Path(dirpath) / name
            if not sub.is_symlink():
                apply(sub, 0o700)
        for name in filenames:
            file = Path(dirpath) / name
            try:
                if not stat.S_ISREG(os.lstat(file).st_mode):
                    continue  # symlink, socket, ...
            except OSError:
                continue
            apply(file, 0o644 if name.endswith(".pub") else 0o600)
    return changes
