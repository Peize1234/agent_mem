# Query Completion Summary (S001-S005)

- Gold-bearing Queries: 343
- Queries without Gold requirements (excluded): 5
- Completion values use each Query's complete Gold requirement set as the denominator.

| Configuration | Queries | Mean | Median | P25 | P75 | 0% Complete | 100% Complete | >=50% | >=80% |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Short | 343 | 84.09% | 100.00% | 50.00% | 100.00% | 0 (0.00%) | 246 (71.72%) | 87.17% | 71.72% |
| Mid | 343 | 6.58% | 0.00% | 0.00% | 0.00% | 289 (84.26%) | 0 (0.00%) | 7.00% | 0.00% |
| Long | 343 | 4.28% | 0.00% | 0.00% | 0.00% | 308 (89.80%) | 1 (0.29%) | 3.79% | 0.29% |
| Short + Mid | 343 | 90.67% | 100.00% | 100.00% | 100.00% | 0 (0.00%) | 274 (79.88%) | 95.63% | 79.88% |
| Short + Long | 343 | 88.07% | 100.00% | 100.00% | 100.00% | 0 (0.00%) | 261 (76.09%) | 93.00% | 76.09% |
| Mid + Long | 343 | 8.33% | 0.00% | 0.00% | 0.00% | 278 (81.05%) | 1 (0.29%) | 9.04% | 0.29% |
| Short + Mid + Long | 343 | 92.13% | 100.00% | 100.00% | 100.00% | 0 (0.00%) | 281 (81.92%) | 97.38% | 81.92% |

## Validation and metric scope

- Existing baseline reproduced: 348 Queries, 559 Gold requirements, 5 OR groups, ShortTerm 410/559, MidTerm C3 @5 59/149, Overall Memory Coverage 484/559.
- Direct LongTerm@5 provenance matching hits 38/559 requirements. The existing routed/eligible LongTerm metric reports 37/261.
- Scope difference requirement: S005-Q042::G1.
- The one-hit difference is an eligibility/accounting difference; the direct three-layer requirement union still reproduces 484/559.
- All pairwise and three-layer per-Query monotonicity checks passed; every OR group is one denominator unit.
