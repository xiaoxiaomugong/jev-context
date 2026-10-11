"""Explicit, bounded reads with optional content-free interval deduplication."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat

from .retrieval import _discover, _safe_path, source_lines
from .safe_io import open_directory_nofollow, read_source_bytes

MAX_READ_LINES = 300
MAX_METADATA_BYTES = 4 * 1024 * 1024


class ReaderFailure(ValueError):
    """A fixed public diagnostic, containing no repository body or raw error."""


def _relative(value):
    if (not isinstance(value, str) or not value or "\\" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ReaderFailure("path must be a safe repository-relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ReaderFailure("path must be a safe repository-relative path")
    return path.as_posix()


def _range(start, end):
    if type(start) is not int or type(end) is not int or not 1 <= start <= end:
        raise ReaderFailure("requested inclusive line range is invalid")
    return start, end


def _merge(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def _subtract(intervals, seen):
    result = []
    for start, end in intervals:
        cursor = start
        for old_start, old_end in seen:
            if old_end < cursor:
                continue
            if old_start > end:
                break
            if cursor < old_start:
                result.append([cursor, old_start - 1])
            cursor = max(cursor, old_end + 1)
            if cursor > end:
                break
        if cursor <= end:
            result.append([cursor, end])
    return result


def _metadata_path(value, repo=None):
    path = Path(value).absolute()
    for alias, canonical in ((Path('/tmp'), Path('/private/tmp')), (Path('/var'), Path('/private/var'))):
        if path.is_relative_to(alias) and alias.resolve() == canonical:
            path = canonical / path.relative_to(alias)
    if (".." in path.parts or path.is_symlink()
            or (repo is not None and path.is_relative_to(repo))):
        raise ReaderFailure("state must be outside the repository without symlink paths")
    descriptor = open_directory_nofollow(path.parent)
    os.close(descriptor)
    return path


def _load_json(path, missing_ok=False):
    parent = open_directory_nofollow(path.parent)
    try:
        try:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=parent)
        except FileNotFoundError:
            if missing_ok:
                return None
            raise ReaderFailure("requested metadata file is unavailable") from None
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_METADATA_BYTES:
                raise ReaderFailure("metadata must be a bounded regular JSON file")
            raw = stream.read(MAX_METADATA_BYTES + 1)
            if len(raw) > MAX_METADATA_BYTES:
                raise ReaderFailure("metadata exceeds the size limit")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            raise ReaderFailure("metadata must be valid UTF-8 JSON") from None
        if not isinstance(value, dict):
            raise ReaderFailure("metadata must be a JSON object")
        return value
    except OSError:
        raise ReaderFailure("metadata could not be safely opened") from None
    finally:
        os.close(parent)


def _hash(value):
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ReaderFailure("metadata contains an invalid SHA256 fingerprint")
    return value


def _state(path, repo):
    value = _load_json(path, missing_ok=True)
    if value is None:
        return dict(repo=str(repo), files={})
    if value.get("repo") != str(repo) or not isinstance(value.get("files"), dict):
        raise ReaderFailure("state repository does not match or state is invalid")
    files = {}
    for relative, item in value["files"].items():
        name = _relative(relative)
        if (name != relative or not isinstance(item, dict)
                or not isinstance(item.get("ranges"), list)):
            raise ReaderFailure("state contains invalid source intervals")
        intervals = []
        for pair in item["ranges"]:
            if not isinstance(pair, list) or len(pair) != 2:
                raise ReaderFailure("state contains invalid source intervals")
            intervals.append(_range(*pair))
        files[name] = dict(sha256=_hash(item.get("sha256")), ranges=_merge(intervals))
    # Rebuild only the allowed fields: no content, credentials, or extra metadata.
    return dict(repo=str(repo), files=files)


def _plan(path, repo):
    value = _load_json(path)
    repo_value = value.get("repo")
    if (not isinstance(repo_value, str) or not Path(repo_value).is_absolute()
            or Path(repo_value).resolve() != repo
            or not isinstance(value.get("source_hashes"), dict)
            or not isinstance(value.get("reading_list"), list)):
        raise ReaderFailure("plan repository does not match or plan is invalid")
    return value["source_hashes"]


def _save_state(path, value):
    encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_METADATA_BYTES:
        raise ReaderFailure("state exceeds the metadata size limit")
    parent = open_directory_nofollow(path.parent)
    temporary = ".read-context-" + secrets.token_hex(12) + ".tmp"
    try:
        try:
            old = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(old.st_mode):
                raise ReaderFailure("state destination must be a regular file")
        except FileNotFoundError:
            pass
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
    except OSError:
        raise ReaderFailure("state could not be atomically saved") from None
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def read_ranges(repo, ranges, *, state=None, repeat=False, plan=None):
    """Validate the whole request, persist metadata, then return exact code text."""
    try:
        root = Path(repo).resolve(strict=True)
        if not root.is_dir():
            raise ReaderFailure("repository directory is unavailable")
        requested = {}
        for relative, start, end in ranges:
            name = _relative(relative)
            requested.setdefault(name, []).append(_range(start, end))
        if not requested:
            raise ReaderFailure("explicit --range PATH START END is required")
        requested = {path: _merge(intervals) for path, intervals in requested.items()}
        if sum(end - start + 1 for intervals in requested.values()
               for start, end in intervals) > MAX_READ_LINES:
            raise ReaderFailure("request exceeds the 300-line limit; use smaller explicit ranges")
        state_path = _metadata_path(state, root) if state is not None else None
        saved = _state(state_path, root) if state_path is not None else dict(repo=str(root), files={})
        plan_path = _metadata_path(plan) if plan is not None else None
        fingerprints = _plan(plan_path, root) if plan_path is not None else {}
        diagnostics = []
        discovered = set(_discover(root, [], diagnostics))
        if any("unavailable" in item or "failed" in item or "timed out" in item
               or "discovery output limit" in item for item in diagnostics):
            raise ReaderFailure("safe file discovery is unavailable")
        sources, hashes = {}, {}
        for relative, intervals in requested.items():
            if relative not in discovered or _safe_path(root, relative, []) is None:
                raise ReaderFailure("requested file is unavailable, ignored, or outside scan safety limits")
            raw = read_source_bytes(root, relative)
            if b"\0" in raw:
                raise ReaderFailure("binary source content is unavailable")
            try:
                sources[relative] = source_lines(raw.decode("utf-8"))
            except UnicodeError:
                raise ReaderFailure("source must be valid UTF-8 text") from None
            if any(end > len(sources[relative]) for _, end in intervals):
                raise ReaderFailure("requested inclusive line range is unavailable")
            fingerprint = hashes[relative] = hashlib.sha256(raw).hexdigest()
            previous = saved["files"].get(relative)
            if previous is not None and previous["sha256"] != fingerprint:
                raise ReaderFailure("source changed since prior read; retrieve again and use a new state")
            if relative in fingerprints and _hash(fingerprints[relative]) != fingerprint:
                raise ReaderFailure("source changed since the plan; retrieve again and save a new plan")
        output = []
        for relative, intervals in requested.items():
            previous = saved["files"].get(relative, {}).get("ranges", [])
            delivery = intervals if repeat else _subtract(intervals, previous)
            for start, end in delivery:
                output.append(f"{relative}:{start}-{end}\n")
                for number in range(start, end + 1):
                    line = sources[relative][number - 1]
                    output.append(f"{number}:" + line + ("" if line.endswith("\n") else "\n"))
            saved["files"][relative] = dict(sha256=hashes[relative],
                                            ranges=_merge([*previous, *intervals]))
        if state_path is not None:
            _save_state(state_path, saved)
        return "".join(output)
    except ReaderFailure:
        raise
    except (OSError, ValueError, RuntimeError, TypeError):
        raise ReaderFailure("safe read is unavailable; check repository, paths, and local tool availability") from None
