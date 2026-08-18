# Memory Retrieval Tuning Report

## Dataset

- Path: `/home/peize/Code/htzq/mem0/exp/results/auto_tuning_synthetic_smoke/smoke_dataset.xlsx`
- SHA-256: `fec90195ede22b9adc5428b9f7c91787538f73a55f9a1376492bb81da26f08c4`
- Sessions / Queries / requirements: 3 / 18 / 6
- Audit: **DATASET_QUALITY_WARNING**
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
| baseline | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 0.8333 | VALID |
| top_k_sessions:6 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 0.8333 | VALID |
| max_total_pages:10 | 1.0000 | — | — | — | — | — | NOT_VALIDATED |
| max_total_pages:20 | 1.0000 | — | — | — | — | — | NOT_VALIDATED |
| top_k_pages:10 | 1.0000 | — | — | — | — | — | NOT_VALIDATED |
| top_k_pages:3 | 1.0000 | — | — | — | — | — | NOT_VALIDATED |
| top_k_pages:8 | 1.0000 | — | — | — | — | — | NOT_VALIDATED |
| top_k_sessions:4 | 1.0000 | — | — | — | — | — | NOT_VALIDATED |
| top_k_sessions:7 | 1.0000 | — | — | — | — | — | NOT_VALIDATED |

## Diagnostics and conditional search

- Classification: `data_artifact_suspicion`
- Evidence: `{'regime': 'data_artifact_suspicion', 'recall_4k_minus_k_pp': 0.0, 'recall_4k_percent': 100.0, 'stddev_pp': 0.0}`
- Skipped branches: `[{'branch': 'secondary', 'reason': 'diagnostic regime data_artifact_suspicion did not justify it'}, {'branch': 'session_assignment_thresholds', 'reason': 'changes production Session generation and requires a separately frozen LLM-generated source run'}, {'branch': 'page_or_summary_representation', 'reason': 'changes production Page embeddings/generation; no exact-provenance regenerated artifact was available'}, {'branch': 'alternative_embedding_model', 'reason': 'no exact production-contract embedding checkpoint was available in this run'}, {'branch': 'frozen_replay', 'reason': 'no complete exact-provenance frozen ranking found'}, {'branch': 'expensive_llm', 'reason': 'quick budget disables new LLM generation'}, {'branch': 'final_longterm_union_regression', 'reason': 'no exact complete production full-memory trace; MidTerm source generation intentionally avoided extra LongTerm LLM extraction'}]`
- Stop reason: `validation_frontier_selected`
- OVERFIT configs: `[]`

## Cost, cache, and parallel execution

- New LLM calls: 15
- Embedding calls: 63
- Source generation LLM / embedding calls: 15 / 63
- Reused artifacts: ['433d6c40eefea860c03f2edd5c765e12214a5db9c0433b3a8761c4a577ad2f02', '9b0e1c9e51d4bad0a846160d334dd223bc0263f89b6d3e3918ebdf81b85563e6', 'a92160be4576661833d91f8dde93816758ff2735dc59da7c619826109746bf90']
- Runtime: 47.863s
- Actual concurrency: `{'cpu_count': 8, 'available_memory_gib': 8.705585479736328, 'gpu_count': 1, 'max_parallel_sessions': 3, 'max_parallel_candidates': 2, 'max_parallel_llm_calls': 2, 'adaptive_reductions': [], 'source_worker_parallelism': 2}`
- Estimated serial work: 11.627s
- Estimated parallel time saved: 0.000s

## Dataset warnings

- `DEPENDENCY_DISTANCE_CONCENTRATION`: {'code': 'DEPENDENCY_DISTANCE_CONCENTRATION', 'distance': 4, 'ratio': 1.0, 'count': 6}
- `REPEATED_QUERY_TEMPLATE`: {'code': 'REPEATED_QUERY_TEMPLATE', 'ratio': 1.0, 'count': 18}

## Limitations

- MidTerm baseline backend: `production_midterm`; winner selection uses only production-checkpoint MidTerm validation.
- Full-memory regression baseline backend: `None`; it never enters winner selection.
- MidTerm baseline prompt provenance: `explicit prompt hashes validated`.
- Frozen rankings are eligible only when dataset provenance and the `production_midterm_v1` retrieval contract validate.
- Query/Page/embedding branches without a production-contract adapter are reported as skipped, not evaluated by a surrogate.
- New LLM prompt generation is deferred unless a repository-native generator with exact prompt/model provenance is available.
