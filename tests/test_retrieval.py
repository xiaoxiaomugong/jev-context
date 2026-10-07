import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


MODULE = Path(__file__).resolve().parents[1] / "scripts" / "jev_context" / "retrieval.py"


def load_module():
    spec = importlib.util.spec_from_file_location("jev_context_retrieval", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        self.module = load_module()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, path, text):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def collect(self, **kwargs):
        return self.module.collect(self.repo, kwargs.pop("task", "repair expired token"), kwargs.pop("terms", ["expired token"]), **kwargs)

    def test_multiline_method_and_decorator_are_preserved(self):
        self.write("service.py", "class Service:\n    @staticmethod\n    def refresh(\n        token: str,\n        retries: int = 2,\n    ) -> str:\n        if token == 'expired token':\n            return 'fresh'\n        return token\n\n    def unrelated(self):\n        return 1\n")
        result = self.collect(expand=False)
        candidate = result["candidates"][0]
        self.assertEqual((candidate["start_line"], candidate["end_line"]), (2, 9))
        self.assertEqual(candidate["symbol"], "Service.refresh")
        self.assertEqual(candidate["kind"], "method")
        self.assertIn("retries: int = 2", candidate["signature"])
        self.assertEqual(candidate["extraction_mode"], "python_ast")
        self.assertFalse(candidate["incomplete"])

    def test_imported_symbol_and_corresponding_test_expand(self):
        self.write("app.py", "from lib import renew\n\ndef refresh(token):\n    # expired token\n    return renew(token)\n")
        self.write("lib.py", "def renew(token):\n    return token + '-fresh'\n\ndef unrelated():\n    return 1\n")
        self.write("tests/test_app.py", "from app import refresh\n\ndef test_refresh():\n    assert refresh('old') == 'old-fresh'\n")
        result = self.collect()
        found = {(c["path"], c["symbol"]): c for c in result["candidates"]}
        self.assertIn(("lib.py", "renew"), found)
        self.assertIn(("tests/test_app.py", "test_refresh"), found)
        self.assertTrue(any(r["type"] == "local_import" for r in found[("lib.py", "renew")]["relations"]))
        self.assertTrue(any("test" in r["type"] for r in found[("tests/test_app.py", "test_refresh")]["relations"]))

    def test_chinese_task_with_english_terms(self):
        self.write("cache.py", "def expire():\n    return 'cache expiration'\n")
        result = self.collect(task="修复缓存过期问题", terms=["cache expiration"], expand=False)
        self.assertEqual(result["candidates"][0]["symbol"], "expire")

    def test_empty_and_zero_hits(self):
        self.assertEqual(self.collect()["status"], "empty")
        self.write("hello.py", "def hello():\n    return 1\n")
        result = self.collect(task="zzzzmissing", terms=["xxxnohit"])
        self.assertEqual(result["status"], "no_hits")
        self.assertEqual(result["candidates"], [])

    def test_syntax_damage_and_other_languages_are_text_fallbacks(self):
        self.write("broken.py", "def broken(:\n    # expired token\n")
        self.write("client.js", "function renew() {\n  throw new Error('expired token');\n}\n")
        result = self.collect(expand=False)
        self.assertEqual({c["extraction_mode"] for c in result["candidates"]}, {"text_fallback"})
        self.assertTrue(any("syntax" in d for d in result["diagnostics"]))

    def test_ignore_secrets_binary_and_symlink_are_excluded(self):
        self.write(".gitignore", "ignored.py\n")
        self.write("ignored.py", "expired token\n")
        self.write(".env", "expired token\n")
        self.write("node_modules/pkg.py", "expired token\n")
        self.write("safe.py", "def safe():\n    return 'expired token'\n")
        self.write("image.bin", "expired token\x00")
        outside = self.repo.parent / (self.repo.name + "_outside.py")
        outside.write_text("expired token\n", encoding="utf-8")
        try:
            (self.repo / "link.py").symlink_to(outside)
            result = self.collect(expand=False)
            self.assertEqual({c["path"] for c in result["candidates"]}, {"safe.py"})
            self.assertNotIn(".env", result["files"])
        finally:
            outside.unlink()

    def test_large_function_keeps_full_symbol_range_and_actual_range(self):
        source = "def long_function():\n" + "    value = 1\n" * 400 + "    return 'expired token'\n"
        self.write("large.py", source)
        candidate = self.collect(expand=False)["candidates"][0]
        self.assertTrue(candidate["incomplete"])
        self.assertEqual(candidate["symbol_start_line"], 1)
        self.assertEqual(candidate["symbol_end_line"], 402)
        self.assertLessEqual(candidate["end_line"] - candidate["start_line"] + 1, 300)
        self.assertLessEqual(len(candidate["excerpt"].encode("utf-8")), 1500)
        self.assertGreater(candidate["estimated_read_tokens"], len(candidate["signature"]) // 3)

    def test_explicit_symbols_and_paths_pin_located_entries(self):
        self.write("entry.py", "def start():\n    return 1\n\ndef worker():\n    return 2\n")
        symbol_result = self.collect(terms=["xxxnohit"], symbols=["start"], expand=False)
        self.assertEqual(symbol_result["candidates"][0]["symbol"], "start")
        self.assertTrue(symbol_result["candidates"][0]["pinned"])
        path_result = self.collect(terms=["xxxnohit"], paths=["entry.py"], expand=False)
        self.assertTrue(path_result["candidates"])
        self.assertTrue(all(c["pinned"] for c in path_result["candidates"]))

    def test_selection_does_not_take_rg_first_lines(self):
        for index in range(10):
            self.write(f"aaa{index}.py", "def weak():\n    return 'token'\n")
        self.write("zzz.py", "def core():\n    return 'expired token'\n")
        result = self.collect(terms=["token", "expired token"], candidate_limit=3, expand=False)
        self.assertIn("zzz.py", {c["path"] for c in result["candidates"]})
        self.assertEqual(len(result["candidates"]), 3)
        self.assertTrue(any("candidate" in d and "limit" in d for d in result["diagnostics"]))

    def test_evaluation_trace_identifies_candidate_truncation_without_gold(self):
        for index in range(5):
            self.write(f"file{index}.py", "def relevant():\n    return 'expired token'\n")
        result = self.collect(expand=False, candidate_limit=2, trace=True)
        trace = result["retrieval_trace"]
        self.assertEqual(len(trace["pre_truncation_candidates"]), 5)
        self.assertEqual(len(trace["post_per_file_candidates"]), 5)
        self.assertEqual(len(trace["selected_candidate_ids"]), 2)
        self.assertEqual(len(trace["discovered_files"]), 5)
        self.assertEqual(trace["scan"]["scan_limit_skipped"], 0)

    def test_evaluation_trace_separates_scan_bounds_from_safety_exclusions(self):
        self.write("a.py", "expired token\n")
        self.write("b.py", "expired token\n")
        self.write(".env", "synthetic placeholder\n")
        with patch.object(self.module, "MAX_FILES", 1):
            result = self.collect(expand=False, trace=True)
        trace = result["retrieval_trace"]
        self.assertEqual(trace["scan"]["scan_limit_skipped"], 1)
        self.assertEqual(trace["scan"]["safety_excluded"], 1)
        self.assertEqual(trace["scan"]["scan_limit_files"], ["b.py"])

    def test_symlink_parent_and_explicit_output_directory_are_excluded(self):
        self.write("safe.py", "def safe():\n    return 'expired token'\n")
        self.write("output/debug.py", "def debug():\n    return 'expired token'\n")
        self.write(".ssh/key_without_suffix", "expired token\n")
        with tempfile.TemporaryDirectory() as external:
            external_path = Path(external)
            (external_path / "outside.py").write_text("expired token\n", encoding="utf-8")
            (self.repo / "linked_directory").symlink_to(external_path, target_is_directory=True)
            result = self.collect(expand=False, excluded_dirs=[self.repo / "output"])
        self.assertEqual({c["path"] for c in result["candidates"]}, {"safe.py"})

    def test_potential_reference_has_no_call_graph_claim(self):
        self.write("app.py", "def refresh():\n    return 'expired token'\n")
        self.write("documentation.md", "The refresh function has useful behavior.\n")
        result = self.collect()
        reference = next(c for c in result["candidates"] if c["path"] == "documentation.md")
        self.assertEqual(reference["relations"][0]["type"], "potential_text_reference")
        self.assertEqual(reference["relations"][0]["certainty"], "potential")

    def test_configuration_literal_is_a_bounded_relation(self):
        self.write("app.py", "def refresh():\n    # expired token\n    return 'settings.toml'\n")
        self.write("settings.toml", "retries = 3\n")
        result = self.collect()
        config = next(c for c in result["candidates"] if c["path"] == "settings.toml")
        self.assertEqual(config["relations"][0]["type"], "configuration_literal")
        self.assertEqual(config["relations"][0]["source_line"], 3)

    def test_ast_disabled_baseline_does_not_parse_broken_python(self):
        self.write("broken.py", "def broken(:\n    # expired token\n")
        result = self.collect(expand=False, ast_enabled=False)
        self.assertEqual(result["candidates"][0]["extraction_mode"], "text_fallback")
        self.assertFalse(any("syntax" in d for d in result["diagnostics"]))

    def test_explicit_directory_uses_only_discovered_safe_files(self):
        self.write("package/first.py", "def entry():\n    return 1\n")
        self.write("package/second.py", "def other():\n    return 2\n")
        self.write("package/.env", "expired token\n")
        self.write("elsewhere.py", "def elsewhere():\n    return 3\n")
        result = self.collect(terms=["zzmissing"], task="zzmissing", paths=["package"], expand=False)
        self.assertEqual({c["path"] for c in result["candidates"]}, {"package/first.py", "package/second.py"})
        self.assertTrue(all(c["pinned"] for c in result["candidates"]))

    def test_literal_error_whitespace_and_multiline_diagnostic(self):
        self.write("client.py", "def send():\n    return 'error  code\\tFAIL'\n")
        result = self.collect(terms=["error  code"], task="zzmissing", expand=False)
        self.assertEqual(result["candidates"][0]["symbol"], "send")
        self.assertIn("error  code", result["candidates"][0]["retrieval_signals"][0])
        result = self.collect(terms=["error\ncode"], task="zzmissing", expand=False)
        self.assertEqual(result["candidates"], [])
        self.assertTrue(any("multiline" in d for d in result["diagnostics"]))

    def test_linked_ignore_metadata_is_rejected_before_rg_loads_it(self):
        self.write("safe.py", "def safe():\n    return 'expired token'\n")
        with tempfile.TemporaryDirectory() as external:
            outside = Path(external)
            (outside / "rules").write_text("safe.py\n", encoding="utf-8")
            for name in [".ignore", ".gitignore", ".rgignore"]:
                with self.subTest(name=name):
                    link = self.repo / name
                    link.symlink_to(outside / "rules")
                    result = self.collect(expand=False)
                    self.assertEqual(result["status"], "degraded")
                    self.assertTrue(any("ignore" in d and "symlink" in d for d in result["diagnostics"]))
                    link.unlink()
            (outside / "git" / "info").mkdir(parents=True)
            (outside / "git" / "info" / "exclude").write_text("safe.py\n", encoding="utf-8")
            (self.repo / ".git").symlink_to(outside / "git", target_is_directory=True)
            result = self.collect(expand=False)
            self.assertEqual(result["status"], "degraded")

    def test_normal_repo_exclude_is_respected_without_parent_ignore_loading(self):
        self.write(".git/info/exclude", "excluded.py\n")
        self.write("excluded.py", "def secret_noise():\n    return 'expired token'\n")
        self.write("safe.py", "def safe():\n    return 'expired token'\n")
        result = self.collect(expand=False)
        self.assertEqual({c["path"] for c in result["candidates"]}, {"safe.py"})

    def test_excluded_directory_ignore_link_is_never_opened(self):
        import os
        import subprocess
        from unittest.mock import patch
        self.write("safe.py", "def safe():\n    return 'expired token'\n")
        (self.repo / "node_modules").mkdir()
        with tempfile.TemporaryDirectory() as external:
            fifo = Path(external) / "outside-ignore"
            os.mkfifo(fifo)
            (self.repo / "node_modules/.ignore").symlink_to(fifo)
            original = subprocess.run
            def limited_run(*args, **kwargs):
                kwargs["timeout"] = min(kwargs.get("timeout", 1), 0.3)
                return original(*args, **kwargs)
            with patch.object(subprocess, "run", side_effect=limited_run):
                result = self.collect(expand=False)
            self.assertEqual({c["path"] for c in result["candidates"]}, {"safe.py"})

    def test_ripgrep_config_cannot_execute_repository_preprocessor(self):
        self.write("app.py", "def refresh():\n    return 'expired token'\n")
        self.write("preprocessor.py", "#!/usr/bin/env python3\nfrom pathlib import Path\nPath('EXECUTED_MARKER').write_text('ran')\nprint('expired token')\n")
        (self.repo / "preprocessor.py").chmod(0o755)
        self.write("rg-config", "--pre\n./preprocessor.py\n")
        with patch.dict(os.environ, {"RIPGREP_CONFIG_PATH": str(self.repo / "rg-config")}):
            result = self.collect(expand=False)
        self.assertTrue(result["candidates"])
        self.assertFalse((self.repo / "EXECUTED_MARKER").exists())

    def test_parenthesized_decorator_keeps_opening_at_line(self):
        self.write("decorated.py", "def identity(fn):\n    return fn\n\n@(\n    identity\n)\ndef refresh():\n    return 'expired token'\n")
        candidate = self.collect(expand=False)["candidates"][0]
        self.assertEqual(candidate["start_line"], 4)
        self.assertTrue(candidate["excerpt"].startswith("@("))
        self.assertFalse(candidate["incomplete"])

    def test_import_in_unrelated_function_is_not_syntactic_dependency(self):
        self.write("app.py", "def unrelated():\n    from dependency import renew\n    return renew()\n\ndef refresh():\n    # expired token\n    return renew('old')\n")
        self.write("dependency.py", "def renew(token):\n    return token\n")
        result = self.collect()
        imported = [r for c in result["candidates"] if c["path"] == "dependency.py" for r in c["relations"] if r["type"] == "local_import"]
        self.assertEqual(imported, [])


if __name__ == "__main__":
    unittest.main()
