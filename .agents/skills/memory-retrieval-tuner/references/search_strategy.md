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

Never copy the historical sequence of successful changes as if it were universally optimal.

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
- at most 5 Tune stages and 2 Branches per stage;
- validate top 3–5.

### deep

For final research runs.

- wider cheap search;
- multiple justified secondary branches;
- expensive prompt variants with exact provenance;
- online model discovery/download after metadata screening;
- at most 8 Tune stages and 3 Branches per stage;
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

Candidate order:

1. `original`;
2. `bounded_reference_resolution` if an exact-dataset frozen query artifact is available; embed it once with the production model and cache the vector derivative;
3. other low-risk query representations already present in the repository.

Do not assume bounded reference resolution is better.

Avoid generic free-form rewrite by default unless diagnostics justify it.

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

Start with the current production model. Scan the local Hugging Face cache first. Under `budget=deep`, dynamically search model metadata when local candidates are insufficient. Select up to roughly two suitable multilingual/general and two finance-domain candidates, without padding the pool with poor fits.

For each candidate record model ID, immutable revision, model type, source, License, selection rationale, resource estimate and cache/download state. Run metadata screening, download/cache validation, smoke testing, Tune-subset screening, then full Tune only for survivors. A gated model, unavailable dependency or resource failure is `UNAVAILABLE`, not a run failure.

A new embedding model must use the same text representation and evaluation contract.

### Lexical / hybrid retrieval

Search when:

- exact entities/numbers/terms matter;
- dense retrieval misses lexically obvious Gold;
- existing BM25/fusion code can be reused.

Search dense-heavy fusion weights first.

### Reranking

Search when R@(4K) is strong but R@K is weak.

Do not rerank a tiny candidate pool that already excludes the Gold.

Use the production-routed Page pool. The built-in lightweight field-aware reranker is always available; cross-encoder candidates use the same local-first/dynamic discovery contract as embedding models.

### Field-aware / multi-vector

Use weighted production Page fields for the lightweight candidate. `multi_vector_maxsim` requires provenance-keyed field vectors and is a deep/high-cost variant. Do not label either variant as production behavior.

### Branch Registry contract

The orchestrator does not construct Branch-specific candidates. Every registered Branch declares:

- name and diagnostic regimes;
- cost level and resource requirements;
- required artifacts;
- candidate generator and execution adapter;
- provenance contract.

Built-in Branches are RetrievalControl, QueryRepresentation, PageRepresentation, HybridRetrieval, Reranking, Embedding, FieldAwareMultiVector and MemoryWriteAddPrompt. Add a new adapter to the registry when a diagnosis requires an unsupported technique; do not add another conditional candidate block to the orchestrator.

## 10. Stage D — expensive LLM search

Only run when the top candidates justify the cost.

### Memory-write / Add prompt

Use when Page content itself is missing dependency-bearing evidence.

Variants may include:

- production Add;
- conservative/local-context Add;
- context-aware Add;
- task/evidence-focused summary.

Historical variants are priors, not guaranteed improvements.

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

Reuse a valid frozen artifact rather than regenerate it.

## 11. Search algorithm

Default algorithm: **staged successive filtering**.

For each stage:

1. Generate a small candidate set from the current frontier.
2. Evaluate on tune Sessions.
3. Rank by configured R@K.
4. Keep candidates within the configured frontier tolerance of the best, plus any diagnostically unique candidate.
5. Combine only surviving dimensions.
6. Re-run diagnostics on the Tune frontier.
7. Stop when `min_improvement_pp`/`patience_stages`, frontier convergence, budget, data quality, or resource constraints say to stop.

Validation is not available to this loop. Only after a stop reason is frozen may the frontier be evaluated on held-out Sessions.

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
- no meaningful tune improvement for the configured patience;
- frontier converged;
- diagnostic evidence says the remaining bottleneck is dataset quality;
- expensive branch cost exceeds configured budget without expected benefit;
- all budget-eligible diagnostic Branches have been attempted or are unavailable.

Always record the stop reason.

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
