#!/usr/bin/env python3
"""Executable entry point; all default output is bounded reading advice."""

import argparse
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

if sys.version_info < (3, 12):
    print('jev-context: Python 3.12+ required; use a supported interpreter', file=sys.stderr)
    raise SystemExit(2)

from jev_context.output import render
from jev_context.pipeline import public_report, run, save_plan


def main():
    parser = argparse.ArgumentParser(description="Local retrieval → budgeted reading advice (read-only; Jev explicitly optional).")
    parser.add_argument("--check", action="store_true", help="Check runtime and tool availability without reading a repository or scoring.")
    parser.add_argument("--repo", type=Path)
    task = parser.add_mutually_exclusive_group()
    task.add_argument("--task")
    task.add_argument("--task-file", type=Path)
    parser.add_argument("--term", action="append", default=[], help="Repeatable literal retrieval term; English terms help Chinese tasks.")
    parser.add_argument("--path", action="append", default=[], help="Repeatable repo-relative explicit file/directory (pinned when located).")
    parser.add_argument("--symbol", action="append", default=[], help="Repeatable explicit Python symbol (pinned when located).")
    parser.add_argument("--read-budget", type=int, default=1200)
    parser.add_argument("--list-budget", type=int, default=600)
    parser.add_argument("--candidate-limit", type=int, default=60)
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mode", choices=["local", "jev"], default="local", help="Default local; jev sends task and code excerpts to configured provider.")
    mode.add_argument("--local-only", action="store_true", help="Compatibility alias for --mode local.")
    parser.add_argument("--jev-bin", default="jev", help="Existing CLI executable; no installation or auth management.")
    parser.add_argument("--jev-timeout", type=float, default=45, help="Entire batch wall timeout in seconds; no wrapper retries.")
    parser.add_argument("--debug-dir", type=Path, help="Explicitly save private candidate cards/scores in a local directory.")
    parser.add_argument("--save-plan", type=Path, help="Save advice and source fingerprints outside target repo; refuses existing files.")
    parser.add_argument("--tokenizer", action="store_true", help="Opt into installed tiktoken cl100k_base; may download its encoding on first use. Falls back if unavailable.")
    args = parser.parse_args()
    safe_interfaces = (os.name == 'posix' and hasattr(os, 'O_NOFOLLOW')
                       and hasattr(os, 'O_DIRECTORY') and os.open in os.supports_dir_fd)
    if args.check:
        ready = shutil.which("rg") is not None and safe_interfaces
        print(json.dumps(dict(ready=ready, python=sys.version.split()[0],
            python_required=">=3.12", rg=shutil.which("rg"), default_mode="local",
            jev_optional=shutil.which(args.jev_bin),
            tokenizer_optional=importlib.util.find_spec("tiktoken") is not None,
            safe_reading_available=safe_interfaces)))
        return 0 if ready else 2
    if not safe_interfaces:
        print('jev-context: safe POSIX file interfaces unavailable', file=sys.stderr)
        return 2
    if args.repo is None or (args.task is None and args.task_file is None):
        parser.error("retrieval requires --repo and --task or --task-file; use --check for environment checks")
    try:
        task_text = args.task if args.task is not None else args.task_file.read_text(encoding="utf-8")
        if not task_text.strip() or len(task_text.encode("utf-8")) > 65536:
            parser.error("task must be nonempty and <=64KiB")
        result = run(args.repo, task_text, args.term, paths=args.path, symbols=args.symbol,
                     read_budget=args.read_budget, list_budget=args.list_budget, candidate_limit=args.candidate_limit,
                     local_only=args.mode == "local", jev_bin=args.jev_bin, jev_timeout=args.jev_timeout,
                     debug_dir=args.debug_dir, output_format=args.format, tokenizer=args.tokenizer)
        if args.save_plan:
            save_plan(result, args.save_plan)
        sys.stdout.write(render(public_report(result), args.format))
        return 2 if result['status'] == 'degraded' else 0
    except (OSError, ValueError, RuntimeError) as exc:
        # Do not echo target file content, CLI stderr or credentials in exceptions.
        message = 'source changed; rerun retrieval' if 'source changed' in str(exc) else 'check repo/options, output path and tool availability'
        print(f"jev-context: {type(exc).__name__}; {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
