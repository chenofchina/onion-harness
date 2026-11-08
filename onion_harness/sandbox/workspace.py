"""Browsing the workspace, and tracking what the agent actually changed.

The diff panel used to show only ChangeLedger entries, which are meta-operations on
Onion's own configuration. The files the agent edits while working were invisible -
the agent could rewrite a source file and the user had no way to see it. This module
supplies the missing half: a workspace tree to browse, file contents to read, and a
record of what changed during a task.

**Every path is resolved and then checked against the workspace root.** A path like
``../../.ssh/id_rsa`` or an absolute path outside the root is refused, because the
browser is reachable from the model's own event stream and from the GUI. Resolving
*before* comparing is what makes that check meaningful: comparing the raw string
would miss ``subdir/../..`` and symlinks.

Snapshots are taken from the filesystem, not parsed out of tool output. A shell
command can create, delete or rewrite anything; trying to infer the result from the
command text would be wrong more often than right.
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("onion.workspace")

# Files above this size are not read into memory for diffing; a 50 MB log has no
# useful unified diff and would stall the UI.
MAX_DIFF_BYTES = 2_000_000
# What the viewer will render. Larger files are reported as too large rather than
# truncated silently, so "the file looks short" is never a lie.
MAX_VIEW_BYTES = 1_000_000
MAX_TREE_ENTRIES = 2000
MAX_DIFF_LINES = 4000

# Directories that are never interesting to browse and can be enormous.
_SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv",
    "dist", "build", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".idea", ".vscode", "target", ".next", ".nuxt",
}

_BINARY_HINTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svgz",
    ".zip", ".gz", ".tar", ".7z", ".rar", ".pdf", ".exe", ".dll", ".pyd",
    ".so", ".dylib", ".bin", ".pyc", ".woff", ".woff2", ".ttf", ".otf",
    ".mp3", ".mp4", ".mov", ".wav", ".sqlite3", ".db",
}


class WorkspaceError(RuntimeError):
    """A path could not be used. Safe to surface to the UI."""

    def __init__(self, message: str, *, code: str = "workspace_error") -> None:
        super().__init__(message)
        self.code = code


def safe_join(root: Path, relative: str) -> Path:
    """Resolve ``relative`` under ``root``, refusing anything that escapes it.

    The check is done on the *resolved* paths. Comparing the raw strings would let
    ``a/../../etc/passwd`` through, and would follow a symlink out of the tree.
    """
    if not relative or relative in (".", "/"):
        return root

    candidate = Path(relative)
    if candidate.is_absolute():
        raise WorkspaceError("path must be relative to the workspace", code="bad_path")

    resolved_root = root.resolve()
    target = (resolved_root / candidate).resolve()

    # Windows resolves short names and case differently; `is_relative_to` handles
    # both, but a bare startswith on strings does not.
    try:
        target.relative_to(resolved_root)
    except ValueError:
        raise WorkspaceError("path is outside the workspace", code="outside_workspace") from None
    return target


def relative_to(root: Path, target: Path) -> str:
    try:
        return target.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return target.name


def is_binary(path: Path) -> bool:
    if path.suffix.lower() in _BINARY_HINTS:
        return True
    try:
        with path.open("rb") as handle:
            chunk = handle.read(4096)
    except OSError:
        return True
    # A NUL byte in the first block is the standard heuristic.
    return b"\x00" in chunk


@dataclass
class FileEntry:
    """One node in the browsable tree."""

    path: str
    name: str
    is_dir: bool
    size: int = 0
    modified: float = 0.0
    # Set when the file changed during the current task.
    changed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "is_dir": self.is_dir,
            "size": self.size,
            "modified": self.modified,
            "changed": self.changed,
        }


@dataclass
class FileView:
    """A file's contents, or the reason there are none."""

    path: str
    text: str = ""
    language: str = ""
    size: int = 0
    binary: bool = False
    truncated: bool = False
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "text": self.text,
            "language": self.language,
            "size": self.size,
            "binary": self.binary,
            "truncated": self.truncated,
            "error": self.error,
        }


_LANGUAGE_BY_SUFFIX = {
    ".py": "python", ".pyw": "python",
    ".ts": "typescript", ".tsx": "tsx", ".js": "javascript", ".jsx": "jsx",
    ".json": "json", ".md": "markdown", ".css": "css", ".html": "html",
    ".ps1": "powershell", ".psm1": "powershell", ".cmd": "batch", ".bat": "batch",
    ".sh": "bash", ".yml": "yaml", ".yaml": "yaml", ".toml": "toml",
    ".rs": "rust", ".go": "go", ".java": "java", ".c": "c", ".h": "c",
    ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp", ".rb": "ruby",
    ".sql": "sql", ".xml": "xml", ".ini": "ini", ".cfg": "ini",
}


def language_for(path: Path) -> str:
    return _LANGUAGE_BY_SUFFIX.get(path.suffix.lower(), "text")


# --------------------------------------------------------------------- tree
def list_tree(
    root: Path,
    relative: str = "",
    *,
    depth: int = 1,
    changed: Optional[Iterable[str]] = None,
) -> List[FileEntry]:
    """List one directory level, or recurse ``depth`` levels.

    ``depth=1`` is the cheap case the GUI starts with; deeper expansion is
    per-directory so opening the root of a large repository does not walk it all.
    """
    base = safe_join(root, relative)
    if not base.exists():
        raise WorkspaceError(f"no such directory: {relative}", code="not_found")
    if not base.is_dir():
        raise WorkspaceError(f"not a directory: {relative}", code="not_a_directory")

    touched = set(changed or [])
    entries: List[FileEntry] = []
    _walk(base, root, entries, depth=max(1, min(depth, 6)), touched=touched)
    entries.sort(key=lambda entry: (not entry.is_dir, entry.name.lower()))
    return entries[:MAX_TREE_ENTRIES]


def _walk(
    directory: Path,
    root: Path,
    out: List[FileEntry],
    *,
    depth: int,
    touched: set,
) -> None:
    if len(out) >= MAX_TREE_ENTRIES:
        return
    try:
        children = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except (PermissionError, OSError):
        return

    for child in children:
        if len(out) >= MAX_TREE_ENTRIES:
            return
        name = child.name
        if child.is_dir() and name in _SKIP_DIRS:
            continue
        rel = relative_to(root, child)
        try:
            stat = child.stat()
        except OSError:
            continue
        is_dir = child.is_dir()
        out.append(
            FileEntry(
                path=rel,
                name=name,
                is_dir=is_dir,
                size=0 if is_dir else stat.st_size,
                modified=stat.st_mtime,
                changed=rel in touched,
            )
        )
        if is_dir and depth > 1:
            _walk(child, root, out, depth=depth - 1, touched=touched)


def read_file(root: Path, relative: str) -> FileView:
    """Read one file for display. Never raises for ordinary problems."""
    try:
        target = safe_join(root, relative)
    except WorkspaceError as exc:
        return FileView(path=relative, error=str(exc))

    if not target.exists():
        return FileView(path=relative, error="file not found")
    if not target.is_file():
        return FileView(path=relative, error="not a file")

    try:
        size = target.stat().st_size
    except OSError as exc:
        return FileView(path=relative, error=f"cannot stat file: {exc}")

    if is_binary(target):
        return FileView(
            path=relative, size=size, binary=True, error="binary file, not shown as text"
        )

    try:
        raw = target.read_bytes()
    except OSError as exc:
        return FileView(path=relative, size=size, error=f"cannot read file: {exc}")

    truncated = len(raw) > MAX_VIEW_BYTES
    if truncated:
        raw = raw[:MAX_VIEW_BYTES]

    text = raw.decode("utf-8", errors="replace")
    return FileView(
        path=relative,
        text=text,
        language=language_for(target),
        size=size,
        truncated=truncated,
    )


# ---------------------------------------------------------------- snapshots
def fingerprint(path: Path) -> str:
    """Cheap content hash used to detect that a file changed."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(65536)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


@dataclass
class _SnapEntry:
    digest: str
    size: int
    text: str = ""


@dataclass
class FileSnapshot:
    """The state of a workspace file before a task started.

    Content is retained, not just a hash. A diff whose "before" side is missing is
    not a diff - it is a file view with a misleading label - and the hash alone
    cannot reconstruct what was overwritten.
    """

    entries: Dict[str, _SnapEntry] = field(default_factory=dict)

    @classmethod
    def take(cls, root: Optional[Path], *, max_files: int = 5000) -> "FileSnapshot":
        """Snapshot every text file under the workspace.

        Hashing (rather than mtime) is what catches an edit landing in the same
        second as the snapshot, which is common in a fast tool loop.
        """
        snapshot = cls()
        if root is None or not root.exists():
            return snapshot

        scanned = 0
        for directory, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS]
            for filename in filenames:
                path = Path(directory) / filename
                if path.suffix.lower() in _BINARY_HINTS:
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if size > MAX_DIFF_BYTES:
                    continue
                scanned += 1
                if scanned > max_files:
                    # A ceiling so a huge workspace does not make every task slower
                    # just to support a diff view.
                    return snapshot
                snapshot.entries[relative_to(root, path)] = _SnapEntry(
                    digest=fingerprint(path),
                    size=size,
                    text=_read_text(path),
                )
        return snapshot


@dataclass
class FileChange:
    """One file that differs from the snapshot."""

    path: str
    kind: str  # added | modified | deleted
    before: str = ""
    after: str = ""
    diff: str = ""
    truncated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "before": self.before,
            "after": self.after,
            "diff": self.diff,
            "truncated": self.truncated,
        }

    def summary(self, limit: int = 160) -> Dict[str, Any]:
        """Metadata without the bodies, for the task event stream.

        Line counts come from the diff when bodies were not requested, so the card
        can show "+3 -1" without shipping file contents in the event stream. Using
        the file's total line count instead would report a 400-line file as
        "400 added" after a one-line edit.
        """
        added = removed = 0
        first = ""
        if self.diff:
            for line in self.diff.splitlines():
                if line.startswith("+") and not line.startswith("+++"):
                    added += 1
                elif line.startswith("-") and not line.startswith("---"):
                    removed += 1
                elif not first and line.startswith("@@"):
                    first = line
        else:
            added = _count_lines(self.after) if self.kind != "deleted" else 0
            removed = _count_lines(self.before) if self.kind != "added" else 0
        if self.kind == "added" and not added:
            added = _count_lines(self.after)
        if self.kind == "deleted" and not removed:
            removed = _count_lines(self.before)
        return {
            "path": self.path,
            "kind": self.kind,
            "added": added,
            "removed": removed,
            "summary": first[:limit],
        }


def _count_lines(text: str) -> int:
    return len(text.splitlines()) if text else 0


def _read_text(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    if len(raw) > MAX_DIFF_BYTES:
        return raw[:MAX_DIFF_BYTES].decode("utf-8", errors="replace")
    return raw.decode("utf-8", errors="replace")


def _unified(before: str, after: str, path: str) -> str:
    lines = list(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )
    if len(lines) > MAX_DIFF_LINES:
        lines = lines[:MAX_DIFF_LINES]
        lines.append("\n... diff truncated ...\n")
    return "".join(lines)


def diff_workspace(
    root: Optional[Path], before: Optional[FileSnapshot], *, include_text: bool = True
) -> List[FileChange]:
    """Compare the workspace against a snapshot, with real before/after bodies."""
    if root is None or before is None:
        return []

    after = FileSnapshot.take(root)
    changes: List[FileChange] = []

    for path, entry in sorted(before.entries.items()):
        current = after.entries.get(path)
        if current is None:
            changes.append(
                FileChange(
                    path=path,
                    kind="deleted",
                    before=entry.text if include_text else "",
                    # The unified diff is always produced: it is what gives an
                    # accurate +/- count for the card, and the snapshot already
                    # holds the text so the cost is paid either way.
                    diff=_unified(entry.text, "", path),
                )
            )
            continue
        if current.digest == entry.digest:
            continue
        changes.append(
            FileChange(
                path=path,
                kind="modified",
                before=entry.text if include_text else "",
                after=current.text if include_text else "",
                diff=_unified(entry.text, current.text, path),
            )
        )

    for path, entry in sorted(after.entries.items()):
        if path in before.entries:
            continue
        changes.append(
            FileChange(
                path=path,
                kind="added",
                after=entry.text if include_text else "",
                diff=_unified("", entry.text, path),
            )
        )

    return changes


__all__ = [
    "WorkspaceError",
    "FileEntry",
    "FileView",
    "FileChange",
    "FileSnapshot",
    "safe_join",
    "list_tree",
    "read_file",
    "compare",
    "diff_workspace",
    "fingerprint",
    "language_for",
    "MAX_VIEW_BYTES",
]