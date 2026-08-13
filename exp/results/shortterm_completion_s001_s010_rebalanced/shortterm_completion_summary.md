# S001-S010 Rebalanced Workbook: ShortTerm Completion Audit

- Dataset: `/home/peize/Code/htzq/mem0/exp/金融分析数据集_前10个Session长短期记忆再平衡.xlsx`
- Queries: 752
- Gold requirements: 786
- OR groups: 0
- Gold-bearing Queries: 742
- Queries without Gold: 10

## ShortTerm (latest 3 QA turns)

- Requirement recall: 400/786 (50.89%)
- Mean Query Completion: 50.13%
- Median / P25 / P75: 50.00% / 0.00% / 100.00%
- 0% Complete: 354 (47.71%)
- 100% Complete: 356 (47.98%)
- Completion >=50%: 52.29%
- Completion >=80%: 47.98%

## Frozen artifact compatibility

- Source QA matches existing frozen artifacts: False
- Changed Query inputs/answers: 354
- Existing frozen C3 rankings and LongTerm provenance cover only the prior dataset contract and cannot be reused for this workbook.
- MidTerm, LongTerm, pairwise, and three-layer completion were intentionally not emitted. They require a new S001-S010 retrieval run on this exact workbook.
