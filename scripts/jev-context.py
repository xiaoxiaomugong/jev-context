#!/usr/bin/env python3
"""Executable entry point; all default output is bounded reading advice."""

import argparse
import sys
from pathlib import Path

from jev_context.output import render
from jev_context.pipeline import public_report, run


def main():
    parser = argparse.ArgumentParser(description="Local retrieval → Jev relevance → budgeted reading list (read-only).")
    parser.add_argument("--repo", required=True, type=Path)
    task = parser.add_mutually_exclusive_group(required=True)
    task.add_argument("--task")
    task.add_argument("--task-file", type=Path)
    parser.add_argument("--term", action="append", default=[], help="Repeatable literal retrieval term; English terms help Chinese tasks.")
    parser.add_argument("--path", action="append", default=[], help="Repeatable repo-relative explicit file/directory (pinned when located).")
    parser.add_argument("--symbol", action="append", default=[], help="Repeatable explicit Python symbol (pinned when located).")
    parser.add_argument("--read-budget", type=int, default=8000)
    parser.add_argument("--list-budget", type=int, default=1000)
    parser.add_argument("--candidate-limit", type=int, default=60)
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    parser.add_argument("--local-only", action="store_true")
    parser.add_argument("--jev-bin", default="jev", help="Existing CLI executable; no installation or auth management.")
    parser.add_argument("--jev-timeout", type=float, default=45, help="Entire batch wall timeout in seconds; no wrapper retries.")
    parser.add_argument("--debug-dir", type=Path, help="Explicitly save private candidate cards/scores in a local directory.")
    args = parser.parse_args()
    try:
        task_text = args.task if args.task is not None else args.task_file.read_text(encoding="utf-8")
        if not task_text.strip() or len(task_text.encode("utf-8")) > 65536:
            parser.error("task must be nonempty and <=64KiB")
        result = run(args.repo, task_text, args.term, paths=args.path, symbols=args.symbol,
                     read_budget=args.read_budget, list_budget=args.list_budget, candidate_limit=args.candidate_limit,
                     local_only=args.local_only, jev_bin=args.jev_bin, jev_timeout=args.jev_timeout,
                     debug_dir=args.debug_dir, output_format=args.format)
        sys.stdout.write(render(public_report(result), args.format))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        # Do not echo target file content, CLI stderr or credentials in exceptions.
        print(f"jev-context: {type(exc).__name__}; check repo/options and local tool availability", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
