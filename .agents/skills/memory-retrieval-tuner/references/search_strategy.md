# Search Strategy

This document defines the adaptive search policy used by `memory-retrieval-tuner`.

The goal is not exhaustive enumeration. The goal is to find a strong, stable retrieval configuration quickly after changing datasets.

## 1. Core principle

Use a **coarse-to-fine, diagnostic-driven search**:

```text
Audit
  -> Baseline
  -> Session split
  -> Cheap representation/retrieval search
  -> Diagnose failure regime
  -> Open only justified secondary branches
  -> Expensive LLM search only for finalists
  -> Held-out validation
  -> Stable selection
```

For the current complete Agent Memory contract, the operational order is:

```text
Production Baseline
 -> Static Retrieval Tuning
 -> Source-generation Screening
 -> Cheap Retrieval Re-tuning
 -> Within-session Stateful Replay
 -> Cross-session structural replay (not evaluated)
 -> Joint Refinement
 -> Held-out Validation
```

`required_context` is fixed Gold; ShortTerm capacity never changes its denominator. Source-changing, within-session-stateful and cross-session-temporal parameters are separate from retrieval-only controls. Fine-grained LongTerm is produced per complete QA and is cross-session retrievable with Production's dual-route/session-weight contract. Because the current benchmark has no reliable cross-session Gold, `longterm_other_session_weight` and Promotion remain fixed Production defaults; structural replay is `STRUCTURAL_ONLY_NOT_EVALUATED` with status `CROSS_SESSION_TUNING_UNSUPPORTED_NO_GOLD`, excluded from winner selection.

Research decisions use only the current run's Tune evidence and current-run experiment trajectory; previous-run winners, metrics or lessons are never search priors.

Production changes remain explicit and narrowly scoped. Complete diagnostic state, hybrid scoring presets, Prompt overrides, cache/experiment state, and benchmark-only context caps remain inside this Skill. `DiagnosticMidTermRetriever` must return exactly the same public rows as production for the same query/store/config/turn clock, while retaining its trace only on the Skill-owned instance. The tuner `<=5` Agentic/Mid-term union is an evaluation constraint and does not change Production `max_total_results`; Production's fixed `max_tool_result_chars` is read from the effective config and must match every reused Agentic trace row.

## 2. Configurable Recall@K

`K` is a run parameter.

Default:

```yaml
evaluation:
  k: 5
```

Invocation may override it:

```text
$memory-retrieval-tuner dataset=... k=10
```

The primary metric then becomes `R@10`.

Derived depth metrics:

```text
primary        = R@K
secondary_1    = R@(2K)
secondary_2    = R@(4K)
```

Cap derived depths to the available candidate pool and report the cap.

All pruning and selection logic must use the configured `K`, not a hard-coded `5`.

## 3. Dataset gate

Do not tune before proving the benchmark is interpretable.

### Hard failures

Stop on:

- invalid/missing Gold IDs;
- future dependency;
- illegal cross-session dependency;
- duplicate identifiers that make Gold ambiguous;
- evaluation/routing denominator inconsistency;
- incomplete source run;
- incompatible artifact provenance.

### Quality warnings

Warn on:

- long-range dependencies overwhelmingly concentrated at one fixed distance;
- large fractions of queries explicitly quoting or naming the historical target;
- repetitive turn-position phrases;
- strong answer template duplication;
- unnatural dependency construction.

A warning may permit tuning, but the final report must state that the measured optimum may exploit benchmark artifacts.

## 4. Session split

Split by Session, never by Query.

Preferred policy:

### >= 8 Sessions

Use a deterministic 70/30 Session split, stratified approximately by:

- number of eligible Gold requirements;
- dependency-distance mix;
- Session size.

Use a fixed seed.

### 5–7 Sessions

Use a deterministic holdout of at least 2 Sessions if possible. Also report per-Session robustness.

### 3–4 Sessions

Prefer leave-one-session-out or repeated Session holdout. Aggregate validation across folds.

### < 3 Sessions

Run tuning only as exploratory. Do not claim robust generalization.

Do not alter the split in response to candidate performance.

## 5. Search budgets

### quick

Purpose: smoke test or rapid transfer.

- baseline;
- dataset audit;
- small cheap-search matrix;
- no new LLM generation unless necessary;
- at most medium-cost Branches;
- no model download or new LLM generation;
- at most 3 Tune stages and 1 Branch per stage;
- validate top 2.

### standard

Default.

- baseline;
- full cheap search;
- diagnostic branch selection;
- high-cost Branches are allowed, but model candidates must already be local;
- at most 8 Tune stages and 2 Branches per stage;
- validate top 3–5.

### deep

For final research runs.

- wider cheap search;
- multiple justified secondary branches;
- expensive prompt variants with exact provenance;
- online model discovery/download after metadata screening;
- at most 12 Tune stages and 3 Branches per stage;
- repeated/folded validation where feasible;
- robustness analysis.

Budget changes breadth, not the metric definition.

## 6. Stage A — baseline

The baseline is the current production behavior under the current benchmark.

Freeze:

- dataset hash/path;
- Session set;
- Gold parser;
- ShortTerm window;
- routing logic;
- configured `K`;
- candidate visibility;
- model/prompt/config hashes;
- code revision when available.

Record both target-layer and end-to-end metrics.

## 7. Stage B — cheap search

Start from dimensions that can usually be evaluated with frozen artifacts or inexpensive embeddings.

### B1. Query representation

Keep `original` as the production reference in every round. On a new dataset, generate up to three controlled Prompt directions around the current best Prompt, cache every Query independently, screen/Tune them using Tune Sessions only, and repeat around a winning direction for at most three rounds. Reuse an artifact only when dataset, parent Candidate, Prompt/model config and immutable hashes match exactly.

Derive rewrite history from every production manifest's exact `memory_config.midterm.short_term_capacity / 2`. History entries are QA turns, not messages. Pass only prior QA turns still visible in production ShortTerm; reject missing, odd/non-positive, or manifest/config-inconsistent capacities. Include both window units, the history policy, and production config hash in artifact identity so an old wider-window artifact cannot resume.

Prompt direction selection may summarize missed Tune Query patterns without reading Gold answers or Validation rows. Stop this Branch when a round does not reach `min_improvement_pp`, all variants fail to improve, original remains best, the direction leaves the frontier, resources are exhausted, or round three completes. Do not assume a historical reference-resolution winner transfers.

### B2. Page representation

Use the Page fields frozen by the production MidTerm checkpoint. Recompose controlled representations and re-embed them with a provenance-keyed derivative; keep production Session routing unless the Branch explicitly re-embeds Sessions. Never use workbook answers as Page summaries.

### B3. Retrieval controls

Implemented query-time coarse search:

- candidate pool / `top_k_pages`;
- `top_k_sessions`;
- maximum total pages;
- production dense Page retrieval;
- Qdrant BM25+dense Page-score fusion inside production dense Session routing.

Session assignment thresholds and embedding/keyword assignment weights alter generated Session artifacts. They are
not query-time knobs and are skipped unless matching regenerated production artifacts exist.

Do not run all Cartesian combinations. Use coordinate or successive-halving style search:

1. test one dimension around baseline;
2. keep winners/near-ties;
3. combine only promising values.

### B4. Candidate pruning

Drop a candidate early if, on tune Sessions, it is clearly dominated on:

- R@K;
- R@(2K)/R@(4K);
- Session stability;
- cost.

Keep diagnostically distinct candidates even if not top-ranked when they reveal different failure modes.

## 8. Diagnostic regime classification

After cheap search, classify the dominant failure regime.

### Regime 1 — ranking bottleneck

Typical signature:

```text
R@K is modest
R@(4K) is high
Gold often appears just below K
```

Search next:

- query representation;
- field-aware score;
- dense/keyword fusion;
- reranking;
- rank calibration.

Do **not** immediately regenerate Pages.

### Regime 2 — candidate coverage bottleneck

Typical signature:

```text
R@(4K) is also low
Gold often absent from candidate pool
```

Search next:

- Page representation;
- Page-generation/Add summary;
- embedding model;
- candidate-generation breadth;
- session routing.

### Regime 3 — session instability

Typical signature:

- high Micro average but large per-Session spread;
- candidate wins are concentrated in a few Sessions.

Search next:

- simpler representations;
- robust thresholds;
- per-session error categories;
- avoid over-specialized prompts.

Promote by validation robustness, not Micro score alone.

### Regime 4 — data artifact suspicion

Typical signature:

- retrieval succeeds mainly on explicit historical wording;
- one fixed dependency distance dominates;
- simple lexical/original-query method dramatically beats contextual methods.

Action:

- inspect dataset audit;
- do not automatically “fix” this with retrieval tuning;
- label final result benchmark-sensitive;
- if the audit is severe, rebuild benchmark before further tuning.

## 9. Stage C — secondary search

Open branches only when justified.

### Embedding models

Search when candidate coverage is poor or representation changes fail.

Start with the current production model. Under `standard`, screen only models already present in the local Hugging Face cache and never access the network. Under `budget=deep`, scan the cache and online metadata, merge and deduplicate by model ID plus immutable revision, then rank the combined pool with one quality rule. Cache state affects download cost only; it must not boost selection priority. Select up to roughly two suitable multilingual/general and two finance-domain candidates, without padding the pool with poor fits.

Prefer structured model-card/model-index metrics from MTEB Retrieval, C-MTEB/Chinese Retrieval, FinMTEB, multilingual retrieval, and the corresponding reranking suites over model-name, tags, or download-count heuristics. Preserve every reliable score as benchmark/task/dataset/metric/score metadata. Compare or normalize raw values only when all four identity fields match; never combine unrelated Benchmark scales into a synthetic raw-score average.

For each candidate record model ID, immutable revision, model type, source, License, selection rationale, resource estimate and cache/download state. Run metadata screening, download/cache validation, smoke testing, Tune-subset screening, then full Tune only for survivors. A gated model, unavailable dependency or resource failure is `UNAVAILABLE`, not a run failure.

A new embedding model must use the same text representation and evaluation contract. Resolve and record the model-owned Query/Document prefix or instruction, `prompt_name`, normalization and pooling first. If an instruction-tuned model's contract cannot be determined reliably, mark it `UNAVAILABLE` rather than bare-encoding it.

### Lexical / hybrid retrieval

Search when:

- exact entities/numbers/terms matter;
- dense retrieval misses lexically obvious Gold;
- existing BM25/fusion code can be reused.

Search dense-heavy fusion weights first.

### Reranking

Search when R@(4K) is strong but R@K is weak.

Do not rerank a tiny candidate pool that already excludes the Gold.

Use the production-routed Page pool and require the configured R@(4K)-minus-R@K gap. The built-in lightweight field-aware reranker is always available. `standard` may use a compatible cross-encoder already in local cache; only `deep` may discover or download a new one.

### Field-aware / multi-vector

Use weighted production Page fields for the lightweight candidate. `multi_vector_maxsim` requires provenance-keyed field vectors and is a deep/high-cost variant. Do not label either variant as production behavior.

### Branch Registry contract

The orchestrator does not construct Branch-specific candidates. Every registered Branch declares:

- name and diagnostic regimes;
- cost level and resource requirements;
- required artifacts;
- candidate generator and execution adapter;
- provenance contract.

Built-in Branches are RetrievalControl, AgenticRetrieval, QueryRewritePrompt, PageRepresentation, HybridRetrieval, Reranking, Embedding, FieldAwareMultiVector, MidtermSourceConfig, MidtermEvolution, FineGrainedLongtermRetrieval, MidtermPageSummaryPrompt, MidtermSessionMergePrompt and FineGrainedLongtermExtractionPrompt. AgenticRetrieval executes only when a complete `production_agentic_trace` matches the current Anchor's explicit retrieval/source/query/LongTerm semantic parent identity, fixed `max_tool_result_chars`, and exact parameter variant; an ordinary MidTerm result or a trace from an earlier Anchor can never satisfy that artifact contract. Promotion remains registered as future infrastructure but is disabled in the current default coverage because Cross-session Gold is unavailable. The old `SessionLongterm*` class names remain compatibility aliases only. The legacy QueryRepresentation adapter also remains only for compatibility and is excluded from normal coverage, so the two prompt paths are not searched twice. Add a new adapter to the registry when a diagnosis requires an unsupported technique; do not add another conditional candidate block to the orchestrator.

## 10. Stage D — expensive LLM search

Only run when a frontier Candidate and the diagnosis justify the cost.

### Memory-write / Add prompt

Use when Page content itself is missing dependency-bearing evidence.

For a new dataset, `budget=deep` runs the isolated production Add → eviction → Page/Session generation → embedding chain. Variants include:

- production Add;
- conservative Add;
- context-aware Add;
- evidence-focused Page Summary;
- one diagnosis-controlled Page Summary.

Production Add/Summary remains the unchanged reference. Context is limited to runtime-visible previous dialogue and never replaces persisted raw Page dialogue. Prompt variants are generated only from current-run Tune aggregate failures; workbook answers are never substituted for Page Summary.

Prompt candidates are lazy production-source specifications. Materialize only the configured screening Sessions first; complete all Tune Sessions only for promoted candidates, and generate held-out Session artifacts only after Tune search has stopped. Persist each Session independently so a resumed run never repeats completed Add, summary, or embedding calls.

### Query prompt

Use when current Queries contain unresolved anaphora/reference structure.

Prefer bounded reference resolution over unconstrained rewriting as the first prompt-based candidate.

### Rules for LLM-generated artifacts

For every variant record:

- prompt text;
- prompt SHA-256;
- model;
- temperature/reasoning settings where applicable;
- source dataset hash;
- source turn IDs;
- generated artifact count;
- cache reuse count.

Reuse a valid same-provenance artifact rather than regenerate it. A different dataset's frozen artifact is a prior only and never makes a new-dataset Branch executable.

## 11. Search algorithm

Default algorithm: **staged successive filtering**.

For each stage:

1. Generate a small candidate set from the current frontier.
2. Evaluate on tune Sessions.
3. Rank by configured R@K.
4. Keep candidates within the configured frontier tolerance of the best, plus any diagnostically unique candidate.
5. Combine only surviving dimensions.
6. Re-run diagnostics on the Tune frontier.
7. Treat patience/frontier convergence as a hard stop only after the current diagnosis has no meaningful untried or refinable budget-eligible Branch. Otherwise record a soft patience event and continue the configured cheap-to-expensive coverage order.

Candidates inherit the current frontier anchor by default so winning retrieval controls, representations and models compose. A baseline-only ablation must set `ablation_from_baseline=true`. Track Branch rounds separately from Branch names: a winning numeric Branch may re-enter for coarse-to-fine refinement, while effective Candidate config hashes prevent duplicate execution. Branch-specific `max_rounds` and global `max_stages` prevent infinite loops.

Validation is not available to this loop. Only after a stop reason is frozen may the frontier be evaluated on held-out Sessions.

### Research decision boundary

Stage 1 remains deterministic `RetrievalControl`. At every Stage 2+ boundary, Python first computes the diagnosis, coverage snapshot, unchanged `registry.select()` deterministic plan, and a hard-constrained legal action space. A Research LLM may then choose only legal `action_id` values; Candidate parameters and execution still come from the registered Branch adapter. Python validates required actions, budget, max rounds, resources, action count and stop legality before execution.

Classify coverage as `required`, `selectable`, or `expensive_gated`. Only required coverage is a hard minimum in Research mode. Not choosing a selectable action is a temporary `DEPRIORITIZED` event, never `EXHAUSTED`; changed Tune evidence makes it eligible again. Python alone sets exhausted/blocked state. Expensive actions become legal when Python hard constraints say they are currently executable (budget, max cost, remaining expensive candidates, max rounds, resources, exhausted/blocked, frontier-anchor requirements). Do not wait for every cheaper/selectable Branch to be consumed. The Research LLM must give a deprioritized reason for every unselected Branch action. Once required coverage is complete and patience/frontier convergence is active, Python may expose a legal stop action so the search remains result-oriented rather than requiring every selectable Branch. If both stop triggers fire, record `frontier_converged` rather than `global_patience`.

The Research prompt contains Tune-only aggregate metrics, config diffs, failure distribution, stage/round history, frontier, diagnosis, deterministic plan, Branch state and hard budgets. Never include held-out Validation, Gold labels/dependencies, answers or future turns. Retry invalid/API responses up to three total attempts, then execute the precomputed `registry.select()` result unchanged. Persist REQUEST before every call and RESPONSE afterwards in `research_trace.jsonl`; cache identical decisions using evidence/legal-action/anchor/registry/Prompt/model hashes.

Avoid Bayesian/grid-search complexity until the parameter surface is actually numeric and cheap enough to justify it.

For continuous numeric parameters, use coarse-to-fine values:

```text
coarse -> keep neighborhood of winner -> refine
```

Example for a weight:

```text
0.0, 0.25, 0.5, 0.75, 1.0
```

then refine only around the best interval.

## 12. Validation

Only a small frontier reaches validation.

Recommended frontier:

- quick: top 2;
- standard: top 3–5;
- deep: top 5 plus robustness variants.

Do not tune on validation feedback. Validation is for selection, not another search loop.

If a candidate wins tune but loses validation substantially, mark `OVERFIT`.

## 13. Selection

Primary:

```text
validation requirement-level R@K
```

Tie handling:

Candidates within `tie_tolerance_pp` are treated as near-tied.

Tie-break:

1. higher Macro/session R@K;
2. lower per-Session variance / fewer catastrophic Sessions;
3. higher MRR;
4. higher R@(2K), then R@(4K);
5. lower LLM calls;
6. lower embedding/runtime cost;
7. simpler configuration.

This prevents selecting a fragile +0.1 pp configuration over a simpler stable one.

## 14. Stop conditions

Stop when any of the following applies:

- budget exhausted;
- deterministic mode: no meaningful tune improvement/frontier change after relevant Branch coverage;
- Research mode: required coverage is complete and a Python-legal stop action is selected after patience/frontier convergence;
- diagnostic evidence says the remaining bottleneck is dataset quality;
- expensive branch cost exceeds configured budget without expected benefit;
- all budget-eligible diagnostic Branches have been attempted or are unavailable.

Always record the stop reason.

For each stage and final stop, record the diagnostic regime plus relevant, attempted (including generation rounds/effective config hashes), exhausted, remaining, revisitable, and budget/resource-blocked Branches. Use `converged_after_relevant_branch_coverage`, `stage_budget_exhausted`, `resource_budget_exhausted`, `no_applicable_branch`, `frontier_converged`, `global_patience`, or `data_artifact_suspicion` as the corresponding auditable terminal reason.

## 15. Resume and cache

A tuning run should be resumable.

Cache keys should include at least:

- dataset hash;
- Session scope;
- query representation;
- page representation;
- prompt/model hash where applicable;
- embedding model;
- retrieval config;
- configured K only when the artifact itself depends on K.

Note: embeddings and generated summaries generally should not be regenerated merely because `K` changed. Rankings may often be reusable for a new K if sufficiently deep candidate lists were stored.

## 16. Required comparison table

For every finalist report:

| Candidate | Tune R@K | Validation R@K | Macro R@K | R@(2K) | R@(4K) | MRR | Cost | Status |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| baseline | | | | | | | | |
| candidate A | | | | | | | | |
| candidate B | | | | | | | | |

Replace `K` with the actual value in rendered output, e.g. `R@5` or `R@10`.
