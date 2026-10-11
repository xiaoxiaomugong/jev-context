"""Private byte snapshots for bounded searches of safely opened sources."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import tempfile

from .safe_io import SourceReadError, SourceReadLimitError, read_source_bytes, temporary_root


@dataclass
class SourceSnapshot:
    root: Path
    sources: dict[str, bytes]
    io_metrics: dict[str, int | bool]
    safety_excluded: int
    scan_limit_files: list[str]


@contextmanager
def capture_sources(repo: Path, files: list[str], excluded, diagnostics: list[str],
                    *, max_files: int, max_total_bytes: int, max_file_bytes: int):
    """Capture once; callers search and parse these exact bytes until cleanup.

    Discovery supplies the ignore-filtered paths. Descriptor-anchored reads
    reject source symlink swaps, then actual byte lengths enforce scan bounds.
    """
    sources: dict[str, bytes] = {}
    metrics = dict(source_read_bytes=0, source_read_lookahead_bytes=0,
                   source_capture_bytes=0, snapshot_written_bytes=0,
                   source_captured_files=0, source_skipped_files=0,
                   source_capture_limit_reached=False)
    safety_excluded = 0
    scan_limit_files = []
    with tempfile.TemporaryDirectory(prefix="jev-context-source-", dir=temporary_root(repo)) as temporary:
        root = Path(temporary)
        for index, relative in enumerate(files):
            if len(sources) >= max_files or metrics["source_read_bytes"] >= max_total_bytes:
                scan_limit_files.extend(files[index:])
                break
            try:
                remaining = max_total_bytes - metrics["source_read_bytes"]
                raw = read_source_bytes(repo, relative, excluded,
                                        max_bytes=min(max_file_bytes, remaining))
            except SourceReadLimitError as error:
                metrics["source_read_bytes"] += error.bytes_read
                metrics["source_read_lookahead_bytes"] += error.lookahead_bytes
                scan_limit_files.append(relative)
                continue
            except SourceReadError as error:
                metrics['source_read_bytes'] += error.bytes_read
                metrics['source_read_lookahead_bytes'] += error.lookahead_bytes
                safety_excluded += 1
                continue
            except (OSError, ValueError):
                safety_excluded += 1
                continue
            metrics["source_read_bytes"] += len(raw)
            if (len(raw) > max_file_bytes
                    or metrics["snapshot_written_bytes"] + len(raw) > max_total_bytes):
                scan_limit_files.append(relative)
                continue
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(raw)
            sources[relative] = raw
            metrics["source_capture_bytes"] += len(raw)
            metrics["snapshot_written_bytes"] += len(raw)
            metrics["source_captured_files"] += 1
        if safety_excluded:
            diagnostics.append(f"source snapshot capture failed for {safety_excluded} discovered files; retrieval unavailable for those files")
        if scan_limit_files:
            diagnostics.append(f"source snapshot capture limit reached for {len(scan_limit_files)} discovered files; retrieval unavailable for those files")
        metrics["source_skipped_files"] = safety_excluded + len(scan_limit_files)
        metrics["source_capture_limit_reached"] = bool(scan_limit_files)
        yield SourceSnapshot(root, sources, metrics, safety_excluded, scan_limit_files)
