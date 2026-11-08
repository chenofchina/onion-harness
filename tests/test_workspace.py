"""Workspace browsing and file-change diffing.

The path tests are security tests: the browser is reachable from the GUI and its
paths can be influenced by model output, so anything that escapes the workspace root
is a way to read arbitrary files on the machine.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from onion_harness.sandbox.workspace import (
    FileSnapshot,
    WorkspaceError,
    diff_workspace,
    fingerprint,
    is_binary,
    language_for,
    list_tree,
    read_file,
    relative_to,
    safe_join,
)


class _Workspace(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="onion-ws-"))
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
        (self.root / "src" / "util.ts").write_text("export const x = 1\n", encoding="utf-8")
        (self.root / "README.md").write_text("# Project\n", encoding="utf-8")


class PathSafetyTest(_Workspace):
    """Every escape attempt must be refused, not silently resolved."""

    def test_relative_path_resolves_inside(self):
        target = safe_join(self.root, "src/app.py")
        self.assertEqual(target, (self.root / "src" / "app.py").resolve())

    def test_empty_path_is_the_root(self):
        self.assertEqual(safe_join(self.root, ""), self.root)

    def test_parent_traversal_refused(self):
        with self.assertRaises(WorkspaceError) as ctx:
            safe_join(self.root, "../outside.txt")
        self.assertEqual(ctx.exception.code, "outside_workspace")

    def test_traversal_hidden_mid_path_refused(self):
        # `src/../../x` looks relative but resolves above the root.
        with self.assertRaises(WorkspaceError) as ctx:
            safe_join(self.root, "src/../../etc/passwd")
        self.assertEqual(ctx.exception.code, "outside_workspace")

    def test_absolute_path_refused(self):
        with self.assertRaises(WorkspaceError) as ctx:
            safe_join(self.root, "C:/Windows/win.ini")
        self.assertEqual(ctx.exception.code, "bad_path")

    def test_nested_inside_is_allowed(self):
        self.assertTrue(str(safe_join(self.root, "src/util.ts")).endswith("util.ts"))

    def test_windows_style_traversal(self):
        with self.assertRaises(WorkspaceError):
            safe_join(self.root, "..\\..\\secret.txt")

    def test_junction_escape_refused(self):
        """A Windows directory junction is the realistic local escape vector.

        Creating a symlink needs administrator rights on Windows, so a junction is
        what an attacker or a stray setup script would actually use. It looks like
        an ordinary subdirectory until the path is resolved.
        """
        import subprocess

        outside = Path(tempfile.mkdtemp(prefix="onion-outside-")) / "secret.txt"
        outside.write_text("secret", encoding="utf-8")
        link = self.root / "escape_junction"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside.parent)],
            capture_output=True,
        )
        if result.returncode != 0:
            self.skipTest("could not create a junction on this system")

        with self.assertRaises(WorkspaceError) as ctx:
            safe_join(self.root, "escape_junction/secret.txt")
        self.assertEqual(ctx.exception.code, "outside_workspace")
        # And the reader refuses too, rather than returning the secret.
        self.assertEqual(read_file(self.root, "escape_junction/secret.txt").text, "")

    def test_symlink_escape_refused(self):
        """A link pointing outside the root must not be followed.

        Symlinks are the case a naive string check misses entirely, because the
        path text looks perfectly contained.
        """
        outside = Path(tempfile.mkdtemp(prefix="onion-outside-")) / "secret.txt"
        outside.write_text("secret", encoding="utf-8")
        link = self.root / "escape"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("symlinks unavailable on this system")
        with self.assertRaises(WorkspaceError):
            safe_join(self.root, "escape")


class TreeTest(_Workspace):
    def test_lists_one_level(self):
        entries = list_tree(self.root)
        names = {entry.name for entry in entries}
        self.assertIn("src", names)
        self.assertIn("README.md", names)

    def test_directories_sort_first(self):
        entries = list_tree(self.root)
        self.assertTrue(entries[0].is_dir)

    def test_skips_vendor_directories(self):
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "x.js").write_text("x", encoding="utf-8")
        names = {entry.name for entry in list_tree(self.root)}
        self.assertNotIn("node_modules", names)

    def test_subdirectory(self):
        entries = list_tree(self.root, "src")
        self.assertEqual({entry.name for entry in entries}, {"app.py", "util.ts"})

    def test_depth(self):
        deep = self.root / "src" / "inner"
        deep.mkdir()
        (deep / "deep.py").write_text("x", encoding="utf-8")
        shallow = {entry.name for entry in list_tree(self.root, depth=1)}
        self.assertNotIn("inner", shallow)
        deeper = {entry.name for entry in list_tree(self.root, depth=2)}
        self.assertIn("inner", deeper)

    def test_changed_flag(self):
        entries = list_tree(self.root, "src", changed={"src/app.py"})
        by_name = {entry.name: entry for entry in entries}
        self.assertTrue(by_name["app.py"].changed)
        self.assertFalse(by_name["util.ts"].changed)

    def test_missing_directory(self):
        with self.assertRaises(WorkspaceError):
            list_tree(self.root, "nope")

    def test_file_is_not_a_directory(self):
        with self.assertRaises(WorkspaceError) as ctx:
            list_tree(self.root, "README.md")
        self.assertEqual(ctx.exception.code, "not_a_directory")

    def test_entries_have_relative_posix_paths(self):
        entries = list_tree(self.root, "src")
        self.assertTrue(all("/" in entry.path or entry.path for entry in entries))
        self.assertIn("src/app.py", {entry.path for entry in entries})


class ReadFileTest(_Workspace):
    def test_reads_text(self):
        view = read_file(self.root, "src/app.py")
        self.assertIn("print('hello')", view.text)
        self.assertEqual(view.language, "python")
        self.assertFalse(view.error)

    def test_language_detection(self):
        self.assertEqual(read_file(self.root, "src/util.ts").language, "typescript")
        self.assertEqual(read_file(self.root, "README.md").language, "markdown")

    def test_missing_file(self):
        view = read_file(self.root, "ghost.txt")
        self.assertTrue(view.error)
        self.assertIn("not found", view.error)

    def test_directory_is_refused(self):
        view = read_file(self.root, "src")
        self.assertTrue(view.error)

    def test_escape_returns_an_error_not_content(self):
        view = read_file(self.root, "../secret.txt")
        self.assertTrue(view.error)
        self.assertEqual(view.text, "")

    def test_binary_is_reported_not_decoded(self):
        blob = self.root / "image.png"
        blob.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
        view = read_file(self.root, "image.png")
        self.assertTrue(view.binary)
        self.assertEqual(view.text, "")

    def test_nul_byte_marks_binary(self):
        blob = self.root / "weird.dat"
        blob.write_bytes(b"text\x00more")
        self.assertTrue(is_binary(blob))

    def test_truncation_is_flagged(self):
        big = self.root / "big.txt"
        big.write_text("x" * 1_200_000, encoding="utf-8")
        view = read_file(self.root, "big.txt")
        self.assertTrue(view.truncated)

    def test_language_helper(self):
        self.assertEqual(language_for(Path("a.py")), "python")
        self.assertEqual(language_for(Path("a.unknown")), "text")


class SnapshotDiffTest(_Workspace):
    def test_no_change_means_no_diff(self):
        snapshot = FileSnapshot.take(self.root)
        self.assertEqual(diff_workspace(self.root, snapshot), [])

    def test_modified_file_has_before_and_after(self):
        snapshot = FileSnapshot.take(self.root)
        (self.root / "src" / "app.py").write_text("print('goodbye')\n", encoding="utf-8")
        changes = diff_workspace(self.root, snapshot)
        self.assertEqual(len(changes), 1)
        change = changes[0]
        self.assertEqual(change.kind, "modified")
        self.assertIn("hello", change.before)
        self.assertIn("goodbye", change.after)
        self.assertIn("+print('goodbye')", change.diff)
        self.assertIn("-print('hello')", change.diff)

    def test_added_file(self):
        snapshot = FileSnapshot.take(self.root)
        (self.root / "new.py").write_text("x = 1\n", encoding="utf-8")
        changes = diff_workspace(self.root, snapshot)
        self.assertEqual([change.kind for change in changes], ["added"])
        self.assertEqual(changes[0].before, "")
        self.assertIn("x = 1", changes[0].after)

    def test_deleted_file(self):
        snapshot = FileSnapshot.take(self.root)
        (self.root / "README.md").unlink()
        changes = diff_workspace(self.root, snapshot)
        self.assertEqual([change.kind for change in changes], ["deleted"])
        self.assertIn("# Project", changes[0].before)

    def test_multiple_changes_are_all_reported(self):
        snapshot = FileSnapshot.take(self.root)
        (self.root / "src" / "app.py").write_text("changed\n", encoding="utf-8")
        (self.root / "added.txt").write_text("new\n", encoding="utf-8")
        (self.root / "README.md").unlink()
        kinds = {change.kind for change in diff_workspace(self.root, snapshot)}
        self.assertEqual(kinds, {"modified", "added", "deleted"})

    def test_same_size_edit_is_detected(self):
        """Hashing, not size or mtime, is what makes this reliable."""
        target = self.root / "same.txt"
        target.write_text("aaaa", encoding="utf-8")
        snapshot = FileSnapshot.take(self.root)
        target.write_text("bbbb", encoding="utf-8")
        self.assertEqual(len(diff_workspace(self.root, snapshot)), 1)

    def test_include_text_false_skips_bodies_but_keeps_counts(self):
        """The event stream must not carry file contents, but the card still needs
        an accurate +/- count, so the diff is computed and only bodies dropped."""
        snapshot = FileSnapshot.take(self.root)
        (self.root / "src" / "app.py").write_text("one\ntwo\n", encoding="utf-8")
        changes = diff_workspace(self.root, snapshot, include_text=False)
        self.assertEqual(changes[0].after, "")
        self.assertEqual(changes[0].before, "")
        self.assertEqual(changes[0].kind, "modified")
        self.assertGreater(changes[0].summary()["added"], 0)

    def test_summary_counts_actual_edits_not_file_length(self):
        """A one-line edit to a long file must read "+1", not "+400"."""
        long_file = self.root / "long.txt"
        newline = "\n"
        long_file.write_text(newline.join(f"line {i}" for i in range(400)) + newline, encoding="utf-8")
        snapshot = FileSnapshot.take(self.root)
        long_file.write_text(
            newline.join(
                ("line 0 CHANGED" if i == 0 else f"line {i}") for i in range(400)
            )
            + newline,
            encoding="utf-8",
        )
        change = next(c for c in diff_workspace(self.root, snapshot) if c.path == "long.txt")
        summary = change.summary()
        self.assertEqual(summary["added"], 1)
        self.assertEqual(summary["removed"], 1)

    def test_tree_marks_changed_paths(self):
        snapshot = FileSnapshot.take(self.root)
        (self.root / "src" / "app.py").write_text("changed", encoding="utf-8")
        changed = {change.path for change in diff_workspace(self.root, snapshot)}
        entries = list_tree(self.root, "src", changed=changed)
        by_name = {entry.name: entry for entry in entries}
        self.assertTrue(by_name["app.py"].changed)
        self.assertFalse(by_name["util.ts"].changed)

    def test_summary_has_no_bodies(self):
        snapshot = FileSnapshot.take(self.root)
        (self.root / "src" / "app.py").write_text("a\nb\nc\n", encoding="utf-8")
        summary = diff_workspace(self.root, snapshot)[0].summary()
        self.assertEqual(summary["path"], "src/app.py")
        self.assertNotIn("before", summary)
        self.assertIn("added", summary)

    def test_missing_root_means_no_changes(self):
        self.assertEqual(diff_workspace(None, None), [])

    def test_fingerprint_changes_with_content(self):
        target = self.root / "f.txt"
        target.write_text("one", encoding="utf-8")
        first = fingerprint(target)
        target.write_text("two", encoding="utf-8")
        self.assertNotEqual(first, fingerprint(target))

    def test_relative_to(self):
        self.assertEqual(relative_to(self.root, self.root / "src" / "app.py"), "src/app.py")


class ServiceEndpointTest(_Workspace):
    """The service must refuse to browse without a configured workspace."""

    def setUp(self):
        super().setUp()
        os.environ.setdefault("ONION_SECRET_STORE", "memory")
        from onion_harness.api.service import OnionService

        self.service = OnionService(data_dir=Path(tempfile.mkdtemp(prefix="onion-wssvc-")))
        self.service.start()

    def tearDown(self):
        self.service.stop()

    def test_no_workspace_means_empty_tree(self):
        self.service.update_settings({"workspace": ""})
        data = self.service.workspace_tree()
        self.assertEqual(data["entries"], [])
        self.assertIn("no workspace", data["error"])

    def test_tree_and_file_with_a_workspace(self):
        self.service.update_settings({"workspace": str(self.root)})
        data = self.service.workspace_tree()
        self.assertTrue(any(entry["name"] == "src" for entry in data["entries"]))
        view = self.service.workspace_file("src/app.py")
        self.assertIn("print('hello')", view["text"])

    def test_file_escape_is_rejected(self):
        from onion_harness.api.service import ApiError

        self.service.update_settings({"workspace": str(self.root)})
        with self.assertRaises(ApiError):
            self.service.workspace_file("../../secret.txt")

    def test_missing_file_is_404(self):
        from onion_harness.api.service import ApiError

        self.service.update_settings({"workspace": str(self.root)})
        with self.assertRaises(ApiError) as ctx:
            self.service.workspace_file("ghost.txt")
        self.assertEqual(ctx.exception.status, 404)

    def test_task_diff_reports_changes(self):
        self.service.update_settings({"workspace": str(self.root)})
        self.service._take_snapshot("task-1")
        (self.root / "src" / "app.py").write_text("modified\n", encoding="utf-8")
        data = self.service.task_file_changes("task-1")
        self.assertEqual(len(data["changes"]), 1)
        self.assertEqual(data["changes"][0]["path"], "src/app.py")

    def test_task_diff_without_snapshot(self):
        data = self.service.task_file_changes("never-ran")
        self.assertEqual(data["changes"], [])
        self.assertIn("no snapshot", data["note"])


if __name__ == "__main__":
    unittest.main()