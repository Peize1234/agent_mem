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
- Tune Sessions: fold-specific (N-1 Sessions per fold)
- Validation Sessions: every Session held out once across LOSO folds

## Baseline and best stable configuration

- Baseline validation R@5: 0.5278
- Best stable: **baseline**
- Best validation R@5: 0.5278
- Improvement: **+0.00 pp**
- R@10 / R@20: 0.5278 / 0.6019
- MRR: 0.3422
- Macro R@5 / session stddev: 0.5411 / 0.0757

## Memory-layer metrics

- ShortTerm coverage: 0.5200
- MidTerm R@5: 0.5278
- LongTerm R@5: 0.4167 (unchanged_production_baseline)
- Short+target union: 0.7733
- All-memory R@5 / query completion: 0.6756 / 0.6756
- Production baseline layer metrics: `{'evaluated_query_count': 108, 'eligible_requirement_count': 108, 'recall_at_k': 0.5277777777777778, 'recall_at_2k': 0.5277777777777778, 'recall_at_4k': 0.6018518518518519, 'macro_session_recall_at_k': 0.5411263666412408, 'session_stddev': 0.07572747384235104, 'worst_session_recall_at_k': 0.43478260869565216, 'mrr': 0.3422227883119696, 'mean_gold_rank': 3.876923076923077, 'median_gold_rank': 2, 'shortterm_coverage': 0.52, 'target_layer_union': 0.7733333333333333, 'all_memory_union': 0.6755555555555556, 'query_completion': 0.6755555555555556, 'midterm_recall_at_k': 0.5277777777777778, 'longterm_recall_at_k': 0.4166666666666667, 'all_memory_recall_at_k': 0.6755555555555556}`

## Tune and validation finalists

| Candidate | Tune R@5 | Validation R@5 | Macro R@5 | R@10 | R@20 | MRR | Status |
|---|---:|---:|---:|---:|---:|---:|---|
| baseline | 0.5278 | 0.5278 | 0.5411 | 0.5278 | 0.6019 | 0.3422 | VALID |

## Diagnostics and conditional search

- Classification: `data_artifact_suspicion`
- Evidence: `{'regime': 'data_artifact_suspicion', 'recall_4k_minus_k_pp': 7.4074074074074066, 'recall_4k_percent': 60.18518518518518, 'stddev_pp': 7.572747384235104}`
- Skipped branches: `[{'branch': 'secondary', 'reason': 'diagnostic regime data_artifact_suspicion did not justify it'}, {'branch': 'production_contract_cheap_search', 'reason': 'complete production trace exists, but replayable Page/Session checkpoints are unavailable'}, {'branch': 'session_assignment_thresholds', 'reason': 'changes production Session generation and requires a separately frozen LLM-generated source run'}, {'branch': 'page_or_summary_representation', 'reason': 'changes production Page embeddings/generation; no exact-provenance regenerated artifact was available'}, {'branch': 'alternative_embedding_model', 'reason': 'no exact production-contract embedding checkpoint was available in this run'}, {'branch': 'frozen_replay', 'reason': 'no complete exact-provenance frozen ranking found'}, {'branch': 'expensive_llm', 'reason': 'quick budget disables new LLM generation'}]`
- Stop reason: `validation_frontier_selected`
- OVERFIT configs: `[]`

## Cost, cache, and parallel execution

- New LLM calls: 0
- Embedding calls: 0
- Reused artifacts: ['470b7a9cc0507b2febd5f0657886cc6158176fd2956edbe7d2dc3180683b9578', 'a2e96a531c7106ee381eac0e8ff5c91c1fff6452543507f0b8748432ebae3ecd', 'ed8b3dd6d21d8d91b6afa3b0ba812280a33c323b7e11e811b70f28a4424e4be8']
- Runtime: 6.983s
- Actual concurrency: `{'cpu_count': 8, 'available_memory_gib': 8.733318328857422, 'gpu_count': 1, 'max_parallel_sessions': 4, 'max_parallel_candidates': 2, 'max_parallel_llm_calls': 4, 'adaptive_reductions': []}`
- Estimated serial work: 0.145s
- Estimated parallel time saved: 0.000s

## Dataset warnings

- `EXPLICIT_HISTORY_POSITION_LEAKAGE`: {'code': 'EXPLICIT_HISTORY_POSITION_LEAKAGE', 'ratio': 0.47417840375586856, 'count': 101, 'examples': ['S001-Q006', 'S001-Q008', 'S001-Q010', 'S001-Q012', 'S001-Q014', 'S001-Q016', 'S001-Q018', 'S001-Q020', 'S001-Q022', 'S001-Q024', 'S001-Q026', 'S001-Q028', 'S001-Q030', 'S001-Q032', 'S001-Q034', 'S001-Q036', 'S001-Q038', 'S001-Q040', 'S001-Q042', 'S001-Q044']}

## Limitations

- Baseline backend: `production_trace`; no source-turn BM25 fallback is permitted.
- Baseline prompt provenance: `legacy source git commit; explicit prompt hashes unavailable`.
- Frozen rankings are eligible only when dataset provenance and the `production_midterm_v1` retrieval contract validate.
- Query/Page/embedding branches without a production-contract adapter are reported as skipped, not evaluated by a surrogate.
- New LLM prompt generation is deferred unless a repository-native generator with exact prompt/model provenance is available.
