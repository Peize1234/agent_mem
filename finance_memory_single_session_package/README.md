# Agent Mem 严格单 Session 测试包

## 这版修正了什么

上一版数据包含跨 Session 问题，搜索只按 `user_id` 过滤，因此长期和中期记忆可以从其他 `run_id` 返回。

这版明确改为：

```text
每个 Session 完全隔离
短期、中期、长期都只能访问当前 user_id + run_id
每个问题只属于一个 Session
不测试跨 Session 检索
```

五个 Session 仍按 S001 → S005 的顺序运行，但前一个 Session 的任何记忆都不能进入后一个 Session 的搜索结果或 Prompt。

## 严格隔离的检索条件

```python
memory.search(
    question,
    filters={
        "user_id": user_id,
        "run_id": current_session_id,
    },
    top_k=top_k,
)
```

中期 Session/Page 全量查看也使用相同过滤器：

```python
filters = {
    "user_id": user_id,
    "run_id": current_session_id,
}
```

## 新数据中的9道题

| Session | 问题 |
|---|---|
| S001 | 初始10%风险值 |
| S001 | 债券利率与久期解释 |
| S001 | 宽基偏好和排斥产品 |
| S002 | 购房日期、金额和独立账户 |
| S003 | 本Session中的购房日期更新 |
| S003 | 5%风险限制与新能源首付判断 |
| S004 | 收入删除和隐私拒答 |
| S005 | 回答结构偏好 |
| S005 | 按本Session风格解释复利 |

Q005和Q006虽然涉及更新和多条证据，但所有证据都在S003内部，不依赖S001或S002。

## 中期记忆开关

开启：

```bash
--midterm-enabled
```

关闭：

```bash
--no-midterm-enabled
```

脚本默认会修复原代码中的短期窗口耦合问题：

```text
中期开启：短期最多4条，淘汰QA进入中期
中期关闭：短期仍最多4条，淘汰QA直接丢弃
```

这样开关实验只改变中期层，不会把短期窗口从4条变成10条。

## 放置目录

将完整包解压到 `agent_mem` 仓库根目录：

```text
agent_mem/
├── mem0/
└── finance_memory_single_session_package/
    ├── run_agent_mem_single_session_test.py
    ├── finance_memory_single_session_timeline.json
    └── parse_and_evaluate.py
```

进入包目录或在仓库根目录指定脚本路径均可。

## 运行

配置Key：

```bash
export DEEPSEEK_API_KEY="你的Key"
```

检查数据：

```bash
python finance_memory_single_session_package/run_agent_mem_single_session_test.py \
  --dry-run
```

开启中期：

```bash
python finance_memory_single_session_package/run_agent_mem_single_session_test.py \
  --run-name single_session_midterm_on \
  --midterm-enabled \
  --short-term-capacity 4 \
  --top-k 5 \
  --judge
```

关闭中期：

```bash
python finance_memory_single_session_package/run_agent_mem_single_session_test.py \
  --run-name single_session_midterm_off \
  --no-midterm-enabled \
  --short-term-capacity 4 \
  --top-k 5 \
  --judge
```

两组必须使用不同 `run-name`。

结果目录已存在时，脚本会直接报错，防止重复写入。确实需要覆盖：

```bash
--overwrite
```

## 历史回答写入模式

端到端Agent流程：

```bash
--history-write-mode generated
```

执行：

```text
search → LLM生成回答 → add(用户问题, 模型回答)
```

严格中期开关消融推荐：

```bash
--history-write-mode reference
```

仍会调用LLM并记录模型回答，但向两组Memory写入完全相同的数据集参考回答，避免中期开关改变早期回答后进一步污染后续记忆。

## 结果文件

```text
results/<run_name>/
├── full_trace.txt
├── interactions.jsonl
├── evaluation_predictions.jsonl
├── evaluation_details.json
├── scores.json
├── layered_scores.json
├── final_memory_state_by_session.json
├── run_config.json
├── history.db
└── qdrant/
```

`final_memory_state_by_session.json` 按S001～S005分别保存最终三层状态，不再把各Session混在一起。

`layered_scores.json` 中：

```text
session_isolation_violation_total
```

必须为0。任何检索结果的 `run_id` 与当前Session不一致，都会被记录为隔离违规。
