# Memory Retrieval Tuning Report

## Dataset

- Path: `/home/peize/Code/htzq/mem0/exp/金融分析数据集_前10个Session长短期记忆再平衡.xlsx`
- SHA-256: `161f550721955ec056d0c9612d74eb8cc67d255a8cb36a1412bd8575b201a0f2`
- Sessions / Queries / requirements: 3 / 216 / 225
- Audit: **DATASET_QUALITY_WARNING**
- Primary metric: **R@3**
- Deeper diagnostics: **R@6 / R@12**

## Split

- Method: `leave_one_session_out` (low_leave_one_session_out)
- Tune Sessions: S001_贵州茅台_投研, S002_贵州茅台_内审
- Validation Sessions: every Session held out once across LOSO folds

## Baseline and best stable configuration

- Baseline validation R@3: 0.3981
- Best stable: **baseline**
- Best validation R@3: 0.3981
- Improvement: **+0.00 pp**
- R@6 / R@12: 0.5278 / 0.5556
- MRR: 0.3422
- Macro R@3 / session stddev: 0.4269 / 0.1339

## Memory-layer metrics

- ShortTerm coverage: 0.5200
- MidTerm R@3: 0.3981
- LongTerm R@3: 0.3426 (unchanged_production_baseline)
- Short+target union: 0.7111
- All-memory R@3 / query completion: 0.0933 / 0.0933
- Production baseline layer metrics: `{'evaluated_query_count': 108, 'eligible_requirement_count': 108, 'recall_at_k': 0.39814814814814814, 'recall_at_2k': 0.5277777777777778, 'recall_at_4k': 0.5555555555555556, 'macro_session_recall_at_k': 0.42693236714975846, 'session_stddev': 0.1338810470241012, 'worst_session_recall_at_k': 0.2391304347826087, 'mrr': 0.3422227883119696, 'mean_gold_rank': 3.876923076923077, 'median_gold_rank': 2, 'shortterm_coverage': 0.52, 'target_layer_union': 0.7111111111111111, 'all_memory_union': 0.09333333333333334, 'query_completion': 0.09333333333333334, 'midterm_recall_at_k': 0.39814814814814814, 'longterm_recall_at_k': 0.3425925925925926, 'all_memory_recall_at_k': 0.09333333333333334}`

## Tune and validation finalists

| Candidate | Tune R@3 | Validation R@3 | Macro R@3 | R@6 | R@12 | MRR | Status |
|---|---:|---:|---:|---:|---:|---:|---|
| baseline | 0.5161 | 0.3981 | 0.4269 | 0.5278 | 0.5556 | 0.3422 | VALID |
| frozen:midterm_c3_rebalanced_s001_s010:default | 0.4677 | — | — | — | — | — | NOT_VALIDATED |
| frozen:midterm_positive_transfer_ablation_s001_s010:default | 0.0161 | — | — | — | — | — | NOT_VALIDATED |

## Diagnostics and conditional search

- Classification: `data_artifact_suspicion`
- Evidence: `{'regime': 'data_artifact_suspicion', 'recall_4k_minus_k_pp': 12.903225806451612, 'recall_4k_percent': 64.51612903225806, 'stddev_pp': 2.0833333333333313}`
- Skipped branches: `[{'branch': 'secondary', 'reason': 'diagnostic regime data_artifact_suspicion did not justify it'}, {'branch': 'top_k_sessions', 'reason': 'generic source-turn adapter has no Session router; production trace is read-only'}, {'branch': 'expensive_llm', 'reason': 'quick budget disables new LLM generation'}]`
- Stop reason: `validation_frontier_selected`
- OVERFIT configs: `[]`

## Cost, cache, and parallel execution

- New LLM calls: 0
- Embedding calls: 0
- Reused artifacts: ['470b7a9cc0507b2febd5f0657886cc6158176fd2956edbe7d2dc3180683b9578', '4b9e124fbe80f0b5db26be73521fbb2f7147eb0df5f447b804beb59b29c12271', '7d013255b4df2d54bdcfe3d68b56aaf32049879a532ce0d1d0324340eefb29d1', 'a2e96a531c7106ee381eac0e8ff5c91c1fff6452543507f0b8748432ebae3ecd', 'ed8b3dd6d21d8d91b6afa3b0ba812280a33c323b7e11e811b70f28a4424e4be8']
- Runtime: 7.581s
- Actual concurrency: `{'cpu_count': 8, 'available_memory_gib': 8.972034454345703, 'gpu_count': 2, 'max_parallel_sessions': 2, 'max_parallel_candidates': 2, 'max_parallel_llm_calls': 4, 'adaptive_reductions': []}`
- Estimated serial work: 0.242s
- Estimated parallel time saved: 0.000s

## Dataset warnings

- `EXPLICIT_HISTORY_POSITION_LEAKAGE`: {'code': 'EXPLICIT_HISTORY_POSITION_LEAKAGE', 'ratio': 0.47417840375586856, 'count': 101, 'examples': ['S001-Q006', 'S001-Q008', 'S001-Q010', 'S001-Q012', 'S001-Q014', 'S001-Q016', 'S001-Q018', 'S001-Q020', 'S001-Q022', 'S001-Q024', 'S001-Q026', 'S001-Q028', 'S001-Q030', 'S001-Q032', 'S001-Q034', 'S001-Q036', 'S001-Q038', 'S001-Q040', 'S001-Q042', 'S001-Q044']}

## Limitations

- Baseline backend: `production_trace`. Exact complete production traces are preferred; the source-turn BM25 adapter is used only as a fallback/candidate.
- Frozen rankings are compared only when dataset hash and source IDs validate; incomplete artifacts are excluded.
- New LLM prompt generation is intentionally deferred and skipped unless a repository-native generator with matching provenance is available.
