import os
from pathlib import Path

from mem0 import Memory


DATA_DIR = Path("./quick_run_data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

USER_ID = "demo_user"
SESSION_ID = "demo_session"

config = {
    "llm": {
        "provider": "deepseek",
        "config": {
            "model": "deepseek-v4-flash",
            "api_key": os.environ["DEEPSEEK_API_KEY"],
        },
    },
    "embedder": {
        "provider": "huggingface",
        "config": {
            "model": "BAAI/bge-small-zh-v1.5",
        },
    },
    "vector_store": {
        "provider": "qdrant",
        "config": {
            "collection_name": "quick_run",
            "path": str(DATA_DIR / "qdrant"),
            "embedding_model_dims": 512,
        },
    },
    "history_db_path": str(DATA_DIR / "history.db"),
    "midterm": {
        "enabled": True,
    },
    "profile": {
        "enabled": True,
        "extraction_mode": "explicit_and_inferred",
        "llm_max_tokens": 4096,
        "llm_request_options": {
            "extra_body": {
                "thinking": {
                    "type": "enabled",
                },
            },
        },
    },
}


memory = Memory.from_config(config)

try:
    query = "我税后月收入约2万元，每月固定支出8000元，应该如何规划储蓄？"

    # 1. 检索短期、中期、长期记忆和用户画像，
    #    构造用于模型回答的 messages。
    messages = memory.build_agent_answer_messages(
        query,
        user_id=USER_ID,
        session_id=SESSION_ID,
    )

    # 2. 调用模型生成回答。
    answer = memory.llm.generate_response(messages=messages)

    print(f"用户：{query}")
    print(f"助手：{answer}")

    # 3. 将本轮完整问答写入记忆系统。
    memory.add(
        [
            {"role": "user", "content": query},
            {"role": "assistant", "content": answer},
        ],
        user_id=USER_ID,
        run_id=SESSION_ID,
    )

    # 4. 示例脚本立即退出，因此等待后台记忆任务处理完成。
    memory.flush_background_tasks(timeout=120)

finally:
    memory.close()
