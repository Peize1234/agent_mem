# Query Completion Summary (S001-S005)

- Gold workbook: `/home/peize/Code/htzq/mem0/exp/金融分析数据集_必要依赖重新核验版.xlsx`
- Dataset: 348 Queries, 384 Gold requirements, 0 OR groups
- Gold-bearing Queries: 343
- Queries without Gold requirements (excluded): 5
- Completion values use each Query's complete Gold requirement set as the denominator.

| Configuration | Queries | Mean | Median | P25 | P75 | 0% Complete | 100% Complete | >=50% | >=80% |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Short | 343 | 95.04% | 100.00% | 100.00% | 100.00% | 0 (0.00%) | 309 (90.09%) | 100.00% | 90.09% |
| Mid | 343 | 0.73% | 0.00% | 0.00% | 0.00% | 338 (98.54%) | 0 (0.00%) | 1.46% | 0.00% |
| Long | 343 | 1.17% | 0.00% | 0.00% | 0.00% | 335 (97.67%) | 0 (0.00%) | 2.33% | 0.00% |
| Short + Mid | 343 | 95.77% | 100.00% | 100.00% | 100.00% | 0 (0.00%) | 314 (91.55%) | 100.00% | 91.55% |
| Short + Long | 343 | 96.21% | 100.00% | 100.00% | 100.00% | 0 (0.00%) | 317 (92.42%) | 100.00% | 92.42% |
| Mid + Long | 343 | 1.60% | 0.00% | 0.00% | 0.00% | 332 (96.79%) | 0 (0.00%) | 3.21% | 0.00% |
| Short + Mid + Long | 343 | 96.65% | 100.00% | 100.00% | 100.00% | 0 (0.00%) | 320 (93.29%) | 100.00% | 93.29% |

## Validation and metric scope

- Frozen artifact baseline reproduced before relabeling: 348 Queries, 559 Gold requirements, 5 OR groups, ShortTerm 410/559, MidTerm C3 @5 59/149, Overall Memory Coverage 484/559.
- Persisted C3 ranking reproduction: 59/149 (PASS).
- Rechecked ShortTerm: 350/384.
- Rechecked routed MidTerm C3 @5 contribution: 5/384.
- New Gold contains 34 requirements outside ShortTerm; 25 corresponding Queries have no frozen C3 ranking and are counted as MidTerm misses under the routing contract.
- Rechecked direct LongTerm@5 provenance hits: 8/384.
- Rechecked three-layer requirement union: 361/384.
- All pairwise and three-layer per-Query monotonicity checks passed; every OR group is one denominator unit.
