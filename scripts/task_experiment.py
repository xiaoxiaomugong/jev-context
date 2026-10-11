#!/usr/bin/env python3
"""Public, local paired-task reader with exact visible-text accounting.

Create RUN/config.json with {repo: absolute_path, task_id: string,
method: baseline|local|jev}. RUN must be outside the target and have no symlinks.
All methods get files/search/read; local and explicit jev get advice. Jev also
requires an existing absolute submission_ledger outside the target. Only its
first advice may submit a candidate batch; later advice is local. No target code runs.

  python3 scripts/task_experiment.py --run-dir RUN files --glob '*.py'
  python3 scripts/task_experiment.py --run-dir RUN search 'retry|timeout'
  python3 scripts/task_experiment.py --run-dir RUN read worker.py 1 80
  python3 scripts/task_experiment.py --run-dir RUN advice --task 'trace retry'
  python3 scripts/task_experiment.py --run-dir RUN summary

Summary describes preceding events and is then itself logged. Counts are local
UTF-8 byte estimates; API usage and hidden/tool-envelope costs remain unknown.
"""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import fcntl
import io
import json
import math
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import tempfile
import time

from jev_context import pipeline
from jev_context.output import render
from jev_context.reader import ReaderFailure, _load_json, _relative, _save_state, read_ranges
from jev_context.retrieval import _discover, _rg_options, source_lines
from jev_context.safe_io import open_directory_nofollow, read_source_bytes, temporary_root
from jev_context.tokens import VisibilityLedger
import evaluation_jev

MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_RG_BYTES = 8 * 1024 * 1024
MAX_LOG_BYTES = 128 * 1024 * 1024


class Failure(ValueError):
    """Only fixed, source-free diagnostics are allowed in failure deliveries."""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Failure("invalid command arguments; check command and bounds")


def _directory(value):
    path = Path(value).absolute()
    for alias, canonical in ((Path('/tmp'), Path('/private/tmp')),
                             (Path('/var'), Path('/private/var'))):
        if path.is_relative_to(alias) and alias.resolve() == canonical:
            path = canonical / path.relative_to(alias)
    descriptor = open_directory_nofollow(path)
    os.close(descriptor)
    return path


def _config(directory):
    value = _load_json(directory / 'config.json')
    if not isinstance(value.get('repo'), str) or not Path(value['repo']).is_absolute():
        raise Failure('config requires an absolute repository directory')
    repo = _directory(value['repo'])
    if directory.is_relative_to(repo):
        raise Failure('run directory must be outside the read-only repository')
    if (not isinstance(value.get('task_id'), str) or not value['task_id'].strip()
            or len(value['task_id'].encode('utf-8')) > 1024
            or value.get('method') not in ('baseline', 'local', 'jev')):
        raise Failure('config requires task_id and method baseline, local, or jev')
    if value['method'] == 'jev':
        if not isinstance(value.get('submission_ledger'), str):
            raise Failure('jev config requires an absolute existing submission ledger')
        value['submission_ledger'] = str(evaluation_jev.validate_ledger(value['submission_ledger'], repo))
    return repo, value


def _metadata(directory):
    parent = open_directory_nofollow(directory)
    try:
        for name in ['config.json', 'state.json', 'scoring-batch.json', *sorted(p.name for p in directory.glob('plan-*.json'))]:
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode):
                raise Failure('run metadata must be regular files without symlinks')
    finally:
        os.close(parent)


def _event_stream(directory):
    parent = open_directory_nofollow(directory)
    try:
        descriptor = os.open('events.jsonl', os.O_RDWR | os.O_APPEND | os.O_CREAT |
                             os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
    finally:
        os.close(parent)
    stream = os.fdopen(descriptor, 'r+', encoding='utf-8', newline='')
    try:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_size > MAX_LOG_BYTES):
            raise Failure('event log must be a bounded regular file without hardlinks')
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        return stream
    except BaseException:
        stream.close()
        raise


def _ledger(stream):
    ledger = VisibilityLedger()
    stream.seek(0)
    elapsed = 0.0
    for line in stream:
        if len(line.encode('utf-8')) > 4 * MAX_TEXT_BYTES:
            raise Failure('event entry exceeds its size limit')
        item = json.loads(line)
        if (not isinstance(item, dict) or not isinstance(item.get('kind'), str)
                or not item['kind'] or len(item['kind'].encode('utf-8')) > 128
                or not isinstance(item.get('text'), str)):
            raise Failure('event log contains invalid fields')
        groups = item.get('source_ranges', [])
        if not isinstance(groups, list):
            raise Failure('event source ranges must be a list')
        groups = list(groups)
        if item.get('source_lines') is not None:
            groups.append(item)
        for group in groups:
            if (not isinstance(group, dict) or not isinstance(group.get('path'), str)
                    or type(group.get('start_line')) is not int or group['start_line'] < 1
                    or not isinstance(group.get('source_lines'), list)
                    or not all(isinstance(source, str) for source in group['source_lines'])):
                raise Failure('event source range contains invalid fields')
            _relative(group['path'])
        duration = item.get('elapsed_seconds', 0.0)
        if type(duration) not in (int, float):
            raise Failure('event elapsed_seconds is invalid')
        try:
            duration = float(duration)
        except OverflowError:
            raise Failure('event elapsed_seconds is invalid') from None
        if not math.isfinite(duration) or duration < 0:
            raise Failure('event elapsed_seconds is invalid')
        ledger.record(item['kind'], item['text'], path=item.get('path'),
                      start_line=item.get('start_line'), source_lines=item.get('source_lines'),
                      source_ranges=item.get('source_ranges'))
        elapsed += duration
        if not math.isfinite(elapsed):
            raise Failure('total event elapsed_seconds exceeds its finite range')
    return ledger, elapsed


def _emit(stream, ledger, kind, text, elapsed, *, source_ranges=None, io_metadata=None,
          request_argv=None, scoring_metadata=None):
    if len(text.encode('utf-8')) > MAX_TEXT_BYTES:
        raise Failure('delivery exceeds its bounded text limit; refine the request')
    try:
        event = ledger.record(kind, text, source_ranges=source_ranges or [])
    except ValueError:
        raise Failure('source changed or provenance is invalid; start a new run') from None
    event['elapsed_seconds'] = elapsed
    if io_metadata is not None:
        event['io_metadata'] = io_metadata
    if request_argv is not None:
        event['request_argv'] = request_argv
    if scoring_metadata:
        event['scoring_metadata'] = scoring_metadata
    encoded = json.dumps(event, ensure_ascii=False) + '\n'
    if len(encoded.encode('utf-8')) > 4 * MAX_TEXT_BYTES:
        raise Failure('encoded event size limit reached; request smaller explicit ranges')
    if os.fstat(stream.fileno()).st_size + len(encoded.encode('utf-8')) > MAX_LOG_BYTES:
        raise Failure('event log size limit reached; start a new run')
    stream.write(encoded)
    stream.flush()
    os.fsync(stream.fileno())
    sys.stdout.write(text)


def _limit(maximum):
    def parse(value):
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError('limit must be an integer') from None
        if not 1 <= number <= maximum:
            raise argparse.ArgumentTypeError('limit is outside permitted bounds')
        return number
    return parse


def _parser():
    parser = Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--run-dir', required=True)
    commands = parser.add_subparsers(dest='action', required=True, parser_class=Parser)
    files = commands.add_parser('files')
    files.add_argument('--glob', action='append', default=[])
    files.add_argument('--limit', type=_limit(5000), default=200)
    search = commands.add_parser('search')
    search.add_argument('pattern')
    search.add_argument('--path')
    search.add_argument('--glob', action='append', default=[])
    search.add_argument('--fixed', action='store_true')
    search.add_argument('--ignore-case', action='store_true')
    search.add_argument('--heading', action='store_true')
    search.add_argument('--limit', type=_limit(1000), default=200)
    read = commands.add_parser('read')
    read.add_argument('path')
    read.add_argument('start', type=int)
    read.add_argument('end', type=int)
    read.add_argument('--repeat', action='store_true')
    advice = commands.add_parser('advice')
    task = advice.add_mutually_exclusive_group(required=True)
    task.add_argument('--task')
    task.add_argument('--task-file')
    for name in ('term', 'path', 'symbol'):
        advice.add_argument('--' + name, action='append', default=[])
    advice.add_argument('--read-budget', type=int, default=1200)
    advice.add_argument('--list-budget', type=int, default=600)
    advice.add_argument('--candidate-limit', type=int, default=60)
    commands.add_parser('summary')
    return parser


def _rg(command, repo, *, raw=None, timeout=20, scratch=None):
    # Raw rg errors are never copied into a delivery or the ledger.
    scratch = temporary_root(repo) if scratch is None else scratch
    with tempfile.TemporaryFile(dir=scratch) as output, tempfile.TemporaryFile(dir=scratch) as errors:
        completed = subprocess.run(command, cwd=repo, input=raw, stdout=output,
                                   stderr=errors, timeout=timeout, check=False)
        if completed.returncode not in (0, 1):
            raise Failure('local rg request failed; check regex/globs and tool availability')
        output.seek(0)
        content = output.read(MAX_RG_BYTES + 1)
        if len(content) > MAX_RG_BYTES:
            raise Failure('rg output size limit exceeded; refine the request')
        return content


def _files(repo, globs, path=None):
    diagnostics = []
    files = _discover(repo, [], diagnostics)
    if any(word in item for item in diagnostics for word in
           ('unavailable', 'failed', 'timed out', 'discovery output limit')):
        raise Failure('safe file discovery unavailable; check ripgrep and ignore controls')
    if len(globs) > 32 or any(not glob or len(glob.encode('utf-8')) > 2000
                              or any(ord(c) < 32 for c in glob) for glob in globs):
        raise Failure('glob values must be bounded single-line patterns')
    if globs:
        command = ['rg', *_rg_options(repo), '--files', '--hidden', '--null', '--no-require-git',
                   *['--glob=' + glob for glob in globs]]
        matching = set(_rg(command, repo).decode('utf-8').split('\0'))
        files = [name for name in files if name in matching]
    if path is not None:
        relative = _relative(path)
        files = [name for name in files if name == relative or name.startswith(relative.rstrip('/') + '/')]
        if not files:
            raise Failure('search path is unavailable, ignored, or outside safety limits')
    # Avoid ambiguous/control-injected paths while preserving normal rg glob semantics.
    if any(any(ord(c) < 32 or ord(c) == 127 for c in name) for name in files):
        raise Failure('discovered file names contain unsafe control characters')
    return files, diagnostics


def _notice(diagnostics):
    return 'Notice: discovery excluded files or reached safety scan limits.\n' if diagnostics else ''


def _list_files(repo, args):
    files, diagnostics = _files(repo, args.glob)
    text = ''.join(name + '\n' for name in files[:args.limit])
    if len(files) > args.limit:
        text += f'Notice: file limit {args.limit} reached; refine globs or increase --limit (max 5000).\n'
    if not files:
        return 'files_failure', 'No files in the safely discovered selection.\n', [], 1
    return 'files_output', text + _notice(diagnostics), [], 0


def _snapshot_write(directory, name, raw):
    """All snapshot components are private and opened without following links."""
    descriptor = open_directory_nofollow(directory)
    try:
        parts = Path(_relative(name)).parts
        for part in parts[:-1]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        source = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=descriptor)
        with os.fdopen(source, 'wb') as stream:
            stream.write(raw)
    finally:
        os.close(descriptor)


def _search(repo, args, metadata):
    if (not args.pattern or len(args.pattern.encode('utf-8')) > 65536
            or any(c in args.pattern for c in ('\0', '\n', '\r'))):
        raise Failure('search pattern must be nonempty, single-line and at most 64 KiB')
    files, diagnostics = _files(repo, args.glob, args.path)
    hits, ranges = [], []
    delivered_bytes = 0
    bounded = False
    heading_path = None
    deadline = time.monotonic() + 60
    with tempfile.TemporaryDirectory(prefix='jev-task-search-', dir=temporary_root(repo)) as temporary:
        snapshot = _directory(temporary)
        sources = {}
        for name in files:
            if time.monotonic() >= deadline:
                raise Failure('search time limit reached; refine the request')
            raw = read_source_bytes(repo, name)
            metadata['source_read_bytes'] += len(raw)
            if b'\0' in raw:
                diagnostics.append('binary source excluded')
                continue
            try:
                source = source_lines(raw.decode('utf-8'))
            except UnicodeError:
                diagnostics.append('non-UTF-8 source excluded')
                continue
            _snapshot_write(snapshot, name, raw)
            sources[name] = source
            metadata['snapshot_written_bytes'] += len(raw)
            metadata['snapshot_files'] += 1
        # Ignore/glob selection happened on the target. Search the private safe
        # snapshot once, without loading config or ignore rules from its bodies.
        command = ['rg', *_rg_options(snapshot), '--no-ignore', '--hidden', '--sort', 'path',
                   '--json', '--max-count', str(args.limit + 1), '--regexp=' + args.pattern]
        if args.fixed:
            command.append('--fixed-strings')
        if args.ignore_case:
            command.append('--ignore-case')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Failure('search time limit reached; refine the request')
        content = _rg([*command, '--', '.'], snapshot, timeout=min(30, remaining), scratch=snapshot)
        for line in content.splitlines():
            item = json.loads(line)
            if item.get('type') != 'match':
                continue
            name = item['data']['path'].get('text')
            if not isinstance(name, str):
                raise Failure('rg returned an unavailable source path')
            if name.startswith('./'):
                name = name[2:]
            if name not in sources:
                raise Failure('rg returned an unavailable source path')
            source = sources[name]
            number = item['data']['line_number']
            if type(number) is not int or not 1 <= number <= len(source):
                raise Failure('rg returned an invalid source position')
            visible = source[number - 1]
            if not visible.endswith('\n'):
                visible += '\n'
            if args.heading:
                text = (name + '\n' if heading_path != name else '') + f'{number}:' + visible
            else:
                text = f'{name}:{number}:' + visible
            if len(hits) >= args.limit or delivered_bytes + len(text.encode('utf-8')) > MAX_TEXT_BYTES - 1024:
                bounded = True
                break
            hits.append(text)
            heading_path = name
            delivered_bytes += len(text.encode('utf-8'))
            ranges.append(dict(path=name, start_line=number, source_lines=[visible]))
    text = ''.join(hits)
    if bounded:
        text += f'Notice: search limit {args.limit} or text-size bound reached; refine pattern/path/globs or increase --limit (max 1000).\n'
    text += _notice(diagnostics)
    if not hits:
        return 'search_failure', 'No matches in safely discovered files.\n' + text, [], 1, metadata
    return 'search_output', text, ranges, 0, metadata


def _read(repo, directory, args, state):
    plans = sorted(directory.glob('plan-*.json'))
    if (type(args.start) is not int or type(args.end) is not int
            or not 1 <= args.start <= args.end):
        raise ReaderFailure('requested inclusive line range is invalid')
    if args.end - args.start + 1 > 300:
        raise ReaderFailure('request exceeds the 300-line limit; use smaller explicit ranges')
    name = _relative(args.path)
    files, _ = _files(repo, [], name)
    if name not in files:
        raise ReaderFailure('requested file is unavailable, ignored, or outside scan safety limits')
    raw = read_source_bytes(repo, name)
    if b'\0' in raw:
        raise ReaderFailure('binary source content is unavailable')
    try:
        eof_end = min(args.end, len(source_lines(raw.decode('utf-8'))))
    except UnicodeError:
        raise ReaderFailure('source must be valid UTF-8 text') from None
    if args.start > eof_end:
        raise ReaderFailure('requested start line is beyond end of file')
    text = read_ranges(repo, [(args.path, args.start, eof_end)],
                       state=state, repeat=args.repeat,
                       plan=plans[-1] if plans else None)
    ranges = []
    lines = source_lines(text)
    cursor = 0
    while cursor < len(lines):
        name, interval = lines[cursor].rstrip('\n').rsplit(':', 1)
        start, end = map(int, interval.split('-'))
        cursor += 1
        source = []
        for number in range(start, end + 1):
            prefix = f'{number}:'
            if cursor >= len(lines) or not lines[cursor].startswith(prefix):
                raise Failure('read delivery contains invalid source positions')
            source.append(lines[cursor][len(prefix):])
            cursor += 1
        ranges.append(dict(path=name, start_line=start, source_lines=source))
    if not text:
        text = 'Already delivered; use --repeat to request it again.\n'
    if eof_end < args.end:
        text += f'Notice: requested end line {args.end} clamped to EOF line {eof_end}.\n'
    return 'read_output', text, ranges, 0


def _commit_state(directory, pending):
    """Commit only after the exact delivery was validated, logged and returned."""
    parent = open_directory_nofollow(directory)
    try:
        for name in (pending.name, 'state.json'):
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                if name == pending.name:
                    raise Failure('pending read state is unavailable') from None
                continue
            if not stat.S_ISREG(info.st_mode):
                raise Failure('read state must be a regular file without symlinks')
        os.replace(pending.name, 'state.json', src_dir_fd=parent, dst_dir_fd=parent)
    finally:
        os.close(parent)


def _remove_pending(directory, pending):
    parent = open_directory_nofollow(directory)
    try:
        try:
            os.unlink(pending.name, dir_fd=parent)
        except FileNotFoundError:
            pass
    finally:
        os.close(parent)


def _task(directory, args):
    if args.task_file is None:
        task = args.task
    else:
        path = Path(args.task_file).absolute()
        if path.name != 'task.txt' or _directory(path.parent) != directory:
            raise Failure('task file must be the current run task.txt')
        parent = open_directory_nofollow(directory)
        try:
            try:
                raw = evaluation_jev._read_bytes(parent, 'task.txt', 65536)
                task = raw.decode('utf-8')
            except (OSError, ValueError, UnicodeError):
                raise Failure('task file must be bounded 64 KiB regular UTF-8 without links') from None
        finally:
            os.close(parent)
    if not isinstance(task, str) or not task.strip() or len(task.encode('utf-8')) > 65536:
        raise Failure('task must be nonempty and at most 64 KiB')
    return task


def _advice(repo, config, directory, args, scoring_metadata):
    if config['method'] not in ('local', 'jev'):
        raise Failure('advice is available only for method local or jev')
    task = _task(directory, args)
    options = dict(terms=args.term, paths=args.path, symbols=args.symbol,
                   read_budget=args.read_budget, list_budget=args.list_budget,
                   candidate_limit=args.candidate_limit, tokenizer=False, output_format='markdown')
    if config['method'] == 'jev':
        result = evaluation_jev.run_advice(pipeline, repo, directory, config['submission_ledger'],
                                          task, metadata=scoring_metadata, **options)
    else:
        with evaluation_jev._PIPELINE_LOCK:
            result = pipeline.run(repo, task, local_only=True, **options)
    plan = dict(repo=result['repo'], source_hashes=result['source_hashes'],
                **pipeline.public_report(result))
    if scoring_metadata:
        plan['evaluation_scoring'] = scoring_metadata
    number = 1
    while (directory / f'plan-{number:04d}.json').exists():
        number += 1
    _save_state(directory / f'plan-{number:04d}.json', plan)
    return 'advice_output', render(pipeline.public_report(result), 'markdown'), [], (2 if result['status'] == 'degraded' else 0)


def main(argv=None):
    started = time.monotonic()
    argv = list(sys.argv[1:] if argv is None else argv)
    stream = None
    pending = None
    delivered_elapsed = 0.0
    ledger = VisibilityLedger()
    io_metadata = dict(source_read_bytes=0, snapshot_written_bytes=0, snapshot_files=0)
    scoring_metadata = {}
    action = next((arg for arg in argv if arg in ('files', 'search', 'read', 'advice', 'summary')), 'request')
    try:
        position = argv.index('--run-dir')
        directory = _directory(argv[position + 1])
        repo, config = _config(directory)
        stream = _event_stream(directory)
        ledger, elapsed = _ledger(stream)
        _metadata(directory)
        capture = io.StringIO()
        with redirect_stdout(capture), redirect_stderr(capture):
            try:
                args = _parser().parse_args(argv)
            except SystemExit as error:
                if error.code != 0:
                    raise Failure('invalid command arguments') from None
                args = None
        if args is None:
            kind, text, ranges, status = 'help_output', capture.getvalue(), [], 0
        elif args.action == 'summary':
            summary = dict(ledger.summary(), event_count=len(ledger.events), elapsed_seconds=elapsed)
            kind, text, ranges, status = 'summary_output', json.dumps(summary, ensure_ascii=False, indent=2) + '\n', [], 0
        elif args.action == 'files':
            kind, text, ranges, status = _list_files(repo, args)
        elif args.action == 'search':
            kind, text, ranges, status, io_metadata = _search(repo, args, io_metadata)
        elif args.action == 'read':
            pending = directory / ('.pending-state-' + secrets.token_hex(12) + '.json')
            previous = _load_json(directory / 'state.json', missing_ok=True)
            if previous is not None:
                _save_state(pending, previous)
            kind, text, ranges, status = _read(repo, directory, args, pending)
        else:
            kind, text, ranges, status = _advice(repo, config, directory, args, scoring_metadata)
        _emit(stream, ledger, kind, text, time.monotonic() - started, source_ranges=ranges,
              io_metadata=io_metadata if args is not None and args.action == 'search' else None,
              request_argv=argv, scoring_metadata=scoring_metadata)
        delivered_elapsed = time.monotonic() - started
        if pending is not None:
            _commit_state(directory, pending)
            pending = None
        return status
    except (OSError, ValueError, KeyError, TypeError, IndexError, RuntimeError, subprocess.SubprocessError) as error:
        detail = str(error) if isinstance(error, (Failure, ReaderFailure, evaluation_jev.ScoringFailure)) else 'local request unavailable'
        text = f'failure: {detail}.\n'
        if stream is not None:
            try:
                _emit(stream, ledger, action + '_failure', text,
                      max(0.0, time.monotonic() - started - delivered_elapsed),
                      io_metadata=io_metadata if action == 'search' else None,
                      request_argv=argv, scoring_metadata=scoring_metadata)
                return 1
            except (OSError, ValueError, TypeError):
                pass
        sys.stdout.write(text + 'failure: safe event log unavailable; delivery was not recorded.\n')
        return 1
    finally:
        if pending is not None:
            try:
                _remove_pending(directory, pending)
            except (OSError, ValueError):
                pass
        if stream is not None:
            stream.close()


if __name__ == '__main__':
    raise SystemExit(main())
