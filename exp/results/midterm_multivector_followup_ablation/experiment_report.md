# MidTerm Multi-Vector 后续两项固定诊断

Page、P2 Query、visibility、Gold 全部冻结；无 LLM、无文本再生成、无参数搜索。

| Retrieval | Micro R@5 | Gold@5 | Macro R@5 | R@10 | R@20 | MRR | Mean Gold Rank |
|---|---:|---:|---:|---:|---:|---:|---:|
| C3 BGE-small Dense | 38.31% | 59/154 | 39.80% | 54.55% | 81.82% | 0.3199 | 11.55 |
| BGE-M3 Dense | 37.01% | 57/154 | 38.26% | 59.74% | 81.17% | 0.2674 | 12.20 |
| BGE-M3 Original MaxSim | 34.42% | 53/154 | 34.58% | 61.04% | 81.82% | 0.2676 | 12.27 |
| BGE-small Raw-token Pure MaxSim | 37.01% | 57/154 | 38.54% | 57.79% | 76.62% | 0.2777 | 12.93 |
| C3 Top60 + BGE-small Raw-token MaxSim | 37.01% | 57/154 | 38.54% | 57.79% | 76.62% | 0.2777 | 12.69 |
| BGE-M3 IDF-weighted MaxSim | 35.71% | 55/154 | 35.53% | 59.74% | 83.12% | 0.2587 | 12.18 |
| BGE-M3 IDF-filtered MaxSim | 31.82% | 49/154 | 31.63% | 58.44% | 81.17% | 0.2623 | 12.22 |

## Session R@5

| Retrieval | S001 | S002 | S003 | S004 | S005 |
|---|---:|---:|---:|---:|---:|
| C3 BGE-small Dense | 47.62% | 34.38% | 30.23% | 40.62% | 46.15% |
| BGE-M3 Dense | 47.62% | 43.75% | 30.23% | 31.25% | 38.46% |
| BGE-M3 Original MaxSim | 28.57% | 43.75% | 32.56% | 21.88% | 46.15% |
| BGE-small Raw-token Pure MaxSim | 42.86% | 37.50% | 25.58% | 40.62% | 46.15% |
| C3 Top60 + BGE-small Raw-token MaxSim | 42.86% | 37.50% | 25.58% | 40.62% | 46.15% |
| BGE-M3 IDF-weighted MaxSim | 28.57% | 50.00% | 34.88% | 21.88% | 42.31% |
| BGE-M3 IDF-filtered MaxSim | 23.81% | 43.75% | 30.23% | 21.88% | 38.46% |

## Movement

- BGE-M3 Original MaxSim vs C3：promoted=15，demoted=21，net=-6，rescued=12，hurt=18。
- BGE-small Raw-token Pure MaxSim vs C3：promoted=16，demoted=18，net=-2，rescued=12，hurt=15。
- C3 Top60 + BGE-small Raw-token MaxSim vs C3：promoted=16，demoted=18，net=-2，rescued=12，hurt=15。
- BGE-M3 IDF-weighted MaxSim vs C3：promoted=19，demoted=23，net=-4，rescued=16，hurt=20。
- BGE-M3 IDF-filtered MaxSim vs C3：promoted=17，demoted=27，net=-10，rescued=14，hurt=24。
- BGE-M3 IDF-weighted MaxSim vs Original MaxSim：promoted=4，demoted=2，net=+2。
- BGE-M3 IDF-filtered MaxSim vs Original MaxSim：promoted=4，demoted=8，net=-4。

## 四个问题

1. BGE-small raw-token MaxSim 没有超过 C3：37.01% vs 38.31%。
2. BGE-M3 IDF-weighted/filtered 为 35.71%/31.82%；相对 Original 34.42% 分别变化 +1.30/-2.60pp，均未超过 C3。
3. B1/B2 hard-negative margin mean 为 -0.0349/-0.0373，Original 为 -0.0339：没有缩小 hard-negative score gap。
4. 同 backbone 的 raw-token MaxSim 仍退化，说明 zero-shot MaxSim scoring 本身有损失；BGE-M3 Original 进一步下降，说明 BGE-M3 representation/backbone 也贡献了退化。IDF 只能小幅恢复，0.8 DF filtering 则删除了同质语料中仍有用的财务/状态 token。两项均未突破 C3，因此应停止当前 zero-shot multi-vector 路线，若继续则转向带 hard negatives 的学习。

## Token / cache facts

- BGE-small raw-token Page/Query 平均向量数：468.69/59.44；truncation=138/0。
- B2 共过滤 1504/4165 个 Query token rows；其中 high-DF=828。

Validation：PASS。
