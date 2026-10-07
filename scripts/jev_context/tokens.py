"""Token accounting, independent of the target repository."""

import math
from collections import defaultdict


class TokenCounter:
    def __init__(self, use_tokenizer=True):
        self.encoding = None
        if use_tokenizer:
            try:
                import tiktoken
                self.encoding = tiktoken.get_encoding("cl100k_base")
            except (ImportError, OSError, ValueError):
                pass
        self.method = ("tiktoken:cl100k_base" if self.encoding is not None
                       else "utf8_bytes_div_3_estimate")

    def count(self, text):
        if self.encoding is not None:
            return len(self.encoding.encode(text, disallowed_special=()))
        return math.ceil(len(text.encode("utf-8")) / 3)

    def truncate(self, text, limit):
        if self.count(text) <= limit:
            return text
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.count(text[:mid]) <= limit:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo]


class VisibilityLedger:
    """Evaluation text events: charge every delivery, deduplicate code separately.

    These counts measure supplied text with a consistent local counter, never
    provider billing. Tool envelopes and hidden harness prompts are unavailable.
    """

    def __init__(self, counter=None):
        self.counter = counter or TokenCounter(use_tokenizer=False)
        self.events = []
        self._code = defaultdict(dict)

    def record(self, kind, text, *, path=None, start_line=None, source_lines=None, source_ranges=None):
        ranges = list(source_ranges or [])
        if source_lines is not None:
            ranges.append(dict(path=path, start_line=start_line, source_lines=list(source_lines)))
        staged = defaultdict(dict)
        for item in ranges:
            range_path, first, lines = item["path"], item["start_line"], item["source_lines"]
            if not range_path or type(first) is not int or first < 1:
                raise ValueError("source events need a path and positive start line")
            for number, line in enumerate(lines, first):
                prior = staged[range_path].get(number, self._code[range_path].get(number))
                if prior is not None and prior != line:
                    raise ValueError("source changed within visibility ledger")
                staged[range_path][number] = line
        for range_path, lines in staged.items():
            self._code[range_path].update(lines)
        event = dict(kind=kind, text=text, tokens=self.counter.count(text))
        if source_lines is not None:
            event.update(path=path, start_line=start_line,
                         end_line=start_line + len(source_lines) - 1,
                         source_lines=list(source_lines))
        if source_ranges is not None:
            event["source_ranges"] = ranges[:len(source_ranges)]
        self.events.append(event)
        return event

    def summary(self):
        costs = defaultdict(int)
        repeated = 0
        seen = set()
        seen_source_lines = set()
        returned_source = repeated_source = 0
        for event in self.events:
            costs[event["kind"]] += event["tokens"]
            if event["text"] in seen:
                repeated += event["tokens"]
            seen.add(event["text"])
            ranges = list(event.get("source_ranges", []))
            if "source_lines" in event:
                ranges.append(event)
            for item in ranges:
                returned_source += self.counter.count("".join(item["source_lines"]))
                repeat_group = []
                for number, line in enumerate(item["source_lines"], item["start_line"]):
                    key = (item["path"], number)
                    if key in seen_source_lines:
                        repeat_group.append(line)
                    else:
                        repeated_source += self.counter.count("".join(repeat_group))
                        repeat_group = []
                    seen_source_lines.add(key)
                repeated_source += self.counter.count("".join(repeat_group))
        # Charge each contiguous range separately, like actual file reads.
        union_cost = 0
        for lines in self._code.values():
            group = []
            previous = None
            for number, line in sorted(lines.items()):
                if previous is not None and number != previous + 1:
                    union_cost += self.counter.count("".join(group))
                    group = []
                group.append(line)
                previous = number
            union_cost += self.counter.count("".join(group))
        return dict(token_basis=self.counter.method, by_kind=dict(costs),
                    visible_text_tokens=sum(costs.values()),
                    repeat_output_tokens=repeated, code_range_union_tokens=union_cost,
                    returned_source_tokens=returned_source, repeated_source_tokens=repeated_source,
                    api_usage=None, envelope_and_hidden_prompt_tokens="unavailable")
