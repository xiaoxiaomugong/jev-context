import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from jev_context import retrieval, pipeline, source_snapshot
from jev_context.safe_io import read_source_bytes
from jev_context.pipeline import run


class SourceSnapshotPipelineTests(unittest.TestCase):
    def test_pipeline_source_io_includes_capture_and_both_fingerprint_reads(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            content = b'def retry():\n    return "retry"\n'
            (repo / 'app.py').write_bytes(content)
            capture_reads, fingerprint_reads = [], []

            def capture(*args, **kwargs):
                raw = read_source_bytes(*args, **kwargs)
                capture_reads.append(len(raw))
                return raw

            def fingerprint(*args, **kwargs):
                raw = read_source_bytes(*args, **kwargs)
                fingerprint_reads.append(len(raw))
                return raw

            with patch.object(source_snapshot, 'read_source_bytes', capture), \
                    patch.object(pipeline, 'read_source_bytes', fingerprint):
                result = run(repo, 'retry', ['retry'], expand=False)
            metrics = result['metrics']
            self.assertEqual(metrics['source_read_bytes'], sum(capture_reads + fingerprint_reads))
            self.assertEqual(metrics['source_capture_read_bytes'], sum(capture_reads))
            self.assertEqual(metrics['source_fingerprint_read_bytes'], sum(fingerprint_reads))
            self.assertEqual(metrics['source_capture_bytes'], len(content))
            self.assertEqual(metrics['snapshot_written_bytes'], len(content))

    def test_source_changed_between_hits_and_parse_is_rejected_before_reporting(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            source = repo / "app.py"
            source.write_text("def retry():\n    return 'retry'\n", encoding="utf-8")
            original = retrieval._rg_hits

            def change_after_search(*args, **kwargs):
                hits = original(*args, **kwargs)
                source.write_text("def unrelated():\n    return 1\n", encoding="utf-8")
                return hits

            with patch.object(retrieval, "_rg_hits", change_after_search):
                with self.assertRaisesRegex(ValueError, "source changed"):
                    run(repo, "retry", ["retry"], expand=False, local_only=True)


if __name__ == "__main__":
    unittest.main()
