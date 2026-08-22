from pathlib import Path

from mem0 import Memory
from mem0.configs.production import load_production_memory_config

DATA_DIR = Path("./quick_run_data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

USER_ID = "demo_user"
SESSION_ID = "demo_session"

config = load_production_memory_config(
    {
        "vector_store": {
            "config": {
                "collection_name": "quick_run",
                "path": str(DATA_DIR / "qdrant"),
            },
        },
        "history_db_path": str(DATA_DIR / "history.db"),
        "profile": {
            "llm_request_options": {
                "extra_body": {
                    "thinking": {
                        "type": "enabled",
                    },
                },
            },
        },
    }
)


memory = Memory(config)

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
