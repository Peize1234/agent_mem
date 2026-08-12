# S001–S005 全量 Memory Recall（V3 OR Gold）

## 数据集

- Session：5
- Query：348
- Gold requirements：559
- OR requirements：5

## 汇总

| Layer | Gold / Eligible | Hit | Recall |
|---|---:|---:|---:|
| ShortTerm（3 QA） | 559 | 410 | 73.35% |
| MidTerm C3 @5 | 149 | 59 | 39.60% |
| LongTerm Production @5 | 261 | 37 | 14.18% |
| Overall Memory Coverage | 559 | 484 | 86.58% |

## MidTerm

- R@5：39.60% (59/149)
- R@10：56.38%
- R@20：81.88%
- MRR：0.3249
- Mean Gold Rank：11.26

## Session

| Session | ShortTerm | MidTerm R@5 | LongTerm | Overall |
|---|---:|---:|---:|---:|
| S001 | 73.75% | 47.62% | 16.67% | 88.75% |
| S002 | 74.14% | 36.67% | 12.96% | 86.21% |
| S003 | 72.37% | 30.95% | 12.50% | 83.55% |
| S004 | 73.11% | 40.62% | 12.28% | 86.55% |
| S005 | 73.91% | 50.00% | 19.05% | 90.22% |

LongTerm 结果来自相同 source QA、相同生产配置生成的冻结 provenance trace；新版 Excel 只重组了 5 个 OR Gold label。
