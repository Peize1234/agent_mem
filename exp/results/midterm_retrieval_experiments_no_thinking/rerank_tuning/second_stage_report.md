# S001 Mid-term Page Rerank Tuning — Second Stage

## 1. Baseline 校验

复用第一轮 immutable snapshot、Q0 embedding 与 Page stored embedding 后，Baseline 仍为 R@5 **33.33%**、R@20 **85.71%**、MRR 0.3322、NDCG@5 0.2644。逐 Query Top-K 与第一轮一致，校验状态：`PASS`。

## 2. Candidate Source

相同最终候选预算下，覆盖率最高的受控 candidate 配置是 `Q0 + P8 union_fixed Top20`：Candidate Recall **95.24%**（20/21），平均实际候选 16.00。Dense、BM25、固定预算 Union、扩大 Union、RRF60、0.8/0.2 与 0.6/0.4 normalized fusion 均已在 K=10/15/20/30 比较；完整数值见 `rerank_candidate_tuning.csv`。

扩大 Union 的 K 是“每路 K”，不是最终预算；它只作为 coverage 上界，未混入固定预算生产候选比较。

| P8 / K=20 source | Candidate Recall | Mean actual K | Pre-rerank R@5 |
|---|---:|---:|---:|
| dense | 80.95% | 16.00 | 33.33% |
| bm25 | 90.48% | 16.00 | 23.81% |
| union_fixed | 95.24% | 16.00 | 33.33% |
| rrf60 | 90.48% | 16.00 | 33.33% |
| weighted_dense_0.8 | 80.95% | 16.00 | 33.33% |
| weighted_dense_0.6 | 85.71% | 16.00 | 33.33% |
| union_expanded | 100.00% | 20.29 | 33.33% |

## 3. Dense / BM25 Complementarity

在 `P8`、K=20 下：Dense-only 2、BM25-only 4、Both 15、Neither 0。因此 Dense/BM25 **存在双向互补**。逐 Gold rank 与 hit 见 `candidate_complementarity.csv`。

## 4. candidate_k

K=10/15/20/30 的 candidate coverage、最终 local/DeepSeek R@5 与 warm latency/token 已分开统计。推荐 K 为 **20**；判断依据不是只看 coverage，而是更大 K 是否真正增加最终 Top-5 命中。

| K | 同预算 coverage 最佳 source | Candidate Recall | Gold | Mean actual K |
|---:|---|---:|---:|---:|
| 10 | P8 dense | 66.67% | 14/21 | 9.21 |
| 15 | P8 dense | 80.95% | 17/21 | 12.79 |
| 20 | P8 union_fixed | 95.24% | 20/21 | 16.00 |
| 30 | P8 dense | 100.00% | 21/21 | 20.64 |

| Local K（固定 P0 RRF60 + P8 rerank） | Candidate Recall | R@5 | MRR | NDCG@5 | Warm mean ms | Warm p95 ms |
|---:|---:|---:|---:|---:|---:|---:|
| 10 | 47.62% | 38.10% | 0.2521 | 0.2269 | 513.6 | 632.5 |
| 15 | 66.67% | 42.86% | 0.2786 | 0.2700 | 738.4 | 1019.7 |
| 20 | 90.48% | 47.62% | 0.3098 | 0.2976 | 938.2 | 1359.8 |
| 30 | 100.00% | 42.86% | 0.2928 | 0.2713 | 1244.4 | 2051.6 |

K=20 是唯一达到 10/21 的 local 配置；K=15 为 9/21，K=10 为 8/21，K=30 又回落到 9/21。因此不能把 K20 无损降到 10/15，K30 也没有最终 Recall 收益。

## 5. Reranker Representation

P0、P1、P8 作为 reranker 输入独立于 candidate representation 比较。最佳 local rerank representation 为 `P8`；最佳 DeepSeek representation 为 `P8`。P3 raw dialogue 平均约 9.7k 字，明显违背本轮低成本目标，因此没有进入主网格。

| Local rerank representation | R@5 | MRR | NDCG@5 |
|---|---:|---:|---:|
| P0 | 28.57% | 0.2303 | 0.1867 |
| P1 | 42.86% | 0.2381 | 0.2433 |
| P8 | 47.62% | 0.3098 | 0.2976 |

| DeepSeek rerank representation（P8 BM25 K20） | R@5 | MRR | NDCG@5 | Mean tokens | P95 tokens |
|---|---:|---:|---:|---:|---:|
| P0 | 38.10% | 0.2145 | 0.2019 | 5252 | 6691 |
| P1 | 23.81% | 0.1759 | 0.1322 | 4152 | 5300 |
| P8 | 47.62% | 0.2993 | 0.2769 | 4759 | 6058 |

## 6. Local Reranker

最佳无在线 LLM 链路：`Q0 + P0 rrf60 Top20 + P8 BAAI/bge-reranker-base -> Top5`。R@5 **47.62%**，相对生产 Baseline **+14.29 pp**，MRR 0.3098，NDCG@5 0.2976，warm mean 938.2 ms，warm p95 1359.8 ms，online LLM calls/query = 0。

该 reranker 将 5 个 Gold 推入 Top-5，同时把 2 个原 Top-5 Gold 推出，净增 3 个。Recall 33.33%→47.62%，但 MRR 0.3322→0.3098；原因是 micro Gold 命中净增，而若干 Query 的首个高位 Gold 被推后。NDCG@5 0.2644→0.2976，说明 Top-5 整体仍改善。逐 Gold 见 `rerank_case_analysis.csv`。

更强本地模型状态与是否值得使用：BAAI/bge-reranker-large 相对 base -3 Gold，单次缓存构建 mean 51361.2 ms；未达到 +2 Gold，当前不推荐增加模型成本。 其 R@5 为 33.33%，mean/p95 为 51361.2/150396.2 ms，首次下载+加载约 620002.7 ms；不值得替换 base。

## 7. DeepSeek no-thinking Reranker

最佳链路：`Q0 + P8 bm25 Top20 + P8 deepseek-v4-flash no-thinking -> Top5`。R@5 **47.62%**，相对生产 Baseline **+14.29 pp**，MRR 0.2993，NDCG@5 0.2769；平均/p95 prompt tokens 为 4759/6058，平均/p95 LLM latency 为 1151.4/1569.0 ms，1 次 no-thinking LLM/query。

| Candidate source（K20/P8 rerank） | Candidate Recall | R@5 | MRR |
|---|---:|---:|---:|
| P8 bm25 | 90.48% | 47.62% | 0.2993 |
| P8 dense | 80.95% | 28.57% | 0.1888 |
| P8 union_fixed | 95.24% | 28.57% | 0.1777 |
| P8 rrf60 | 90.48% | 28.57% | 0.2311 |
| P8 weighted_dense_0.8 | 80.95% | 19.05% | 0.1638 |
| P8 weighted_dense_0.6 | 85.71% | 28.57% | 0.2327 |
| P0 rrf60 | 90.48% | 28.57% | 0.2504 |

| DeepSeek K（P8 BM25/P8） | Candidate Recall | R@5 | Mean tokens | LLM p95 ms |
|---:|---:|---:|---:|---:|
| 10 | 52.38% | 33.33% | 2838 | 1697.8 |
| 15 | 71.43% | 33.33% | 3825 | 1973.8 |
| 20 | 90.48% | 47.62% | 4759 | 1569.0 |
| 30 | 100.00% | 38.10% | 6068 | 1665.3 |

P8 相比 P0 同时提高 DeepSeek R@5（47.62% vs 38.10%）并减少约 9.4% mean prompt tokens；P1 更短但 R@5 只有 23.81%，不能用 token 节省抵消质量损失。K20 最佳；K30 coverage 达到 100% 但 R@5 降为 38.10%。

## 8. Local vs DeepSeek

DeepSeek 相比最佳 local 多命中 **0** 个 available Gold。Local/DeepSeek 分别有 5/5 个 Gold 被推进 Top-5、2/2 个原命中被推出。两者 R@5 相同，但 local 的 MRR/NDCG 更高且 0 在线 LLM，因此额外约一次在线调用不值得。

## 9. Lightweight Query Contextualization

LQ4 相对同一最佳 local 链路的 Q0 改变 -3 个 Gold。未达到 +2 Gold 门槛，停止 Query Rewrite 路线。 LQ1/LQ2 只加入历史 User Question；LQ4 只看当前问题与最近最多 3 个 User Question，不含 Assistant 长回答、Gold、required_context 或未来消息。5 条固定样本人工审计为 4 PASS / 1 FAIL；Q011 错加了输入中不存在的 2022 年锚点，因此当前 rewrite 方案判定为 **UNSTABLE**。明细见 `lightweight_query_audit.csv`。

| Query variant / chain | R@5 | MRR | Rewrite LLM calls/query |
|---|---:|---:|---:|
| LQ0 | 47.62% | 0.3098 | 0.0 |
| LQ1 | 38.10% | 0.3244 | 0.0 |
| LQ2 | 42.86% | 0.1896 | 0.0 |
| LQ4 | 33.33% | 0.2397 | 1.0 |
| LQ4_BEST_QUALITY | 38.10% | 0.3395 | 1.0 |

## 10. 最佳方案

- Best Quality: `Q0 + P0 rrf60 Top20 + P8 BAAI/bge-reranker-base -> Top5` — R@5 47.62%，MRR 0.3098。
- Best Local / No-Online-LLM: `Q0 + P0 rrf60 Top20 + P8 BAAI/bge-reranker-base -> Top5` — R@5 47.62%。
- Best Low-Latency: `Q0 + P0 rrf60 Top10 + P8 BAAI/bge-reranker-base -> Top5` — R@5 38.10%，p95 632.5 ms。
- Recommended Pareto: `Q0 + P0 rrf60 Top15 + P8 BAAI/bge-reranker-base -> Top5` — R@5 42.86%。

Pareto 规则是：R@5 至少达到 Best Quality 减 1 个 Gold（1/21），满足后优先 0 在线 LLM，再按 warm p95、token/memory cost 排序。

| Scheme | R@5 | MRR | Total mean ms | Conservative p95 ms | LLM calls/query | Mean prompt tokens |
|---|---:|---:|---:|---:|---:|---:|
| baseline | 33.33% | 0.3322 | 17.7 | 40.8 | 0 | 0 |
| best_local | 47.62% | 0.3098 | 1049.7 | 1593.3 | 0 | 0 |
| best_low_latency | 38.10% | 0.2521 | 618.2 | 837.9 | 0 | 0 |
| recommended_pareto | 42.86% | 0.2786 | 842.6 | 1227.0 | 0 | 0 |
| best_deepseek | 47.62% | 0.2993 | 1194.5 | 1658.1 | 1 | 4759 |

总延迟把 query embedding、candidate retrieval、rerank 相加；p95 是各段 p95 相加的保守上界。模型下载/加载与 3 次 warmup 不计入 warm latency。Base reranker load 约 5.8s；BGE-small load 约 4.8s。

## 11. 是否值得修改生产

**YES**。S001 上无在线 LLM 链路至少稳定增加 2 个 available Gold，且 candidate_k/representation/rank movement 均已有受控证据；建议进入多 Session 小流量复验后再改默认链路。

本任务没有修改生产链路。Page Prompt 仍建议暂不修改：当前诊断的主要证据继续指向 candidate coverage 后的 Top-5 fine ranking，且本轮只调 candidate/rerank/query 构造，没有重新生成 Page。

## 12. 验收问题逐项结论

1. Dense 和 BM25 **真正互补**：P8/K20 下 Dense-only 2、BM25-only 4、Both 15、Neither 0。
2. 给 reranker 的最佳 coverage pool 是 `P8 union_fixed Top20`（20/21）；但最终 local 最佳 source 是 `P0 RRF60 Top20`，DeepSeek 最佳是 `P8 BM25 Top20`。coverage 最优不能替代 rerank 后实测。
3. 最佳完整质量 candidate_k 是 **20**；Pareto K 是 **15**。
4. 最佳 reranker Page representation 是 **P8**（local 与 DeepSeek 一致）。
5. `bge-reranker-base` 最佳 R@5 是 **47.62%**，即 10/21、相对生产 +14.29 pp。
6. `bge-reranker-large` 不值得：R@5 33.33%，比 base 少 3 Gold 且延迟显著恶化。
7. DeepSeek no-thinking 最佳 R@5 是 **47.62%**，即 10/21、相对生产 +14.29 pp。
8. DeepSeek 比最佳 local 多召回 **0** 个 Gold。
9. 该差异不值得约 1 次在线 LLM：Recall 持平，local MRR/NDCG 更高。
10. K20 不能无损降到 K10/15：分别损失 2/1 个 Gold；若允许少 1 Gold，K15 是规则化 Pareto。
11. Lightweight contextualization 无效且不稳定：Best Quality local 上 LQ4 比 Q0 少 3 个 Gold，5 条审计中 1 条添加了不存在的年份，停止 rewrite 路线。
12. Best Quality 是 `Q0 + P0 rrf60 Top20 + P8 BAAI/bge-reranker-base -> Top5`。
13. Best No-Online-LLM 是 `Q0 + P0 rrf60 Top20 + P8 BAAI/bge-reranker-base -> Top5`。
14. Best Low-Latency 是 `Q0 + P0 rrf60 Top10 + P8 BAAI/bge-reranker-base -> Top5`。
15. Recommended Pareto 是 `Q0 + P0 rrf60 Top15 + P8 BAAI/bge-reranker-base -> Top5`。
16. 推荐链真实组件只有：Q0、P0 dense+中文 BM25 RRF60 candidate、BGE-small query embedding、P8 cross-encoder 输入、`bge-reranker-base`；没有无效的 BGE-base embedding 或在线 LLM。
17. 仍建议暂不改 Page Prompt：20-candidate coverage 可达 95.24%，主要矛盾仍是 fine ranking。
18. 是否已有足够证据进入生产代码修改阶段：**YES**，但应先在更多 Session 做小流量复验，不能从 S001 直接改默认值。
