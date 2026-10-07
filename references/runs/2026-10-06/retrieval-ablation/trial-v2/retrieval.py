"""Bounded, local-only retrieval. Never execute code from the searched repository."""

from __future__ import annotations

import ast
from copy import deepcopy
import io
import heapq
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import tokenize


MAX_FILES = 5000
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_FILE_BYTES = 1024 * 1024
MAX_TERMS = 16
MAX_HITS_PER_FILE_TERM = 12
MAX_CANDIDATES_PER_FILE = 12
MAX_EXPLICIT_DIRECTORY_FILES = 20
MAX_RANGE_LINES = 300
MAX_EXCERPT_BYTES = 1500
EXCLUDED_PARTS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "vendor", "dist", "build", "target", "coverage", ".coverage", ".tox",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".next",
    ".cache", ".jev-context", "jev-context-output", "site-packages",
    ".ssh", ".aws", ".gnupg",
}
BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip",
    ".gz", ".bz2", ".xz", ".tar", ".7z", ".rar", ".exe", ".dll",
    ".so", ".dylib", ".o", ".a", ".class", ".pyc", ".pyo", ".bin",
    ".woff", ".woff2", ".ttf", ".otf", ".mp3", ".mp4", ".sqlite",
    ".db", ".lock", ".map", ".min.js", ".min.css",
}
SECRET_NAMES = {"credentials", "credentials.json", "secrets.json", "secrets.yaml", "secrets.yml", "id_rsa", "id_ed25519", ".npmrc", ".pypirc", ".netrc"}
STOP_WORDS = {"the", "and", "for", "with", "from", "that", "this", "when", "into", "please", "implement", "repair", "fix", "task", "code", "file", "function"}


def _tokens(text: str) -> int:
    return max(1, math.ceil(len(text.encode("utf-8")) / 3))


def _terms(task: str) -> list[str]:
    values = re.findall(r"[A-Za-z_][A-Za-z_0-9]{2,}|[\u3400-\u9fff]{2,32}", task)
    return list(dict.fromkeys(v for v in values if v.casefold() not in STOP_WORDS))[:MAX_TERMS]


def _safe_path(repo: Path, relative: str, excluded: list[Path]) -> Path | None:
    path = repo / relative
    try:
        parts = path.relative_to(repo).parts
        if not parts or any(p in EXCLUDED_PARTS for p in parts):
            return None
        name = path.name.casefold()
        if name.startswith(".env") or name in SECRET_NAMES or name.endswith((".pem", ".key", ".p12", ".pfx")):
            return None
        if any(name.endswith(s) for s in BINARY_SUFFIXES):
            return None
        cursor = repo
        for part in parts:
            cursor = cursor / part
            if cursor.is_symlink():
                return None
        resolved = path.resolve()
        if not resolved.is_relative_to(repo) or any(resolved == p or resolved.is_relative_to(p) for p in excluded):
            return None
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path
    except (OSError, ValueError):
        return None


def _safe_ignore_metadata(repo: Path, diagnostics: list[str]) -> bool:
    """Fail closed before rg implicitly opens symlinked ignore controls."""
    visited = 0
    try:
        for root, dirs, names in os.walk(repo, followlinks=False):
            visited += 1
            if visited > MAX_FILES:
                diagnostics.append("ignore metadata directory limit reached; retrieval unavailable")
                return False
            here = Path(root)
            for name in (".ignore", ".gitignore", ".rgignore", ".git"):
                if (here / name).is_symlink():
                    diagnostics.append("ignore metadata symlink detected; retrieval unavailable")
                    return False
            git_dir = here / ".git"
            if git_dir.is_dir():
                for path in (git_dir / "info", git_dir / "info/exclude"):
                    if path.is_symlink():
                        diagnostics.append("git ignore metadata symlink detected; retrieval unavailable")
                        return False
            dirs[:] = [d for d in dirs if d not in EXCLUDED_PARTS and not (here / d).is_symlink()]
    except OSError:
        diagnostics.append("ignore metadata inspection failed; retrieval unavailable")
        return False
    return True


def _rg_options(repo: Path) -> list[str]:
    options = ["--no-config", "--no-ignore-parent", "--no-ignore-global", "--no-ignore-exclude"]
    for name in sorted(EXCLUDED_PARTS):
        options.extend(["--glob", f"!**/{name}"])
        options.extend(["--glob", f"!**/{name}/**"])
    # Preserve safe root exclude rules without automatic outside gitdir lookup.
    exclude = repo / ".git/info/exclude"
    if exclude.is_file() and not any(p.is_symlink() for p in (repo / ".git", repo / ".git/info", exclude)):
        options.extend(["--ignore-file", str(exclude)])
    return options


def _discover(repo: Path, excluded: list[Path], diagnostics: list[str], trace: dict | None = None) -> list[str]:
    if not shutil.which("rg"):
        diagnostics.append("rg unavailable: local retrieval requires ripgrep")
        return []
    if not _safe_ignore_metadata(repo, diagnostics):
        return []
    command = ["rg", *_rg_options(repo), "--files", "--hidden", "--null", "--no-require-git"]
    try:
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            result = subprocess.run(command, cwd=repo, stdout=output, stderr=errors, timeout=20, check=False)
            output.seek(0)
            # File discovery itself is bounded; names are never printed to stdout.
            raw = output.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                diagnostics.append("file discovery output limit reached")
                raw = raw[:4 * 1024 * 1024].rsplit(b"\0", 1)[0]
            if result.returncode not in (0, 1):
                diagnostics.append("rg file discovery failed; no repository content echoed")
            discovered = [p.decode("utf-8", errors="surrogateescape") for p in raw.split(b"\0") if p]
    except (OSError, subprocess.TimeoutExpired):
        diagnostics.append("rg file discovery failed or timed out")
        return []
    files = []
    total = 0
    skipped = 0
    safety_excluded = scan_limit_skipped = 0
    scan_limit_files = []
    for relative in sorted(set(discovered)):
        path = _safe_path(repo, relative, excluded)
        if path is None:
            skipped += 1
            safety_excluded += 1
            continue
        try:
            size = path.stat().st_size
        except OSError:
            skipped += 1
            continue
        if len(files) >= MAX_FILES or total + size > MAX_TOTAL_BYTES:
            skipped += 1
            scan_limit_skipped += 1
            scan_limit_files.append(relative)
            continue
        files.append(relative)
        total += size
    if skipped:
        diagnostics.append(f"excluded or bounded {skipped} discovered files (ignore, safety, {MAX_FILES} files / 32 MiB scan limits)")
    if trace is not None:
        trace.update(discovered_count=len(discovered), scanned_count=len(files),
                     scanned_bytes=total, safety_excluded=safety_excluded,
                     scan_limit_skipped=scan_limit_skipped, scan_limit_files=scan_limit_files)
    return files


def _rg_hits(repo: Path, files: list[str], terms: list[str], diagnostics: list[str], deadline: float | None = None) -> dict[str, dict[int, list[str]]]:
    hits: dict[str, dict[int, list[str]]] = {}
    if not terms or not files:
        return hits
    if not _safe_ignore_metadata(repo, diagnostics):
        return hits
    allowed = set(files)
    deadline = deadline if deadline is not None else time.monotonic() + 30
    for term in terms:
        for offset in range(0, len(files), 150):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                diagnostics.append("local search time limit reached; retained prior bounded matches")
                return hits
            command = ["rg", *_rg_options(repo), "--json", "--fixed-strings", "--ignore-case", "--max-count", str(MAX_HITS_PER_FILE_TERM), "--max-columns", "4000", "--max-columns-preview", "--file", "-", "--", *files[offset:offset + 150]]
            try:
                with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
                    result = subprocess.run(command, cwd=repo, input=(term + "\n").encode("utf-8"), stdout=output, stderr=errors, timeout=min(20, remaining), check=False)
                    output.seek(0)
                    consumed = 0
                    for raw in output:
                        consumed += len(raw)
                        if consumed > 8 * 1024 * 1024:
                            diagnostics.append("rg search output limit reached; retained bounded matches")
                            break
                        try:
                            row = json.loads(raw)
                            if row.get("type") != "match":
                                continue
                            data = row["data"]
                            path = data["path"].get("text")
                            line = data.get("line_number")
                            if path in allowed and isinstance(line, int) and line > 0:
                                hits.setdefault(path, {}).setdefault(line, []).append(term)
                        except (ValueError, KeyError, TypeError):
                            continue
                    if result.returncode not in (0, 1):
                        diagnostics.append("rg search failed for one bounded batch; retained prior matches")
            except (OSError, subprocess.TimeoutExpired):
                diagnostics.append("rg search failed or timed out for one bounded batch")
    return hits


class _Source:
    def __init__(self, path: str, text: str, diagnostics: list[str], parse_python: bool = True):
        self.path = path
        self.text = text
        self.lines = text.splitlines(keepends=True)
        self.tree = None
        self.scopes: list[dict] = []
        self.imports: list[ast.AST] = []
        self.import_owners: dict[int, ast.AST | None] = {}
        self.decorator_lines: list[tuple[int, int]] = []
        if parse_python and Path(path).suffix == ".py":
            try:
                self.tree = ast.parse(text, filename=path)
                self.imports = [n for n in ast.walk(self.tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
                self.decorator_lines = [(token.start[0], token.start[1]) for token in tokenize.generate_tokens(io.StringIO(text).readline) if token.type == tokenize.OP and token.string == "@" and self.lines[token.start[0] - 1].lstrip().startswith("@")]
                self._index_import_owners(self.tree, None)
                self._visit(self.tree, [], False)
            except (SyntaxError, ValueError, RecursionError):
                self.tree = None
                self.scopes.clear()
                self.imports.clear()
                self.import_owners.clear()
                diagnostics.append(f"python syntax fallback: {path}")

    def _index_import_owners(self, node: ast.AST, owner: ast.AST | None):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            self.import_owners[id(node)] = owner
        nested_owner = node if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else owner
        for child in ast.iter_child_nodes(node):
            self._index_import_owners(child, nested_owner)

    def visible_imports(self, node: ast.AST | None) -> list[ast.AST]:
        result = []
        for imported in self.imports:
            owner = self.import_owners.get(id(imported))
            if owner is None or owner is node:
                result.append(imported)
            elif node is not None and isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)) and owner.lineno <= getattr(node, "lineno", 0) and getattr(node, "end_lineno", 0) <= owner.end_lineno:
                result.append(imported)
        return result

    def _visit(self, node: ast.AST, parents: list[str], inside_class: bool):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                if child.decorator_list:
                    opening = [line for line, column in self.decorator_lines if column == child.col_offset and line <= start]
                    if opening:
                        start = max(opening)
                first = child.body[0]
                if first.lineno == child.lineno:
                    signature = self.lines[child.lineno - 1][:first.col_offset].strip()
                else:
                    signature = "".join(self.lines[child.lineno - 1:first.lineno - 1]).rstrip()
                kind = "class" if isinstance(child, ast.ClassDef) else "method" if inside_class else "function"
                self.scopes.append({"node": child, "start": start, "end": child.end_lineno, "symbol": ".".join([*parents, child.name]), "name": child.name, "kind": kind, "signature": signature})
                self._visit(child, [*parents, child.name], isinstance(child, ast.ClassDef))
            else:
                self._visit(child, parents, inside_class)

    def scope_at(self, line: int) -> dict | None:
        scopes = [s for s in self.scopes if s["start"] <= line <= s["end"]]
        return min(scopes, key=lambda s: (s["end"] - s["start"], -s["start"])) if scopes else None


def _excerpt(lines: list[str], start: int, end: int, anchor: int) -> tuple[str, int, int]:
    # Keep the hit itself and grow by nearby complete lines where possible.
    anchor = max(start, min(anchor, end))
    selected_start = anchor
    selected_end = anchor
    text = lines[anchor - 1]
    if len(text.encode("utf-8")) > MAX_EXCERPT_BYTES:
        raw = text.encode("utf-8")[:MAX_EXCERPT_BYTES]
        return raw.decode("utf-8", errors="ignore"), anchor, anchor
    for distance in range(1, max(anchor - start, end - anchor) + 1):
        for next_line in (anchor - distance, anchor + distance):
            if next_line < start or next_line > end:
                continue
            candidate = "".join(lines[min(selected_start, next_line) - 1:max(selected_end, next_line)])
            if len(candidate.encode("utf-8")) <= MAX_EXCERPT_BYTES:
                selected_start = min(selected_start, next_line)
                selected_end = max(selected_end, next_line)
                text = candidate
    return text.rstrip(), selected_start, selected_end


def _candidate(source: _Source, line: int, signals: list[str], score: float, relation: dict | None = None, pinned: bool = False, ast_enabled: bool = True) -> dict:
    line = max(1, min(line, len(source.lines)))
    scope = source.scope_at(line) if ast_enabled else None
    if scope:
        full_start, full_end = scope["start"], scope["end"]
        start, end = full_start, full_end
        if end - start + 1 > MAX_RANGE_LINES:
            start = max(full_start, line - MAX_RANGE_LINES // 2)
            end = min(full_end, start + MAX_RANGE_LINES - 1)
            start = max(full_start, end - MAX_RANGE_LINES + 1)
        symbol, kind, signature = scope["symbol"], scope["kind"], scope["signature"]
        mode = "python_ast"
    else:
        start, end = max(1, line - 12), min(len(source.lines), line + 12)
        full_start, full_end = start, end
        symbol, kind, signature, mode = None, "text_window", "", "text_fallback"
    excerpt, excerpt_start, excerpt_end = _excerpt(source.lines, start, end, line)
    return {
        "path": source.path, "start_line": start, "end_line": end,
        "symbol": symbol, "kind": kind, "signature": signature,
        "excerpt": excerpt, "excerpt_start_line": excerpt_start, "excerpt_end_line": excerpt_end,
        "retrieval_signals": signals, "relations": [relation] if relation else [],
        "estimated_read_tokens": _tokens("".join(source.lines[start - 1:end])),
        "extraction_mode": mode, "incomplete": start != full_start or end != full_end,
        "symbol_start_line": full_start, "symbol_end_line": full_end,
        "local_score": score, "pinned": pinned, "anchor_line": line,
    }


def _matched_terms(candidate: dict) -> set[str]:
    return {signal.split("text match: ", 1)[1].rsplit(" at line ", 1)[0]
            for signal in candidate["retrieval_signals"] if "text match: " in signal}


def _merge_candidate(candidates: dict[tuple, dict], candidate: dict, evidence_coverage: bool = False) -> dict:
    key = (candidate["path"], candidate["start_line"], candidate["end_line"], candidate["symbol"])
    if key not in candidates:
        candidates[key] = candidate
        return candidate
    prior = candidates[key]
    prior["retrieval_signals"] = list(dict.fromkeys([*prior["retrieval_signals"], *candidate["retrieval_signals"]]))
    for relation in candidate["relations"]:
        if relation not in prior["relations"]:
            prior["relations"].append(relation)
    prior["local_score"] = max(prior["local_score"], candidate["local_score"]) + (0 if evidence_coverage else 0.15)
    prior["pinned"] = prior["pinned"] or candidate["pinned"]
    return prior


def _module_target(source_path: str, module: str | None, level: int, files: set[str]) -> str | None:
    parent = list(Path(source_path).parent.parts)
    if parent == ["."]:
        parent = []
    if level:
        if level - 1 > len(parent):
            return None
        prefix = parent[:len(parent) - level + 1]
    else:
        prefix = []
    pieces = prefix + (module.split(".") if module else [])
    stem = "/".join(pieces)
    for path in (stem + ".py", stem + "/__init__.py"):
        if path in files:
            return path
    # Common src layout: only an unambiguous observed module can resolve.
    endings = ("/" + stem + ".py", "/" + stem + "/__init__.py")
    matches = [p for p in files if any(p.endswith(s) for s in endings)]
    return matches[0] if len(matches) == 1 else None


def _relation(kind: str, seed: dict, line: int, detail: str, certainty: str) -> dict:
    return {"type": kind, "source_path": seed["path"], "source_line": line, "detail": detail, "certainty": certainty}


def _expand(seeds: list[dict], source_for, files: list[str], candidates: dict, diagnostics: list[str], ast_enabled: bool, evidence_coverage: bool = False):
    file_set = set(files)
    reference_cache: dict[str, dict] = {}
    for seed in seeds[:8]:
        source = source_for(seed["path"])
        if not source:
            continue
        scope = source.scope_at(seed["anchor_line"]) if ast_enabled else None
        node = scope["node"] if scope else source.tree
        names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} if node else set()
        attrs = {(n.value.id, n.attr) for n in ast.walk(node) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)} if node else set()
        added = 0

        def add(path: str, line: int, kind: str, detail: str, certainty: str, score: float, relation_line: int | None = None):
            nonlocal added
            if added >= 6:
                return
            other = source_for(path)
            if not other or not other.lines:
                return
            relation = _relation(kind, seed, relation_line or seed["anchor_line"], detail, certainty)
            signal = f"{kind}: {detail} (from {seed['path']}:{relation['source_line']})"
            _merge_candidate(candidates, _candidate(other, line, [signal], score, relation, ast_enabled=ast_enabled), evidence_coverage)
            added += 1

        # Imports are syntactic edges. They do not claim runtime dispatch or execution.
        for imported in source.visible_imports(node):
            if added >= 3:
                break
            if isinstance(imported, ast.ImportFrom):
                target = _module_target(source.path, imported.module, imported.level, file_set)
                if not target:
                    continue
                other = source_for(target)
                if not other:
                    continue
                for alias in imported.names:
                    visible = alias.asname or alias.name
                    if visible not in names or alias.name == "*":
                        continue
                    matches = [s for s in other.scopes if s["name"] == alias.name and "." not in s["symbol"]]
                    for match in matches[:1]:
                        add(target, match["start"], "local_import", f"imported symbol {alias.name}", "syntactic", 3.5, imported.lineno)
            else:
                for alias in imported.names:
                    visible = alias.asname or alias.name.split(".")[0]
                    target = _module_target(source.path, alias.name, 0, file_set)
                    if visible not in names or not target:
                        continue
                    other = source_for(target)
                    if other:
                        for root, attr in sorted(attrs):
                            if root == visible:
                                for match in [s for s in other.scopes if s["name"] == attr and "." not in s["symbol"]][:1]:
                                    add(target, match["start"], "local_import", f"imported attribute {visible}.{attr}", "syntactic", 3.5, imported.lineno)
        if scope:
            for local in source.scopes:
                if added >= 4:
                    break
                if local["name"] in names and local["symbol"] != scope["symbol"] and "." not in local["symbol"]:
                    ref = next((n.lineno for n in ast.walk(node) if isinstance(n, ast.Name) and n.id == local["name"]), seed["anchor_line"])
                    add(source.path, local["start"], "local_symbol_reference", f"AST name reference {local['name']}", "syntactic", 3.0, ref)
        symbol_name = scope["name"] if scope else None
        if symbol_name:
            # A matching test filename is evidence of correspondence, not proof of coverage.
            stem = Path(seed["path"]).stem
            test_paths = sorted(p for p in files if Path(p).suffix == ".py" and (Path(p).name.startswith("test_") or Path(p).name.endswith("_test.py")))
            test_paths.sort(key=lambda p: (not (Path(p).stem in ("test_" + stem, stem + "_test")), p))
            for path in test_paths[:100]:
                if added >= 6:
                    break
                other = source_for(path)
                if not other:
                    continue
                for test_scope in other.scopes:
                    if not test_scope["name"].startswith("test"):
                        continue
                    references = [n for n in ast.walk(test_scope["node"]) if (isinstance(n, ast.Name) and n.id == symbol_name) or (isinstance(n, ast.Attribute) and n.attr == symbol_name)]
                    if references:
                        imported_from_seed = any(isinstance(imp, ast.ImportFrom) and _module_target(path, imp.module, imp.level, file_set) == seed["path"] and any(alias.name == symbol_name and (alias.asname or alias.name) == symbol_name for alias in imp.names) for imp in other.visible_imports(test_scope["node"]))
                        certainty = "syntactic" if imported_from_seed and not any(isinstance(n, ast.Attribute) for n in references) else "potential"
                        add(path, references[0].lineno, "test_symbol_reference", f"test references name {symbol_name}", certainty, 3.2)
                if added < 6 and Path(path).stem in ("test_" + stem, stem + "_test") and not any(c["path"] == path for c in candidates.values()):
                    add(path, other.scopes[0]["start"] if other.scopes else 1, "corresponding_test_filename", f"test filename matches {stem}", "potential", 2.7)
            # A strictly bounded same-name text search supplies only potential edges.
            if added < 6:
                if symbol_name not in reference_cache:
                    reference_cache[symbol_name] = _rg_hits(Path(source_for.repo), files, [symbol_name], diagnostics, source_for.deadline)
                references = reference_cache[symbol_name]
                for path in sorted(references):
                    if added >= 6:
                        break
                    if path == seed["path"] or Path(path).name.startswith("test_"):
                        continue
                    line = min(references[path])
                    add(path, line, "potential_text_reference", f"same-name text {symbol_name}", "potential", 2.0)
        # Local configuration literals mentioned in the selected syntax get one hop.
        if node and added < 6:
            for literal in ast.walk(node):
                if isinstance(literal, ast.Constant) and isinstance(literal.value, str):
                    value = literal.value
                    if len(value) > 200 or Path(value).suffix not in {".toml", ".json", ".yaml", ".yml", ".ini", ".cfg"}:
                        continue
                    for path in (str(Path(seed["path"]).parent / value), value):
                        if path in file_set:
                            add(path, 1, "configuration_literal", f"string names config path {value}", "potential", 2.5, literal.lineno)
                            break


def _select(candidates: list[dict], limit: int, diagnostics: list[str]) -> list[dict]:
    rank = lambda c: (-c["local_score"], c["path"], c["start_line"])
    ordered = sorted(candidates, key=rank)
    pinned = [c for c in ordered if c["pinned"]]
    selected = pinned[:limit]
    if len(pinned) > limit:
        diagnostics.append(f"{len(pinned) - limit} pinned entries deferred by candidate limit; increase candidate limit")
    available = [c for c in ordered if not c["pinned"]]
    # Prefer high evidence, then reserve a small share for one-hop source coverage.
    primary_count = max(0, limit - len(selected) - min(3, max(0, (limit - len(selected)) // 4)))
    selected.extend(available[:primary_count])
    for relation_kind in ("local_import", "test_symbol_reference", "local_symbol_reference", "potential_text_reference", "configuration_literal"):
        if len(selected) >= limit:
            break
        item = next((c for c in available if c not in selected and any(r["type"] == relation_kind for r in c["relations"])), None)
        if item:
            selected.append(item)
    seen_files = {c["path"] for c in selected}
    for c in available:
        if len(selected) >= limit:
            break
        if c not in selected and c["path"] not in seen_files:
            selected.append(c)
            seen_files.add(c["path"])
    for c in available:
        if len(selected) >= limit:
            break
        if c not in selected:
            selected.append(c)
    if len(candidates) > len(selected):
        diagnostics.append(f"candidate limit {limit}: selected {len(selected)} of {len(candidates)} using local evidence and source coverage")
    return sorted(selected, key=lambda c: (not c["pinned"], *rank(c)))


def _spread(items: list, count: int) -> list:
    """Deterministic coverage across an ordered tier, including both ends."""
    if len(items) <= count:
        return items
    if count <= 1:
        return items[:count]
    return [items[round(i * (len(items) - 1) / (count - 1))] for i in range(count)]


def _resample_hits(hits, source_for, terms, diagnostics, deadline, trace=None):
    """One cached-source pass per saturated file; never enlarge the hit quota."""
    started = time.monotonic()
    stats = dict(saturated_pairs=0, scanned_bytes=0, anchors=0, interrupted=False)
    result = deepcopy(hits)
    for path, matches in sorted(hits.items()):
        saturated = [term for term in terms if sum(term in values for values in matches.values()) >= MAX_HITS_PER_FILE_TERM]
        if not saturated:
            continue
        stats["saturated_pairs"] += len(saturated)
        if time.monotonic() >= deadline:
            stats["interrupted"] = True
            break
        source = source_for(path)
        if not source:
            continue
        # rg uses Unicode simple folding, not Python regex's extra I variants
        # or casefold's multi-character expansions (e.g. sharp-s -> ss).
        folding = {}
        for character in set(source.text + "".join(terms)):
            folded = character.casefold()
            if len(folded) != 1:
                folded = character.lower()
            if len(folded) == 1 and folded != character:
                folding[ord(character)] = folded
        literals = {term: term.translate(folding) for term in terms}
        scopes = sorted(enumerate(source.scopes), key=lambda pair: pair[1]["start"])
        cursor, active, groups = 0, [], {}
        for line, text in enumerate(source.lines, 1):
            if line % 64 == 1 and time.monotonic() >= deadline:
                stats["interrupted"] = True
                break
            stats["scanned_bytes"] += len(text.encode("utf-8"))
            folded_text = text.translate(folding)
            matched = {term for term, literal in literals.items() if literal in folded_text}
            if not matched:
                continue
            while cursor < len(scopes) and scopes[cursor][1]["start"] <= line:
                index, scope = scopes[cursor]
                heapq.heappush(active, (scope["end"] - scope["start"], -scope["start"], index, scope["end"]))
                cursor += 1
            while active and active[0][3] < line:
                heapq.heappop(active)
            scope = source.scopes[active[0][2]] if active else None
            key = ("symbol", active[0][2]) if scope else ("text", (line - 1) // 25)
            group = groups.setdefault(key, dict(term_lines={}, terms=set(),
                symbol=scope["symbol"] if scope else "", section=key[1]))
            group["terms"].update(matched)
            for term in matched:
                prior = group["term_lines"].get(term)
                if prior is None or len(matched) > prior[0]:
                    group["term_lines"][term] = (len(matched), line)
        if stats["interrupted"]:
            break  # Keep the original bounded hits for this unfinished file.
        for term in saturated:
            eligible = [g for g in groups.values() if term in g["terms"]]
            tiers = {}
            for group in eligible:
                strength = (term.casefold() in group["symbol"].casefold(), len(group["terms"]))
                tiers.setdefault(strength, []).append(group)
            chosen = []
            for strength in sorted(tiers, reverse=True):
                ordered = sorted(tiers[strength], key=lambda g: (g["symbol"], g["section"]))
                chosen.extend(_spread(ordered, MAX_HITS_PER_FILE_TERM - len(chosen)))
                if len(chosen) == MAX_HITS_PER_FILE_TERM:
                    break
            for line in list(result[path]):
                result[path][line] = [t for t in result[path][line] if t != term]
                if not result[path][line]:
                    del result[path][line]
            for group in chosen:
                # Save one matching anchor per term during the single source pass.
                anchor = group["term_lines"][term][1]
                result[path].setdefault(anchor, []).append(term)
            stats["anchors"] += len(chosen)
    if stats["interrupted"]:
        diagnostics.append("local resampling time limit reached; retained prior bounded matches")
    stats["elapsed_seconds"] = round(time.monotonic() - started, 6)
    if trace is not None:
        trace.update(stats)
    return result


def _evidence_select(candidates: list[dict], limit: int, terms: list[str], decisions: list | None = None) -> list[dict]:
    """Pinned entries, direct relationships, one term witness per source/test, rank fill."""
    rank = lambda c: (-c["local_score"], c["path"], c["start_line"], c["end_line"])
    ordered = sorted(candidates, key=rank)
    selected, reasons = [], {}

    def add(candidate, reason):
        if candidate is not None and candidate not in selected and len(selected) < limit:
            selected.append(candidate)
            reasons[id(candidate)] = reason

    for candidate in ordered:
        if candidate["pinned"]:
            add(candidate, "explicit_entry")
    for kind in ("local_import", "local_symbol_reference", "test_symbol_reference"):
        add(next((c for c in ordered if any(r["type"] == kind for r in c["relations"])), None), "relation:" + kind)
    for term in terms:
        for test in (False, True):
            witnesses = [c for c in ordered if term in _matched_terms(c) and
                         ("/tests/" in "/" + c["path"] or Path(c["path"]).name.startswith("test_") or
                          Path(c["path"]).name.endswith("_test.py")) == test]
            witnesses.sort(key=lambda c: (term.casefold() not in (c["symbol"] or "").casefold(), *rank(c)))
            witness = witnesses[0] if witnesses else None
            if test and witness:
                # A nested fixture alone misses the assertions that exercise it.
                # Reuse an already-recalled enclosing test, never a whole class.
                enclosing = [c for c in witnesses if c["path"] == witness["path"] and
                    (c["symbol"] or "").split(".")[-1].startswith("test") and c["kind"] != "class" and
                    c["start_line"] <= witness["start_line"] and c["end_line"] >= witness["end_line"]]
                if enclosing:
                    witness = min(enclosing, key=lambda c: (c["end_line"] - c["start_line"], *rank(c)))
            add(witness, f"term:{term}:{'test' if test else 'source'}")
    for candidate in ordered:
        add(candidate, "local_rank_fill")
    if decisions is not None:
        decisions.extend(dict(path=c["path"], start_line=c["start_line"], end_line=c["end_line"],
            symbol=c["symbol"], selected=c in selected, reason=reasons.get(id(c), "quota_exhausted")) for c in ordered)
    return sorted(selected, key=lambda c: (not c["pinned"], *rank(c)))


def collect(repo: Path, task: str, terms: list[str], *, paths: list[str] | None = None, symbols: list[str] | None = None, candidate_limit: int = 60, expand: bool = True, ast_enabled: bool = True, excluded_dirs: list[Path] | None = None, trace: bool = False, resample_hits: bool = False, evidence_coverage: bool = False) -> dict:
    """Return local candidate cards plus cached lines, without printing source text.

    status is ok, empty, no_hits, or degraded. Diagnostics contain bounded facts,
    never repository source or raw subprocess stderr.
    """
    repo = Path(repo).resolve()
    started = time.perf_counter()
    stage_elapsed = {}
    deadline = time.monotonic() + 60
    diagnostics: list[str] = []
    if not repo.is_dir():
        return {"candidates": [], "diagnostics": ["repository directory unavailable"], "status": "degraded", "files": {}}
    excluded = [Path(p).resolve() for p in (excluded_dirs or [])]
    scan_trace = {}
    files = _discover(repo, excluded, diagnostics, scan_trace if trace else None)
    stage_elapsed["discovery"] = round(time.perf_counter() - started, 6)
    if not files:
        status = "degraded" if any("unavailable" in d or "failed" in d for d in diagnostics) else "empty"
        return {"candidates": [], "diagnostics": diagnostics[:12], "status": status, "files": {}}
    source_cache: dict[str, _Source | None] = {}

    def source_for(path: str) -> _Source | None:
        if path not in source_cache:
            safe = _safe_path(repo, path, excluded)
            try:
                raw = safe.read_bytes() if safe else b"\0"
                source_cache[path] = _Source(path, raw.decode("utf-8"), diagnostics, parse_python=ast_enabled or bool(symbols)) if b"\0" not in raw else None
            except (OSError, UnicodeError):
                source_cache[path] = None
        return source_cache[path]

    source_for.repo = repo
    source_for.deadline = deadline
    explicit_terms = []
    for term in terms:
        if not isinstance(term, str) or not term.strip():
            continue
        if "\n" in term or "\r" in term:
            diagnostics.append("multiline search term skipped: provide separate literal single-line terms")
            continue
        explicit_terms.append(term.strip())
    explicit_terms = list(dict.fromkeys(explicit_terms))
    if len(explicit_terms) > MAX_TERMS:
        diagnostics.append(f"search terms bounded to {MAX_TERMS}")
    if any(len(t) > 200 for t in explicit_terms):
        diagnostics.append("search term length bounded to 200 characters")
    search_terms = [t[:200] for t in (explicit_terms or _terms(task))[:MAX_TERMS]]
    candidates: dict[tuple, dict] = {}

    def merge(candidate):
        return _merge_candidate(candidates, candidate, evidence_coverage)

    def add_hits(hits: dict[str, dict[int, list[str]]], fallback: bool = False):
        for path in sorted(hits):
            source = source_for(path)
            if not source or not source.lines:
                continue
            for line, matched in sorted(hits[path].items(), key=lambda item: (-len(set(item[1])), item[0])):
                signals = [f"{'expanded ' if fallback else ''}text match: {term} at line {line}" for term in sorted(set(matched))]
                score = sum(2.0 + min(len(term), 80) / 40 for term in set(matched))
                merge(_candidate(source, line, signals, score, ast_enabled=ast_enabled))

    started = time.perf_counter()
    primary_hits = _rg_hits(repo, files, search_terms, diagnostics, deadline)
    possible_hit_limits = []
    if trace:
        for path, matches in primary_hits.items():
            for term in search_terms:
                if sum(term in terms_at_line for terms_at_line in matches.values()) >= MAX_HITS_PER_FILE_TERM:
                    possible_hit_limits.append([path, term])
    resampling = {}
    if resample_hits:
        primary_hits = _resample_hits(primary_hits, source_for, search_terms, diagnostics, deadline, resampling)
    add_hits(primary_hits)
    requested_paths = []
    for path_hint in paths or []:
        candidate_path = Path(path_hint)
        try:
            relative = str(candidate_path.relative_to(repo)) if candidate_path.is_absolute() else candidate_path.as_posix()
        except ValueError:
            diagnostics.append("explicit path outside repository skipped")
            continue
        located_paths = [relative] if relative in files else [p for p in files if relative in (".", "") or p.startswith(relative.rstrip("/") + "/")]
        if not located_paths:
            diagnostics.append(f"explicit path not discovered or excluded: {relative}")
            continue
        if len(located_paths) > MAX_EXPLICIT_DIRECTORY_FILES:
            diagnostics.append(f"explicit directory {relative}: limited to {MAX_EXPLICIT_DIRECTORY_FILES} of {len(located_paths)} discovered safe files")
            located_paths = located_paths[:MAX_EXPLICIT_DIRECTORY_FILES]
        requested_paths.extend(located_paths)
    for relative in dict.fromkeys(requested_paths):
        source = source_for(relative)
        if source and source.lines:
            scopes = [s for s in source.scopes if "." not in s["symbol"]] if ast_enabled else []
            for line in ([s["start"] for s in scopes[:MAX_CANDIDATES_PER_FILE]] or [1]):
                merge(_candidate(source, line, [f"explicit path: {relative}"], 10.0, pinned=True, ast_enabled=ast_enabled))
            for c in candidates.values():
                if c["path"] == relative:
                    c["pinned"] = True
    requested_symbols = set(symbols or [])
    if requested_symbols:
        for path in files:
            if Path(path).suffix != ".py":
                continue
            source = source_for(path)
            if not source:
                continue
            for scope in source.scopes:
                module_symbol = Path(path).with_suffix("").as_posix().replace("/", ".") + "." + scope["symbol"]
                if scope["name"] in requested_symbols or scope["symbol"] in requested_symbols or module_symbol in requested_symbols:
                    merge(_candidate(source, scope["start"], [f"explicit symbol: {scope['symbol']}"], 12.0, pinned=True, ast_enabled=ast_enabled))
        located = {c["symbol"] for c in candidates.values() if c["pinned"]}
        if not located:
            diagnostics.append("explicit symbols not located by Python AST")
    if not candidates:
        extension = [t for t in _terms(task) if t.casefold() not in {s.casefold() for s in search_terms}]
        if extension:
            add_hits(_rg_hits(repo, files, extension[:8], diagnostics, deadline), fallback=True)
        for path in files:
            matched = [t for t in [*search_terms, *extension[:8]] if t.casefold() in Path(path).stem.casefold()]
            if matched:
                source = source_for(path)
                if source and source.lines:
                    merge(_candidate(source, source.scopes[0]["start"] if source.scopes and ast_enabled else 1, [f"expanded filename match: {matched[0]}"], 1.0, ast_enabled=ast_enabled))
        diagnostics.append("zero-hit bounded expansion attempted once")
    hit_candidates = deepcopy(list(candidates.values())) if trace else None
    stage_elapsed["post_hits"] = round(time.perf_counter() - started, 6)
    started = time.perf_counter()
    if expand and candidates:
        seeds = sorted(candidates.values(), key=lambda c: (not c["pinned"], -c["local_score"], c["path"], c["start_line"]))
        _expand(seeds, source_for, files, candidates, diagnostics, ast_enabled, evidence_coverage)
    # Per-file bounds apply after evidence ranking, never to rg's first global rows.
    pre_truncation = deepcopy(list(candidates.values())) if trace else None
    stage_elapsed["pre_truncation"] = round(time.perf_counter() - started, 6)
    started = time.perf_counter()
    bounded = []
    decisions = {"per_file": [], "global": []}
    if evidence_coverage:
        groups = {}
        for candidate in candidates.values():
            groups.setdefault(candidate["path"], []).append(candidate)
        for path, group in sorted(groups.items()):
            bounded.extend(_evidence_select(group, MAX_CANDIDATES_PER_FILE + sum(c["pinned"] for c in group),
                                            search_terms, decisions["per_file"] if trace else None))
        stage_elapsed["post_per_file"] = round(time.perf_counter() - started, 6)
        started = time.perf_counter()
        selected = _evidence_select(bounded, max(1, candidate_limit), search_terms, decisions["global"] if trace else None)
        if len(bounded) > len(selected):
            diagnostics.append(f"candidate limit {candidate_limit}: selected {len(selected)} of {len(bounded)} using evidence coverage")
    else:
        per_file: dict[str, int] = {}
        for c in sorted(candidates.values(), key=lambda c: (not c["pinned"], -c["local_score"], c["path"], c["start_line"])):
            if per_file.get(c["path"], 0) < MAX_CANDIDATES_PER_FILE or c["pinned"]:
                bounded.append(c)
                per_file[c["path"]] = per_file.get(c["path"], 0) + 1
        stage_elapsed["post_per_file"] = round(time.perf_counter() - started, 6)
        started = time.perf_counter()
        selected = _select(bounded, max(1, candidate_limit), diagnostics)
    stage_elapsed["post_global"] = round(time.perf_counter() - started, 6)
    # IDs are assigned from locations, independently of ranking and response order.
    for index, candidate in enumerate(sorted(selected, key=lambda c: (c["path"], c["start_line"], c["end_line"], c["symbol"] or "")), 1):
        candidate["id"] = f"c{index}"
    status = "ok" if selected else "no_hits"
    if any("failed" in d or "unavailable" in d or "timed out" in d or "time limit" in d for d in diagnostics):
        status = "degraded"
    result = {"candidates": selected, "diagnostics": list(dict.fromkeys(diagnostics))[:12], "status": status, "files": {path: source.lines for path, source in source_cache.items() if source}}
    if trace:
        result["retrieval_trace"] = dict(discovered_files=files, scan=scan_trace,
            resampling=resampling, hit_candidates=hit_candidates, trim_decisions=decisions,
            stage_elapsed_seconds=stage_elapsed,
            pre_truncation_candidates=pre_truncation, post_per_file_candidates=deepcopy(bounded),
            selected_candidate_ids=[c["id"] for c in selected], possible_hit_limits=possible_hit_limits)
    return result
