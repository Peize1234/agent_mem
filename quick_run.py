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




import sys

def get_max_score(n, k, v):
    max_score = 0

    for a in range(min(n, k) + 1):
        for b in range(min(n - a, k - a) + 1):
            hand = []

            if a > 0:
                hand.extend(v[:a])

            if b > 0:
                hand.extend(v[n - b:])

            hand.sort()

            rem_ops = k - (a + b)
            current_score = 0

            for card in hand:
                if card < 0 and rem_ops > 0:
                    rem_ops -= 1
                else:
                    current_score += card

            max_score = max(max_score, current_score)

    return max_score

input_data = sys.stdin.read().split()

if len(input_data) >= 2:
    N = int(input_data[0])
    K = int(input_data[1])
    V = [int(x) for x in input_data[2: 2 + N]]

    ans = get_max_score(N, K, V)

    print(ans)