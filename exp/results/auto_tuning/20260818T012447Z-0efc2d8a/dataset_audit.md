# Dataset Audit

Status: **DATASET_QUALITY_WARNING**

- Dataset: `/home/peize/Code/htzq/mem0/exp/金融分析数据集_前10个Session长短期记忆再平衡.xlsx`
- Sessions: 3
- Queries: 216
- Gold requirements: 225
- ShortTerm / outside ShortTerm: 117 / 108
- AND / OR requirements: 225 / 0

## Hard errors

- None

## Quality warnings

- `EXPLICIT_HISTORY_POSITION_LEAKAGE`: {'code': 'EXPLICIT_HISTORY_POSITION_LEAKAGE', 'ratio': 0.47417840375586856, 'count': 101, 'examples': ['S001-Q006', 'S001-Q008', 'S001-Q010', 'S001-Q012', 'S001-Q014', 'S001-Q016', 'S001-Q018', 'S001-Q020', 'S001-Q022', 'S001-Q024', 'S001-Q026', 'S001-Q028', 'S001-Q030', 'S001-Q032', 'S001-Q034', 'S001-Q036', 'S001-Q038', 'S001-Q040', 'S001-Q042', 'S001-Q044']}
