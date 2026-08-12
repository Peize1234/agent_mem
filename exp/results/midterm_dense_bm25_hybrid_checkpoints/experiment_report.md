# MidTerm Dense + 生产中文 BM25 Hybrid Checkpoint 实验

| Checkpoint | Dense R@5 | Hybrid R@5 | Δpp | Dense Gold@5 | Hybrid Gold@5 | Promoted | Demoted | Net | MRR |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| C0 | 32.47% | 25.32% | -7.14 | 50 | 39 | 11 | 22 | -11 | 0.2065 |
| C1 | 35.06% | 27.27% | -7.79 | 54 | 42 | 11 | 23 | -12 | 0.2043 |
| C2 | 35.71% | 27.92% | -7.79 | 55 | 43 | 9 | 21 | -12 | 0.2326 |
| C3 | 38.31% | 29.87% | -8.44 | 59 | 46 | 12 | 25 | -13 | 0.2551 |

| Checkpoint | Hybrid Macro R@5 | Hybrid R@10 | Hybrid R@20 | Hybrid MRR | Hybrid Mean Gold Rank | Rescued Q | Hurt Q |
|---|---:|---:|---:|---:|---:|---:|---:|
| C0 | 25.24% | 46.75% | 72.08% | 0.2065 | 14.77 | 7 | 19 |
| C1 | 26.77% | 48.70% | 73.38% | 0.2043 | 14.75 | 7 | 19 |
| C2 | 27.72% | 51.30% | 73.38% | 0.2326 | 15.54 | 7 | 17 |
| C3 | 31.07% | 60.39% | 82.47% | 0.2551 | 12.16 | 8 | 23 |

## Session Hybrid R@5

| Checkpoint | S001 | S002 | S003 | S004 | S005 |
|---|---:|---:|---:|---:|---:|
| C0 | 19.05% | 31.25% | 23.26% | 21.88% | 30.77% |
| C1 | 14.29% | 31.25% | 25.58% | 28.12% | 34.62% |
| C2 | 19.05% | 31.25% | 25.58% | 28.12% | 34.62% |
| C3 | 38.10% | 31.25% | 23.26% | 28.12% | 34.62% |

## 结论

- Hybrid 最好的是 C3，Micro R@5=29.87%；仍低于对应 Dense。
- C3 从 59/154（38.31%）降至 46/154（29.87%），净变化 -13 Gold（-8.44pp）。
- 四个 checkpoint 都是负增益，因此当前固定 production Hybrid 不值得直接引入 MidTerm Retriever。

Hybrid 新进入 Top5 的 Non-Gold Page 数（跨 Query 计次）：C0=227；C1=239；C2=229；C3=223。

## BM25 有效 Query 类型

- C3 / 指标名：8 个 promoted Gold。
- C1 / 指标名：7 个 promoted Gold。
- C0 / 指标名：5 个 promoted Gold。
- C2 / 指标名：5 个 promoted Gold。
- C2 / 年份/数字：4 个 promoted Gold。
- C3 / 判断修订：3 个 promoted Gold。
- C0 / 实体：3 个 promoted Gold。
- C1 / 年份/数字：3 个 promoted Gold。
- C3 / 年份/数字：3 个 promoted Gold。
- C0 / 判断修订：2 个 promoted Gold。
- C1 / 判断修订：2 个 promoted Gold。
- C2 / 判断修订：2 个 promoted Gold。

C3 gross promoted 的主要重叠类别（同一 Gold 可属于多类）：指标名=8；判断修订=3；年份/数字=3；基期判断=2；实体=2；证据层级/证据强度=2；反证/反例=1；近似周转=1；近似杠杆=1。

C3 demoted 的主要重叠类别：指标名=15；其它=5；判断修订=5；证据层级/证据强度=5；年份/数字=4；反证/反例=3；实体=3；近似周转=2；信息缺口=1；基期判断=1。

C3 新进入 Top5 的 Non-Gold 高频共同词：的(185)、归母(95)、结论(86)、净利润(69)、和(68)、三年(58)、连续(48)、与(44)、判断(41)、来源(39)、收入(38)、总资产(35)、营业(35)、降(33)、披露(33)。

## 实现与冻结

- 文本预处理、金融词典、中文 sparse encoder、Qdrant IDF、sigmoid normalization 与 additive scoring 均直接调用生产组件。
- Entity Boost 为空；没有 BM25-only 评测、权重扫描、reranker 或其它信号。
- 每个 Query 的临时 Qdrant collection 只包含其 frozen visible_page_ids，因此未来 Page 不参与 IDF、候选或最终评分。
- MRR/Mean Gold Rank 使用同一生产 scorer 对全部可见 Dense 候选的诊断性完整排序；R@5/R@10 使用生产 Top5/10 的 60-candidate over-fetch，R@20 使用 80-candidate over-fetch。

Validation：PASS。

详细 movement 与分数见 `representative_cases.md`。
