# Query Completion Summary (S001-S010 Rebalanced)

- Dataset: `/home/peize/Code/htzq/mem0/exp/金融分析数据集_前10个Session长短期记忆再平衡.xlsx`
- Queries: 752
- Gold requirements: 786
- OR groups: 0
- Gold-bearing Queries: 742
- Queries without Gold (excluded): 10

| Configuration | Queries | Mean | Median | P25 | P75 | 0% Complete | 100% Complete | >=50% | >=80% |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Short | 742 | 50.13% | 50.00% | 0.00% | 100.00% | 354 (47.71%) | 356 (47.98%) | 52.29% | 47.98% |
| Mid | 742 | 23.32% | 0.00% | 0.00% | 0.00% | 563 (75.88%) | 167 (22.51%) | 24.12% | 22.51% |
| Long | 742 | 20.08% | 0.00% | 0.00% | 0.00% | 587 (79.11%) | 143 (19.27%) | 20.89% | 19.27% |
| Short + Mid | 742 | 73.45% | 100.00% | 0.00% | 100.00% | 187 (25.20%) | 535 (72.10%) | 74.80% | 72.10% |
| Short + Long | 742 | 70.22% | 100.00% | 0.00% | 100.00% | 211 (28.44%) | 511 (68.87%) | 71.56% | 68.87% |
| Mid + Long | 742 | 28.23% | 0.00% | 0.00% | 100.00% | 524 (70.62%) | 201 (27.09%) | 29.38% | 27.09% |
| Short + Mid + Long | 742 | 78.37% | 100.00% | 100.00% | 100.00% | 153 (20.62%) | 574 (77.36%) | 79.38% | 77.36% |

## Requirement-level results and validation

- ShortTerm: 400/786.
- MidTerm C3 routed @5: 179/386 outside-ShortTerm requirements.
- MidTerm C3 layer hits over the unified completion denominator: 179/786; unrouted Queries count as misses.
- LongTerm@5 direct frozen provenance: 155/786.
- Three-layer requirement union: 618/786.
- Largest pairwise mean-completion gain over its better constituent: Short + Mid (+23.32%).
- Per-Query pairwise/all-memory monotonicity passed; OR groups are counted once.
