"""Production reader behavior uses only synthetic repositories and no Jev calls."""

import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SCRIPT = ROOT / "scripts/read-context.py"


class ReaderTests(unittest.TestCase):
    def test_safe_source_read_handles_legal_short_chunks(self):
        from jev_context.safe_io import read_source_bytes
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            expected = b'def refresh():\n    return "expired token"\n'
            (repo / 'source.py').write_bytes(expected)
            real_fdopen = os.fdopen
            class ShortChunks:
                def __init__(self, stream):
                    self.stream = stream
                def __enter__(self):
                    self.stream.__enter__()
                    return self
                def __exit__(self, *args):
                    return self.stream.__exit__(*args)
                def fileno(self):
                    return self.stream.fileno()
                def read(self, size):
                    return self.stream.read(min(size, 7))
            with patch('jev_context.safe_io.os.fdopen', side_effect=lambda *a, **k: ShortChunks(real_fdopen(*a, **k))):
                self.assertEqual(read_source_bytes(repo, 'source.py'), expected)

    def test_snapshot_accounts_partial_bytes_before_read_error(self):
        from jev_context.source_snapshot import capture_sources
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            for name in ('a.py', 'b.py'):
                (repo / name).write_bytes(b'1234567')
            real_fdopen = os.fdopen
            observed = [0]
            class PartialThenError:
                def __init__(self, stream):
                    self.stream, self.done = stream, False
                def __enter__(self):
                    self.stream.__enter__()
                    return self
                def __exit__(self, *args):
                    return self.stream.__exit__(*args)
                def fileno(self):
                    return self.stream.fileno()
                def read(self, size):
                    if self.done:
                        raise OSError('synthetic read failure')
                    self.done = True
                    chunk = self.stream.read(min(size, 7))
                    observed[0] += len(chunk)
                    return chunk
            with patch('jev_context.safe_io.os.fdopen', side_effect=lambda *a, **k: PartialThenError(real_fdopen(*a, **k))):
                with capture_sources(repo, ['a.py', 'b.py'], [], [], max_files=10,
                                     max_total_bytes=10, max_file_bytes=100) as snapshot:
                    self.assertEqual(snapshot.io_metrics['source_read_bytes'], observed[0])
                    self.assertLessEqual(observed[0], 11)
                    self.assertFalse(snapshot.sources)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.state = self.base / "read-state.json"
        self.write("entry.py", "first\nsecond\nthird\nfourth\nfifth\nsixth\n")

    def write(self, relative, text):
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        return path

    def invoke(self, *args, state=False, plan=None):
        command = [sys.executable, str(SCRIPT), "--repo", str(self.repo)]
        if state:
            command.extend(["--state", str(self.state)])
        if plan is not None:
            command.extend(["--plan", str(plan)])
        result = subprocess.run([*command, *map(str, args)], capture_output=True, check=False)
        return result.returncode, result.stdout.decode("utf-8"), result.stderr.decode("utf-8")

    def success(self, *args, **options):
        status, text, error = self.invoke(*args, **options)
        self.assertEqual(status, 0, error)
        self.assertEqual(error, "")
        return text

    def failure(self, *args, **options):
        status, text, error = self.invoke(*args, **options)
        self.assertNotEqual(status, 0)
        self.assertEqual(text, "")
        self.assertNotIn("Traceback", error)
        self.assertIn("read-context:", error)
        return error

    def plan(self, **changes):
        value = dict(repo=str(self.repo), source_hashes={
            "entry.py": hashlib.sha256((self.repo / "entry.py").read_bytes()).hexdigest()},
            reading_list=[dict(path="entry.py", start_line=1, end_line=6)])
        value.update(changes)
        path = self.base / "plan.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_explicit_read_has_one_header_per_contiguous_range(self):
        text = self.success("--range", "entry.py", 1, 2,
                            "--range", "entry.py", 4, 4)
        self.assertEqual(text, "entry.py:1-2\n1:first\n2:second\nentry.py:4-4\n4:fourth\n")

    @unittest.skipUnless(Path('/tmp').is_symlink() and Path('/tmp').resolve() == Path('/private/tmp'), 'macOS system alias only')
    def test_macos_system_tmp_alias_supports_plan_and_state(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as directory:
            folder = Path(directory)
            plan = folder / 'plan.json'
            plan.write_text(json.dumps(dict(repo=str(self.repo), source_hashes={}, reading_list=[])))
            alias_plan = Path('/tmp') / plan.relative_to('/private/tmp')
            state = Path('/tmp') / folder.relative_to('/private/tmp') / 'state.json'
            from jev_context.reader import read_ranges
            self.assertIn('1:first', read_ranges(self.repo, [('entry.py', 1, 1)], plan=alias_plan, state=state))
            self.assertEqual(read_ranges(self.repo, [('entry.py', 1, 1)], state=state), '')

    def test_no_range_never_delivers_source(self):
        self.failure()

    def test_no_state_can_read_again_and_merges_request_overlap(self):
        args = ("--range", "entry.py", 1, 3, "--range", "entry.py", 2, 4)
        expected = "entry.py:1-4\n1:first\n2:second\n3:third\n4:fourth\n"
        self.assertEqual(self.success(*args), expected)
        self.assertEqual(self.success(*args), expected)

    def test_state_subtracts_intersection_and_repeat_explicitly_rereads(self):
        self.success("--range", "entry.py", 2, 3, state=True)
        text = self.success("--range", "entry.py", 1, 5, state=True)
        self.assertEqual(text, "entry.py:1-1\n1:first\nentry.py:4-5\n4:fourth\n5:fifth\n")
        self.assertEqual(self.success("--range", "entry.py", 1, 5, state=True), "")
        self.assertEqual(self.success("--range", "entry.py", 2, 3, "--repeat", state=True),
                         "entry.py:2-3\n2:second\n3:third\n")
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(saved["repo"], str(self.repo))
        self.assertEqual(saved["files"]["entry.py"]["ranges"], [[1, 5]])
        self.assertNotIn("first", self.state.read_text(encoding="utf-8"))
        self.assertEqual(os.stat(self.state).st_mode & 0o777, 0o600)

    def test_modified_state_source_fails_even_with_repeat_before_any_output(self):
        self.success("--range", "entry.py", 1, 1, state=True)
        original_state = self.state.read_bytes()
        self.write("entry.py", "MODIFIED_BODY_SENTINEL\nsecond\n")
        for flag in ((), ("--repeat",)):
            error = self.failure("--range", "entry.py", 2, 2, *flag, state=True)
            self.assertIn("changed", error)
            self.assertNotIn("MODIFIED_BODY_SENTINEL", error)
            self.assertEqual(self.state.read_bytes(), original_state)

    def test_plan_validates_requested_hash_but_allows_extra_read(self):
        plan = self.plan()
        self.write("helper.py", "helper\n")
        self.assertEqual(self.success("--range", "helper.py", 1, 1, plan=plan),
                         "helper.py:1-1\n1:helper\n")
        self.write("entry.py", "CHANGED_PLAN_SOURCE\n")
        error = self.failure("--range", "helper.py", 1, 1,
                             "--range", "entry.py", 1, 1, plan=plan)
        self.assertIn("changed", error)
        self.assertNotIn("CHANGED_PLAN_SOURCE", error)

    def test_plan_repo_mismatch_fails(self):
        self.failure("--range", "entry.py", 1, 1,
                     plan=self.plan(repo=str(self.base)))

    def test_unrequested_plan_source_need_not_be_read(self):
        plan = self.plan()
        self.write("entry.py", "modified\n")
        self.write("helper.py", "helper\n")
        self.assertEqual(self.success("--range", "helper.py", 1, 1, plan=plan),
                         "helper.py:1-1\n1:helper\n")

    def test_invalid_range_fails_whole_request_without_state_write(self):
        for start, end in ((0, 1), (3, 2), (1, 9), ("wrong", 2)):
            with self.subTest(start=start, end=end):
                self.failure("--range", "entry.py", 1, 1,
                             "--range", "entry.py", start, end, state=True)
                self.assertFalse(self.state.exists())

    def test_total_limit_is_not_hidden_truncation(self):
        self.write("long.py", "BODY_LINE\n" * 301)
        self.failure("--range", "long.py", 1, 301)
        self.failure("--range", "long.py", 1, 200, "--range", "long.py", 201, 301)
        text = self.success("--range", "long.py", 1, 300)
        self.assertTrue(text.endswith("300:BODY_LINE\n"))
        self.assertNotIn("301:", text)

    def test_ignore_secret_binary_symlink_and_unsafe_paths_fail(self):
        self.write(".gitignore", "ignored.py\n")
        self.write("ignored.py", "IGNORED_SOURCE\n")
        self.write(".env", "SYNTHETIC_PRIVATE_SOURCE\n")
        self.write("data.py", "BINARY_SOURCE\0\n")
        self.write("invalid_utf8.py", "placeholder\n").write_bytes(b"\xff\n")
        (self.repo / "link.py").symlink_to(self.repo / "entry.py")
        (self.repo / "linked").symlink_to(self.repo, target_is_directory=True)
        for path in ("ignored.py", ".env", "data.py", "invalid_utf8.py", "link.py",
                     "linked/entry.py", "../repo/entry.py", str(self.repo / "entry.py"),
                     "bad\nname.py", "missing.py"):
            with self.subTest(path=path):
                error = self.failure("--range", "entry.py", 1, 1,
                                     "--range", path, 1, 1)
                self.assertNotIn("SOURCE", error)

    def test_lf_line_numbers_preserve_unicode_separators_and_bare_cr(self):
        self.write("中文.py", "甲\u2028乙\u2029丙\v丁\f戊\r己\n下一行\r\n末行")
        text = self.success("--range", "中文.py", 1, 3)
        self.assertEqual(text, "中文.py:1-3\n1:甲\u2028乙\u2029丙\v丁\f戊\r己\n2:下一行\r\n3:末行\n")

    def test_state_must_be_external_and_without_symlink_components(self):
        for state in (self.repo / "state.json", self.base / "state-link.json"):
            if state.name == "state-link.json":
                state.symlink_to(self.base / "outside.json")
            self.state = state
            self.failure("--range", "entry.py", 1, 1, state=True)
        real_parent = self.base / "actual"
        real_parent.mkdir()
        linked_parent = self.base / "linked"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        self.state = linked_parent / "state.json"
        self.failure("--range", "entry.py", 1, 1, state=True)
        self.assertFalse((real_parent / "state.json").exists())

    def test_invalid_or_other_repo_state_fails_without_overwriting(self):
        for value in ({"repo": str(self.base), "files": {}},
                      {"repo": str(self.repo), "files": {"entry.py": {
                          "sha256": "bad", "ranges": [[1, 2]]}}},
                      {"repo": str(self.repo), "files": {"entry.py": {
                          "sha256": "0" * 64, "ranges": [[True, 2]]}}}):
            self.state.write_text(json.dumps(value), encoding="utf-8")
            before = self.state.read_bytes()
            self.failure("--range", "entry.py", 1, 1, state=True)
            self.assertEqual(self.state.read_bytes(), before)

    def test_safe_open_refuses_symlink_swap_after_path_check(self):
        try:
            module = importlib.import_module("jev_context.safe_io")
        except ModuleNotFoundError:
            self.fail("safe_io must open repository files without following swapped symlinks")
        outside = self.base / "outside.py"
        outside.write_text("OUTSIDE_NEVER_READ\n", encoding="utf-8")
        source = self.repo / "entry.py"
        original = module._safe_path

        def swap(repo, relative, excluded):
            result = original(repo, relative, excluded)
            source.unlink()
            source.symlink_to(outside)
            return result

        with patch.object(module, "_safe_path", side_effect=swap):
            with self.assertRaises(ValueError):
                module.read_source_bytes(self.repo, "entry.py")

    def test_safe_open_refuses_parent_directory_swap_after_path_check(self):
        module = importlib.import_module("jev_context.safe_io")
        self.write("package/source.py", "safe\n")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "source.py").write_text("OUTSIDE_NEVER_READ\n", encoding="utf-8")
        original = module._safe_path

        def swap(repo, relative, excluded):
            result = original(repo, relative, excluded)
            (self.repo / "package").rename(self.repo / "saved-package")
            (self.repo / "package").symlink_to(outside, target_is_directory=True)
            return result

        with patch.object(module, "_safe_path", side_effect=swap):
            with self.assertRaises(ValueError):
                module.read_source_bytes(self.repo, "package/source.py")

    def test_requested_file_outside_scan_bound_is_not_read(self):
        module = importlib.import_module("jev_context.reader")
        self.write("z.py", "SCAN_EXCLUDED_BODY\n")
        with patch("jev_context.retrieval.MAX_FILES", 1):
            with self.assertRaises(module.ReaderFailure):
                module.read_ranges(self.repo, [("z.py", 1, 1)])

    def test_unavailable_discovery_and_symlink_ignore_control_fail_closed(self):
        module = importlib.import_module("jev_context.reader")
        with patch("jev_context.retrieval.shutil.which", return_value=None):
            with self.assertRaises(module.ReaderFailure):
                module.read_ranges(self.repo, [("entry.py", 1, 1)])
        outside = self.base / "ignore"
        outside.write_text("", encoding="utf-8")
        (self.repo / ".gitignore").symlink_to(outside)
        self.failure("--range", "entry.py", 1, 1)

    def test_safe_open_without_platform_support_fails_closed(self):
        module = importlib.import_module("jev_context.safe_io")
        with patch.object(module.os, "supports_dir_fd", set()):
            with self.assertRaisesRegex(ValueError, "POSIX"):
                module.read_source_bytes(self.repo, "entry.py")


if __name__ == "__main__":
    unittest.main()
