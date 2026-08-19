# Memory Retrieval Tuning Report

## Dataset

- Path: `/home/peize/Code/htzq/mem0/exp/results/auto_tuning_synthetic_smoke/smoke_dataset.xlsx`
- SHA-256: `fec90195ede22b9adc5428b9f7c91787538f73a55f9a1376492bb81da26f08c4`
- Sessions / Queries / requirements: 3 / 18 / 6
- Audit: **PASS**
- Primary metric: **R@5**
- Deeper diagnostics: **R@10 / R@20**
- ShortTerm window: **3 QA turns** (`MATCH`)

## Split

- Method: `leave_one_session_out` (low_leave_one_session_out)
- Tune Sessions: fold-specific (N-1 Sessions per fold)
- Validation Sessions: every Session held out once across LOSO folds

## Baseline and best stable configuration

- Baseline validation R@5: 1.0000
- Best stable: **baseline**
- Best validation R@5: 1.0000
- Improvement: **+0.00 pp**
- R@10 / R@20: 1.0000 / 1.0000
- MRR: 0.8333
- Macro R@5 / session stddev: 1.0000 / 0.0000

## Memory-layer metrics

- MidTerm winner R@5: 1.0000
- Full-memory regression: `SKIPPED` (backend: `None`)
- Regression ShortTerm coverage: N/A
- Regression LongTerm R@5: N/A
- Regression Short+Mid union: N/A
- Regression All-memory R@5 / query completion: N/A / N/A
- Regression skipped reason: No complete production trace is available; ShortTerm/LongTerm/All-memory/Union regression was not inferred from MidTerm checkpoints.

## Tune and validation finalists

| Candidate | Tune R@5 | Validation R@5 | Macro R@5 | R@10 | R@20 | MRR | Status |
|---|---:|---:|---:|---:|---:|---:|---|
| HybridRetrieval:dense_weight=0.50:from-a5a269 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 0.8333 | VALID |
| baseline | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 0.8333 | VALID |
| RetrievalControl:max_total_pages=15:from-a5a269 | 1.0000 | — | — | — | — | — | NOT_VALIDATED |
| RetrievalControl:max_total_pages=35:from-a5a269 | 1.0000 | — | — | — | — | — | NOT_VALIDATED |
| RetrievalControl:top_k_pages=10:from-a5a269 | 1.0000 | — | — | — | — | — | NOT_VALIDATED |
| RetrievalControl:top_k_pages=20:from-a5a269 | 1.0000 | — | — | — | — | — | NOT_VALIDATED |
| RetrievalControl:top_k_sessions=4:from-a5a269 | 1.0000 | — | — | — | — | — | NOT_VALIDATED |
| RetrievalControl:top_k_sessions=6:from-a5a269 | 1.0000 | — | — | — | — | — | NOT_VALIDATED |
| RetrievalControl:top_k_sessions=7:from-a5a269 | 1.0000 | — | — | — | — | — | NOT_VALIDATED |

## Staged successive filtering

- Executed stages: 2
- Stop reason: `resource_budget_exhausted`

| Stage | Branches | Candidates | Best Tune R@5 | Improvement pp | Diagnostic after |
|---:|---|---:|---:|---:|---|
| 1 | RetrievalControl | 7 | 1.0000 | +0.00 | balanced_or_plateau |
| 2 | HybridRetrieval | 1 | 1.0000 | +0.00 | balanced_or_plateau |

## Experiment Branches

- Stage 1 `RetrievalControl`: **READY**, candidates=7
- Stage 2 `__research_decision__`: **VALIDATED**, candidates=0, reason=mock runtime follows the deterministic plan
- Stage 2 `HybridRetrieval`: **READY**, candidates=1
- Stage 2 `__search_policy__`: **SEARCH_STOP**, candidates=0, reason=resource_budget_exhausted

## Diagnostics and conditional search

- Classification: `balanced_or_plateau`
- Evidence: `{'after_stage': 2, 'regime': 'balanced_or_plateau', 'recall_4k_minus_k_pp': 0.0, 'recall_4k_percent': 100.0, 'stddev_pp': 0.0, 'worst_session_gap_from_macro_pp': 0.0}`
- Relevant branches: `['RetrievalControl', 'HybridRetrieval', 'QueryRepresentation', 'PageRepresentation']`
- Attempted branches: `{'RetrievalControl': {'attempts': 1, 'max_rounds': 2, 'minimum_attempts': 1, 'rounds': [{'generation_round': 1, 'anchor': 'baseline', 'best_candidate': 'RetrievalControl:top_k_sessions=4:from-a5a269', 'improvement_pp': 0.0, 'candidate_count': 7, 'effective_config_hashes': ['0ff3f3944036e92a6466cd9b5b15dd4023d53438a7c57a8b381044824c72f2a4', '30f69318beb62e84e8fc5a290ba1692305e4366f29094d31d9772b1520e82ba2', '4436166be3dc746c2e859e4450303b7ec1b5b224e7342bd332c81df25902cba5', '47d0a087c4e3c1ecbda80669ea21b44b31860776b61e23295b981f5cbb3c4d1a', '7dee0f17d9125b4794e692aa11b536469e065b5625776a01b8286b456d31b700', '842cf6c657133b1eae233a57197a98994e56280fd656eed4e57572fc417c916b', 'e9c1367ae8243548ad870d8ac4a829022422452aa161a075aacbd33e477d7a0a'], 'frontier_winner': False}]}, 'HybridRetrieval': {'attempts': 1, 'max_rounds': 2, 'minimum_attempts': 0, 'rounds': [{'generation_round': 1, 'anchor': 'baseline', 'best_candidate': 'HybridRetrieval:dense_weight=0.50:from-a5a269', 'improvement_pp': 0.0, 'candidate_count': 1, 'effective_config_hashes': ['bbdc5f8c22455d37186b3ecb8dd9702586a9ad1d095422ce2f25d20de8aadab3'], 'frontier_winner': True}]}}`
- Exhausted branches: `[{'branch': 'RetrievalControl', 'attempts': 1, 'reason': 'no valid Candidate met min_improvement_pp (+0.000 < +0.250)', 'coverage_class': 'required'}, {'branch': 'HybridRetrieval', 'attempts': 1, 'reason': 'no valid Candidate met min_improvement_pp (+0.000 < +0.250)', 'coverage_class': 'selectable'}]`
- Remaining branches: `[]`
- Budget/resource blocked branches: `[{'branch': 'QueryRepresentation', 'attempts': 0, 'reason': 'budget blocks cost=high above max_cost=medium', 'coverage_class': 'selectable'}, {'branch': 'PageRepresentation', 'attempts': 0, 'reason': 'budget blocks cost=high above max_cost=medium', 'coverage_class': 'selectable'}]`
- Relevant branch coverage complete: `True`
- Skipped branches: `[{'branch': 'Reranking', 'status': 'NOT_RELEVANT', 'reason': 'not relevant to final diagnostic=balanced_or_plateau'}, {'branch': 'Embedding', 'status': 'NOT_RELEVANT', 'reason': 'not relevant to final diagnostic=balanced_or_plateau'}, {'branch': 'FieldAwareMultiVector', 'status': 'NOT_RELEVANT', 'reason': 'not relevant to final diagnostic=balanced_or_plateau'}, {'branch': 'QueryRepresentation', 'status': 'BUDGET_OR_RESOURCE_BLOCKED', 'reason': 'budget blocks cost=high above max_cost=medium'}, {'branch': 'PageRepresentation', 'status': 'BUDGET_OR_RESOURCE_BLOCKED', 'reason': 'budget blocks cost=high above max_cost=medium'}, {'branch': 'MemoryWriteAddPrompt', 'status': 'NOT_RELEVANT', 'reason': 'not relevant to final diagnostic=balanced_or_plateau'}, {'branch': 'final_longterm_union_regression', 'reason': 'no exact complete production full-memory trace; MidTerm source generation intentionally avoided extra LongTerm LLM extraction'}]`
- Stop reason: `resource_budget_exhausted`
- OVERFIT configs: `[]`

## Research LLM decisions

- Enabled: `True`
- Decisions / cache hits / deterministic fallbacks: 5 / 1 / 0
- LLM calls (successful / failed): 4 (4 / 0)
- Full attempt trace: `/home/peize/Code/htzq/mem0/exp/results/auto_tuning_research_smoke/20260819T014847Z-0c9525a5/research_trace.jsonl`
- Stage 2 `8f062bbe55c994363dc5054de151d41d891fca3c0c4597ba0ae92166878b0311`: selected=['HybridRetrieval']; deterministic=['HybridRetrieval']; fallback=False; cache_hit=True; reason=mock runtime follows the deterministic plan

## Cost, cache, and parallel execution

- Cumulative LLM calls: 19
- Cumulative embedding calls: 63
- Source generation LLM / embedding calls: 0 / 0
- Branch generation LLM / embedding calls: 0 / 0
- Research decision LLM calls: 4
- Reused artifacts: ['0f639a27fce766fdbdaa483e9cb36ad70cd6ee3b7f9aad54bbe3277fd71e0fb3', '10b82a37b70d9ef055a3149b58214a5dbdfc464842758381ab56ed289fdd79d7', 'ba3f4a58764ea1e5a3707d41349b40dd284acf51389e5b03406dbd73e4cf2d72']
- Model discovery: `/home/peize/Code/htzq/mem0/exp/results/auto_tuning_research_smoke/20260819T014847Z-0c9525a5/model_discovery.json`
- Runtime: 89.544s
- Actual concurrency: `{'cpu_count': 8, 'available_memory_gib': 8.926197052001953, 'gpu_count': 1, 'max_parallel_sessions': 2, 'max_parallel_candidates': 2, 'max_parallel_llm_calls': 2, 'adaptive_reductions': [], 'source_worker_parallelism': 0}`
- Estimated serial work: 7.871s
- Estimated parallel time saved: 0.000s

## Dataset warnings

- None

## Limitations

- MidTerm baseline backend: `production_midterm`; winner selection uses only production-checkpoint MidTerm validation.
- Full-memory regression baseline backend: `None`; it never enters winner selection.
- MidTerm baseline prompt provenance: `explicit prompt hashes validated`.
- Frozen rankings are eligible only when dataset provenance and the `production_midterm_v1` retrieval contract validate.
- Query/Page/embedding variants are derived from production MidTerm checkpoints and remain explicitly marked as Benchmark candidates, not production behavior.
- Query Prompt generation requires standard/deep budget; Add/Page Prompt generation requires deep budget. Missing API/model resources are recorded as unavailable rather than replaced by surrogate artifacts.
