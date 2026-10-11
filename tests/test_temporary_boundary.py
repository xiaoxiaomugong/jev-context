"""Audit writes during retrieval; final clean files alone are insufficient."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from jev_context.pipeline import run
from jev_context.tokens import TokenCounter


class TemporaryBoundaryTests(unittest.TestCase):
    def test_environment_and_cached_temp_roots_never_write_inside_target(self):
        observer = '''
import json, os, runpy, sys, tempfile
from pathlib import Path
script, repo, report, cached = sys.argv[1:]
repo = Path(repo).resolve()
tempfile.tempdir = str(repo) if cached == 'yes' else None
writes = []
def audit(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).resolve()
        if path.is_relative_to(repo) and args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            writes.append(str(path))
    elif event in ('tempfile.mkdtemp', 'tempfile.mkstemp'):
        path = Path(args[0]).resolve()
        if path.is_relative_to(repo):
            writes.append(str(path))
sys.addaudithook(audit)
sys.path.insert(0, str(Path(script).parent))
sys.argv = [script, '--repo', str(repo), '--task', 'retry', '--term', 'retry']
try:
    runpy.run_path(script, run_name='__main__')
finally:
    Path(report).write_text(json.dumps(writes))
'''
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            repo = base / 'repo'
            repo.mkdir()
            source = repo / 'app.py'
            original = b'def retry():\n    return "retry"\n'
            source.write_bytes(original)
            for cached in ('no', 'yes'):
                with self.subTest(cached=cached):
                    report = base / ('audit-' + cached + '.json')
                    result = subprocess.run([
                        sys.executable, '-c', observer, str(ROOT / 'scripts/jev-context.py'),
                        str(repo), str(report), cached,
                    ], env={**os.environ, 'TMPDIR': str(repo), 'PYTHONDONTWRITEBYTECODE': '1'},
                        capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(report.read_text()), [], 'temporary writes entered the target')
                    self.assertEqual(source.read_bytes(), original)
                    self.assertEqual(sorted(p.name for p in repo.iterdir()), ['app.py'])

    def test_tokenizer_cache_inside_target_falls_back_before_loading_encoding(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary).resolve()
            (repo / 'app.py').write_text('def retry():\n    return "retry"\n')
            marker = repo / 'unexpected-cache-write'

            def encoding(_name):
                marker.write_text('wrong')
                raise OSError('cache is inside target')

            with patch.dict(sys.modules, {'tiktoken': SimpleNamespace(get_encoding=encoding)}), \
                    patch.dict(os.environ, {'TIKTOKEN_CACHE_DIR': str(repo)}):
                result = run(repo, 'retry', ['retry'], tokenizer=True, expand=False)
            self.assertFalse(marker.exists())
            self.assertEqual(result['token_counting'], 'utf8_bytes_div_3_estimate')
            self.assertTrue(any('tokenizer' in item and 'cache' in item for item in result['diagnostics']))

    def test_default_tokenizer_cache_does_not_probe_a_target_local_temp_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary).resolve()
            fake = SimpleNamespace(get_encoding=lambda _: self.fail('unsafe default cache was loaded'))
            with patch.dict(sys.modules, {'tiktoken': fake}), \
                    patch.dict(os.environ, {'TMPDIR': str(repo)}, clear=True), \
                    patch.object(tempfile, 'tempdir', None):
                counter = TokenCounter(True, readonly_repo=repo)
            self.assertEqual(counter.method, 'utf8_bytes_div_3_estimate')
            self.assertIn('cache', counter.diagnostic)


if __name__ == '__main__':
    unittest.main()
