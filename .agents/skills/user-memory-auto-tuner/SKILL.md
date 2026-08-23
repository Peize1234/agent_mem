---
name: user-memory-auto-tuner
description: 手动从 SQLite 原始多 Session 对话生成历史依赖 Benchmark，复用 memory-retrieval-tuner 离线调参，并把安全的用户级 retrieval overrides 写入审计表。
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

默认数据门槛为至少 3 个 Session、3 个含 Gold 的 Session、3 个有效历史依赖样本。门槛可用 `min_sessions`、`min_gold_sessions`、`min_valid_dependency_samples` 调整；低于门槛返回 `NOT_ENOUGH_DATA`，不调用 tuner。

## 固定流程

1. 以 SQLite 只读连接读取 `messages`，按当前 `_build_session_scope()` 格式精确匹配 `user_id`。
2. 按完整 `session_scope` 分组，并按 `turn_index / created_at / rowid` 恢复顺序；只使用原始 user/assistant 文本。
3. 对每个当前 QA 执行 `Labeler -> Verifier -> deterministic validation`。
4. 生成 tuner 原生 schema 的 `generated_benchmark.xlsx` 和真实 Session/turn 映射 manifest。
5. 使用现有 `memory-retrieval-tuner` 的 `dataset_audit.py` 审查数据集。
6. 直接调用现有 `tuner.orchestrator.run_tuning()`；Dataset Audit、Baseline、Session split、Branch Search、Validation、overfit 与 winner selection 均由现有 tuner 完成。
7. 对 winner 调用现有 `production_overrides_from_candidate()`，去除只是复述 frozen effective baseline 的值，只保留 retrieval/query-time 参数，经 Repository Production `MemoryConfig` 校验后写入配置表。

生产迁移完成后已经从 `messages` 删除的溢出行不会被 summary、LongTerm 或 retrieval result 反向重建；本 Demo 只使用运行时数据库中仍实际存在的原始消息。因此历史保留量不足会直接进入 `NOT_ENOUGH_DATA`。

当前 active user config（如果存在）只作为本次离线 tuner baseline override，并在保存 winner 时保留未被新 Candidate 改写的安全字段。生产代码不会读取该配置。

## 标签与 Gold 规则

- Labeler 只能看到当前 QA 和同一 Session 的 earlier turns，不接收未来 turn 或其他 Session。
- `requirements` 之间为 AND；同一 requirement 的多个依赖 ID 只有在提供等价信息时才表示 OR。
- 每个 `required_contexts` 成员必须与对应 earlier QA 的原文片段逐字匹配。
- Verifier 独立检查必要性、原文支持和 AND/OR 语义。
- 确定性校验拒绝不存在、当前/未来、重复、证据不匹配、低置信度或 Verifier 拒绝的标签。
- 被拒绝的 turn 保留为 source-only workbook 行，但不会成为 Gold；详细原因写入 `dependency_labels.json` 与 manifest。
- 当前只生成同一 Session 内 Gold；状态固定为 `CROSS_SESSION_TUNING_UNSUPPORTED_NO_GOLD`。

## 自动写入边界

允许自动写入的字段仅限：

- Query rewrite prompt；
- MidTerm top-k、page/session limits、threshold、candidate-pool、dense/BM25 fusion 和 layer reranker；
- Fine-grained LongTerm query-time top-k、threshold、candidate-pool、session weight、score weights、entity threshold 和 layer reranker；
- Agentic query/result query-time limits。

以下字段不会写入 active config，并在结果中标记 `REBUILD_REQUIRED`：

- ShortTerm capacity；
- Page representation；
- Session assignment/formation 参数；
- Page summary / Session merge prompt 与 request options；
- Fine-grained LongTerm extraction prompt 与 request options；
- embedding/vector source、retention/heat/promotion 等需要 source regeneration 或 state replay 的字段；
- 任何未明确列入安全 allowlist 的字段。

Promoted/cross-session 参数标记 `CROSS_SESSION_TUNING_UNSUPPORTED_NO_GOLD`。本 Skill 不实现 rebuild。

## SQLite 表

Skill 自己执行：

```sql
CREATE TABLE IF NOT EXISTS user_memory_tuning_configs (...);
CREATE TABLE IF NOT EXISTS user_memory_tuning_config_history (...);
```

`user_memory_tuning_configs` 对每个用户仅保留一行 active config，字段为 `user_id`、`config_version`、`config_overrides_json`、`source_run_dir`、`dataset_hash`、`validation_metrics_json`、`created_at`、`updated_at`。更新前的完整 active 行写入 history 表。

## 输出

`result.json` 至少记录：`user_id`、Session/QA/有效/过滤数量、Benchmark 和 manifest 路径、tuner run dir、baseline/best validation metrics、最终 production overrides、rebuild/unsupported 字段、config version 与 `deployed | skipped | failed`。

`deployed` 只表示 overrides 已写入 `user_memory_tuning_configs`，不表示线上 `Memory` 已经生效。
