# C3 Field-aware Multi-vector Retrieval

固定分数：`0.4 × Task cosine + 0.4 × Fact cosine + 0.2 × Relation cosine`。

| Retrieval | Micro R@5 | Gold@5 | Macro R@5 | R@10 | R@20 | MRR | Mean Gold Rank |
|---|---:|---:|---:|---:|---:|---:|---:|
| C3 Dense baseline | 38.31% | 59 | 39.80% | 54.55% | 81.82% | 0.3199 | 11.55 |
| Task / Fact / Relation Multi-vector | 33.77% | 52 | 34.02% | 55.84% | 76.62% | 0.3097 | 12.33 |

相对 C3：promoted=14，demoted=21，net=-7，rescued Query=10，hurt Query=18。

## Session R@5

| Session | C3 Dense | Field-aware | Delta |
|---|---:|---:|---:|
| S001 | 47.62% | 38.10% | -9.52pp |
| S002 | 34.38% | 34.38% | +0.00pp |
| S003 | 30.23% | 34.88% | +4.65pp |
| S004 | 40.62% | 28.12% | -12.50pp |
| S005 | 46.15% | 34.62% | -11.54pp |

## Gold 字段分数

| Transition | Count | Mean Task | Mean Fact | Mean Relation | Mean Final |
|---|---:|---:|---:|---:|---:|
| PROMOTED | 14 | 0.6411 | 0.5182 | 0.6588 | 0.5955 |
| DEMOTED | 21 | 0.5975 | 0.5107 | 0.6142 | 0.5661 |
| UNCHANGED | 119 | 0.5824 | 0.4981 | 0.6067 | 0.5535 |

## Query-conditioned Gold / Non-Gold separation

| Score | Mean margin | Median margin | Positive rate |
|---|---:|---:|---:|
| C3 Dense | -0.0376 | -0.0336 | 15.15% |
| Task cosine | -0.0754 | -0.0728 | 12.12% |
| Fact cosine | -0.0309 | -0.0233 | 12.12% |
| Relation cosine | -0.0582 | -0.0530 | 12.12% |
| Field-aware final | -0.0384 | -0.0392 | 17.17% |

## 结论

1. 当前固定 0.4/0.4/0.2 方案比 C3 少 7 个 Top5 Gold，不能替换 C3。
2. R@10 从 54.55% 升至 55.84%，且产生 14 个 promotion，说明字段信号存在；但 21 个 demotion 和 S001/S004/S005 的下降表明 Top5 稳定性不足。
3. 字段粒度不对称：Query/Page 平均字符数分别为 Task 49.3/88.1，Fact 26.6/341.9，Relation 41.4/122.4。Page Fact/Relation 仍承载较多完整底表与通用限制。
4. 值得继续研究字段定义，但应先收紧 Page 字段的语义边界和粒度对称性，再做预先冻结的小规模权重消融；当前结果不支持直接围绕 0.4/0.4/0.2 做细粒度调参，也不支持修改 Production。

Query 字段仅由冻结 P2 Query 抽取；Page 字段仅由冻结 C3 `Summary + Keywords` 抽取。
没有向字段抽取器提供 Gold、visibility、rank、score、原始长对话或其它 Page。

Validation：PASS。
