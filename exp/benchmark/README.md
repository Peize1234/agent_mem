# Agent Memory 召回率与并发测试

本目录仅新增实验文件，不修改 `mem0` 原有实现。

- `run_recall_benchmark.py`：评测短期、中期、长期及中长期联合召回指标。
- `run_concurrency_benchmark.py`：按指定 RPS 对 `build_agent_answer_messages()` 和 `add()` 施加并发负载。
- 两个脚本使用独立的 SQLite、Qdrant 路径和 collection，不会共享实验数据库。

## 1. 放置位置

将整个 `mem0/exp/benchmark` 目录复制到仓库根目录下，最终结构应为：

```text
agent_mem/
├── pyproject.toml
├── mem0/
│   ├── memory/
│   └── exp/
│       ├── enterprise_finance_memory_sessions_100_v3_realistic_2023_2025.xlsx
│       └── benchmark/
│           ├── run_recall_benchmark.py
│           ├── run_concurrency_benchmark.py
│           └── ...
```

所有命令都应在仓库根目录执行。

## 2. 安装依赖

如果当前仓库环境已经能正常运行你的 Demo，只需要安装实验脚本额外依赖：

```bash
pip install -r mem0/exp/benchmark/requirements.txt
```

全新环境建议安装仓库的 `extras`，再安装实验依赖：

```bash
pip install -e ".[extras]"
pip install -r mem0/exp/benchmark/requirements.txt
```

`memory_config.json` 默认使用本地 Hugging Face 模型 `BAAI/bge-small-zh-v1.5`。首次运行时可能下载模型，需要确保模型已缓存或机器能够访问 Hugging Face。

## 3. 配置模型

默认配置文件：

```text
mem0/exp/benchmark/memory_config.json
```

真实 LLM 模式使用：

```json
{
  "llm": {
    "provider": "deepseek",
    "config": {
      "model": "deepseek-v4-flash",
      "api_key": "${DEEPSEEK_API_KEY}"
    }
  }
}
```

运行真实 LLM 测试前设置环境变量：

```bash
export DEEPSEEK_API_KEY="你的密钥"
```

如果当前项目正式运行使用了不同的 LLM、Embedder、中期参数或 Worker 数量，请直接把 `memory_config.json` 改成当前系统使用的配置。脚本只会自动覆盖以下字段：

- `history_db_path`
- Qdrant 的 `path` 和 `collection_name`
- `agentic_retrieval.enabled`
- 本次实验指定的画像开关
- `background.enabled=true`

结果目录中的 `effective_memory_config.json` 会保存实际配置，但密钥、令牌和密码会被脱敏。

## 4. 先运行召回率冒烟测试

冒烟配置只执行前两个 Session、每个 Session 前 15 轮：

```bash
python mem0/exp/benchmark/run_recall_benchmark.py \
  --config mem0/exp/benchmark/recall_benchmark_smoke.json
```

结果目录：

```text
mem0/exp/results/recall_smoke/
```

重点检查：

- `recall_summary.json` 中 `failed_turns` 是否为 0。
- `recall_turn_results.csv` 中 `error` 是否为空。
- `cross_session_leak_count` 和 `future_turn_leak_count` 是否全部为 0。
- 后台任务是否能在超时时间内完成。

## 5. 运行完整召回率测试

```bash
python mem0/exp/benchmark/run_recall_benchmark.py \
  --config mem0/exp/benchmark/recall_benchmark.json
```

默认行为：

- 加载全部 Sheet。
- 最多同时运行 50 个 Session。
- 单个 Session 内严格按 Excel 行顺序执行。
- 回答不调用回答模型，直接使用 Excel 的“最终回答”调用 `add()`。
- 内部记忆提取使用真实 LLM。
- 关闭用户画像更新，避免无关模型调用。
- 只把依赖距离超过短期 QA 容量的问题作为主要召回评测样本。
- 在需要评测长距离问题前，只等待该 Session 当前积累的迁移任务，不做全局屏障。

主要结果文件：

```text
recall_turn_results.jsonl       每轮完整原始结果
recall_turn_results.csv         每轮召回与耗时指标
recall_session_summary.csv      每个 Session 汇总
recall_failures.jsonl           未完整命中 Gold 依赖的诊断信息
recall_summary.json             全局汇总
```

召回结果分层：

- `short_*`：短期消息和未完成迁移桥接消息。
- `mid_page_*`：中期 Page。
- `mid_session_*`：中期 Session 摘要。
- `mid_*`：中期 Page 与 Session 的并集。
- `long_*`：长期向量记忆。
- `mid_long_*`：中期与长期并集，建议作为主要记忆召回指标。
- `all_*`：短期、中期、长期并集，表示最终上下文是否覆盖依赖。

每层包含：

- `recall`
- `precision`
- `hit`
- `full_recall`
- `mrr`

这里的召回率和准确率基于数据集中的“关联前序对话”做确定性来源匹配：

- `recall` 衡量 Gold 依赖被覆盖的比例。
- `precision` 衡量已召回来源中 Gold 依赖所占比例。
- `full_recall` 表示是否完整召回全部依赖。

脚本不会额外调用一个评审 LLM 判断语义，以免增加成本和随机性。数据集的“所需前文信息”会写入逐轮结果；未完整命中的案例还会在 `recall_failures.jsonl` 中保存截断后的召回内容，便于人工或后续离线语义评测。

## 6. 运行 50 RPS 基础设施并发测试

默认并发配置使用 Mock 内部 LLM，不调用 DeepSeek，主要测试：

- SQLite 并发写入与读取。
- Qdrant 检索与写入。
- 异步请求调度。
- 中期、长期和画像后台 Worker。
- 队列积压、租约、重试、Watchdog 恢复。
- `build_agent_answer_messages()` 和 `add()` 的耗时分布。

运行命令：

```bash
python mem0/exp/benchmark/run_concurrency_benchmark.py \
  --config mem0/exp/benchmark/concurrency_benchmark.json
```

默认参数：

```text
target_rps       = 50
max_in_flight    = 100
traffic_model    = uniform
warmup_requests  = 100
max_requests     = 1000
workload         = mixed
```

`mixed` 表示每个业务请求执行：

```text
build_agent_answer_messages
→ 直接读取 Excel 最终回答
→ add
```

正式计时之前，每个 Session 会先预填充 6 轮，并等待后台任务排空，使系统进入已经存在短中长期记忆的状态。

结果目录：

```text
mem0/exp/results/concurrency_50rps_mock/
```

## 7. 运行真实 LLM 并发测试

真实 LLM 配置默认只执行 300 个请求，避免首次测试产生过高成本：

```bash
export DEEPSEEK_API_KEY="你的密钥"

python mem0/exp/benchmark/run_concurrency_benchmark.py \
  --config mem0/exp/benchmark/concurrency_benchmark_real.json
```

结果目录：

```text
mem0/exp/results/concurrency_50rps_real_llm/
```

如果要扩大规模，修改：

```json
{
  "load": {
    "max_requests": 300,
    "duration_seconds": null
  }
}
```

两种控制方式只能命中先到达的限制：

- 按请求数：设置 `max_requests`，将 `duration_seconds` 设为 `null`。
- 按持续时间：设置 `duration_seconds`，将 `max_requests` 设为足够大或 `null`。

## 8. 并发测试结果

主要文件：

```text
concurrency_requests.jsonl
concurrency_requests.csv
concurrency_background_jobs.jsonl
concurrency_background_jobs.csv
concurrency_queue_samples.jsonl
concurrency_queue_samples.csv
concurrency_errors.jsonl
concurrency_summary.json
```

`concurrency_summary.json` 重点字段：

- `actual_schedule_rps`：调度器实际发起速率。
- `actual_frontend_completion_rps`：前台请求实际完成吞吐量。
- `max_in_flight_observed`：最大实际在途请求数。
- `schedule_lag_ms`：计划时间与真实开始时间的差距。
- `build_total_ms`：召回阶段耗时分布。
- `add_submit_ms`：`add()` 写入短期并完成后台入队的耗时。
- `migration_complete_ms`：迁移任务完成延迟。
- `profile_complete_ms`：画像任务完成延迟。
- `add_complete_ms`：当前 `add()` 相关后台任务全部完成的延迟。
- `background_drain_seconds`：停止发请求后后台队列排空时间。
- `queue_and_system_max`：队列、CPU、内存、线程数及事件循环延迟峰值。

判断系统能否稳定支持 50 RPS 时，不应只看 `add_submit_ms`。还应确认：

1. `actual_schedule_rps` 接近 50。
2. `schedule_lag_ms` 不随时间持续增长。
3. 后台 pending/retry 数量没有持续单向增长。
4. 停止流量后能够在超时时间内排空。
5. 错误率、超时率、SQLite 锁错误符合预期。
6. p95、p99 没有随着测试时长持续恶化。

## 9. 常用配置修改

### 改为每秒瞬时发送 50 个请求

```json
{
  "load": {
    "target_rps": 50,
    "traffic_model": "burst"
  }
}
```

### 使用泊松到达

```json
{
  "load": {
    "target_rps": 50,
    "traffic_model": "poisson"
  }
}
```

### 只测召回接口

```json
{
  "load": {
    "workload": "retrieval_only"
  }
}
```

### 只测添加接口

```json
{
  "load": {
    "workload": "add_only"
  }
}
```

### 评测所有需要前文的问题

修改 `recall_benchmark.json`：

```json
{
  "evaluation": {
    "evaluate_only_long_range": false
  }
}
```

### 降低真实模型并发

```json
{
  "execution": {
    "session_concurrency": 20
  }
}
```

召回测试中的 `session_concurrency` 控制同时推进的 Session 数；同一 Session 内始终保持顺序，不会并行执行多个轮次。

## 10. 数据安全与重复运行

- `reset_storage=true` 会删除当前配置中的实验 `runtime_dir`，不会删除正式数据库。
- 召回和并发配置使用不同 `runtime_dir` 与 collection。
- 不要把实验 `runtime_dir` 修改为正式运行目录。
- 每次重复运行默认从空实验数据库开始，便于比较结果。
