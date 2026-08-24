# PPT Evidence Pack

| 类型 | 候选数 | FOUND / NOT_FOUND | 推荐 Session | 推荐 Query / Job | 是否建议展示 |
| -- | --: | ----------------- | ---------- | -------------- | ------ |
| A 中期记忆表示生成 | 592 | FOUND | S001_贵州茅台_投研 | S001-Q016 | 是 |
| B 两层检索 | 43 | FOUND | S001_贵州茅台_投研 | S001-Q009 | 是 |
| C 混合检索与 Reranker | 133 | FOUND | S002_贵州茅台_内审 | S002-Q011 | 是 |
| D 模型主动二次补查 | 12 | SIMULATED | S001_贵州茅台_投研 | S001-Q011 | 仅作模拟演示 |
| E Query 上下文重写 | 84 | FOUND | S001 | S001-Q013 | 是 |
| F 上下文感知归档 | 333 | FOUND | S001 | S001-Q024 | 是 |
| G 记忆衰减与强化 | 37 | FOUND | S001_贵州茅台_投研 | S001-Q024 | 是 |
| H 高频记忆跨 Session 沉淀 | 12 | SIMULATED | S001_贵州茅台_投研 | S001-Q005 | 仅作模拟演示 |
| I 跨 Session 降权 | 10 | SIMULATED | S001_贵州茅台_投研 | S001-Q002 | 仅作模拟演示 |
| J 用户画像真实更新 | 12 | SIMULATED | S001_贵州茅台_投研 | S001-Q001 | 仅作模拟演示 |
| K Profile + Custom Prompt | 12 | SIMULATED | S001_贵州茅台_投研 | S001-Q001 | 仅作模拟演示 |
| L 异步后台沉淀 | 602 | FOUND | run_id=S001_贵州茅台_投研&user_id=r… | S001-Q006 | 是 |
| M 多 Session 并发、单 Session 有序 | 24 | FOUND | S001 + S002 | 34b75e22-659f-5350-baa7-79702… | 是 |
| N Migration 数据保护 | 592 | FOUND | run_id=S001_贵州茅台_投研&user_id=r… | S001-Q029 | 是 |
| O Failure / Retry / Recovery / Degraded | 4 | FOUND | run_id=S002_贵州茅台_内审&user_id=r… | S002-Q005 | 是 |
| P 自动调参真实过程 | 29 | FOUND | tune split: S002/S003/S005/S0… | 966ffacdbf96bd16e523f96f8d71b… | 是 |
| Q 真实用户历史 → Benchmark → 个性化调参 | 10 | FOUND | S007_美的集团_授信 |  | 是 |

> 证据口径：`FOUND` 卡片的机制过程来自真实持久化记录；`SIMULATED` 卡片只复用真实对话/Memory，机制步骤没有真实发生，HTML 与 Excel 均保留模拟标签。

## A. 中期记忆表示生成

- 状态 / 数量：FOUND / 592 条。
- Top 3：A-S001-1c1ca3de-fdb4-5683-adcd-c13d331cd401, A-S001-43711fe9-79f1-526b-8d75-ca4adca60443, A-S001-95851d7f-0b1e-54a4-b150-2f78ac6d16c8。
- Top 1 原因：S001-Q016 将 10,489 字原始问答压缩为 765 字检索表示（压缩 92.7%），摘要、关键词与问题三段结构清楚。
- 数据完整度：达到 10 条。
- 扫描统计：`{"final_snapshot_page_count": 592, "session_count": 10}`
- 结构化证据：[`raw/A_midterm_representation.json`](raw/A_midterm_representation.json)

## B. 两层检索

- 状态 / 数量：FOUND / 43 条。
- Top 3：B-S001-S001-Q009, B-S001-S001-Q017, B-S001-S001-Q018。
- Top 1 原因：同一条真实查询同时保留 Session 路由、Page 候选和最终历史；正确依赖最终 Rank 1。
- 数据完整度：达到 10 条。
- 扫描统计：`{"S001_checkpoint_count": 50, "dependent_query_count": 43}`
- 结构化证据：[`raw/B_hierarchical_retrieval.json`](raw/B_hierarchical_retrieval.json)

## C. 混合检索与 Reranker

- 状态 / 数量：FOUND / 133 条。
- Top 3：C-S002-Q011-f70d87dd, C-S002-Q013-f70d87dd, C-S002-Q021-691ce2a8。
- Top 1 原因：Gold 历史从 Dense Rank 6 → Hybrid Rank 7 → Reranker Rank 1（净变化 +5），三阶段排名来自冻结实验。
- 数据完整度：达到 10 条。
- 扫描统计：`{"movement_rows": 154, "usable_candidates": 133, "changed": 126, "improved": 53, "unchanged": 14, "worse": 66}`
- 结构化证据：[`raw/C_hybrid_ranking.json`](raw/C_hybrid_ranking.json)

## D. 模型主动二次补查

- 状态 / 数量：SIMULATED / 12 条。
- Top 3：D-SIM-S001-S001-Q011, D-SIM-S001-S001-Q004, D-SIM-S001-S001-Q003。
- Top 1 原因：使用真实连续对话 ['S001-Q008', 'S001-Q009', 'S001-Q010'] 与真实依赖 S001-Q001 构造二次补查演示；Tool Call 与判断明确为模拟。
- 数据完整度：目标运行没有 Agentic Tool 事件；以下为真实对话驱动的显式模拟，不是运行证据。。
- 扫描统计：`{"observed_agentic_event_count": 0, "simulated_scenario_count": 12}`
- 结构化证据：[`raw/D_agentic_retrieval.json`](raw/D_agentic_retrieval.json)

## E. Query 上下文重写

- 状态 / 数量：FOUND / 84 条。
- Top 3：E-S001-S001-Q013, E-S003-S003-Q052, E-S005-S005-Q011。
- Top 1 原因：用最近 3 轮可见上下文，把 49 字省略式问题改写为 123 字独立问题；新增信息有审计分类。
- 数据完整度：达到 10 条。
- 扫描统计：`{"P1_changed_query_count": 84, "generation_input_excluded_gold": true}`
- 结构化证据：[`raw/E_query_rewrite.json`](raw/E_query_rewrite.json)

## F. 上下文感知归档

- 状态 / 数量：FOUND / 333 条。
- Top 3：F-S001-S001-Q024, F-S001-S001-Q012, F-S001-S001-Q030。
- Top 1 原因：真实归档输入合同同时记录当前 QA 与 6 条前/后文；审计识别出 6 个超出当前轮的信息项。
- 数据完整度：达到 10 条。
- 扫描统计：`{"successful_context_generation_rows": 333, "rows_with_nonempty_neighbor_context": 333}`
- 结构化证据：[`raw/F_context_archive.json`](raw/F_context_archive.json)

## G. 记忆衰减与强化

- 状态 / 数量：FOUND / 37 条。
- Top 3：G-S001-S001-Q024, G-S001-S001-Q032, G-S001-S001-Q033。
- Top 1 原因：同一检索中真实保留原始分、遗忘因子、Heat 因子与调整后分；代表项 Rank 2 → 1。
- 数据完整度：达到 10 条。
- 扫描统计：`{"S001_checkpoint_count": 50, "queries_with_observed_rank_change": 37}`
- 结构化证据：[`raw/G_decay_heat.json`](raw/G_decay_heat.json)

## H. 高频记忆跨 Session 沉淀

- 状态 / 数量：SIMULATED / 12 条。
- Top 3：H-SIM-S001-S001-Q002, H-SIM-S001-S001-Q010, H-SIM-S001-S001-Q011。
- Top 1 原因：以真实 Memory S001-Q002 和随后三条连续真实问题演示阈值链；召回归属、Heat 达标、Job 与跨 Session 写入均标为模拟。
- 数据完整度：目标运行扫描到 0 个 promotion event / job；以下仅为真实连续对话驱动的阈值演示。。
- 扫描统计：`{"observed_promotion_event_count": 0, "simulated_scenario_count": 12, "target_promotion_event_count": 0, "target_promotion_job_count": 0}`
- 结构化证据：[`raw/H_promotion.json`](raw/H_promotion.json)

## I. 跨 Session 降权

- 状态 / 数量：SIMULATED / 10 条。
- Top 3：I-SIM-S001-S002-2, I-SIM-S001-S002-1, I-SIM-S009-S010-2。
- Top 1 原因：使用同公司两个真实 Session 的真实 Memory 文本，按生产配置 other_session_weight=0.7 演示反超；相关分数与同次检索为模拟。
- 数据完整度：目标运行的 652 个 turn 中跨 Session recall 为 0；以下为真实 Memory 文本 + 真实配置权重的模拟排序。。
- 扫描统计：`{"observed_cross_session_recall_count": 0, "simulated_scenario_count": 10, "target_cross_session_valid_recall_count": 0, "target_turns_with_cross_session_results": 0}`
- 结构化证据：[`raw/I_cross_session_weight.json`](raw/I_cross_session_weight.json)

## J. 用户画像真实更新

- 状态 / 数量：SIMULATED / 12 条。
- Top 3：J-SIM-S001-S001-Q001, J-SIM-S001-S001-Q002, J-SIM-S001-S001-Q003。
- Top 1 原因：用真实用户表达演示画像从 before 到 after 的单字段更新；LLM Plan、校验和 DB 操作均为模拟并单独标注。
- 数据完整度：目标数据库没有 Profile Job/Profile 行；以下是同一 Session 连续真实表达驱动的累计画像模拟。。
- 扫描统计：`{"observed_profile_update_count": 0, "simulated_scenario_count": 12}`
- 结构化证据：[`raw/J_profile_update.json`](raw/J_profile_update.json)

## K. Profile + Custom Prompt

- 状态 / 数量：SIMULATED / 12 条。
- Top 3：K-SIM-S001-S001-Q001, K-SIM-S001-S001-Q002, K-SIM-S001-S001-Q003。
- Top 1 原因：长期画像由前序真实表达累计模拟，本轮 Custom Prompt 直接取自真实问题约束，最终回答为数据集中的真实回答。
- 数据完整度：Profile 应用未真实发生；Query、Custom Prompt 文本和最终回答取自真实对话，画像合并步骤为模拟。。
- 扫描统计：`{"observed_profile_custom_prompt_request_count": 0, "simulated_scenario_count": 12}`
- 结构化证据：[`raw/K_profile_custom_prompt.json`](raw/K_profile_custom_prompt.json)

## L. 异步后台沉淀

- 状态 / 数量：FOUND / 602 条。
- Top 3：L-S001-S001-Q006, L-S001-S001-Q007, L-S001-S001-Q008。
- Top 1 原因：同一请求的消息、Migration 与 Long-term Job 均有真实时间戳；Profile/Promotion 未触发也明确显示。
- 数据完整度：达到 10 条。
- 扫描统计：`{"target_source_database_count": 10, "matched_request_job_groups": 602}`
- 结构化证据：[`raw/L_async_jobs.json`](raw/L_async_jobs.json)

## M. 多 Session 并发、单 Session 有序

- 状态 / 数量：FOUND / 24 条。
- Top 3：M-S001-S002-1-1, M-S001-S002-2-1, M-S001-S002-1-2。
- Top 1 原因：S001 与 S002 的两段 Job 窗口真实重叠 16.90s；各 Session 内 sequence_no 保持递增。
- 数据完整度：达到 10 条。
- 扫描统计：`{"session_stream_count": 10, "overlapping_four_job_windows": 24}`
- 结构化证据：[`raw/M_concurrency.json`](raw/M_concurrency.json)

## N. Migration 数据保护

- 状态 / 数量：FOUND / 592 条。
- Top 3：N-S001-05688da3-8c6d-4e64-a42f-e1cdff4f649b, N-S001-05975df7-a2fe-4c8a-bec7-974b9fd6452e, N-S001-0e4a111b-6298-40f0-8b7e-48d893e0dc5c。
- Top 1 原因：Job→Page→committed→最终短期消息缺席的链路可观察；中间 staging 与删除时刻明确列为不可观察。
- 数据完整度：达到 10 条。
- 扫描统计：`{"migration_jobs_with_linked_committed_page": 592}`
- 结构化证据：[`raw/N_migration_safety.json`](raw/N_migration_safety.json)

## O. Failure / Retry / Recovery / Degraded

- 状态 / 数量：FOUND / 4 条。
- Top 3：O-producti-S002-cf601e0f-0149-582b-9140-b1a5506d1eca, O-producti-S003-fd880462-f335-5193-bf8e-77726e704802, O-producti-S006-ba02f6cd-1cb9-5630-a297-8e793344a4e3。
- Top 1 原因：真实 Job 保留 attempts=2、错误/恢复字段与最终状态；未持久化的逐次 Attempt 细节不补写。
- 数据完整度：不足 10 条，已保存全部。
- 扫描统计：`{"database_count": 222, "failure_signal_event_count": 4, "final_status_counts": {"succeeded": 3, "running": 1}}`
- 结构化证据：[`raw/O_failure_recovery.json`](raw/O_failure_recovery.json)

## P. 自动调参真实过程

- 状态 / 数量：FOUND / 29 条。
- Top 3：P-017-966ffacd, P-018-96bc859c, P-019-454dc30f。
- Top 1 原因：仅改 fusion, dense_weight，Recall@K 相对父方案 -4.82pp、MRR -0.0102，结果被标记为 REJECTED_NOT_BRANCH_WINNER。
- 数据完整度：目标运行在 stage 3 选型后停止，未生成 leaderboard/session_metrics/requirement_results/best_config/best_config_diff/final_report；只报告已执行候选。。
- 扫描统计：`{"search_trace_raw_rows": 151, "search_trace_unique_candidate_count": 30, "completed_experimental_candidate_count": 29, "research_trace_event_count": 25, "research_status_counts": {"REQUEST": 12, "VALIDATED": 9, "FAILED": 3, "DETERMINISTIC_FALLBACK": 1}, "worker_json_count": 0}`
- 结构化证据：[`raw/P_tuning_trace.json`](raw/P_tuning_trace.json)

## Q. 真实用户历史 → Benchmark → 个性化调参

- 状态 / 数量：FOUND / 10 条。
- Top 3：Q-PARTIAL-S007, Q-PARTIAL-S001, Q-PARTIAL-S003。
- Top 1 原因：可确认该 Session 有 96 条基准 QA、92 条历史依赖 Query；但目标运行不是 user-memory-auto-tuner 完整链路，未产生最终配置。
- 数据完整度：找到 10 个 Benchmark Session 的部分链路，但完整用户历史→Benchmark→held-out final→saved config 链路为 0。。
- 扫描统计：`{}`
- 结构化证据：[`raw/Q_personalized_tuning.json`](raw/Q_personalized_tuning.json)

## 安全验收

Existing repository files modified: NO

All generated files under exp/temp/: YES

SQLite access mode: `mode=ro&immutable=1` + `PRAGMA query_only=ON`

Vector store access: NOT OPENED（仅读取已有 JSON/JSONL 快照）

Experiment rerun / LLM call / Query Rewrite rerun: NO

Git baseline/final comparison: PASS（除允许的 `?? exp/temp/` 外，无新增、删除或改变的状态项；详见 `raw/git_safety_report.json`）

说明：上述结论以任务前后 Git 状态差异检查为准；`exp/temp/` 通常被 Git 忽略，但所有本任务新文件均位于本目录。