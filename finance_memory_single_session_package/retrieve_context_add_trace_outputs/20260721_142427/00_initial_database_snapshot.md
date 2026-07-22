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

## 完整数据库快照

```json
{
  "timestamp": "2026-07-21T06:24:27.751984+00:00",
  "sqlite": {
    "tables": {
      "history": [],
      "messages": [],
      "profile_attributes": [
        {
          "attribute_id": 1,
          "attribute_key": "risk_level",
          "attribute_name": "风险等级",
          "attribute_category": "risk",
          "description": "用户明确表达的投资风险承受等级",
          "value_type": "string",
          "value_schema_json": "{\"type\":\"string\",\"enum\":[\"conservative\",\"balanced\",\"aggressive\"]}",
          "merge_policy": "replace",
          "is_predefined": 1,
          "is_active": 1,
          "created_at": "2026-07-21T06:24:27.742667+00:00",
          "updated_at": "2026-07-21T06:24:27.742667+00:00"
        },
        {
          "attribute_id": 2,
          "attribute_key": "max_acceptable_loss_ratio",
          "attribute_name": "最大可接受亏损比例",
          "attribute_category": "risk",
          "description": "用户可接受的最大本金亏损比例，5%保存为0.05",
          "value_type": "number",
          "value_schema_json": "{\"type\":\"number\",\"minimum\":0,\"maximum\":1}",
          "merge_policy": "replace",
          "is_predefined": 1,
          "is_active": 1,
          "created_at": "2026-07-21T06:24:27.742667+00:00",
          "updated_at": "2026-07-21T06:24:27.742667+00:00"
        },
        {
          "attribute_id": 3,
          "attribute_key": "investment_horizon",
          "attribute_name": "投资期限",
          "attribute_category": "investment",
          "description": "用户明确表达的主要投资期限",
          "value_type": "string",
          "value_schema_json": "{\"type\":\"string\",\"enum\":[\"short_term\",\"medium_term\",\"long_term\"]}",
          "merge_policy": "replace",
          "is_predefined": 1,
          "is_active": 1,
          "created_at": "2026-07-21T06:24:27.742667+00:00",
          "updated_at": "2026-07-21T06:24:27.742667+00:00"
        },
        {
          "attribute_id": 4,
          "attribute_key": "liquidity_requirement",
          "attribute_name": "流动性要求",
          "attribute_category": "investment",
          "description": "用户对资金可随时支取能力的要求",
          "value_type": "string",
          "value_schema_json": "{\"type\":\"string\",\"enum\":[\"low\",\"medium\",\"high\"]}",
          "merge_policy": "replace",
          "is_predefined": 1,
          "is_active": 1,
          "created_at": "2026-07-21T06:24:27.742667+00:00",
          "updated_at": "2026-07-21T06:24:27.742667+00:00"
        },
        {
          "attribute_id": 5,
          "attribute_key": "preferred_products",
          "attribute_name": "偏好产品",
          "attribute_category": "preference",
          "description": "用户明确表示偏好的金融产品",
          "value_type": "string_list",
          "value_schema_json": "{\"type\":\"array\",\"items\":{\"type\":\"string\"},\"uniqueItems\":true}",
          "merge_policy": "append_unique",
          "is_predefined": 1,
          "is_active": 1,
          "created_at": "2026-07-21T06:24:27.742667+00:00",
          "updated_at": "2026-07-21T06:24:27.742667+00:00"
        },
        {
          "attribute_id": 6,
          "attribute_key": "avoided_products",
          "attribute_name": "回避产品",
          "attribute_category": "preference",
          "description": "用户明确表示不愿持有或需要回避的金融产品",
          "value_type": "string_list",
          "value_schema_json": "{\"type\":\"array\",\"items\":{\"type\":\"string\"},\"uniqueItems\":true}",
          "merge_policy": "append_unique",
          "is_predefined": 1,
          "is_active": 1,
          "created_at": "2026-07-21T06:24:27.742667+00:00",
          "updated_at": "2026-07-21T06:24:27.742667+00:00"
        },
        {
          "attribute_id": 7,
          "attribute_key": "financial_knowledge_level",
          "attribute_name": "金融知识水平",
          "attribute_category": "knowledge",
          "description": "用户明确表达或自我评估的金融知识水平",
          "value_type": "string",
          "value_schema_json": "{\"type\":\"string\",\"enum\":[\"beginner\",\"intermediate\",\"advanced\"]}",
          "merge_policy": "replace",
          "is_predefined": 1,
          "is_active": 1,
          "created_at": "2026-07-21T06:24:27.742667+00:00",
          "updated_at": "2026-07-21T06:24:27.742667+00:00"
        },
        {
          "attribute_id": 8,
          "attribute_key": "current_financial_goals",
          "attribute_name": "当前财务目标",
          "attribute_category": "goal",
          "description": "用户当前明确提出的财务目标",
          "value_type": "object_list",
          "value_schema_json": "{\"type\":\"array\",\"items\":{\"type\":\"object\",\"properties\":{\"goal_type\":{\"type\":\"string\"},\"description\":{\"type\":\"string\"},\"target_date\":{\"type\":[\"string\",\"null\"]},\"target_amount\":{\"type\":[\"number\",\"null\"],\"minimum\":0},\"currency\":{\"type\":[\"string\",\"null\"]},\"status\":{\"type\":\"string\",\"enum\":[\"planned\",\"in_progress\",\"completed\",\"cancelled\"]}},\"required\":[\"goal_type\",\"description\"],\"additionalProperties\":false}}",
          "merge_policy": "replace",
          "is_predefined": 1,
          "is_active": 1,
          "created_at": "2026-07-21T06:24:27.742667+00:00",
          "updated_at": "2026-07-21T06:24:27.742667+00:00"
        },
        {
          "attribute_id": 9,
          "attribute_key": "response_preferences",
          "attribute_name": "回答偏好",
          "attribute_category": "communication",
          "description": "用户明确要求的回答语言、长度、格式或表达方式",
          "value_type": "object",
          "value_schema_json": "{\"type\":\"object\",\"properties\":{\"language\":{\"type\":[\"string\",\"null\"]},\"detail_level\":{\"type\":[\"string\",\"null\"]},\"format\":{\"type\":[\"string\",\"null\"]},\"tone\":{\"type\":[\"string\",\"null\"]}},\"additionalProperties\":false}",
          "merge_policy": "replace",
          "is_predefined": 1,
          "is_active": 1,
          "created_at": "2026-07-21T06:24:27.742667+00:00",
          "updated_at": "2026-07-21T06:24:27.742667+00:00"
        }
      ],
      "user_profile_values": []
    }
  },
  "long_term_memory": [],
  "midterm_memory": {
    "sessions": [],
    "pages": []
  },
  "entity_store": {
    "initialized": false,
    "records": []
  },
  "profile_view": {
    "user_id": "invest_user_001",
    "profile": {}
  },
  "warnings": []
}
```
