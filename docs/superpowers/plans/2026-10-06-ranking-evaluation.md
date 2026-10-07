# Jev ranking evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Test whether Jev ranking reduces visible main-model context in real tasks.

**Architecture:** Freeze one retrieval snapshot for paired B/C, vary only ranking, reuse one scoring result for budget scans. Keep A as a separate recall reference. Record code visibility events and provisional source-grounded labels separately.

**Tech Stack:** Python standard library, rg, installed Jev CLI, isolated collaboration agents.

**Spec:** User request in the current chat, 2026-10-06.

## Global Constraints

- Preserve references/evaluation-results.json and evaluation-offline-results.json.
- No target repository edits, global skill installation, new production services, or default-strategy change.
- At most 1000 first-batch submitted Jev judgments; no budget-rescoring.
- Same task, hints, snapshot, clipping policy, counter and budget in paired B/C.
- Labels never enter retrieval/scoring; holdout is fixed before ranking inspection.
- API usage and HTTP retry count remain unknown unless obtained directly.

## Review Focus

- Replay after task, cards, repository, counter or scoring-rule changes must fail.
- Partial/timeout adapters with valid scores must not be called complete Jev success.
- Small pools fitting fully must be marked selection-uninformative.
- Repeated tool text must be counted, separate from unioned code ranges.
- Long fragments must use one clipping eligibility policy across B/C.

### Task 1: Paired offline evaluation

Files: scripts/evaluate.py, scripts/jev_context/selection.py, retrieval.py, pipeline.py, tokens.py; matching tests.
- [x] Add regression tests and observe failures for shared snapshots, replay refusal, one-call scans, partial scoring, neutral clipping, retrieval stage tracing and visibility accounting.
- [x] Implement minimum evaluation interfaces, leave production selection defaults intact.
- [x] Run full unittest suite plus fixture mock scan and CLI fallback checks.

### Task 2: Source corpus

Files: references/real-evaluation-tasks.json, real-task-sources.md.
- [x] Fetch read-only Click 8.1.8 checkout and fix commit.
- [x] Record 12 docs-derived tasks, 6 dev/6 holdout before inspecting ranking, provenance, provisional labels and acceptance criteria.
- [x] Validate ranges and run one real scoring batch; save new snapshot/report files.
- [x] Replay all budgets with scorer disabled and compare deterministic outputs.

### Task 3: Isolated readers

Files: scripts/read_experiment.py, tests/test_read_experiment.py, references/experiment-*.
- [x] Implement logged read/search helper and ledger after failure tests.
- [x] Run 3–4 paired tasks with clean-context agents given only task+method list, same settings and standards.
- [x] Grade answers independently against provisional acceptance; capture initial reads, supplemental reads, failures and repeat text.

### Task 4: Review and delivery

Files: references/evaluation-2026-10-06.md and human-review-2026-10-06.md.
- [x] Fresh review of changes; address correctness findings and verify full tests.
- [x] Summarize per-task/split results and failures, distinguish real/mock/replay.
- [x] Give strategy recommendation without changing default, with at most three next actions.

## Rulings and evidence

- Workspace has no Git metadata; no branch or commit operation applies.
- Initial 56 tests pass. Historical fixtures have capacity640 but reading241–283, therefore no ranking-pressure evidence.
- Existing score-dependent clipping and list pruning confound ranking: evaluation uses rank-only clipping and an unbounded measured public list; CLI defaults remain.
- A documentation-derived corpus is explicitly identified as such, not represented as real bug tickets or human-reviewed labels.

- Final evidence: 90/90 local/mock tests, 720/720 validated real judgments, 0 replay submissions; 3 compliant paired readers plus 1 retained protocol-deviant pair. All 8 answers pass blinded provisional model grading. Human labels/answers remain pending by design.
