"""Bounded POSIX source reads anchored to directory descriptors."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import stat
import tempfile


def _temporary_candidates():
    return [tempfile.tempdir, *(os.environ.get(name) for name in ('TMPDIR', 'TEMP', 'TMP')),
            '/tmp', '/var/tmp', '/usr/tmp']


def temporary_root(repo: Path) -> Path:
    """Choose external scratch storage without gettempdir's write probes."""
    target = Path(repo).resolve()
    for value in _temporary_candidates():
        if value is None:
            continue
        try:
            path = Path(os.fsdecode(value)).resolve()
            if (not path.is_relative_to(target) and path.is_dir()
                    and os.access(path, os.W_OK | os.X_OK)):
                return path
        except (OSError, ValueError, TypeError, RuntimeError):
            continue
    raise ValueError('temporary storage outside the read-only target is unavailable')


def tokenizer_cache_is_external(repo: Path) -> bool:
    """Check the optional library's actual cache configuration before loading it."""
    target = Path(repo).resolve()
    try:
        for name in ('TIKTOKEN_CACHE_DIR', 'DATA_GYM_CACHE_DIR'):
            if name in os.environ:
                value = os.environ[name]
                return not value or not Path(value).resolve().is_relative_to(target)
        # An established cached default causes no write probes. Without one,
        # require an explicit cache rather than letting the library probe cwd
        # after failed directory attempts (e.g. sandbox permission failures).
        if tempfile.tempdir is None:
            return False
        path = Path(os.fsdecode(tempfile.tempdir)).resolve()
        return (not path.is_relative_to(target)
                and not (path / 'data-gym-cache').resolve().is_relative_to(target))
    except (OSError, ValueError, TypeError, RuntimeError):
        return False


class SourceReadError(ValueError):
    """Bounded read rejection with local I/O accounting and no source bytes."""

    def __init__(self, message: str, *, bytes_read: int = 0, lookahead_bytes: int = 0):
        super().__init__(message)
        self.bytes_read = bytes_read
        self.lookahead_bytes = lookahead_bytes


class SourceReadLimitError(SourceReadError):
    """Size rejection, distinguished from a failed read for scan diagnostics."""


def _safe_path(repo, relative, excluded):
    # Lazy import lets retrieval reuse this reader without an import cycle.
    from .retrieval import _safe_path as check
    return check(repo, relative, list(excluded))


def open_directory_nofollow(path: Path) -> int:
    """Open an absolute directory, refusing symlinks in every component."""
    if (os.name != "posix" or not hasattr(os, "O_NOFOLLOW")
            or not hasattr(os, "O_DIRECTORY") or os.open not in os.supports_dir_fd):
        raise ValueError("safe POSIX file opening is unavailable")
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("safe absolute directory is required")
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError:
        os.close(descriptor)
        raise ValueError("safe directory is unavailable") from None


def read_source_bytes(repo: Path, relative: str, excluded=(), *, max_bytes: int | None = None) -> bytes:
    """Read a safe regular source file without following swapped symlinks.

    Callers also enforce discovery/ignore rules before asking for a read.
    This function never imports or executes anything from the target repo.
    max_bytes can narrow the usual per-file bound for a remaining scan budget.
    """
    from .retrieval import MAX_FILE_BYTES
    if max_bytes is not None and (not isinstance(max_bytes, int) or max_bytes < 0):
        raise ValueError("source read byte limit must be a nonnegative integer")
    limit = MAX_FILE_BYTES if max_bytes is None else min(MAX_FILE_BYTES, max_bytes)
    repo = Path(repo)
    path = PurePosixPath(relative)
    if (not relative or path.is_absolute() or ".." in path.parts or not path.parts
            or _safe_path(repo, relative, excluded) is None):
        raise ValueError("requested source file is unavailable or excluded")
    descriptor = None
    consumed = 0
    try:
        descriptor = open_directory_nofollow(repo)
        for part in path.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        source = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=descriptor)
        # Avoid buffered read-ahead beyond a narrowed remaining-byte budget.
        with os.fdopen(source, "rb", buffering=0) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("requested source file is not a bounded regular file")
            if info.st_size > limit:
                raise SourceReadLimitError("requested source file exceeds the size limit")
            chunks, consumed = [], 0
            while consumed < limit + 1:
                chunk = stream.read(limit + 1 - consumed)
                if not chunk:
                    break
                chunks.append(chunk)
                consumed += len(chunk)
            raw = b''.join(chunks)
            if len(raw) > limit:
                raise SourceReadLimitError("requested source file exceeds the size limit",
                                          bytes_read=len(raw), lookahead_bytes=1)
        return raw
    except OSError:
        raise SourceReadError("requested source file could not be safely read", bytes_read=consumed) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
