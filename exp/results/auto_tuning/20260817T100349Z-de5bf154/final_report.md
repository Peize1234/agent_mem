# Memory Retrieval Tuning Report

## Dataset

- Path: `/home/peize/Code/htzq/mem0/exp/金融分析数据集_前10个Session长短期记忆再平衡.xlsx`
- SHA-256: `161f550721955ec056d0c9612d74eb8cc67d255a8cb36a1412bd8575b201a0f2`
- Sessions / Queries / requirements: 3 / 216 / 225
- Audit: **DATASET_QUALITY_WARNING**
- Primary metric: **R@5**
- Deeper diagnostics: **R@10 / R@20**

## Split

- Method: `leave_one_session_out` (low_leave_one_session_out)
- Tune Sessions: S001_贵州茅台_投研, S002_贵州茅台_内审
- Validation Sessions: every Session held out once across LOSO folds

## Baseline and best stable configuration

- Baseline validation R@5: 0.5278
- Best stable: **retrieval:question_bm25**
- Best validation R@5: 0.9537
- Improvement: **+42.59 pp**
- R@10 / R@20: 0.9630 / 0.9907
- MRR: 0.8800
- Macro R@5 / session stddev: 0.9525 / 0.0237

## Memory-layer metrics

- ShortTerm coverage: 0.5200
- MidTerm R@5: 0.9537
- LongTerm R@5: 0.4167 (unchanged_production_baseline)
- Short+target union: 0.9778
- All-memory R@5 / query completion: N/A / N/A
- Production baseline layer metrics: `{'evaluated_query_count': 108, 'eligible_requirement_count': 108, 'recall_at_k': 0.5277777777777778, 'recall_at_2k': 0.5277777777777778, 'recall_at_4k': 0.6018518518518519, 'macro_session_recall_at_k': 0.5411263666412408, 'session_stddev': 0.07572747384235104, 'worst_session_recall_at_k': 0.43478260869565216, 'mrr': 0.3468477995092979, 'mean_gold_rank': 9.0125, 'median_gold_rank': 3.0, 'shortterm_coverage': 0.52, 'target_layer_union': 0.7733333333333333, 'all_memory_union': 0.6755555555555556, 'query_completion': 0.6755555555555556, 'midterm_recall_at_k': 0.5277777777777778, 'longterm_recall_at_k': 0.4166666666666667, 'all_memory_recall_at_k': 0.6755555555555556}`

## Tune and validation finalists

| Candidate | Tune R@5 | Validation R@5 | Macro R@5 | R@10 | R@20 | MRR | Status |
|---|---:|---:|---:|---:|---:|---:|---|
| retrieval:question_bm25 | 0.9355 | 0.9537 | 0.9525 | 0.9630 | 0.9907 | 0.8800 | VALID |
| baseline | 0.5968 | 0.5278 | 0.5411 | 0.5278 | 0.6019 | 0.3422 | VALID |
| frozen:midterm_c3_rebalanced_s001_s010:default | 0.6290 | — | — | — | — | — | NOT_VALIDATED |
| frozen:midterm_positive_transfer_ablation_s001_s010:default | 0.1774 | — | — | — | — | — | NOT_VALIDATED |
| offline:original_bm25 | 0.7258 | — | — | — | — | — | NOT_VALIDATED |
| page:summary | 0.6452 | — | — | — | — | — | NOT_VALIDATED |
| page:summary_keywords | 0.7258 | — | — | — | — | — | NOT_VALIDATED |
| page:summary_keywords_raw_user | 0.7258 | — | — | — | — | — | NOT_VALIDATED |
| retrieval:hybrid_w0.50 | 0.9032 | — | — | — | — | — | NOT_VALIDATED |
| retrieval:hybrid_w0.75 | 0.8065 | — | — | — | — | — | NOT_VALIDATED |
| threshold:0.20 | 0.7097 | — | — | — | — | — | NOT_VALIDATED |
| threshold:0.40 | 0.7097 | — | — | — | — | — | NOT_VALIDATED |

## Diagnostics and conditional search

- Classification: `data_artifact_suspicion`
- Evidence: `{'regime': 'data_artifact_suspicion', 'recall_4k_minus_k_pp': 4.838709677419361, 'recall_4k_percent': 98.38709677419355, 'stddev_pp': 1.8640350877193013}`
- Skipped branches: `[{'branch': 'secondary', 'reason': 'diagnostic regime data_artifact_suspicion did not justify it'}, {'branch': 'expensive_llm', 'reason': 'quick budget disables new LLM generation'}]`
- Stop reason: `validation_frontier_selected`
- OVERFIT configs: `[]`

## Cost, cache, and parallel execution

- New LLM calls: 0
- Embedding calls: 0
- Reused artifacts: ['470b7a9cc0507b2febd5f0657886cc6158176fd2956edbe7d2dc3180683b9578', '4b9e124fbe80f0b5db26be73521fbb2f7147eb0df5f447b804beb59b29c12271', '7d013255b4df2d54bdcfe3d68b56aaf32049879a532ce0d1d0324340eefb29d1', 'a2e96a531c7106ee381eac0e8ff5c91c1fff6452543507f0b8748432ebae3ecd', 'ed8b3dd6d21d8d91b6afa3b0ba812280a33c323b7e11e811b70f28a4424e4be8']
- Runtime: 26.457s
- Actual concurrency: `{'cpu_count': 8, 'available_memory_gib': 8.722888946533203, 'gpu_count': 2, 'max_parallel_sessions': 2, 'max_parallel_candidates': 2, 'max_parallel_llm_calls': 4, 'adaptive_reductions': []}`
- Estimated serial work: 0.891s
- Estimated parallel time saved: 0.000s

## Dataset warnings

- `EXPLICIT_HISTORY_POSITION_LEAKAGE`: {'code': 'EXPLICIT_HISTORY_POSITION_LEAKAGE', 'ratio': 0.47417840375586856, 'count': 101, 'examples': ['S001-Q006', 'S001-Q008', 'S001-Q010', 'S001-Q012', 'S001-Q014', 'S001-Q016', 'S001-Q018', 'S001-Q020', 'S001-Q022', 'S001-Q024', 'S001-Q026', 'S001-Q028', 'S001-Q030', 'S001-Q032', 'S001-Q034', 'S001-Q036', 'S001-Q038', 'S001-Q040', 'S001-Q042', 'S001-Q044']}

## Limitations

- Baseline backend: `production_trace`. Exact complete production traces are preferred; the source-turn BM25 adapter is used only as a fallback/candidate.
- Frozen rankings are compared only when dataset hash and source IDs validate; incomplete artifacts are excluded.
- New LLM prompt generation is intentionally deferred and skipped unless a repository-native generator with matching provenance is available.
