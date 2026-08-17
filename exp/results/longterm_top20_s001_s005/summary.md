# LongTerm Top20 补算报告（旧 S001–S005 V3 评测）

输出目录：`exp/results/longterm_top20_s001_s005`

## 汇总

| 指标 | 值 |
|---|---|
| Outside-ShortTerm Gold | 149 |
| ShortTerm 命中（旧，不变） | 410/559 |
| MidTerm C3 @5（旧，不变） | 59/149 |
| LongTerm @5（旧 trace） | 37/149 |
| LongTerm @5（本次复算） | 36/149 |
| LongTerm @20（本次补算） | 98/149 (65.77%) |
| LongTerm @20 上界（补上无法判定的 4 个缺失 Gold） | 102/149 (68.46%) |
| 无法判定 @20 的 requirement（Gold memory 已被旧运行 cleanup 删除） | 4 |
| LongTerm MRR @20 | 0.1747 |
| Mid@5 ∪ Long@5（旧） | 74/149 |
| Mid@5 ∪ Long@20（本次） | 109/149 |
| Overall（410 + union） | 519/559 = 92.84% |
| Long@20 相比 Long@5 新增命中 | 61 |

## 分 Session

| Session | Eligible | Long@5(复算) | Long@20 | Mid@5 | Mid@5 ∪ Long@20 |
|---|---:|---:|---:|---:|---:|
| S001 | 21 | 7 | 13 | 10 | 15 |
| S002 | 30 | 8 | 22 | 11 | 23 |
| S003 | 42 | 7 | 23 | 13 | 27 |
| S004 | 32 | 6 | 21 | 13 | 25 |
| S005 | 24 | 8 | 19 | 12 | 19 |

## Top5 复算验证（复算 Top5 vs 冻结 trace Top5）

- 验证 query 数：97
- 完全一致：22
- 同一集合、顺序不同：19
- 不一致：56

不一致主要来自接近并列的分数差异（CPU/GPU 嵌入精度、BM25 词典差异）以及少数已被运行内 cleanup 移除的 LongTerm 点；详细逐 query 状态见 `validation.json`。
