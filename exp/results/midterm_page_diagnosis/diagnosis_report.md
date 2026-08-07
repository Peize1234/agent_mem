# S001 Mid-term Page 召回根因诊断

> 口径：最终中期历史只计 `mid_page`，`mid_session` 仅用于第一阶段路由诊断。所有离线排名均按每个评测轮当时已提交的 Page 构造时间快照，排除未来 Page。

## 1. 当前真实有效 Recall

`Short + Mid Page + Long`：Macro Recall **64.29%**，Micro Recall **63.89%**（23/36），Macro Precision **17.54%**，Hit Rate **100.00%**，Full Dependency Recall **28.57%**，MRR **0.3810**。

真正 long-range Gold（distance > 3）共 21 条；`Page + Long` 覆盖 8/21（38.10%），`Short + Page + Long` long-range Recall 为 38.10%。

| Layer | Macro Recall | Micro Recall | Precision (macro) | Hit Rate | Full Recall | MRR |
|---|---:|---:|---:|---:|---:|---:|
| short | 42.86% | 41.67% | 35.71% | 100.00% | 0.00% | 0.3810 |
| mid_page | 16.67% | 16.67% | 9.52% | 35.71% | 0.00% | 0.1667 |
| long | 19.05% | 19.44% | 13.10% | 35.71% | 0.00% | 0.2083 |
| short_mid_page | 59.52% | 58.33% | 19.35% | 100.00% | 21.43% | 0.3810 |
| mid_page_long | 21.43% | 22.22% | 10.62% | 42.86% | 0.00% | 0.1786 |
| short_mid_page_long | 64.29% | 63.89% | 17.54% | 100.00% | 28.57% | 0.3810 |

## 2. 14 个评测问题逐条结果

| Query | Long Gold | Page hit | Long hit | Effective hit | Full dependencies | 主因 |
|---|---:|---:|---:|---:|---:|---|
| S001-Q007 | 1 | 1 | 1 | 1 | yes | [] |
| S001-Q011 | 2 | 2 | 2 | 2 | yes | [] |
| S001-Q013 | 2 | 0 | 1 | 1 | no | ['TOP_K_CUTOFF'] |
| S001-Q014 | 1 | 1 | 1 | 1 | yes | [] |
| S001-Q021 | 1 | 0 | 0 | 0 | no | ['DATASET_WRONG_GOLD'] |
| S001-Q022 | 2 | 0 | 0 | 0 | no | ['QUERY_FORMULATION', 'TOP_K_CUTOFF'] |
| S001-Q026 | 2 | 1 | 2 | 2 | yes | ['TOP_K_CUTOFF'] |
| S001-Q028 | 1 | 0 | 0 | 0 | no | ['PAGE_REPRESENTATION'] |
| S001-Q033 | 2 | 1 | 0 | 1 | no | ['QUERY_FORMULATION'] |
| S001-Q035 | 1 | 0 | 0 | 0 | no | ['TOP_K_CUTOFF'] |
| S001-Q039 | 2 | 0 | 0 | 0 | no | ['TOP_K_CUTOFF'] |
| S001-Q042 | 1 | 0 | 0 | 0 | no | ['TOP_K_CUTOFF'] |
| S001-Q044 | 2 | 0 | 0 | 0 | no | ['EQUIVALENT_ALTERNATIVE_PAGE'] |
| S001-Q049 | 1 | 0 | 0 | 0 | no | ['TOP_K_CUTOFF'] |

## 3. Gold Page availability 与 lineage

21 条 long-range Gold 中：Page 存在 21/21，committed 且在原查询前 job 已完成 21/21，Qdrant 可见 21/21，lineage 正确 21/21。

没有发现 `PAGE_NOT_AVAILABLE`、staging、已提交但不可见或 source-job lineage 错误。原实验 `before_evaluation` 的等待确实生效；这一层不是低 Recall 的来源。

为避免 future leakage，诊断按查询 Qxxx 仅保留 `turn_index <= current_index - 4` 的 Page，并用原 migration job 完成时间校验；重建的 dense Top-5 与原实验 **14/14** 轮逐项完全一致。

## 4. Session Routing 与 Session 划分

最终形成 **1 个 Session**、47 个 Page。所有 Page 都属于同一个 Session；所有评测 Query 的 Gold Session rank 均为 1 且被选中。

| Session ID | Pages | Q range | Summary | Summary keywords | lineage |
|---|---:|---|---|---|---|
| 185dab99-607c-5be0-8b0f-66b1cfa59888 | 47 | ['S001-Q001', 'S001-Q002', 'S001-Q003', 'S001-Q004', 'S001-Q005', 'S001-Q006', 'S001-Q007', 'S001-Q008', 'S001-Q009', 'S001-Q010', 'S001-Q011', 'S001-Q012', 'S001-Q013', 'S001-Q014', 'S001-Q015', 'S001-Q016', 'S001-Q017', 'S001-Q018', 'S001-Q019', 'S001-Q020', 'S001-Q021', 'S001-Q022', 'S001-Q023', 'S001-Q024', 'S001-Q025', 'S001-Q026', 'S001-Q027', 'S001-Q028', 'S001-Q029', 'S001-Q030', 'S001-Q031', 'S001-Q032', 'S001-Q033', 'S001-Q034', 'S001-Q035', 'S001-Q036', 'S001-Q037', 'S001-Q038', 'S001-Q039', 'S001-Q040', 'S001-Q041', 'S001-Q042', 'S001-Q043', 'S001-Q044', 'S001-Q045', 'S001-Q046', 'S001-Q047'] | 针对贵州茅台2023-2025年财务分析（权益研究员视角，关注增长质量、盈利可持续性与资本回报）。数据来源为《贵州茅台2025年年度报告》（2026-04-17披露，巨潮资讯网PDF，URL: https://static.cninfo.com.cn/finalpage/2026-04-17/1225114731.PDF），单位亿元，合并报表归母口径。年报原数：营业收入1476.94→1708.99→1688.38亿元（同比+15... | ['贵州茅台', '2023-2025', '年度报告', '合并报表归母口径', '营业收入', '归母净利润', '总资产', '归母股东权益', '同比', '2024年拐点', '反证检验', '冲突'] | page_ids=True, jobs=True |

## 5. Oracle / Global upper bounds

| Experiment | Long-range Recall@5 | Recall@10 | Gold MRR | Gold mean rank |
|---|---:|---:|---:|---:|
| baseline | 28.57% | 76.19% | 0.2008 | 8.857142857142858 |
| oracle_session | 28.57% | 76.19% | 0.2008 | 8.857142857142858 |
| global_page | 28.57% | 76.19% | 0.2008 | 8.857142857142858 |
| context_query_recent_3 | 38.10% | 66.67% | 0.2562 | 8.142857142857142 |
| raw_dialogue_embedding | 23.81% | 47.62% | 0.1554 | 11.095238095238095 |
| required_context_oracle | 85.71% | 95.24% | 0.6374 | 2.9047619047619047 |

Oracle Session 与 Global Page 都和 baseline 相同，证明两阶段 Session 路由在本 Sheet 上没有造成损失；瓶颈完全发生在单一大 Session 内的 Page 排序/检索信号。

## 6. Page ranking 分布与 Top-K sweep

missed Gold 的 baseline dense rank 中位数为 9，rank 6–10 有 10 条，rank 11–20 有 4 条，rank >20 有 1 条。

| top_k_sessions | top_k_pages | max_total_pages | Recall | Full Recall | Gold MRR |
|---:|---:|---:|---:|---:|---:|
| 1 | 5 | 5 | 28.57% | 21.43% | 0.2008 |
| 1 | 5 | 10 | 28.57% | 21.43% | 0.2008 |
| 1 | 5 | 20 | 28.57% | 21.43% | 0.2008 |
| 1 | 5 | 50 | 28.57% | 21.43% | 0.2008 |
| 1 | 10 | 5 | 28.57% | 21.43% | 0.2008 |
| 1 | 10 | 10 | 76.19% | 64.29% | 0.2008 |
| 1 | 10 | 20 | 76.19% | 64.29% | 0.2008 |
| 1 | 10 | 50 | 76.19% | 64.29% | 0.2008 |
| 1 | 20 | 5 | 28.57% | 21.43% | 0.2008 |
| 1 | 20 | 10 | 76.19% | 64.29% | 0.2008 |
| 1 | 20 | 20 | 95.24% | 92.86% | 0.2008 |
| 1 | 20 | 50 | 95.24% | 92.86% | 0.2008 |
| all | 5 | 5 | 28.57% | 21.43% | 0.2008 |
| all | 5 | 10 | 28.57% | 21.43% | 0.2008 |
| all | 5 | 20 | 28.57% | 21.43% | 0.2008 |
| all | 5 | 50 | 28.57% | 21.43% | 0.2008 |
| all | 10 | 5 | 28.57% | 21.43% | 0.2008 |
| all | 10 | 10 | 76.19% | 64.29% | 0.2008 |
| all | 10 | 20 | 76.19% | 64.29% | 0.2008 |
| all | 10 | 50 | 76.19% | 64.29% | 0.2008 |
| all | 20 | 5 | 28.57% | 21.43% | 0.2008 |
| all | 20 | 10 | 76.19% | 64.29% | 0.2008 |
| all | 20 | 20 | 95.24% | 92.86% | 0.2008 |
| all | 20 | 50 | 95.24% | 92.86% | 0.2008 |

## 7. Query ablation

| Query | Recall@5 | Recall@10 | Gold MRR | Mean rank |
|---|---:|---:|---:|---:|
| current_query | 28.57% | 76.19% | 0.2008 | 8.857142857142858 |
| recent_1_qa_query | 42.86% | 61.90% | 0.1806 | 9.476190476190476 |
| recent_3_qa_query | 38.10% | 66.67% | 0.2562 | 8.142857142857142 |
| required_context_oracle | 85.71% | 95.24% | 0.6374 | 2.9047619047619047 |

当前 Query 单独检索 Recall@5 为 28.57%；拼接最近 1 个 QA 提升到 42.86%，最近 3 个 QA 为 38.10%，但两者的 Recall@10 均下降，说明直接拼接上下文有帮助但噪声明显。`required_context` Oracle 达 85.71%，相对当前 Query 提升 57.14 个百分点，证明检索输入缺少历史语义是强瓶颈；该字段只用于上界，不进入正式检索。

## 8. Page embedding representation ablation

| Representation | Recall@5 | Recall@10 | Gold MRR | Mean rank |
|---|---:|---:|---:|---:|
| current_summary_keywords_user | 28.57% | 76.19% | 0.2008 | 8.857142857142858 |
| user_assistant | 23.81% | 47.62% | 0.1554 | 11.095238095238095 |
| raw_dialogue | 23.81% | 47.62% | 0.1554 | 11.095238095238095 |
| summary_keywords_raw_dialogue | 33.33% | 66.67% | 0.2082 | 9.333333333333334 |
| summary_only | 33.33% | 71.43% | 0.1961 | 8.904761904761905 |

当前实际表示是 `summary + keywords + user_input`。`user_input + assistant_answer` 与 `raw_dialogue` 在本数据中等价，Recall@5/10 为 23.81%/47.62%，低于当前表示的 28.57%/76.19%；因此不能据此把正式表示直接替换为完整原对话。`summary + keywords + raw_dialogue` 的 Recall@5 小幅升至 33.33%，但 Recall@10 降至 66.67%。当前金融数据的 Assistant Answer 高度模板化且重复携带相同底表，完整 raw dialogue 会稀释区分信号；Page 表示是个别失败的原因，不是总体主因。

## 9. Dense / BM25 / Hybrid

代码审计和真实 trace 均确认：Mid-term `search_pages()` 只调用 Qdrant dense `search()`；虽然 Collection 写入了 BM25 sparse vector，但 Mid-term Page ranking 没有调用 `keyword_search()`，因此所谓 current hybrid 实际是 dense-only。

Dense Recall@5=28.57%，BM25-only Recall@5=0.00%，诊断用 RRF Recall@5=28.57%。RRF 只用于诊断，没有改生产算法。

实际 BM25 trace 的 21 条 long-range Gold 全部未排名。原因是中文 sparse encoder 只按空格切 token，而 Mid-term 的 `text_lemmatized` 只是对 embedding text 调用 `.lower()`，没有中文分词；Query 也未被预分词。故本轮不能把 0% 解释成“词法检索天然无效”，只能确认当前 Mid-term BM25 链路既未参与正式排序、按现有文本形态也不可用。

真实 trace 例：Q013→Q001 的 dense/final score=0.394777、rank=9；BM25 无命中；诊断 RRF score=0.014493、rank=9，cutoff=5。各 Gold 的 dense、BM25、RRF、raw、summary 分数与排名均保存在 `page_ranking_analysis.csv`。

## 10. Page Summary / Keywords 质量

missed Gold 中，Query↔raw dialogue 相似度高于 Query↔summary 的有 15/15 条，平均差值 0.0709；raw-dialogue embedding 能把 1 条 baseline miss 拉回 Top-5。

## 11. Dataset Gold audit

| Audit verdict | Count |
|---|---:|
| CORRECT | 34 |
| REDUNDANT | 1 |
| WRONG_TURN | 1 |

审计汇总：明确正确 Gold 34/36，疑似标错 1/36，冗余 1/36，不确定 0/36；严格语义 Judge 在 15 条 exact miss 中确认等价历史 Page 2 条；仅给 current question 时无法唯一定位 exact Gold 2/36。

具体例子：Q013→Q006 被确认是正确且必要的扣非口径依赖；Q039→Q032 被确认是正确且必要的资产/权益同步性依赖，未发现错标。Q044→Q034 的 Top-5 返回 Q035、Q044→Q040 的 Top-5 返回 Q016，严格 Judge 均判定 coverage=1.0，因此 exact-ID 在这两条上是 false negative。

## 12. Exact-ID vs Semantic-equivalent Recall

baseline exact long-range Page Recall@5 为 28.57%；允许严格 Judge 认可的等价 Page 后为 38.10%。Judge 结果不替换 exact-ID 指标，只揭示评价 false negative。

## 13. missed Gold 根因数量和占比

| Primary cause | Count | Share of misses |
|---|---:|---:|
| TOP_K_CUTOFF | 9 | 60.00% |
| EQUIVALENT_ALTERNATIVE_PAGE | 2 | 13.33% |
| QUERY_FORMULATION | 2 | 13.33% |
| DATASET_WRONG_GOLD | 1 | 6.67% |
| PAGE_REPRESENTATION | 1 | 6.67% |

算法/实现相关 12/15（80.00%）；数据集/评价相关 3/15（20.00%）；共同影响 0/15（0.00%）；未归因 0/15。

## 14. 最终结论

当前低 Recall 主要是算法检索信号问题：Session 与 Page 均正常存在，Session Router 没有损失，但指代化 Query、Page 表示和 dense-only Page 排序使 Gold 排名落到 cutoff 之后；数据集 exact-ID 口径也造成次要 false negative。

## 15. 推荐修改优先级

1. P0：修正 benchmark 核心口径，永久移除 mid_session 对最终上下文 Recall 的贡献，并保留 exact-ID 与 semantic-equivalent 两套指标。
2. P1：优先验证 Page candidate/output budget 从 5 提到 10；这是最大实测增益（Recall +47.62%），但需同时评估 Prompt token 与 Precision 成本，不能把它当成排名质量修复。
3. P2：实现并验证 context-aware retrieval query；recent-3 QA 的 Recall@5 实测增益为 +9.52%，required_context Oracle 上界则为 +57.14%。
4. P3：保留当前 Page 表示作为基线，不要直接换成 raw dialogue（实测 -4.76%）；后续应抽取 Assistant Answer 中的特定结论/数字，而非无差别拼接模板化全文。
5. P4：先修复中文 BM25 分词并补齐 Mid-term hybrid trace，再决定是否启用融合；当前不可用 sparse 链路的诊断 RRF 增益为 +0.00%。
6. P5：修正 dataset audit 确认的 1 条错标并复核 1 条冗余；为 2 条严格等价 Page 增加独立 semantic-equivalent 指标，同时保留 exact-ID 指标。

## Appendix: 每条 missed long-range Gold

### S001-Q013 → S001-Q001

distance=12；page_available=True；session_rank=1；session_score=0.48163718；page_rank=9；page_score=0.3947772394019948；cutoff=5；recent1_rank=7；recent3_rank=6；required_context_rank=4；raw_rank=9；long_hit=True；effective_hit=True；semantic_equivalent=False；primary=TOP_K_CUTOFF；secondary=[]。

### S001-Q013 → S001-Q006

distance=7；page_available=True；session_rank=1；session_score=0.48163718；page_rank=7；page_score=0.4104670255677559；cutoff=5；recent1_rank=3；recent3_rank=7；required_context_rank=1；raw_rank=6；long_hit=False；effective_hit=False；semantic_equivalent=False；primary=TOP_K_CUTOFF；secondary=[]。

### S001-Q021 → S001-Q015

distance=6；page_available=True；session_rank=1；session_score=0.5467273；page_rank=9；page_score=0.5320224299116929；cutoff=5；recent1_rank=3；recent3_rank=11；required_context_rank=1；raw_rank=8；long_hit=False；effective_hit=False；semantic_equivalent=False；primary=DATASET_WRONG_GOLD；secondary=['AMBIGUOUS_QUERY_LABEL']。

### S001-Q022 → S001-Q012

distance=10；page_available=True；session_rank=1；session_score=0.54393923；page_rank=8；page_score=0.536408272735022；cutoff=5；recent1_rank=3；recent3_rank=2；required_context_rank=2；raw_rank=17；long_hit=False；effective_hit=False；semantic_equivalent=False；primary=QUERY_FORMULATION；secondary=[]。

### S001-Q022 → S001-Q018

distance=4；page_available=True；session_rank=1；session_score=0.54393923；page_rank=16；page_score=0.505801585013552；cutoff=5；recent1_rank=18；recent3_rank=6；required_context_rank=7；raw_rank=11；long_hit=False；effective_hit=False；semantic_equivalent=False；primary=TOP_K_CUTOFF；secondary=[]。

### S001-Q026 → S001-Q014

distance=12；page_available=True；session_rank=1；session_score=0.46521407；page_rank=18；page_score=0.42373109713402024；cutoff=5；recent1_rank=13；recent3_rank=11；required_context_rank=1；raw_rank=21；long_hit=True；effective_hit=True；semantic_equivalent=False；primary=TOP_K_CUTOFF；secondary=[]。

### S001-Q028 → S001-Q022

distance=6；page_available=True；session_rank=1；session_score=0.47551784；page_rank=10；page_score=0.4305984413831803；cutoff=5；recent1_rank=5；recent3_rank=8；required_context_rank=4；raw_rank=3；long_hit=False；effective_hit=False；semantic_equivalent=False；primary=PAGE_REPRESENTATION；secondary=['SUMMARY_INFORMATION_LOSS']。

### S001-Q033 → S001-Q023

distance=10；page_available=True；session_rank=1；session_score=0.49542856；page_rank=6；page_score=0.4944001522167531；cutoff=5；recent1_rank=14；recent3_rank=3；required_context_rank=1；raw_rank=12；long_hit=False；effective_hit=False；semantic_equivalent=False；primary=QUERY_FORMULATION；secondary=[]。

### S001-Q035 → S001-Q029

distance=6；page_available=True；session_rank=1；session_score=0.44298857；page_rank=7；page_score=0.4573331120967749；cutoff=5；recent1_rank=23；recent3_rank=13；required_context_rank=1；raw_rank=14；long_hit=False；effective_hit=False；semantic_equivalent=False；primary=TOP_K_CUTOFF；secondary=[]。

### S001-Q039 → S001-Q027

distance=12；page_available=True；session_rank=1；session_score=0.4824053；page_rank=19；page_score=0.43028598657154776；cutoff=5；recent1_rank=20；recent3_rank=18；required_context_rank=8；raw_rank=16；long_hit=False；effective_hit=False；semantic_equivalent=False；primary=TOP_K_CUTOFF；secondary=[]。

### S001-Q039 → S001-Q032

distance=7；page_available=True；session_rank=1；session_score=0.4824053；page_rank=9；page_score=0.47584253681265065；cutoff=5；recent1_rank=22；recent3_rank=25；required_context_rank=12；raw_rank=6；long_hit=False；effective_hit=False；semantic_equivalent=False；primary=TOP_K_CUTOFF；secondary=[]。

### S001-Q042 → S001-Q036

distance=6；page_available=True；session_rank=1；session_score=0.5010129；page_rank=9；page_score=0.5084946762147605；cutoff=5；recent1_rank=8；recent3_rank=9；required_context_rank=1；raw_rank=23；long_hit=False；effective_hit=False；semantic_equivalent=False；primary=TOP_K_CUTOFF；secondary=[]。

### S001-Q044 → S001-Q034

distance=10；page_available=True；session_rank=1；session_score=0.40127915；page_rank=8；page_score=0.39585025533747364；cutoff=5；recent1_rank=14；recent3_rank=7；required_context_rank=1；raw_rank=19；long_hit=False；effective_hit=False；semantic_equivalent=True；primary=EQUIVALENT_ALTERNATIVE_PAGE；secondary=[]。

### S001-Q044 → S001-Q040

distance=4；page_available=True；session_rank=1；session_score=0.40127915；page_rank=24；page_score=0.35351580914599096；cutoff=5；recent1_rank=4；recent3_rank=3；required_context_rank=5；raw_rank=26；long_hit=False；effective_hit=False；semantic_equivalent=True；primary=EQUIVALENT_ALTERNATIVE_PAGE；secondary=[]。

### S001-Q049 → S001-Q043

distance=6；page_available=True；session_rank=1；session_score=0.42377004；page_rank=11；page_score=0.4399636931680706；cutoff=5；recent1_rank=7；recent3_rank=14；required_context_rank=2；raw_rank=13；long_hit=False；effective_hit=False；semantic_equivalent=False；primary=TOP_K_CUTOFF；secondary=[]。

