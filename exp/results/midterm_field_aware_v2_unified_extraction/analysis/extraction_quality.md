# V2 Extraction Quality

## 长度对称性

### V1

| Type | Field | Mean chars | Median | P90 | Empty |
|---|---|---:|---:|---:|---:|
| Query | task | 49.28 | 47.0 | 68.4 | 0 |
| Query | fact | 26.64 | 25.0 | 40.2 | 0 |
| Query | relation | 41.35 | 40.0 | 60.2 | 0 |
| Page | task | 88.12 | 87.0 | 128.0 | 0 |
| Page | fact | 341.89 | 318.0 | 454.4 | 0 |
| Page | relation | 122.43 | 114.0 | 183.0 | 0 |

### V2

| Type | Field | Mean chars | Median | P90 | Empty |
|---|---|---:|---:|---:|---:|
| Query | task | 21.92 | 21.0 | 32.0 | 0 |
| Query | fact | 22.86 | 23.0 | 37.0 | 0 |
| Query | relation | 19.49 | 18.0 | 29.4 | 1 |
| Page | task | 28.96 | 29.0 | 39.0 | 0 |
| Page | fact | 166.48 | 159.0 | 242.8 | 0 |
| Page | relation | 73.65 | 70.0 | 96.0 | 0 |

## Page Fact duplicates / provenance / Query deictic

| Variant | Fact duplicate rows | Duplicate rate | Provenance rate | Inappropriate provenance rate | Query Fact deictic rate |
|---|---:|---:|---:|---:|---:|
| V1 | 74 | 22.22% | 99.40% | 100.00% | 31.31% |
| V2 | 126 | 37.84% | 97.90% | 97.10% | 9.09% |

## Page Fact provenance pattern

| Variant | http | cninfo | 披露日期 | 年度报告 | 来源 | 报告摘要 |
|---|---:|---:|---:|---:|---:|---:|
| V1 | 325 | 325 | 330 | 328 | 329 | 311 |
| V2 | 0 | 0 | 316 | 276 | 326 | 272 |

## Task / Relation char-trigram similarity

| Variant | Type | Mean | Median | P90 | >=0.8 |
|---|---|---:|---:|---:|---:|
| V1 | Query | 0.3708 | 0.3594 | 0.6177 | 1.01% |
| V2 | Query | 0.3611 | 0.3731 | 0.6351 | 4.04% |
| V1 | Page | 0.1773 | 0.1433 | 0.4281 | 0.00% |
| V2 | Page | 0.0529 | 0.0224 | 0.1620 | 0.00% |
