---
name: user-memory-auto-tuner
description: 手动从 SQLite 原始多 Session 对话生成历史依赖 Benchmark，复用 memory-retrieval-tuner 离线调参，并把完整的用户级推荐 production overrides 写入审计表。
---

# User Memory Auto Tuner

这是一个手动运行的离线 Demo。它不会改变 `Memory` / `AsyncMemory` 的配置加载、`add/search` 链路、后台任务或线上热更新行为。

## 使用方式

从仓库根目录运行：

```bash
python .agents/skills/user-memory-auto-tuner/scripts/run_user_tuning.py \
  user_id=<user_id> \
  history_db_path=<history.db> \
  output_dir=<run_output_dir> \
  budget=standard k=5
```

Skill 调用形式：

```text
$user-memory-auto-tuner user_id=<user_id> history_db_path=<history.db> output_dir=<run_output_dir>
```

`history_db_path` 默认取 Repository Production `MemoryConfig.history_db_path`。Labeler 默认复用 Repository Production 的 LLM provider/model/config，并从正常环境变量读取凭据；不硬编码 provider、model 或 API key。需要覆盖 Labeler LLM 时，传入只包含 `MemoryConfig` 部分覆盖的 JSON：

```bash
python .agents/skills/user-memory-auto-tuner/scripts/run_user_tuning.py \
  user_id=<user_id> \
  labeler_memory_config=<memory-config-override.json>
```

默认数据门槛为至少 3 个 Session、3 个含 Gold 的 Session、3 个有效历史依赖样本，以及 3 个 Session 中至少 3 个真正超出 ShortTerm 的 retrieval-required 样本。门槛可用 `min_sessions`、`min_gold_sessions`、`min_valid_dependency_samples`、`min_retrieval_required_samples`、`min_retrieval_required_sessions` 调整。普通 Gold 不足返回 `NOT_ENOUGH_DATA`；普通 Gold 足够但 retrieval-required Gold 不足返回 `NOT_ENOUGH_RETRIEVAL_DATA`。两种情况都不调用 tuner。

## 固定流程

1. 以 SQLite 只读连接读取 `messages`，按当前 `_build_session_scope()` 格式精确匹配 `user_id`。
2. 按完整 `session_scope` 分组，并按 `turn_index / created_at / rowid` 恢复顺序；只使用原始 user/assistant 文本。
3. 对每个当前 QA 执行 `Labeler -> Verifier -> deterministic validation`。
4. 按 frozen baseline 的 `midterm.short_term_capacity // 2` 计算 ShortTerm QA 窗口，生成 tuner 原生 schema 的 `generated_benchmark.xlsx` 和真实 Session/turn 映射 manifest。ShortTerm 内的 QA 仍完整保留。
5. 使用现有 `memory-retrieval-tuner` 的 `dataset_audit.py` 审查数据集。
6. 直接调用现有 `tuner.orchestrator.run_tuning()`；Dataset Audit、Baseline、Session split、Branch Search、Validation、overfit 与 winner selection 均由现有 tuner 完成。
7. 对 winner 调用现有 `production_overrides_from_candidate()`，去除只是复述 frozen effective baseline 的值，与上一版完整推荐配置合并，经 Repository Production `MemoryConfig` 校验后完整写入推荐配置表。

生产迁移完成后已经从 `messages` 删除的溢出行不会被 summary、LongTerm 或 retrieval result 反向重建；本 Demo 只使用运行时数据库中仍实际存在的原始消息。因此历史保留量不足会直接进入 `NOT_ENOUGH_DATA`。

当前最新 user recommended config（如果存在）只作为本次离线 tuner baseline override，并在保存 winner 时保留未被新 Candidate 改写的字段。生产代码不会读取该配置。

## 标签与 Gold 规则

- Labeler 只能看到当前 QA 和同一 Session 的 earlier turns，不接收未来 turn 或其他 Session。
- `requirements` 之间为 AND；同一 requirement 的多个依赖 ID 只有在提供等价信息时才表示 OR。
- 每个 `required_contexts` 成员必须与对应 earlier QA 的原文片段逐字匹配。
- Verifier 独立检查必要性、原文支持和 AND/OR 语义。
- 确定性校验拒绝不存在、当前/未来、重复、证据不匹配、低置信度或 Verifier 拒绝的标签。
- 被拒绝的 turn 保留为 source-only workbook 行，但不会成为 Gold；详细原因写入 `dependency_labels.json` 与 manifest。
- 当前只生成同一 Session 内 Gold；状态固定为 `CROSS_SESSION_TUNING_UNSUPPORTED_NO_GOLD`。

## Retrieval-required 判定

- 当前 Query 为 `Qn`、ShortTerm 窗口为 `k` 个 QA 时，`Q(n-k)` 到 `Q(n-1)` 可由 ShortTerm 满足。
- 同一 requirement 内多个 dependency ID 是 OR：任一等价来源位于 ShortTerm 内，该 requirement 就不需要 retrieval；全部位于窗口外才需要。
- 多个 requirements 是 AND：只要任一必要 requirement 无法由 ShortTerm 满足，整条 Query 就计为 retrieval-required。
- manifest 与 `result.json` 记录 `retrieval_required_samples`、`retrieval_required_sessions`、`shortterm_only_dependency_samples` 和实际 `shortterm_qa_turns`。

## 推荐配置保存与未来应用提示

数据库保存 tuner 实际验证并选中的完整合法 `production_overrides`，包括 retrieval/query-time 字段以及：

- ShortTerm capacity；
- Page representation；
- Session assignment/formation 参数；
- Page summary / Session merge prompt 与 request options；
- Fine-grained LongTerm extraction prompt 与 request options；
- retention、heat、promotion 等由当前 tuner 合法产出的 winner 字段。

可能需要重新生成 Memory source 或 state replay 的字段同时写入 `future_apply_notes`，值为 `REBUILD_REQUIRED`。这是未来真正应用推荐配置时的提示，不会从 SQLite 推荐配置中删除这些字段；本 Skill 本身不执行 rebuild，也不会让线上 Memory 读取推荐配置。

Promoted/cross-session 参数仍处于 `CROSS_SESSION_TUNING_UNSUPPORTED_NO_GOLD` 边界。如果 winner 非法包含这类变化，本次保存会 fail closed，而不是部分保存一个没有被 tuner 完整验证的配置。

## SQLite 表

Skill 自己执行：

```sql
CREATE TABLE IF NOT EXISTS user_memory_tuning_configs (...);
CREATE TABLE IF NOT EXISTS user_memory_tuning_config_history (...);
```

`user_memory_tuning_configs` 对每个用户仅保留一行 latest recommended config，字段为 `user_id`、`config_version`、`config_overrides_json`、`source_run_dir`、`dataset_hash`、`validation_metrics_json`、`created_at`、`updated_at`。更新前的完整推荐行写入 history 表。

## 输出

`result.json` 至少记录：`user_id`、Session/QA/有效/过滤/retrieval-required 数量、Benchmark 和 manifest 路径、tuner run dir、baseline/best validation metrics、最终 production overrides、future-apply/unsupported metadata、config version 与 `saved | skipped | failed`。

兼容字段 `deployed=true` 只表示 recommended config 已写入 `user_memory_tuning_configs`，不表示线上 `Memory` 已经生效；新调用方应使用 `status=saved` 与 `saved=true`。
