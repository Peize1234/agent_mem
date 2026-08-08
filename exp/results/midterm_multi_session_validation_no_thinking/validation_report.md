# S001 冻结检索链的 S002–S005 Held-out 验证

## 1. 实验设计与参数冻结

S001 是 development/tuning Session；S002–S005 是 held-out validation。Quality 与 Pareto 的参数在任何 held-out 结果产生前写入 `frozen_config.json`，验证过程没有重新调参。S001 只引用已有 snapshot/cache，未重新完整运行。

S002–S005 首次以 concurrency=4 启动；本地 Qdrant 的并发读写竞态使 S004 在 Q019 中止。完整的 S002/S003/S005 被保留，S004 在独立 runtime 以 concurrency=1 从头重跑。失败的部分 S004 数据被明确排除，未与 retry 数据拼接；这属于运行恢复，不是检索参数调优。

一次性源数据实际 wall-clock 合计 1508.22 秒；按各成功 Session wall-clock 求和的顺序估计为 2578.78 秒，有效 speedup=1.71x。

## 2. Session 信息与逐 Session 结果

| Session | Role | Turns | Pages | Queries | Total Gold | Eligible Gold | Gold Not Available | Baseline R@5 | Quality R@5 | ΔQuality | Pareto R@5 | ΔPareto | Quality Cand R@20 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| S001 | development | 50 | 47 | 14 | 36 | 21 | 14 | 33.33% | 47.62% | +14.29 pp | 42.86% | +9.52 pp | 90.48% |
| S002 | heldout | 73 | 70 | 21 | 57 | 32 | 25 | 43.75% | 43.75% | +0.00 pp | 40.62% | -3.12 pp | 84.38% |
| S003 | heldout | 93 | 90 | 26 | 73 | 43 | 30 | 23.26% | 27.91% | +4.65 pp | 27.91% | +4.65 pp | 67.44% |
| S004 | heldout | 74 | 71 | 21 | 57 | 32 | 25 | 21.88% | 31.25% | +9.38 pp | 21.88% | +0.00 pp | 71.88% |
| S005 | heldout | 58 | 55 | 17 | 45 | 26 | 19 | 46.15% | 30.77% | -15.38 pp | 38.46% | -7.69 pp | 80.77% |

Baseline 统一 evaluator 的完整核心指标：

| Session | R@5 | R@10 | R@20 | MRR | NDCG@5 | Mean Gold Rank |
|---|---:|---:|---:|---:|---:|---:|
| S001 | 33.33% | 57.14% | 85.71% | 0.3322 | 0.2644 | 10.24 |
| S002 | 43.75% | 68.75% | 90.62% | 0.3092 | 0.2641 | 9.78 |
| S003 | 23.26% | 41.86% | 58.14% | 0.2519 | 0.1960 | 20.58 |
| S004 | 21.88% | 43.75% | 68.75% | 0.2319 | 0.1675 | 16.84 |
| S005 | 46.15% | 57.69% | 80.77% | 0.3899 | 0.3291 | 11.23 |

## 3. Held-out Micro / Macro（仅 S002–S005）

| Scheme | Micro R@5 | Micro MRR | Micro NDCG@5 | Macro Session R@5 | Macro MRR | Macro NDCG@5 |
|---|---:|---:|---:|---:|---:|---:|
| baseline | 32.33% | 0.2887 | 0.2324 | 33.76% | 0.2957 | 0.2392 |
| quality | 33.08% | 0.2407 | 0.2147 | 33.42% | 0.2422 | 0.2153 |
| pareto | 31.58% | 0.2426 | 0.2057 | 32.22% | 0.2418 | 0.2064 |

Quality held-out Micro 相对 Baseline：+0.75 pp；Pareto：-0.75 pp。
Quality 的 MRR 从 0.2887 降至 0.2407，NDCG@5 从 0.2324 降至 0.2147；微小 Recall 增益伴随明显的 Query 首个 Gold 与 Top5 排序质量退化。
Quality Session Win/Tie/Loss：2/1/1；Pareto：1/1/2。
Query-level Quality Win/Tie/Loss：11/64/10；收益不是广泛单向改善，Query win 与 loss 基本相抵。

S001–S005 pooled 数字另存于 `all_five_pooled_metrics.json`，但包含已参与调参的 S001，不能作为纯 held-out 泛化证据。

## 4. Candidate depth 与 Dense/BM25 互补

Held-out K20 Gold 分类：Dense-only 17，BM25-only 19，Both 80，Neither 17。

| Session | Dense-only | BM25-only | Both | Neither |
|---|---:|---:|---:|---:|
| S002 | 3 | 2 | 26 | 1 |
| S003 | 7 | 9 | 18 | 9 |
| S004 | 5 | 5 | 17 | 5 |
| S005 | 2 | 3 | 19 | 2 |

Production Dense R@20 为 72.93%；冻结 RRF Candidate R@20 为 75.19%（+2.26 pp）。最终 Quality R@5 为 33.08%；剩余 miss 中 Candidate miss 33，Reranker miss 56。

## 5. P0 → P8 reranker 稳定性

Held-out promoted=14，demoted=13，net gain=1，promotion efficiency=51.85%。这些 movement 直接比较生产 Dense Top5 与冻结 Quality 最终 Top5。

失败 Session S005 的 Quality Candidate 已覆盖 21/26 Gold（80.77%），但 final Top5 仅命中 8/26，而 Baseline 命中 12/26。具体为 promoted=3、demoted=7；Quality miss 中 Candidate miss=5、Reranker miss=13。因此该 Session 的退化主要来自 P8 local reranker 的过度重排，而不是 Gold 大量无法进入 RRF Top20。此处只记录 future hypothesis，没有据此改参或重跑。

## 6. K15 Pareto vs K20 Quality

K20 Quality 比 K15 Pareto 多命中 2 个 held-out eligible Gold。两者 warm E2E p95 差值为 +362.52 ms。注意这比较的是两个完整冻结方案：Pareto 还包含 Top2 protection，因此不能把差异纯归因于 K。

## 7. 真实 warm E2E latency

完整 `query → embedding → Dense → BM25 → RRF → reranker → Top5` 由一个 `time.perf_counter()` 包围；结果不是 component p95 相加。模型加载和下载不计入 warm latency。

| Scheme | Mean ms | p50 | p90 | p95 | Max |
|---|---:|---:|---:|---:|---:|
| baseline | 16.18 | 14.39 | 24.10 | 26.71 | 50.35 |
| quality | 1236.44 | 1376.10 | 1564.54 | 1619.18 | 1798.75 |
| pareto | 967.25 | 1039.49 | 1228.52 | 1256.66 | 1286.22 |

## 8. Bootstrap 不确定性

以 Query 为 bootstrap 单元、seed=42、10000 次重采样。Quality−Baseline ΔR@5 95% CI：[-6.02, 7.52] pp；Pareto−Baseline：[-6.50, 4.96] pp。样本仍只有四个 Session，应保守解释。

## 9. S001 selection overfitting 与最终建议

held-out 方向仍为正，但 Δ=+0.75 pp，显著小于 S001 的 +14.29 pp；说明至少存在收益幅度上的 selection overfitting。

最终状态：**NEEDS_MORE_HELD_OUT_VALIDATION**。

暂不进入正式生产替换；保持参数冻结，扩大到更多未参与选择的 Session 验证。可以准备隔离实现或 shadow 评估，但不应依据本轮四个 Session 继续调参。

冻结验证不支持在本轮恢复 DeepSeek reranker、Query Rewrite 或 Page Prompt 改写；这些组件均未参与验证。Dense/BM25 互补性泛化了，但 P0→P8 + bge-reranker-base 的最终排序并不稳定，因此本轮不建议将完整 Quality 链直接设为生产默认。RRF60 只应保持为下一批冻结验证候选，不能称为全局最优。

## 10. 直接问题回答

1. S002–S005 eligible Gold：S002=32, S003=43, S004=32, S005=26。
2. 各 Session Baseline R@5：S001=33.33%, S002=43.75%, S003=23.26%, S004=21.88%, S005=46.15%
3. 各 Session Quality R@5：S001=47.62%, S002=43.75%, S003=27.91%, S004=31.25%, S005=30.77%
4. 各 Session Pareto R@5：S001=42.86%, S002=40.62%, S003=27.91%, S004=21.88%, S005=38.46%
5. Quality Session Win/Tie/Loss=2/1/1。
6. Pareto Session Win/Tie/Loss=1/1/2。
7. Held-out Micro Baseline R@5=32.33%。
8. Held-out Micro Quality R@5=33.08%。
9. Held-out Quality 相对 Baseline=+0.75 pp。
10. Held-out Macro Session R@5：Baseline=33.76%，Quality=33.42%，Δ=-0.34 pp。
11. Dense/BM25 互补性泛化：K20 分类为 Dense-only=17、BM25-only=19、Both=80、Neither=17，且四个 held-out Session 均同时存在 Dense-only 与 BM25-only Gold。
12. Quality Candidate R@20=75.19%，final R@5=33.08%。
13. Quality miss：Candidate miss=33，Reranker miss=56。
14. Local reranker held-out promoted=14。
15. Local reranker held-out demoted=13。
16. K20 Quality 比 K15 Pareto 多命中 2 个 eligible Gold。
17. K20 Quality 相比 K15 Pareto warm E2E p95 增量=+362.52 ms。
18. Quality 真实 warm E2E p95=1619.18 ms。
19. Pareto 真实 warm E2E p95=1256.66 ms。
20. held-out 方向仍为正，但 Δ=+0.75 pp，显著小于 S001 的 +14.29 pp；说明至少存在收益幅度上的 selection overfitting。
21. P0 candidate + P8 reranker：不建议直接作为生产默认；held-out promoted/demoted=14/13，Quality Macro R@5、MRR、NDCG@5 均未改善。
22. RRF60：Dense/BM25 互补性泛化，且 Candidate R@20 比 Dense R@20 高 2.26 pp；保留为下一批冻结验证候选，但本轮不能证明常数 60 最优或足以进入生产。
23. bge-reranker-base：不建议作为生产默认；虽推进 14 个 Gold，也推出 13 个，净增仅 1。
24. 继续维持无需 DeepSeek reranker：是；本轮没有新增 DeepSeek 检索调用，结论不依赖它。
25. 继续维持无需 Query Rewrite：是；冻结 Q0 链路直接验证。
26. 继续维持暂不修改 Page Prompt：是；本轮没有产生支持修改 Prompt 的新消融证据。
27. 最终状态：NEEDS_MORE_HELD_OUT_VALIDATION。
