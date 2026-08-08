# S001 Mid-term Rank Fusion Tuning — Third Stage

> **范围与风险标记：S001-tuned。** 本轮只读取第一阶段 immutable snapshot/embedding 和第二阶段本地
> reranker score cache；没有重新运行 S001、没有生成 Page/长期记忆、没有加载模型，也没有新增 DeepSeek
> 或本地 reranker 调用。1 个 Gold = 4.76 percentage points，以下参数不是全局最优参数。

## 1. Baseline 与第二阶段复现

校验状态：**PASS**。Production Baseline 完全复现：R@5
**33.33%**（7/21）、R@20 **85.71%**（18/21）、MRR
0.3322、NDCG@5 0.2644。第二阶段 Local
`P0 RRF60 Top20 + P8 BAAI/bge-reranker-base` 也完全复现：R@5 **47.62%**（10/21）。

reranker-only 相对生产 Baseline 推进 **5** 个 Gold、推出
**2** 个原 Top-5 Gold，净增
**3**。所有 K20 Gold rank 与第二阶段 `rerank_case_analysis.csv` 一致。

## 2. Signal 与 Score Fusion 合法性

- `candidate_rank/score` 来自 P0 Dense + 中文 BM25 的 RRF60；candidate score 逐项通过
  `1/(60+dense_rank)+1/(60+bm25_rank)` 公式校验。
- `reranker_rank/score` 来自 P8 + `bge-reranker-base` 的既有 all-visible cache。生产 wrapper 以
  `normalize=True` 保存 sigmoid score，因此它不是未归一化 logit，但保序且具有稳定 [0,1] 语义。
- Score fusion 只在各 Query 当前 candidate pool 内分别 min-max，绝不跨 Query 混合量纲。

结论：合法并已执行。最佳为 `SF1_candidate_0.3_reranker_0.7` / K20，R@5 42.86%，没有超过 reranker-only。

## 3. Rank Fusion

Equal RRF（c=20/60/100）、reranker-weighted（λ=1.5/2/3/4）和 candidate-weighted（λ=1.5/2）均在
K15/K20 完成。严格 rank-fusion 最佳为 `RF2_reranker_weight_2` / K20：R@5
**38.10%**、MRR 0.2606、NDCG@5 0.2352。
它没有超过 10/21 的 reranker-only；连续加权会折中两个排名，但在这 21 个 Gold 上反而破坏了 reranker
的大幅有效跃迁。

Rank fusion 可以在某些权重下降低 DEMOTED，但以损失更多新推进 Gold 为代价；因此没有证据用 RF1/RF2/RF3
替换 reranker-only。

## 4. Conservative Top-N Protection

最佳保护策略 `PR1_candidate_top2_protection` / K20 保持 R@5
**47.62%**（10/21），并把相对生产 Baseline 的 DEMOTED_OUT_OF_TOP5 从 2 降为
1。它保护 4 个 Gold slot、
24 个非 Gold slot；恢复 1
个被 reranker 推出的 Gold，同时阻止 1 个 reranker 新命中留在 Top-5。

与 K20 reranker-only 相比，新增/恢复：S001-Q026/88e975b7-0f5a-5f5c-98ce-e461dcab93b8；损失：S001-Q013/2631e802-75be-5089-9418-baece6a717cf。因此它是“更稳定的等 Recall 排序”，
不是净 Recall 提升。

## 5. K15 与 K20

| K | 推荐策略 | Candidate Recall | R@5 | MRR | NDCG@5 | DEMOTED | Fusion p95 ms | Total conservative p95 ms |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 15 | `PR1_candidate_top2_protection` | 66.67% | 42.86% | 0.3005 | 0.2733 | 1 | 0.112 | 1227.098 |
| 20 | `RF0_reranker_only` | 90.48% | 47.62% | 0.3098 | 0.2976 | 2 | 0.106 | 1593.450 |

Quality 仍选 K20；允许少 1 个 Gold 时，K15 将 local reranker warm p95 从约 1359.8 ms 降到 1019.7 ms，
所以 K15 是待多 Session 验证的 Pareto 版本。Fusion 本身 p95 只有亚毫秒级，新增开销可忽略；总延迟沿用
第二阶段 warm query embedding/candidate/reranker 实测，只重放缓存上的数值排序。

## 6. 典型回退 Case

| Query | Gold Page | Candidate rank | Reranker-only rank | Top2 protection rank | 分类 |
|---|---|---:|---:|---:|---|
| S001-Q026 | `289ba3d6-8b24-5c77-8df9-7af211ee1007` | 19 | 2 | 3 | PRESERVED_HIT |
| S001-Q026 | `88e975b7-0f5a-5f5c-98ce-e461dcab93b8` | 2 | 17 | 2 | RECOVERED_DEMOTED_GOLD |
| S001-Q028 | `44777fdf-abd5-5570-b0aa-8e3f1cb44000` | 5 | 14 | 14 | UNCHANGED |

Q026 达到了预期效果：candidate rank 2 的 Gold 被恢复，同时 reranker 从 rank 19 推到前列的另一个 Gold
仍保留在 Top-5；但全局上保护策略又挤出了 Q013 的一个 Gold，所以总命中仍是 10/21。Q028 的 Gold
candidate rank=5，Top2 protection 不覆盖它，因此仍未恢复。复杂化保护规则会进一步针对 S001 调参，本轮
按约束停止。

## 7. Top-5 Gold 结构与 MRR/Recall

Best Quality 的每 Query Top-5 Gold 均值为 0.714，分布为
0 个=6、1 个=6、2 个=2、
3+ 个=0。Best Stable 的对应均值同为
0.714。

Recall@5 是 21 个 eligible Gold 的 micro 指标；MRR 是 14 个 Query 的最佳 Gold rank macro 指标。当前 Local
相对 Baseline 净增 3 个 Gold，所以 Recall 33.33%→47.62%；但部分 Query 原本最靠前的 Gold 被推后，MRR
0.3322→0.3098。NDCG@5 从 0.2644→0.2976，说明 Top-5 整体仍改善。
Top2 protection 的 MRR/NDCG 为 0.3015/0.2952：demotion 更少，
但其 NDCG 略低于 reranker-only，故按预先规定的 R@5→NDCG→MRR 质量选择规则，Best Quality 仍是 RF0。

## 8. Winners

| Winner | 配置 | R@5 | MRR | NDCG@5 | DEMOTED | Fusion p95 ms |
|---|---|---:|---:|---:|---:|---:|
| T0 Current Local | K20 / `RF0_reranker_only` | 47.62% | 0.3098 | 0.2976 | 2 | 0.106 |
| T1 Best Rank Fusion | K20 / `RF2_reranker_weight_2` | 38.10% | 0.2606 | 0.2352 | 2 | 0.153 |
| T2 Best Score Fusion | K20 / `SF1_candidate_0.3_reranker_0.7` | 42.86% | 0.3318 | 0.2947 | 1 | 0.161 |
| T3 Best Protection / Stable | K20 / `PR1_candidate_top2_protection` | 47.62% | 0.3015 | 0.2952 | 1 | 0.110 |
| T4 Best K15 | K15 / `PR1_candidate_top2_protection` | 42.86% | 0.3005 | 0.2733 | 1 | 0.112 |
| T5 Best K20 / Quality | K20 / `RF0_reranker_only` | 47.62% | 0.3098 | 0.2976 | 2 | 0.106 |

- **Best Quality:** `Q0 + P0 dense/Chinese-BM25 RRF60 Top20 + P8 BAAI/bge-reranker-base + RF0_reranker_only -> Top5`，10/21，47.62%。
- **Best Stable Ranking:** `Q0 + P0 dense/Chinese-BM25 RRF60 Top20 + P8 BAAI/bge-reranker-base + PR1_candidate_top2_protection -> Top5`，同为 10/21，但少推出 1 个原命中。
- **Recommended Pareto:** `Q0 + P0 dense/Chinese-BM25 RRF60 Top15 + P8 BAAI/bge-reranker-base + PR1_candidate_top2_protection -> Top5`，9/21，42.86%，K15 降低 reranker 延迟。

严格生产候选选择规则（R@5→NDCG@5→MRR→更少 DEMOTED→latency）仍选择 K20 reranker-only；Top2
protection 与 K15 protection 是需要在未参与调参 Session 上对照验证的稳定/Pareto 候选，不能从 S001
直接固化。

## 9. 验收问题逐项结论

1. Second-stage Local 47.62%：**完全复现**。
2. reranker-only 推进：**5 Gold**。
3. reranker-only 推出：**2 Gold**。
4. Rank fusion 是否减少 DEMOTED：部分规则可以，但严格 RF 会损失更多新命中；Top2 protection 可在同 Recall 下从 2 降到 1。
5. Rank fusion 是否提高 R@5：**否**。
6. 最佳 Rank Fusion：`RF2_reranker_weight_2` / K20，R@5 38.10%。
7. Score Fusion：合法并已执行。最佳为 `SF1_candidate_0.3_reranker_0.7` / K20，R@5 42.86%，没有超过 reranker-only。
8. Protection：Top2 有效降低过度重排，但只是等量换回 1 个 Gold，R@5 不变。
9. K15 最佳：`PR1_candidate_top2_protection`，42.86%、MRR 0.3005、NDCG 0.2733。
10. K20 最佳：按质量规则为 `RF0_reranker_only`，47.62%。
11. 是否达到 52.38%：**否，最高仍是 10/21=47.62%**。
12. 新增命中：相对 reranker-only 没有净新增；Stable 恢复 S001-Q026/88e975b7-0f5a-5f5c-98ce-e461dcab93b8。
13. 损失：Stable 同时损失 S001-Q013/2631e802-75be-5089-9418-baece6a717cf；Best Quality 没有相对第二阶段损失。
14. MRR/NDCG：Best Quality 0.3098/0.2976；Stable 0.3015/0.2952。
15. Fusion latency：Best Quality p95 0.106 ms；Stable p95 0.110 ms，近乎可忽略。
16. Best Quality：K20 reranker-only，即第二阶段 Local。
17. Best Pareto：K15 Top2 protection，少 1 Gold、reranker p95 约少 340 ms。
18. DeepSeek reranker：**仍不需要**；第二阶段同为 10/21，且 MRR/NDCG 更低并增加 1 次在线 LLM。
19. Query Rewrite：**仍不需要**；本轮没有调用，上轮已显示无稳定增益。
20. Page Prompt：**仍建议不修改**；K20 candidate coverage 已是 19/21，瓶颈仍是精排取舍。
21. 下一步：**停止继续对 S001 调参，转入未参与调参的多 Session 验证。** 当前扫描已出现 1 Gold=4.76 pp 的 selection-overfitting 风险，不能把 S001-tuned 权重/保护规则称作全局最优。

## 10. Cache 与执行边界

Cache reuse 状态：**PASS**。连续第二次执行也只读取这些源产物；前一次 after-state、本次 before-state 与本次 after-state 的 SHA-256、mtime、size 全部一致。执行只读取 snapshot、
Q0/P0 embedding 和 `local_scores.jsonl`。新增 DeepSeek 调用=0、
新增 local reranker 调用=0、新增 embedding=0、Page/长期记忆生成=0、其他 Session=0。
