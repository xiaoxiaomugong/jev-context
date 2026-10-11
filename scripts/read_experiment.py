#!/usr/bin/env python3
"""Supply and log experiment text without executing repository code.

RUN/config.json contains repo (absolute), task_id, method, and initial_ranges.
RUN/events.jsonl stores each exact stdout delivery, including failed requests.

  python3 scripts/read_experiment.py --run-dir RUN read entry.py 1 20
  python3 scripts/read_experiment.py --run-dir RUN search retry --path worker
  python3 scripts/read_experiment.py --run-dir RUN summary

Summary describes preceding events; its own delivery is appended afterward.
Counts use UTF-8 bytes/3, not provider billing or hidden tool envelopes.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile

from jev_context.retrieval import _discover, _rg_options, _safe_path, source_lines
from jev_context.tokens import VisibilityLedger


class ReaderFailure(ValueError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ReaderFailure("invalid command arguments")


def _directory(value):
    directory = Path(value).absolute()
    # These are trusted macOS aliases, also used by tempfile.
    for alias, target in ((Path("/tmp"), Path("/private/tmp")),
                          (Path("/var"), Path("/private/var"))):
        if directory.is_relative_to(alias) and alias.resolve() == target:
            directory = target / directory.relative_to(alias)
    if any(path.is_symlink() for path in (directory, *directory.parents)):
        raise ReaderFailure("directory traverses a symlink")
    return directory


def _relative(value):
    if (not isinstance(value, str) or not value or "\\" in value
            or any(char in value for char in ("\0", "\n", "\r"))):
        raise ReaderFailure("path must be repository-relative")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ReaderFailure("path must be repository-relative")
    return path.as_posix()


def _config(run_dir):
    path = run_dir / "config.json"
    if path.is_symlink():
        raise ReaderFailure("config symlink is unavailable")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(value, dict) or not isinstance(value.get("repo"), str)
            or not Path(value["repo"]).is_absolute()
            or not isinstance(value.get("task_id"), str)
            or value.get("method") not in ("B", "C")
            or not isinstance(value.get("initial_ranges"), list)):
        raise ReaderFailure("invalid local experiment config")
    repo = _directory(value["repo"])
    if not repo.is_dir():
        raise ReaderFailure("repository directory unavailable")
    ranges = set()
    for item in value["initial_ranges"]:
        if (not isinstance(item, dict) or type(item.get("start_line")) is not int
                or type(item.get("end_line")) is not int
                or not 1 <= item["start_line"] <= item["end_line"]):
            raise ReaderFailure("invalid initial range config")
        ranges.add((_relative(item.get("path")), item["start_line"], item["end_line"]))
    return repo, ranges


def _ledger(run_dir):
    ledger = VisibilityLedger()
    path = run_dir / "events.jsonl"
    if path.is_symlink():
        raise ReaderFailure("event log symlink is unavailable")
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            ledger.record(item["kind"], item["text"], path=item.get("path"),
                          start_line=item.get("start_line"), source_lines=item.get("source_lines"),
                          source_ranges=item.get("source_ranges"))
    return ledger


def _emit(run_dir, ledger, kind, text, **source):
    event = ledger.record(kind, text, **source)
    path = run_dir / "events.jsonl"
    if path.is_symlink():
        raise ReaderFailure("event log symlink is unavailable")
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.write(text)


def _read(repo, ranges, ledger, path, start, end, run_dir):
    relative = _relative(path)
    diagnostics = []
    files = _discover(repo, [run_dir], diagnostics)
    safe = _safe_path(repo, relative, [run_dir]) if relative in files else None
    if safe is None:
        raise ReaderFailure("requested file is unavailable or excluded by safety rules")
    raw = safe.read_bytes()
    if b"\0" in raw:
        raise ReaderFailure("binary content is unavailable")
    lines = source_lines(raw.decode("utf-8"))
    if not 1 <= start <= end <= len(lines):
        raise ReaderFailure("requested inclusive line range is unavailable")
    key = (relative, start, end)
    delivered = {(item.get("path"), item.get("start_line"), item.get("end_line"))
                 for item in ledger.events if "source_lines" in item}
    initial = key in ranges and key not in delivered
    if not initial and end - start + 1 > 250:
        raise ReaderFailure("supplemental read exceeds the 250-line experiment limit")
    source = lines[start - 1:end]
    text = "".join(f"{relative}:{number}:" + line.rstrip("\r\n") + "\n"
                   for number, line in enumerate(source, start))
    kind = "initial_code" if initial else "supplemental_code"
    return kind, text, dict(path=relative, start_line=start, source_lines=source)


def _search(repo, term, path, run_dir):
    if not term.strip() or len(term) > 200 or "\n" in term or "\r" in term or "\0" in term:
        raise ReaderFailure("search needs a nonempty single-line term of at most 200 characters")
    diagnostics = []
    files = _discover(repo, [run_dir], diagnostics)
    if any("unavailable" in item or "failed" in item or "timed out" in item for item in diagnostics):
        raise ReaderFailure("safe file discovery unavailable")
    if path is not None:
        relative = _relative(path)
        files = [item for item in files if item == relative or item.startswith(relative.rstrip("/") + "/")]
        if not files:
            raise ReaderFailure("search path is unavailable or excluded")
    hits, source_ranges = [], []
    for offset in range(0, len(files), 150):
        command = ["rg", *_rg_options(repo), "--json", "--fixed-strings", "--ignore-case",
                   "--max-count", "31", "--max-columns", "4000", "--max-columns-preview",
                   "--file", "-", "--", *files[offset:offset + 150]]
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            result = subprocess.run(command, cwd=repo, input=(term + "\n").encode("utf-8"),
                                    stdout=output, stderr=errors, timeout=15, check=False)
            if result.returncode not in (0, 1):
                raise ReaderFailure("bounded local search failed")
            output.seek(0)
            consumed = 0
            for raw in output:
                consumed += len(raw)
                if consumed > 8 * 1024 * 1024:
                    raise ReaderFailure("bounded search output exceeded its limit")
                row = json.loads(raw)
                if row.get("type") != "match":
                    continue
                data = row["data"]
                relative = data["path"].get("text")
                if relative not in files or not isinstance(data.get("line_number"), int):
                    continue
                # At most one bounded source line is supplied for each rg hit.
                raw_source = data["lines"].get("text", "")
                line = raw_source.rstrip("\r\n")
                hits.append(f"{relative}:{data['line_number']}:{line}\n")
                source_ranges.append(dict(path=relative, start_line=data["line_number"],
                                          source_lines=source_lines(raw_source)))
                if len(hits) > 30:
                    break
        if len(hits) > 30:
            break
    text = "".join(hits[:30]) or "No matches in safely discovered files.\n"
    if len(hits) > 30:
        text += "Search bounded to 30 hits; refine the term or path.\n"
    if diagnostics:
        text += "Discovery was bounded or excluded files by safety rules.\n"
    return ("search_output" if hits else "search_failure"), text, dict(source_ranges=source_ranges[:30])


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    run_dir = None
    ledger = VisibilityLedger()
    action = next((item for item in argv if item in ("read", "search", "summary")), "helper")
    try:
        if "--run-dir" in argv:
            run_dir = _directory(argv[argv.index("--run-dir") + 1])
            run_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(run_dir, 0o700)
        parser = Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.add_argument("--run-dir", required=True)
        commands = parser.add_subparsers(dest="action", required=True)
        reader = commands.add_parser("read")
        reader.add_argument("path")
        reader.add_argument("start", type=int)
        reader.add_argument("end", type=int)
        search = commands.add_parser("search")
        search.add_argument("term")
        search.add_argument("--path")
        commands.add_parser("summary")
        args = parser.parse_args(argv)
        ledger = _ledger(run_dir)
        if args.action == "summary":
            kind, text, source = "summary_output", json.dumps(ledger.summary(), ensure_ascii=False, indent=2) + "\n", {}
        else:
            repo, ranges = _config(run_dir)
            if args.action == "read":
                kind, text, source = _read(repo, ranges, ledger, args.path, args.start, args.end, run_dir)
            else:
                kind, text, source = _search(repo, args.term, args.path, run_dir)
        _emit(run_dir, ledger, kind, text, **source)
        return 0
    except (OSError, ValueError, KeyError, TypeError, IndexError, subprocess.SubprocessError) as error:
        detail = str(error) if isinstance(error, ReaderFailure) else "local request unavailable"
        text = f"failure: {detail}.\n"
        if run_dir is not None:
            try:
                _emit(run_dir, VisibilityLedger(), action + "_failure", text)
                return 1
            except (OSError, ValueError, TypeError):
                text += "failure: event log unavailable; delivery could not be recorded.\n"
        sys.stdout.write(text)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
