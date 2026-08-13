# LLM Historical Dependency Reranking

| Configuration | Micro R@5 | Gold@5 | Macro Session R@5 | R@10 | R@20 | MRR | Mean Gold Rank |
|---|---:|---:|---:|---:|---:|---:|---:|
| C3 Dense | 39.60% | 59/149 | 41.17% | 56.38% | 81.88% | 0.3249 | 11.26 |
| C3 Top20 + LLM Dependency Rerank | 30.20% | 45/149 | 31.22% | 53.02% | 81.88% | 0.2761 | 12.13 |

## Movement

- Promoted Gold：2
- Demoted Gold：16
- Net Gold Gain：-14
- Rescued Queries：1
- Hurt Queries：15
- Preserved C3 Top5 Gold：43/59

## API / Cache

- Successful Query outputs：97
- API attempts（本次 / 累计成功生成）：0 / 97
- Cache hits（本次）：97
- Retries（本次 / 累计）：0 / 0

## Session R@5

| Session | C3 | LLM Dependency Rerank |
|---|---:|---:|
| S001 | 47.62% | 33.33% |
| S002 | 36.67% | 33.33% |
| S003 | 30.95% | 23.81% |
| S004 | 40.62% | 28.12% |
| S005 | 50.00% | 37.50% |

## Score Diagnostics

- 全候选同分 Query：36/97
- 每个 Query 的 unique score 数：mean=5.72，median=5.0

候选只来自冻结 C3 query-time visible Top20；Prompt 不包含 Gold、C3 score/rank、Raw QA、邻近 QA或未来对话。
