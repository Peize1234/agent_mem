---
name: memory-retrieval-tuner
description: Automatically audit a memory benchmark, run production-faithful MidTerm retrieval tuning, validate generalization across sessions, and recommend a stable configuration. Use when changing datasets or tuning MidTerm memory retrieval. R@K is configurable; K defaults to 5.
---

# Memory Retrieval Tuner

## Goal

Automate the memory-retrieval tuning workflow after a dataset change:

1. audit the benchmark before tuning;
2. establish a reproducible production baseline;
3. split by Session to separate tuning from validation;
4. search cheap parameters first;
5. use diagnostics to decide which expensive branch to run;
6. validate the best candidates on held-out Sessions;
7. return a stable recommended configuration, not merely the highest in-sample score.

Do **not** encode any historical experiment result as a universal best setting. Treat prior results only as search priors.

Read before running:

- `references/search_strategy.md`
- `references/experiment_lessons.md`
- `search_space.yaml`

## Inputs

Required:

- `dataset`: benchmark dataset path.

Optional:

- `k`: recall cutoff used by the primary metric `R@K`. Default: `5`.
- `budget`: `quick | standard | deep`. Default: `standard`.
- `target`: currently only `midterm`. ShortTerm, LongTerm, and union metrics are final regression checks.
- `sessions`: optional Session subset.
- `seed`: split/search seed. Default comes from `search_space.yaml`.
- `resume`: existing tuning run directory to resume.
- `output_dir`: output root. Default: `exp/results/auto_tuning`.
- `memory_config`: production-compatible Memory config used when a new source run is required. Default: `exp/benchmark/memory_config.json`.
- `llm_mode`: `real | mock`. Default: `real`; `mock` is infrastructure/smoke-test only.
- `overrides`: explicit search-space overrides.

Invocation examples:

```text
$memory-retrieval-tuner dataset=exp/my_dataset.xlsx
```

```text
$memory-retrieval-tuner dataset=exp/my_dataset.xlsx k=10 budget=deep
```

```text
$memory-retrieval-tuner dataset=exp/my_dataset.xlsx k=3 target=midterm sessions=S001-S010
```

User-specified values always override defaults in `search_space.yaml`.

## Executable entry point

Run the deterministic orchestrator from the repository root:

```bash
python .agents/skills/memory-retrieval-tuner/scripts/run_tuner.py \
  dataset=<dataset_path> k=<K> budget=<quick|standard|deep>
```

Pass `resume=<existing_run_dir>` to resume an interrupted run. Pass concurrency overrides as
`max_parallel_sessions=N`, `max_parallel_candidates=N`, and `max_parallel_llm_calls=N`.

The baseline never falls back to a source-turn BM25 surrogate. It must come from either a complete production trace
or replayable `production_midterm_v1` checkpoints. If neither exists, the tuner starts isolated Session subprocesses
and runs the real `AsyncMemory.add -> MidTermUpdater -> MidTermMemory -> MidTermRetriever` pipeline. Pass
`source_run=<path>` to pin an existing source run; an incomplete source run is a hard audit failure.

`production_midterm_adapter` freezes query-time production Page and Session payloads, dense vectors, query vectors,
and source-job lineage. It does not construct Page summaries from workbook answers. Cheap candidates reload those
artifacts into Candidate/Session-isolated Qdrant + SQLite runtimes and call production `MidTermRetriever`.

## Metric contract

The primary objective is **requirement-level Recall@K on held-out validation Sessions**.

Let each Query contain one or more Gold requirements. An AND-separated dependency contributes one requirement per required group. An OR group contributes one requirement and is satisfied if any member is retrieved.

For a configured `k`:

```text
R@K = number of satisfied Gold requirements within top K
      --------------------------------------------------
      number of eligible Gold requirements
```

Rules:

- `k` is configurable and defaults to `5`.
- Never hard-code `R@5` in evaluators, reports, filenames, or stopping logic.
- Derive secondary cutoffs from `k` where possible, e.g. `R@(2K)` and `R@(4K)`.
- For MidTerm tuning, the primary denominator should be the Gold requirements that are actually routed/eligible for MidTerm under the benchmark contract.
- Report ShortTerm coverage separately.
- Also report end-to-end union/completion metrics so local MidTerm gains are not mistaken for overall memory gains.
- Keep Micro requirement-level R@K and Macro/session-level metrics separate.

## Autonomy

Run the workflow end-to-end unless blocked by a genuine missing dependency, invalid dataset, unavailable model/API, or a destructive action requiring approval.

Do not stop after every stage to ask what to try next. Use the search policy below.

Do not modify `mem0/` production code merely to run an experiment. Prefer:

1. existing benchmark/evaluation code;
2. experiment adapters/config overrides;
3. new code under `exp/benchmark/` or this skill's `scripts/`.

Only change production code when the user explicitly requests implementation of the selected configuration.

## Repository reuse

Before writing new experiment code, inspect the local repository and reuse current equivalents of known components. Common reusable paths may include:

- `exp/benchmark/benchmark_common.py`
- `exp/benchmark/benchmark_memory.py`
- `exp/benchmark/run_recall_benchmark.py`
- `exp/benchmark/run_recall_isolated_sessions.py`
- `exp/benchmark/midterm_retrieval_eval.py`
- `exp/benchmark/diagnose_midterm_page_recall.py`
- `exp/benchmark/diagnose_page_representation.py`
- existing query-rewrite/reference-resolution ablations
- existing embedding/BM25/reranking ablations
- existing frozen Page/Query/embedding/ranking artifacts under `exp/results/`

Paths may evolve. Verify what exists locally; if a listed file is absent, search for the current equivalent instead of recreating it blindly.

Prefer frozen artifacts when they are semantically compatible with the current dataset/config and provenance can be validated. Never reuse an artifact merely because the filename looks similar.

## Workflow

### 0. Resolve configuration

Load `search_space.yaml`, then apply user overrides.

Resolve at minimum:

```text
dataset
k
budget
target
seed
sessions
output_dir
```

Validate:

- `k >= 1`;
- retrieval candidate pool is at least `k`;
- any `R@(2K)` / `R@(4K)` report cutoff does not exceed available candidates without being clearly marked as capped;
- configuration is serialized into run metadata.

Create:

```text
<output_dir>/<run_id>/
```

### 1. Dataset audit — mandatory gate

Audit before any tuning.

Check:

- Session, Query, Gold requirement counts;
- Gold-bearing versus independent Queries;
- dependency-distance distribution;
- ShortTerm coverage using the configured ShortTerm window;
- MidTerm/LongTerm eligibility/routing;
- AND/OR Gold parsing;
- invalid or future dependencies;
- cross-session leakage;
- duplicate Query IDs;
- missing prior targets;
- explicit historical-location leakage such as quoted prior questions, turn numbers, or templated “go back N turns” wording;
- suspicious concentration at one dependency distance;
- strong Query/Answer templating or duplication if detectable;
- benchmark provenance consistency.

Write:

```text
dataset_audit.json
dataset_audit.md
```

If a structural correctness check fails, stop with:

```text
DATASET_AUDIT_FAILED
```

If only a quality warning is detected, continue only when metrics remain interpretable and mark the run:

```text
DATASET_QUALITY_WARNING
```

Never “tune around” a broken benchmark.

### 2. Establish baseline

Run or reconstruct the current production baseline using the same dataset, Session scope, routing rules, metric `k`, and evaluation code that candidates will use.

The production contract is:

```text
AsyncMemory.add
-> SQLite ShortTerm QA eviction
-> production Page summary prompt
-> production Page embedding/write
-> dense+keyword Session assignment and production Session merge
-> unchanged Query embedding
-> dense Session routing
-> dense Page retrieval within routed Sessions
-> global Page-score dedupe/sort/cap
```

Query-time dense+BM25 fusion is a benchmark-only candidate extension. It must retain production Session routing and
operate on production-generated Page payloads. It is never labeled as the production baseline.

Record:

- primary R@K;
- Macro/session R@K;
- MRR;
- R@(2K), R@(4K) when available;
- mean/median Gold rank;
- ShortTerm coverage;
- target-layer hits;
- Short+Target union;
- All-memory union/completion;
- runtime;
- LLM calls;
- embedding calls;
- cache reuse;
- failed turns.

No candidate is comparable unless its evaluation contract matches the baseline.

### 3. Split by Session

Never randomly split individual Queries from the same Session across tune and validation sets.

Use the split policy in `references/search_strategy.md`.

Write:

```text
split_manifest.json
```

Freeze this split for the entire run.

If there are too few Sessions for a reliable holdout, use the documented fallback and explicitly reduce confidence in the final recommendation.

### 4. Cheap search

Search low-cost, artifact-reusable dimensions first.

Typical dimensions:

- Query representation;
- Page representation;
- retrieval `top_k` / candidate-pool size;
- similarity thresholds;
- session/page limits;
- dense/keyword score weights;
- inexpensive lexical fusion where existing artifacts permit it.

Only claim branches implemented against the production contract. The current generic adapter supports production
dense retrieval, `top_k_sessions`, `top_k_pages`, `max_total_pages`, and dense+Qdrant-BM25 Page fusion. Query rewrite,
Page representation, alternate embeddings, Session-assignment weights/thresholds, rerankers, and prompt variants
must be skipped unless an exact-provenance production artifact/adapter is present.

Use staged search, not a full Cartesian grid.

After each stage:

1. evaluate candidates on tune Sessions;
2. prune clearly dominated candidates;
3. keep a small frontier;
4. run diagnostic logic before opening a new search branch.

### 5. Diagnostic branch selection

Use the relationship between R@K and deeper recall to decide what to search next.

Examples:

- **High R@(4K), low R@K:** candidates contain Gold but top ranking is poor. Prefer query representation, score fusion, reranking, field weighting.
- **Low R@(4K):** candidate coverage is poor. Prefer Page representation, memory-write summary, embeddings, candidate generation.
- **Large Session variance:** prioritize robust/generalizable configurations; inspect failure clusters before increasing search breadth.
- **Tune improves but validation regresses:** classify as overfit; do not promote.
- **A cheap representation change already wins stably:** do not spend expensive LLM budget merely because more branches exist.

Follow `references/search_strategy.md`.

### 6. Secondary search

Run only if diagnostics justify it.

Potential branches:

- alternative embedding models;
- BM25 / dense+lexical fusion;
- field-aware scoring;
- candidate-pool expansion;
- lightweight reranking.

Use cache/frozen artifacts aggressively when valid.

### 7. Expensive LLM search

Run only for top candidates or when diagnostics show representation generation is the bottleneck.

Potential branches:

- Add/memory-write prompt variants;
- context-aware Add variants;
- Page-summary prompt variants;
- bounded Query reference resolution;
- Query rewrite variants.

Requirements:

- freeze prompt text and hash it;
- record LLM model/config;
- record exact call counts;
- cache generated artifacts;
- never regenerate an existing valid frozen artifact simply to make an ablation “fresh”;
- never compare two prompt variants unless their provenance is distinguishable.

### 8. Validation

Take only the tune frontier into held-out validation.

The primary ranking criterion is validation requirement-level R@K.

Use this tie-break order:

1. validation R@K;
2. Macro/session stability;
3. MRR;
4. R@(2K) / R@(4K);
5. lower cost and lower complexity.

Treat differences inside `selection.tie_tolerance_pp` as practically tied unless repeated/session evidence clearly favors one candidate.

Mark candidates with strong tune gain and validation regression as:

```text
OVERFIT
```

Do not choose them.

### 9. Final recommendation

Recommend one configuration plus up to two alternatives:

- `best_stable`: default recommendation;
- `best_accuracy`: only if meaningfully more accurate but costlier;
- `best_low_cost`: only if materially cheaper with near-tied accuracy.

Never state “best” without saying whether it is tune-best or validation-best.

Write:

```text
leaderboard.csv
search_trace.jsonl
best_config.json
best_config_diff.json
requirement_results.jsonl
session_metrics.csv
final_report.md
run_metadata.json
```

## Candidate promotion rules

A candidate may advance when it is:

- better on primary R@K by a meaningful amount; or
- practically tied on R@K but better on stability/MRR/cost; or
- diagnostically useful enough to justify a downstream branch.

Do not promote a candidate merely because it improves one Session while materially harming the rest.

Do not keep expanding the search space after improvements plateau under the stopping rules in `search_space.yaml`.

## Reporting requirements

`final_report.md` must include:

- dataset and provenance;
- configured `K`;
- ShortTerm window;
- tune/validation split;
- baseline metrics;
- search stages actually executed;
- skipped branches and why;
- tune leaderboard;
- validation leaderboard;
- per-Session metrics;
- best stable configuration;
- diff versus baseline;
- absolute improvement in percentage points;
- R@(2K)/R@(4K) context;
- MRR and rank diagnostics;
- all-memory union/completion impact;
- cost: runtime, LLM calls, embedding calls;
- overfit candidates;
- data-quality warnings;
- stop reason;
- confidence/limitations.

When `k=10`, all primary labels/report prose must say `R@10`, not `R@5`.

## Final checks

Before completion verify:

- [ ] Dataset audit passed or warnings are explicit.
- [ ] `k` came from user override or default `5`.
- [ ] No evaluator hard-coded `5`.
- [ ] Tune and validation are Session-disjoint.
- [ ] Baseline and candidates share the same Gold/routing/evaluation contract.
- [ ] Candidate artifacts belong to the current dataset/config or have validated provenance.
- [ ] Failed-turn count is zero, or failures are explicitly invalidating.
- [ ] No future/cross-session memory leakage.
- [ ] Best configuration is selected by held-out validation, not tune score alone.
- [ ] Search stopped for a recorded reason.
- [ ] `best_config.json` is reproducible.
- [ ] `final_report.md` clearly distinguishes empirical result from historical prior.
