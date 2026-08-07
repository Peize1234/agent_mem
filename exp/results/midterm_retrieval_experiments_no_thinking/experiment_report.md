# S001 no-thinking Mid-term Page Retrieval Experiment Report

本报告只使用阶段 0 已完成的 S001 产物。14 个 evaluated Query 均按原运行时序过滤候选；47 个最终 Page 从未被直接用于早期 Query。统一主口径是 **query 时刻已 committed 的 exact Gold Page micro Recall**，共 21 个 Gold。旧 benchmark 把仍在短期层的 Gold 也计入 Page denominator，因此另列为 legacy 口径，不能与这里混用。

## 1. No-thinking Baseline

- Exact Page R@5 / R@10 / R@20：33.33% / 57.14% / 85.71%
- MRR：0.3322；Mean Gold Rank：10.24
- Stage-0 legacy Page macro/micro R@5：20.24% / 19.44%
- 与旧 thinking snapshot 的 eligible-Gold R@5 28.57% 相比，no-thinking 变化 +4.76 pp。Page 摘要随 thinking mode 改变，排名分布也发生变化。
- 新 evaluator 对 14/14 Query 的生产 Top-5 完全复现：`MATCHED_14_OF_14`。

## 2. Candidate Retrieval 是否已经足够

Candidate R@20 为 **85.71%**。Gold rank 分桶：{"1-5": 7, "6-10": 5, "11-20": 6, "21-30": 3, ">30 / not found": 0}。大量 Gold 位于 rank 6–20，candidate generation 尚可，fine ranking 是主要瓶颈之一。

## 3. Query 是否仍然是主瓶颈

| Variant | Query | R@5 | R@20 | MRR | Avg chars | Truncation expected | Status |
|---|---|---:|---:|---:|---:|---|---|
| Q0 | current user query | 33.33% | 85.71% | 0.3322 | 49 | False | AVAILABLE |
| Q1 | current + previous 1 QA | 23.81% | 85.71% | 0.3254 | 9828 | True | AVAILABLE |
| Q2 | current + previous 2 QA | 23.81% | 85.71% | 0.3254 | 19528 | True | AVAILABLE |
| Q3 | current + previous 3 QA | 23.81% | 85.71% | 0.3254 | 29238 | True | AVAILABLE |
| Q4 | DeepSeek no-thinking standalone rewrite | 33.33% | 80.95% | 0.2308 | 68 | False | AVAILABLE |
| Q5 | original + standalone rewrite | 38.10% | 80.95% | 0.2179 | 152 | False | AVAILABLE |
| Q6 | two-query RRF (one no-thinking LLM call) | 28.57% | 85.71% | 0.2564 | 85 | False | AVAILABLE |
| QO | required_context Oracle (not deployable) | 76.19% | 95.24% | 0.8269 | 206 | False | AVAILABLE |

最佳可部署 Query 是 `Q5`，单独替换 Query 的 R@5 增益为 +4.76 pp。Q1–Q3 保留完整 QA 文本，但当前 Query 被置于最前，最近 QA 优先；生产 E0 的 max sequence length 为 512，表中的长文本会被模型截断，因此增加更多完整 QA 不等于模型实际看见更多轮次。Oracle 只表示理想 Query 上界，不是可部署方案，也不计入 Top-2 或实际增益。

## 4. Page Representation 是否是主瓶颈

| Variant | Page text | R@5 | MRR | Mean pairwise cosine |
|---|---|---:|---:|---:|
| P0 | production: summary + keywords + user_input | 33.33% | 0.3322 | 0.8598 |
| P1 | summary | 33.33% | 0.2963 | 0.8642 |
| P2 | user_input | 19.05% | 0.1500 | 0.5817 |
| P3 | raw_dialogue | 23.81% | 0.1941 | 0.8282 |
| P4 | user_input + assistant_response | 23.81% | 0.1941 | 0.8282 |
| P5 | user_input + summary | 33.33% | 0.2542 | 0.8042 |
| P6 | summary + raw_dialogue | 28.57% | 0.2650 | 0.8365 |
| P7 | user_input + keywords | 23.81% | 0.1749 | 0.7912 |
| P8 | summary + keywords | 33.33% | 0.3813 | 0.8816 |

最佳 Page representation 是 `P8`，固定 Q0/E0 时增益 +0.00 pp。Pairwise cosine 只用于同质化诊断，选择仍以 Recall/Rank 为准。

## 5. Embedding 模型影响

| ID | Model | Query | Page | R@5 | MRR | Dim | Status |
|---|---|---|---|---:|---:|---:|---|
| E0 | BAAI/bge-small-zh-v1.5 | Q5 | P8 | 38.10% | 0.2679 | 512 | AVAILABLE |
| E0 | BAAI/bge-small-zh-v1.5 | Q5 | P0 | 38.10% | 0.2179 | 512 | AVAILABLE |
| E0 | BAAI/bge-small-zh-v1.5 | Q0 | P8 | 33.33% | 0.3813 | 512 | AVAILABLE |
| E0 | BAAI/bge-small-zh-v1.5 | Q0 | P0 | 33.33% | 0.3322 | 512 | AVAILABLE |
| E1 | BAAI/bge-base-zh-v1.5 | Q5 | P8 | 33.33% | 0.2079 | 768 | AVAILABLE |
| E1 | BAAI/bge-base-zh-v1.5 | Q5 | P0 | 38.10% | 0.2155 | 768 | AVAILABLE |
| E1 | BAAI/bge-base-zh-v1.5 | Q0 | P8 | 38.10% | 0.2791 | 768 | AVAILABLE |
| E1 | BAAI/bge-base-zh-v1.5 | Q0 | P0 | 38.10% | 0.2609 | 768 | AVAILABLE |
| E2 | BAAI/bge-large-zh-v1.5 | Q5 | P8 | 33.33% | 0.2509 | 1024 | AVAILABLE |
| E2 | BAAI/bge-large-zh-v1.5 | Q5 | P0 | 19.05% | 0.1994 | 1024 | AVAILABLE |
| E2 | BAAI/bge-large-zh-v1.5 | Q0 | P8 | 23.81% | 0.2958 | 1024 | AVAILABLE |
| E2 | BAAI/bge-large-zh-v1.5 | Q0 | P0 | 28.57% | 0.2219 | 1024 | AVAILABLE |
| E3 | BAAI/bge-m3 | Q5 | P8 | 28.57% | 0.2237 | 1024 | AVAILABLE |
| E3 | BAAI/bge-m3 | Q5 | P0 | 23.81% | 0.2312 | 1024 | AVAILABLE |
| E3 | BAAI/bge-m3 | Q0 | P8 | 23.81% | 0.1861 | 1024 | AVAILABLE |
| E3 | BAAI/bge-m3 | Q0 | P0 | 14.29% | 0.1689 | 1024 | AVAILABLE |

筛选组合下最佳 embedding 为 `E1` / `BAAI/bge-base-zh-v1.5`；相对 baseline 的表观增益为 +4.76 pp。严格固定 Q0/P0 时，最佳模型 `E1` / `BAAI/bge-base-zh-v1.5` 的单纯 embedding 增益为 +4.76 pp。

## 6. Hybrid 是否有效

中文 BM25 sanity case 全部通过后才执行下表；分词使用仓库 `lemmatize_for_bm25(..., language='zh')`，没有沿用旧的全零 sparse 结论。

| Scope | Strategy | R@5 | Candidate R@10 | Candidate R@20 | Candidate R@30 |
|---|---|---:|---:|---:|---:|
| front | H0_dense | 38.10% | 57.14% | 85.71% | 100.00% |
| front | H1_bm25 | 23.81% | 52.38% | 90.48% | 100.00% |
| front | H2_rrf | 33.33% | 57.14% | 80.95% | 100.00% |
| front | H3_dense_0.8_bm25_0.2 | 38.10% | 57.14% | 85.71% | 100.00% |
| front | H4_dense_0.6_bm25_0.4 | 33.33% | 57.14% | 85.71% | 100.00% |
| hybrid_only | H0_dense | 33.33% | 57.14% | 85.71% | 100.00% |
| hybrid_only | H1_bm25 | 19.05% | 47.62% | 85.71% | 100.00% |
| hybrid_only | H2_rrf | 33.33% | 47.62% | 90.48% | 100.00% |
| hybrid_only | H3_dense_0.8_bm25_0.2 | 33.33% | 61.90% | 80.95% | 100.00% |
| hybrid_only | H4_dense_0.6_bm25_0.4 | 33.33% | 47.62% | 85.71% | 100.00% |

最佳 candidate strategy 是 `H1_bm25`，其 Candidate R@20 为 90.48%。严格固定 Q0/P0/E0 的 hybrid-only 最佳方案为 `H0_dense`，R@5 单项增益 +0.00 pp。

## 7. Rerank 是否有效

默认 `candidate_k=20`、`output_k=5`，二者在实现和输出中完全分离。

| Reranker | Candidate R@20 | Output R@5 | MRR | rerank p95 ms | Status |
|---|---:|---:|---:|---:|---|
| R0_no_rerank | 90.48% | 23.81% | 0.2108 | 0.0 | AVAILABLE |
| R1_local_cross_encoder | 90.48% | 42.86% | 0.3062 | 1427.4 | AVAILABLE |
| R2_llm_no_thinking | 90.48% | 47.62% | 0.2993 | 1569.0 | AVAILABLE |

最佳 reranker 是 `R2_llm_no_thinking`；相对同一 candidate 排名直接取前 5 的增益为 +23.81 pp。rank 6–20 的逐例 before/after 位于 `per_query_results.csv`。

## 8. 最佳单项修改

- Query：+4.76 pp
- Page representation：+0.00 pp
- Embedding（固定 Q0/P0）：+4.76 pp
- Hybrid（固定 Q0/P0/E0）：+0.00 pp
- Reranker（相对同候选 R0）：+23.81 pp
- Candidate expansion + local reranker（固定 baseline Q0/P0/E0）：-4.76 pp

最大观察到的单层增益来自 **Rerank on screened candidates：+23.81 pp**。

## 9. 最佳组合

| Scheme | R@5 | MRR | Candidate R@20 | mean ms | p95 ms | online LLM calls/query |
|---|---:|---:|---:|---:|---:|---:|
| C0_baseline | 33.33% | 0.3322 | 85.71% | 46.0 | 174.0 | 0.0 |
| C1_best_query_only | 38.10% | 0.2179 | 80.95% | 1396.9 | 1805.1 | 1.0 |
| C2_best_representation_embedding_only | 38.10% | 0.2791 | 85.71% | 37.7 | 51.3 | 0.0 |
| C3_candidate_expansion_rerank_only | 28.57% | 0.2429 | 85.71% | 1083.3 | 1425.5 | 0.0 |
| C4_best_no_online_llm | 42.86% | 0.3062 | 90.48% | 1028.8 | 1409.4 | 0.0 |
| C5_best_quality | 47.62% | 0.2993 | 90.48% | 1263.9 | 1709.0 | 1.0 |
| C6_best_practical | 38.10% | 0.2791 | 85.71% | 37.7 | 51.3 | 0.0 |

- Best Quality：`Q0 + P8 + E1 + H1_bm25 + R2_llm_no_thinking`，R@5 47.62%，相对 baseline +14.29 pp。
- Best No-Online-LLM：`Q0 + P8 + E1 + H1_bm25 + local_cross_encoder`，R@5 42.86%。
- Recommended Production/Pareto：`Low-latency Pareto selection of C2_best_representation_embedding_only under p95<=217.4 ms: Q0 + P8 + E1 + dense top5`，R@5 38.10%。

## 10. 性能

Baseline 离线检索 mean/p95 为 46.0/174.0 ms。Best Quality mean/p95 为 1263.9/1709.0 ms，mean 相对 baseline +1217.9 ms，在线 LLM 1.0 次/query。Best No-Online-LLM p95 为 1409.4 ms；Pareto p95 为 51.3 ms。

DeepSeek no-thinking Q4 rewrite mean/p95 为 1380.2/1796.0 ms，平均 prompt/completion tokens 为 17257.9/45.5；Q6 multi-query mean/p95 为 1615.5/1927.2 ms。详细分阶段 mean/p50/p90/p95/max 在 `latency_summary.csv`，rerank 与 embedding 构建/Query 编码开销分别在对应 ablation CSV。

本地延迟来自当前单机 CPU 的逐 Query 单次测量，适合方案内相对比较但样本仅 14 条；上线前仍需在目标硬件做稳定压测。LLM 延迟和 token 取自首次成功 API 调用，缓存复跑没有把 cache hit 误记为零成本在线调用。

## 11. 是否应该继续改 Page Prompt

**暂时不需要继续修改 Page Prompt**。理想 Query 下 Candidate Recall@20 已达到阈值，且 rerank 能把候选中的 Gold 推入 Top-5；现有 Page 信息基本可用，应先优化 retrieval pipeline。 理想 Query + best representation/embedding/hybrid 的 Candidate R@20 为 100.00%；当前最佳 candidate R@20 为 95.24%。

## 12. 下一步生产修改建议

1. 先在实验/灰度层验证本报告的 Pareto 方案，并把 `candidate_k` 与 `output_k` 在设计中明确拆开；本任务不修改生产行为。
2. `Q5` 是 Query screening 的最高 R@5，但 Q4 Standalone Rewrite 无增益、Q6 Multi-query 下降，Q5 也牺牲 Candidate R@20/MRR 且增加约一次在线 LLM；因此推荐方案保持 Q0，没有证据支持仅为 Query 构建新增在线 LLM。
3. 对 `R2_llm_no_thinking` 做小流量 candidate expansion + rerank 验证，并把其延迟与失败回退纳入上线门槛。
4. 正确中文 BM25 仅改善 Candidate R@20、未改善 baseline R@5，暂不建议把 Hybrid 直接写入生产默认路径。
5. Oracle required_context 绝不能进入生产；当前证据也不支持继续消耗成本重写 Page Prompt。
6. Baseline miss 自动根因计数为 {"MULTI_FACTOR": 7, "CANDIDATE_DEPTH": 5, "UNRESOLVED": 2}；逐 Gold 证据在 `miss_root_cause.csv`，生产修改应优先覆盖占比最高且有单项 rescue 证据的类别。
