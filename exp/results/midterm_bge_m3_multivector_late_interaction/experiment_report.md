# MidTerm BGE-M3 Multi-Vector Late Interaction

C3 Page、P2 Query、Gold 与 query-time visibility 完全冻结；无 LLM、无 Summary regeneration。

| Retrieval | Micro R@5 | Gold@5 | Macro R@5 | R@10 | R@20 | MRR | Mean Gold Rank |
|---|---:|---:|---:|---:|---:|---:|---:|
| C3 BGE-small Dense | 38.31% | 59/154 | 39.80% | 54.55% | 81.82% | 0.3199 | 11.55 |
| BGE-M3 Dense（max_length=1024） | 37.01% | 57/154 | 38.26% | 59.74% | 81.17% | 0.2674 | 12.20 |
| BGE-M3 Pure Multi-Vector MaxSim | 34.42% | 53/154 | 34.58% | 61.04% | 81.82% | 0.2676 | 12.27 |
| C3 Dense Top60 + BGE-M3 Multi-Vector Rerank | 34.42% | 53/154 | 34.58% | 61.04% | 81.82% | 0.2676 | 12.14 |

## Session R@5

| Retrieval | S001 | S002 | S003 | S004 | S005 |
|---|---:|---:|---:|---:|---:|
| C3 BGE-small Dense | 47.62% | 34.38% | 30.23% | 40.62% | 46.15% |
| BGE-M3 Dense（max_length=1024） | 47.62% | 43.75% | 30.23% | 31.25% | 38.46% |
| BGE-M3 Pure Multi-Vector MaxSim | 28.57% | 43.75% | 32.56% | 21.88% | 46.15% |
| C3 Dense Top60 + BGE-M3 Multi-Vector Rerank | 28.57% | 43.75% | 32.56% | 21.88% | 46.15% |

## Gold movement vs C3

- BGE-M3 Dense（max_length=1024）：promoted=20，demoted=22，net=-2，rescued=15，hurt=17。
- BGE-M3 Pure Multi-Vector MaxSim：promoted=15，demoted=21，net=-6，rescued=12，hurt=18。
- C3 Dense Top60 + BGE-M3 Multi-Vector Rerank：promoted=15，demoted=21，net=-6，rescued=12，hurt=18。

## Fixed-experiment answers

1. Pure Multi-Vector 没有超过 C3：34.42% vs 38.31%（-3.90pp）。
2. C3 Top60 + Multi-Vector rerank 没有超过 C3：34.42% vs 38.31%（-3.90pp）。
3. M2 promoted=15、demoted=21；代表案例与 token-level MaxSim 已独立保存，区分局部指标/关系收益与同主题 hard-negative 干扰。
4. C3 Top60 外 Eligible Gold=0，C3 Top5 Gold 不在 Top60=0；candidate coverage 不是本轮瓶颈。
5. Query/Page multi-vector 均值为 42.07/368.71，两侧 truncation 均为 0/0；结果不是 CLS pooling 或截断造成。
6. 相比 Field-aware V1 的 33.77%，M2 更高：34.42% vs 33.77%（多 1 个 Gold），但仍比 C3 少 6 个 Gold。
7. Pure MaxSim Top5 落在 C3 Top60 外的 Page 数为 0；M1/M2 的 Top5 set 差异 Query 数为 0，说明 Top60 candidate coverage 不是下降原因。
8. Page token 数与 MaxSim 的 per-query Pearson 均值为 -0.1477（median=-0.1928），没有观察到 Page 越长分数越高的整体正偏置。
9. 代表案例显示：局部任务词/指标词确实能救回个别 Gold，但同公司、同期间、同财务底表的 Non-Gold 通常可分别为大量 Query subword 提供高 MaxSim；未加权的 token 平均不能稳定保留任务/关系差异。
10. 本轮结果不支持直接进入 Dense + Multi-vector fusion。若仍研究该方向，query-token filtering 或 IDF-weighted MaxSim 比继续使用纯 MaxSim 更直接；本轮未实际运行这些后续实验。

Validation：PASS。
