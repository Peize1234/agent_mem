# MidTerm Field-aware V2 Unified Extraction

| Retrieval | Micro R@5 | Gold@5 | Macro R@5 | R@10 | R@20 | MRR | Mean Gold Rank |
|---|---:|---:|---:|---:|---:|---:|---:|
| C3 | 38.31% | 59 | 39.80% | 54.55% | 81.82% | 0.3199 | 11.55 |
| Field-aware V1 | 33.77% | 52 | 34.02% | 55.84% | 76.62% | 0.3097 | 12.33 |
| V2-Global | 29.87% | 46 | 30.58% | 51.30% | 71.43% | 0.2422 | 14.51 |
| V2-Field-Rerank | 28.57% | 44 | 28.88% | 46.75% | 71.43% | 0.2611 | 14.67 |

## Session R@5

| Retrieval | S001 | S002 | S003 | S004 | S005 |
|---|---:|---:|---:|---:|---:|
| C3 | 47.62% | 34.38% | 30.23% | 40.62% | 46.15% |
| Field-aware V1 | 38.10% | 34.38% | 34.88% | 28.12% | 34.62% |
| V2-Global | 33.33% | 34.38% | 25.58% | 25.00% | 34.62% |
| V2-Field-Rerank | 33.33% | 28.12% | 27.91% | 28.12% | 26.92% |

## Movements

- V2-Global vs C3：promoted=9，demoted=22，net=-13，rescued=8，hurt=20。
- V2-Global vs Field-aware V1：promoted=13，demoted=19，net=-6，rescued=11，hurt=15。
- V2-Field-Rerank vs C3：promoted=13，demoted=28，net=-15，rescued=9，hurt=22。
- V2-Field-Rerank vs Field-aware V1：promoted=15，demoted=23，net=-8，rescued=13，hurt=18。

## Extraction 结论

- Page Fact duplicate-row rate：V1 22.22% → V2 37.84%。
- Page Fact provenance rate：V1 99.40% → V2 97.90%；非来源任务中的污染率为 100.00% → 97.10%。
- Query Fact deictic residue：V1 31.31% → V2 9.09%。
- Task mean chars（Query/Page）：V1 49.28/88.12 → V2 21.92/28.96。
- Fact mean chars（Query/Page）：V1 26.64/341.89 → V2 22.86/166.48。
- Relation mean chars（Query/Page）：V1 41.35/122.43 → V2 19.49/73.65。
- Page Task boilerplate：V1 61.56% → V2 7.81%；Page Relation boilerplate：45.95% → 47.15%。
- Query Task/Relation char-trigram mean：V1 0.3708 → V2 0.3611；Page：0.1773 → 0.0529。

## Failure attribution

- C3 Top5 Gold lost by V2 Global：22。
- V2 Global Top5 Gold lost by field rerank：12；field rerank promoted=10。
- Eligible Gold outside V2 Global Top60：0；其中 C3 Top5 Gold=0。
- V2 Global 与冻结 P2 resolved_query 完全相同 Query：67/99；与冻结 C3 summary+keywords 完全相同 Page：0/333。

## 明确结论

1. V2-Global 没有保持 C3/P2 的 Global retrieval：R@5 由 38.31% 降为 29.87%，净少 13 个 Gold。
2. V2-Field-Rerank 为 28.57%，低于 V1 的 33.77% 和 C3 的 38.31%；本轮没有超过 C3。
3. V2 缩短了 Query/Page 字段、减少 Query Fact 未解析指代，并显著减少 Page Task boilerplate；但 Fact 粒度仍不对称、重复率升高，来源信息污染几乎未下降，故没有更好地区分同质化 Page。
4. 失败首因是 Global drift：统一调用后 32/99 个 resolved_query 与冻结 P2 不同，所有 333 个 Global Page 文本也都变化，直接使 22 个 C3 Top5 Gold 掉出。
5. Candidate coverage 不是瓶颈：没有 Eligible Gold 落在 V2 Global Top60 之外。Field ordering 本身又从 Global Top5 拉入 10 个、拉出 12 个 Gold，净损失 2。
6. 当前 hard negative 仍主要由同公司、同期间和相同财务底表造成；Page Fact 的大量底表、来源与披露日期使 Fact cosine 缺乏分析操作区分度。
7. 不建议直接进入权重搜索或 Global+Field fusion。应先让统一输出严格复现冻结 P2/C3 Global，并解决 Page Fact 的底表重复和 provenance 污染；否则 fusion 只是在混合两个已漂移信号。
8. 本轮固定 Prompt 和 0.4/0.3/0.3 权重均未根据结果修改，也未运行任何额外配置。

Validation：PASS。
