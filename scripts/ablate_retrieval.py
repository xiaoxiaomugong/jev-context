#!/usr/bin/env python3
"""Run zero-Jev R0/R1/R2/R3 retrieval ablation using a saved code baseline."""

import argparse
import json
from pathlib import Path

from evaluate import evaluate_retrieval_ablation, load_tasks


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--tasks", required=True, type=Path)
    parser.add_argument("--baseline-retrieval", required=True, type=Path)
    parser.add_argument("--trial-retrieval", required=True, type=Path,
                        help="frozen evaluation-only trial module; production retrieval is unchanged")
    parser.add_argument("--dev-only", action="store_true")
    parser.add_argument("--candidate-limit", type=int, default=60)
    parser.add_argument("--read-budget", type=int, default=2048)
    parser.add_argument("--budgets", default="256,512,1024,2048")
    parser.add_argument("--output", required=True, type=Path, help="new JSON file; existing evidence is never overwritten")
    args = parser.parse_args(argv)
    try:
        if args.output.exists() or args.output.is_symlink():
            raise ValueError("output already exists; preserve evidence and choose a new path")
        budgets = [int(value) for value in args.budgets.split(",")]
        report = evaluate_retrieval_ablation(args.repo, load_tasks(args.tasks),
            baseline_retrieval=args.baseline_retrieval, trial_retrieval=args.trial_retrieval,
            dev_only=args.dev_only,
            candidate_limit=args.candidate_limit, read_budget=args.read_budget, budgets=budgets)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        print(json.dumps({"output": str(args.output.absolute()), "task_count": len(report["tasks"]),
                          "dev_only": args.dev_only, "submitted_candidates": 0}))
    except (OSError, ValueError, KeyError) as error:
        parser.error(str(error)[:240])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
