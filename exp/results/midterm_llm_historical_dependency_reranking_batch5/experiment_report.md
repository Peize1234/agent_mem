# 5-candidate Batched LLM Historical Dependency Reranking

| 配置 | Micro R@5 | Gold@5 | Macro Session R@5 | R@10 | R@20 | MRR | Mean Gold Rank |
|---|---:|---:|---:|---:|---:|---:|---:|
| C3 Dense | 39.60% | 59/149 | 41.17% | 56.38% | 81.88% | 0.3249 | 11.26 |
| C3 Top20 + 20-way LLM Historical Dependency Rerank | 30.20% | 45/149 | 31.22% | 53.02% | 81.88% | 0.2761 | 12.13 |
| C3 Top20 + 5-candidate Batched LLM Historical Dependency Rerank | 29.53% | 44/149 | 30.95% | 52.35% | 81.88% | 0.2695 | 12.36 |

## 相对 C3 Movement

- Promoted / Demoted / Net：12 / 27 / -15
- Rescued / Hurt Queries：6 / 20
- Preserved C3 Top5 Gold：32/59

## 相对旧 20-way Movement

- Promoted / Demoted / Net：14 / 15 / -1

## Score discrimination

- 旧 20-way 全候选同分：36/97
- batch5 合并后全候选同分：3/97
- 旧全同分但 batch5 拉开：33/36
- 单 batch 全同分：106/343 (30.90%)
- 合并 Top20 unique score：mean=6.58，median=7.0

## API / Cache

- Successful batch outputs：343
- Actual API attempts（完整实验累计，含失败重试）：364
- 当前执行 API attempts / cache hits：0 / 343
- Prompt / completion / total tokens（完整实验累计）：778467 / 72902 / 851369

## 结论

batch5 显著减少了全候选同分，却没有改善 Historical Dependency 排序：Micro R@5 比旧 20-way 再低 0.67pp，比 C3 低 10.07pp。说明问题不只是一次阅读 20 个同质候选导致无法打分；拆批后的绝对分数在批次间缺乏可靠可比性，而且新增的分数差异没有对应更准确的依赖判断。

## 各 Session R@5

| Session | C3 | 20-way LLM | batch5 LLM |
|---|---:|---:|---:|
| S001 | 47.62% | 33.33% | 23.81% |
| S002 | 36.67% | 33.33% | 40.00% |
| S003 | 30.95% | 23.81% | 19.05% |
| S004 | 40.62% | 28.12% | 21.88% |
| S005 | 50.00% | 37.50% | 50.00% |
