# C3 BM25 输入与 Local-IDF 诊断

| Config | BM25 Query | BM25 Page | IDF Scope | Filter | R@5 | Gold@5 | Δ vs Dense | Δ vs H0 | Promoted | Demoted | Net | R@10 | MRR |
|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| H0 | P2 | Summary + Keywords | Visible Pages | OFF | 29.87% | 46 | -8.44pp | +0.00pp | 12 | 25 | -13 | 60.39% | 0.2551 |
| H1 | Original | Summary + Keywords | Visible Pages | OFF | 31.82% | 49 | -6.49pp | +1.95pp | 13 | 23 | -10 | 61.04% | 0.2621 |
| H2 | P2 | Keywords Only | Visible Pages | OFF | 31.17% | 48 | -7.14pp | +1.30pp | 5 | 16 | -11 | 55.84% | 0.2624 |
| H3 | P2 | Summary + Keywords | Visible Pages | ON | 31.82% | 49 | -6.49pp | +1.95pp | 12 | 22 | -10 | 59.09% | 0.2492 |
| H4 | P2 | Summary + Keywords | Dense Top60 | OFF | 29.22% | 45 | -9.09pp | -0.65pp | 11 | 25 | -14 | 60.39% | 0.2550 |
| H5 | Original | Keywords Only | Dense Top60 | ON | 33.12% | 51 | -5.19pp | +3.25pp | 5 | 13 | -8 | 54.55% | 0.2725 |

| Config | Macro R@5 | R@20 | Mean Gold Rank | Rescued Q vs Dense | Hurt Q vs Dense | Promoted vs H0 | Demoted vs H0 | Net vs H0 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| H0 | 31.07% | 82.47% | 12.16 | 8 | 23 | 0 | 0 | +0 |
| H1 | 33.26% | 81.82% | 11.86 | 10 | 22 | 5 | 2 | +3 |
| H2 | 33.10% | 78.57% | 12.78 | 4 | 16 | 13 | 11 | +2 |
| H3 | 32.93% | 82.47% | 12.42 | 8 | 20 | 3 | 0 | +3 |
| H4 | 30.44% | 82.47% | 11.99 | 7 | 23 | 0 | 1 | -1 |
| H5 | 34.66% | 79.87% | 12.27 | 4 | 13 | 16 | 11 | +5 |

Dense C3 reference：R@5=38.31%（59/154），Macro R@5=39.80%，R@10=54.55%，R@20=81.82%，MRR=0.3199，Mean Gold Rank=11.55。

## Session R@5

| Config | S001 | S002 | S003 | S004 | S005 | Macro |
|---|---:|---:|---:|---:|---:|---:|
| H0 | 38.10% | 31.25% | 23.26% | 28.12% | 34.62% | 31.07% |
| H1 | 42.86% | 28.12% | 25.58% | 31.25% | 38.46% | 33.26% |
| H2 | 47.62% | 25.00% | 25.58% | 25.00% | 42.31% | 33.10% |
| H3 | 38.10% | 34.38% | 25.58% | 28.12% | 38.46% | 32.93% |
| H4 | 38.10% | 28.12% | 23.26% | 28.12% | 34.62% | 30.44% |
| H5 | 47.62% | 25.00% | 30.23% | 28.12% | 42.31% | 34.66% |

## Non-Gold lexical intrusion

- H0：新进入 Top5 的 Non-Gold=223；模板/功能词=459；金融词=385；高区分度任务词=69。
- H4：新进入 Top5 的 Non-Gold=225；模板/功能词=463；金融词=389；高区分度任务词=69。
- H5：新进入 Top5 的 Non-Gold=113；模板/功能词=0；金融词=42；高区分度任务词=8。

## 事实结论

1. H1 相对 H0 为 +1.95pp / +3 Gold：Original Query 更适合当前 BM25；P2 对 Dense 有益，但其补充内容给 lexical matching 带来可测噪声。
2. H2 相对 H0 为 +1.30pp / +2 Gold。Keywords Only 减少 Dense Gold demotion，但同时显著减少 gross promotion，并降低 R@10/R@20，说明它更稳但 lexical coverage 不足。
3. H3 相对 H0 为 +1.95pp / +3 Gold；相对 H0 是 3 promoted、0 demoted。固定功能词/模板词过滤有正向但有限的实际作用。
4. H4 相对 H0 为 -0.65pp / -1 Gold，没有改善。只有 14/99 Query 的 visible corpus 超过 60 Pages；其余 Query 的 Local-IDF 文档集合与 visible corpus 相同。
5. Dense Top60 内同主题金融词仍高度普遍，Local-IDF 没有形成稳定的新区分信号；它也没有减少 Non-Gold lexical intrusion。
6. H5 最好，为 33.12%（51/154），仍比 Dense 低 5.19pp / 8 Gold。H5 的 13 个 demoted Gold 中有 9 个 raw BM25=0。
7. 剩余问题主要是 Keywords lexical coverage 稀疏与 production normalization/等权融合的共同作用：当其它候选有 BM25 命中时，无 lexical match 的 Dense Gold 仍被按双信号分母缩放。Query/Page 去噪能缓解，但 Local-IDF 不能解决这一排序机制。
8. 本轮不支持正式引入 Hybrid；应停止继续调 BM25 输入/IDF。BM25 的 R@10 信号仍存在，若以后继续，只值得单独研究融合/gating，而不是继续扩展本轮输入组合。

## 冻结与实现

- Dense、P2、Page、Gold、visibility 全部复用 C3 frozen artifacts；没有新 embedding 或 LLM 调用。
- Sparse encoding、Qdrant IDF、BM25 normalization 和 additive scoring 均直接调用生产组件。
- H4/H5 的 corpus 与 candidate set 都逐 Query 验证为 Dense Top60；其余配置使用 query-time visible corpus。
- 未扫描融合权重；Entity Boost、reranker、BM25-only 均未加入。

Validation：PASS。
