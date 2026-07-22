# 任务开始前数据库快照

## 任务配置

```json
{
  "selected_sessions": [
    "S002",
    "S004"
  ],
  "user_id": "invest_user_001",
  "dataset_path": "/home/peize/Code/htzq/mem0/finance_memory_single_session_package/finance_memory_single_session_timeline.json",
  "database_path": "/home/peize/Code/htzq/mem0/finance_memory_single_session_package/results/retrieve_context_add_trace_state/history.db",
  "vector_collection": "agent_mem_retrieve_context_add_trace_state",
  "memory_config": {
    "llm": {
      "provider": "deepseek",
      "config": {
        "model": "deepseek-v4-flash",
        "api_key": "***REDACTED***",
        "temperature": 0.2,
        "max_tokens": 2400
      }
    },
    "embedder": {
      "provider": "huggingface",
      "config": {
        "model": "BAAI/bge-small-zh-v1.5"
      }
    },
    "vector_store": {
      "provider": "qdrant",
      "config": {
        "collection_name": "agent_mem_retrieve_context_add_trace_state",
        "path": "/home/peize/Code/htzq/mem0/finance_memory_single_session_package/results/retrieve_context_add_trace_state/qdrant",
        "embedding_model_dims": 512
      }
    },
    "history_db_path": "/home/peize/Code/htzq/mem0/finance_memory_single_session_package/results/retrieve_context_add_trace_state/history.db",
    "midterm": {
      "enabled": true,
      "short_term_capacity": 4,
      "session_similarity_threshold": 0.5,
      "top_k_sessions": 5,
      "top_k_pages": 5,
      "max_total_pages": 5
    },
    "profile": {
      "enabled": true,
      "update_on_add": true
    }
  }
}
```

快照时间：2026-07-21T07:16:30.740544+00:00

## SQLite 数据库

### 表：history

记录数：0

当前表为空。

### 表：messages

记录数：0

当前表为空。

### 表：profile_attributes

记录数：9

| attribute_id | attribute_key | attribute_name | attribute_category | description | value_type | value_schema_json | merge_policy | is_predefined | is_active | created_at | updated_at |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | risk_level | 风险等级 | risk | 用户明确表达的投资风险承受等级 | string | {"type":"string","enum":["conservative","balanced","aggressive"]} | replace | 1 | 1 | 2026-07-21T07:16:30.716162+00:00 | 2026-07-21T07:16:30.716162+00:00 |
| 2 | max_acceptable_loss_ratio | 最大可接受亏损比例 | risk | 用户可接受的最大本金亏损比例，5%保存为0.05 | number | {"type":"number","minimum":0,"maximum":1} | replace | 1 | 1 | 2026-07-21T07:16:30.716162+00:00 | 2026-07-21T07:16:30.716162+00:00 |
| 3 | investment_horizon | 投资期限 | investment | 用户明确表达的主要投资期限 | string | {"type":"string","enum":["short_term","medium_term","long_term"]} | replace | 1 | 1 | 2026-07-21T07:16:30.716162+00:00 | 2026-07-21T07:16:30.716162+00:00 |
| 4 | liquidity_requirement | 流动性要求 | investment | 用户对资金可随时支取能力的要求 | string | {"type":"string","enum":["low","medium","high"]} | replace | 1 | 1 | 2026-07-21T07:16:30.716162+00:00 | 2026-07-21T07:16:30.716162+00:00 |
| 5 | preferred_products | 偏好产品 | preference | 用户明确表示偏好的金融产品 | string_list | {"type":"array","items":{"type":"string"},"uniqueItems":true} | append_unique | 1 | 1 | 2026-07-21T07:16:30.716162+00:00 | 2026-07-21T07:16:30.716162+00:00 |
| 6 | avoided_products | 回避产品 | preference | 用户明确表示不愿持有或需要回避的金融产品 | string_list | {"type":"array","items":{"type":"string"},"uniqueItems":true} | append_unique | 1 | 1 | 2026-07-21T07:16:30.716162+00:00 | 2026-07-21T07:16:30.716162+00:00 |
| 7 | financial_knowledge_level | 金融知识水平 | knowledge | 用户明确表达或自我评估的金融知识水平 | string | {"type":"string","enum":["beginner","intermediate","advanced"]} | replace | 1 | 1 | 2026-07-21T07:16:30.716162+00:00 | 2026-07-21T07:16:30.716162+00:00 |
| 8 | current_financial_goals | 当前财务目标 | goal | 用户当前明确提出的财务目标 | object_list | {"type":"array","items":{"type":"object","properties":{"goal_type":{"type":"string"},"description":{"type":"string"},"target_date":{"type":["string","null"]},"target_amount":{"type":["number","null"],"minimum":0},"currency":{"type":["string","null"]},"status":{"type":"string","enum":["planned","in_progress","completed","cancelled"]}},"required":["goal_type","description"],"additionalProperties":false}} | replace | 1 | 1 | 2026-07-21T07:16:30.716162+00:00 | 2026-07-21T07:16:30.716162+00:00 |
| 9 | response_preferences | 回答偏好 | communication | 用户明确要求的回答语言、长度、格式或表达方式 | object | {"type":"object","properties":{"language":{"type":["string","null"]},"detail_level":{"type":["string","null"]},"format":{"type":["string","null"]},"tone":{"type":["string","null"]}},"additionalProperties":false} | replace | 1 | 1 | 2026-07-21T07:16:30.716162+00:00 | 2026-07-21T07:16:30.716162+00:00 |

### 表：user_profile_values

记录数：0

当前表为空。

## 长期记忆向量库

记录数：0

当前长期记忆向量库为空。

## 中期记忆

### Mid-term Sessions

记录数：0

当前表为空。

### Mid-term Pages

记录数：0

当前表为空。

## Entity Store

状态：未初始化

Entity Store 尚未初始化。

## 用户画像

当前用户画像为空。

## 数据库快照警告

无快照警告。
