# Contract: Evaluation report and baseline (evals/)

Version: 1.0.0. Producer: `evals/run.py`. Consumers: the `eval-runner` build subagent and the
`token-budget-eval` skill. Metric names are pinned to docs/architecture/00-overview.md §13 —
renaming one is a breaking change to this contract and to both consumers.

## Metrics

Every report and baseline carries exactly these eleven metrics, each as
`{"value": <number|null>, "status": "measured" | "measured_scripted" | "not_measured"}`:

| Metric | V1 status | Definition (V1 harness) |
|---|---|---|
| `context_tokens_per_successful_task` | measured | Σ tokens charged (ledger `tokens_returned`) over successful cases ÷ successful case count |
| `duplicate_context_tokens` | measured | tokens charged for evidence IDs already delivered earlier in the same run — cards (L0/L1) and expansions (L2) tracked separately, so the first `open_evidence` on a carded ID is new content, not a duplicate |
| `evidence_reuse_rate` | measured | ledger rows with status `reused` ÷ rows with status `reused` or `approved` |
| `retrieval_calls_per_agent` | measured | ledger rows ÷ distinct `agent_name` values (per-agent table in the report body) |
| `semantic_cache_hit_rate` | measured | `semantic_reuse=true` rows ÷ follow-up (`context.request_more`) rows |
| `llm_calls_per_build` | not_measured | build-plane metric; no real build runs in the harness |
| `embedding_calls_per_build` | not_measured | build-plane metric; no real build runs in the harness |
| `unsupported_claim_rate` | measured_scripted | scripted agent-output claims whose `evidence_ids` fail `validate_evidence_references` against the IDs the broker actually returned ÷ total claims |
| `human_plan_edit_rate` | not_measured | requires human-in-the-loop plan reviews; none exist in the harness |
| `missing_context_rate` | measured | expected docs/files/symbols/tests/open_questions not matched by broker output ÷ total expected items |
| `active_kb_age` | not_measured | meaningless on synthetic fixtures seeded moments before the run |

`not_measured` metrics always carry `value: null` — the harness never fakes a number.
`measured_scripted` flags that the input is scripted case data, not real agent behavior:
`unsupported_claim_rate` measures that evidence-ID discipline *holds end to end*
(broker IDs → output validation), not the claim quality of a live agent.

## Case success

A case **succeeds** when all of:

- every expected doc was returned as an evidence card (doc recall = 1.0), and
- no ledger row for the case's run has status `error`.

Raised tool errors and `denied` / `needs_human_approval` outcomes are **contractual broker
behavior**, not case failures — cases may script and assert denial paths. Only ledger status
`error` (the broker's failure record, per docs/contracts/mcp-tools-contract.md) fails a case.
Unmatched expected items (docs, files, symbols, tests, open questions) feed
`missing_context_rate` whether or not the case succeeded.

## Report (`evals/report.json`)

```json
{
  "schema_version": "1.0.0",
  "created_at": "<ISO-8601 UTC>",
  "git_sha": "<short sha or null>",
  "cases": [
    {
      "id": "<case id>",
      "task_type": "<one of the six benchmark task types>",
      "succeeded": true,
      "missing": ["<unmatched expected items>"],
      "tokens_charged": 0
    }
  ],
  "metrics": { "<name>": { "value": 0.0, "status": "measured" } },
  "per_agent_calls": { "<agent_name>": 0 },
  "baseline": {
    "present": true,
    "deltas": { "<name>": { "old": 0.0, "new": 0.0, "relative": 0.0 } },
    "verdict": "improved | flat | regressed | no_baseline",
    "biggest_mover": "<metric name or null>"
  }
}
```

`run.py` also prints a compact table (metric, value, delta) plus the one-line verdict —
that stdout is what the `eval-runner` subagent reads. The eval-runner's **stable subset** is the
seven measurable metrics (mirrored as `EVAL_RUNNER_METRICS` in `evals/harness/metrics.py`;
renaming any of these breaks `.claude/agents/eval-runner.md`):
`context_tokens_per_successful_task`, `duplicate_context_tokens`, `evidence_reuse_rate`,
`retrieval_calls_per_agent`, `semantic_cache_hit_rate`, `unsupported_claim_rate`,
`missing_context_rate`.

In `deltas`, `relative` is `null` when the baseline value was `0` and the new value is not
(an infinite relative change — kept out of the JSON so it stays strict-parseable). The verdict
and `biggest_mover` still treat that delta as the maximal mover in its direction.

## Baseline (`evals/baseline.json`)

```json
{
  "schema_version": "1.0.0",
  "created_at": "<ISO-8601 UTC>",
  "git_sha": "<short sha or null>",
  "metrics": { "<name>": { "value": 0.0, "status": "measured" } }
}
```

Updated only via `run.py --update-baseline`; the file is committed so deltas are reviewable.

## Verdict semantics

Direction per metric — lower is better: `context_tokens_per_successful_task`,
`duplicate_context_tokens`, `retrieval_calls_per_agent`, `unsupported_claim_rate`,
`missing_context_rate`; higher is better: `evidence_reuse_rate`, `semantic_cache_hit_rate`.
Comparing only metrics measured in **both** runs:

- `regressed` — any metric worsens by more than 5% relative,
- `improved` — any metric improves by more than 5% relative and none regressed,
- `flat` — everything within ±5%,
- `no_baseline` — no baseline file present (first run).

`biggest_mover` is the metric with the largest absolute relative delta.

## Boundary

`evals/` is a dev-only benchmark project, never deployed. It takes a path dependency on
`services/mcp-server` and drives broker functions in-process against a migrated
`TEST_DATABASE_URL` Postgres with the `FakeSearchClient` — local runs never require Azure, and
evals never carries Alembic (kb-builder owns migrations). Evals must not modify product code to
make a case pass: a real defect gets filed, not masked.
