#!/usr/bin/env python3
"""Deliver only explicitly requested, bounded source ranges; never execute them."""

import argparse
import sys

if sys.version_info < (3, 12):
    print('read-context: Python 3.12+ required; use a supported interpreter', file=sys.stderr)
    raise SystemExit(2)

from jev_context.reader import ReaderFailure, read_ranges


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ReaderFailure("invalid arguments; use --repo REPO --range PATH START END")


def main(argv=None):
    parser = Parser(description="Read explicit safe source ranges with optional interval deduplication.")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--range", nargs=3, action="append", default=[],
                        metavar=("PATH", "START", "END"))
    parser.add_argument("--state", help="Optional JSON metadata outside the target repo; no symlink paths.")
    parser.add_argument("--repeat", action="store_true", help="Explicitly reread intervals present in state.")
    parser.add_argument("--plan", help="Optional saved retrieval plan; validates involved source hashes.")
    try:
        args = parser.parse_args(argv)
        try:
            ranges = [(path, int(start), int(end)) for path, start, end in args.range]
        except ValueError:
            raise ReaderFailure("range START and END must be positive inclusive line numbers") from None
        text = read_ranges(args.repo, ranges, state=args.state, repeat=args.repeat, plan=args.plan)
        sys.stdout.write(text)
        return 0
    except ReaderFailure as exc:
        print(f"read-context: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
