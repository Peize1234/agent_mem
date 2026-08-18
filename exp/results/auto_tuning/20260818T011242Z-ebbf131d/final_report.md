# Memory Retrieval Tuning Report

## Dataset

- Path: `/home/peize/Code/htzq/mem0/exp/enterprise_finance_memory_sessions_100_v3_realistic_2023_2025.xlsx`
- SHA-256: `10d07e773464df0e1adf7143b6e7cd136ade506c0d0ff4b61e2ab0951e9c5cfc`
- Sessions / Queries / requirements: 5 / 348 / 559
- Audit: **DATASET_QUALITY_WARNING**
- Primary metric: **R@5**
- Deeper diagnostics: **R@10 / R@20**

## Split

- Method: `deterministic_holdout` (reduced)
- Tune Sessions: S001_贵州茅台_投研, S002_贵州茅台_内审, S005_宁德时代_合规
- Validation Sessions: S003_比亚迪_风委, S004_比亚迪_IR

## Baseline and best stable configuration

- Baseline validation R@5: 0.2297
- Best stable: **frozen:rankings:M2**
- Best validation R@5: 0.2838
- Improvement: **+5.41 pp**
- R@10 / R@20: 0.5405 / 0.7568
- MRR: 0.1845
- Macro R@5 / session stddev: 0.2760 / 0.0573

## Memory-layer metrics

- ShortTerm coverage: 0.7269
- MidTerm R@5: 0.2838
- LongTerm R@5: 0.2483 (unchanged_production_baseline)
- Short+target union: 0.8044
- All-memory R@5 / query completion: N/A / N/A
- Production baseline layer metrics: `{'evaluated_query_count': 97, 'eligible_requirement_count': 149, 'recall_at_k': 0.3221476510067114, 'recall_at_2k': 0.38926174496644295, 'recall_at_4k': 0.5100671140939598, 'macro_session_recall_at_k': 0.3363690476190476, 'session_stddev': 0.09775331938735976, 'worst_session_recall_at_k': 0.21875, 'mrr': 0.18688318057435185, 'mean_gold_rank': 6.407894736842105, 'median_gold_rank': 4.0, 'shortterm_coverage': 0.7334525939177102, 'target_layer_union': 0.8193202146690519, 'all_memory_union': 0.6243291592128801, 'query_completion': 0.6243291592128801, 'midterm_recall_at_k': 0.3221476510067114, 'longterm_recall_at_k': 0.2483221476510067, 'all_memory_recall_at_k': 0.6243291592128801}`

## Tune and validation finalists

| Candidate | Tune R@5 | Validation R@5 | Macro R@5 | R@10 | R@20 | MRR | Status |
|---|---:|---:|---:|---:|---:|---:|---|
| frozen:rankings:M2 | 0.4267 | 0.2838 | 0.2760 | 0.5405 | 0.7568 | 0.1845 | VALID |
| baseline | 0.4133 | 0.2297 | 0.2284 | 0.2703 | 0.3649 | 0.1391 | VALID |

## Diagnostics and conditional search

- Classification: `data_artifact_suspicion`
- Evidence: `{'regime': 'data_artifact_suspicion', 'recall_4k_minus_k_pp': 24.0, 'recall_4k_percent': 65.33333333333333, 'stddev_pp': 5.400617248673218}`
- Skipped branches: `[{'branch': 'secondary', 'reason': 'diagnostic regime data_artifact_suspicion did not justify it'}, {'branch': 'expensive_llm', 'reason': 'quick budget disables new LLM generation'}]`
- Stop reason: `validation_frontier_selected`
- OVERFIT configs: `[]`

## Cost, cache, and parallel execution

- New LLM calls: 0
- Embedding calls: 0
- Reused artifacts: ['0baab51652785fa0d2163135e37bb54d2734ec25c1eb289787a30644af3949e7', '212ce744f7f00b179740a01fff4c7a492e55f269570aace06e21a87611b6528a', 'b4d2ee8896da289aeb4be67db3be74093d35ff216d233c3dcf37eb99298ac61f', 'f98af18495089261860e37eb5b1243854172e8e69b27e9a6fdff45f2fa39ca99']
- Runtime: 133.683s
- Actual concurrency: `{'cpu_count': 8, 'available_memory_gib': 8.721927642822266, 'gpu_count': 2, 'max_parallel_sessions': 3, 'max_parallel_candidates': 2, 'max_parallel_llm_calls': 4, 'adaptive_reductions': []}`
- Estimated serial work: 233.778s
- Estimated parallel time saved: 100.094s

## Dataset warnings

- `DEPENDENCY_DISTANCE_CONCENTRATION`: {'code': 'DEPENDENCY_DISTANCE_CONCENTRATION', 'distance': 1, 'ratio': 0.6081560283687943, 'count': 343}

## Limitations

- Baseline backend: `production_trace`. Exact complete production traces are preferred; the source-turn BM25 adapter is used only as a fallback/candidate.
- Frozen rankings are compared only when dataset hash and source IDs validate; incomplete artifacts are excluded.
- New LLM prompt generation is intentionally deferred and skipped unless a repository-native generator with matching provenance is available.
