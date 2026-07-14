# FinanceMemory-Mini-CN × Mem0 测试脚本

## 文件

- `run_finance_memory_benchmark.py`：主测试脚本
- 需要与下列 v4 数据文件放在同一目录：
  - `finance_memory_mini_timeline.json`
  - `parse_and_evaluate.py`

## 运行前

不要把 API Key 直接写进代码：

```bash
export DEEPSEEK_API_KEY="你的 DeepSeek API Key"
```

Windows PowerShell：

```powershell
$env:DEEPSEEK_API_KEY="你的 DeepSeek API Key"
```

## 先检查执行计划

```bash
python run_finance_memory_benchmark.py --dry-run
```

## 推荐：低 Token 增量模式

```bash
python run_finance_memory_benchmark.py \
  --mode incremental \
  --run-name my_memory_v1 \
  --top-k 5 \
  --cutoffs 1,3,5 \
  --answer-top-k 5
```

处理顺序：

```text
按时间写入 S001
    ↓
测试 Q001、Q002
    ↓
写入 S002
    ↓
测试 Q003
    ↓
写入 S003
    ↓
测试 Q004、Q005
    ↓
写入 S004
    ↓
测试 Q006、Q007、Q009
    ↓
写入 S005
    ↓
测试 Q008
```

测试问题和生成答案不会调用 `m.add()`，因此不会污染后续 Memory。

## 官方 LongMemEval 风格隔离模式

```bash
python run_finance_memory_benchmark.py \
  --mode isolated \
  --run-name my_memory_v1_isolated
```

每个问题使用不同的 `user_id`，并重新写入该题的 `visible_session_ids`。
隔离更严格，但会重复调用记忆抽取 LLM。

## 启用六维 LLM Judge

```bash
python run_finance_memory_benchmark.py \
  --mode incremental \
  --run-name my_memory_v1_judged \
  --judge
```

六个维度：

1. 金融正确性
2. 记忆忠实性
3. 时间与更新一致性
4. 个性化相关性
5. 安全与不确定性处理
6. 清晰度与是否答非所问

不加 `--judge` 时，仍会使用数据包中的规则评分。

## 只测试写入和检索

```bash
python run_finance_memory_benchmark.py \
  --mode incremental \
  --run-name retrieval_only \
  --skip-answer
```

## 接入自定义 Mem0

脚本中的 `build_memory()` 与 `quick_run.py` 使用相同的：

```python
Memory.from_config(config)
```

将你自己的短期、中期记忆参数加入 `build_memory()` 中的 `config` 即可。
后面的调用保持不变：

```python
m.add(...)
m.search(...)
m.llm.generate_response(...)
m.get_all(...)
```

## 输出

```text
results/<run-name>/
├── predictions.jsonl
├── raw_results.json
├── ingestion_log.jsonl
├── memory_snapshots.json
├── mem0_id_metadata_map.json
├── run_config.json
└── scores.json
```

`predictions.jsonl` 可以直接交给数据包中的 `parse_and_evaluate.py`。
