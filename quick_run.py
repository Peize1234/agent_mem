import os
from typing import List, Dict, Any

# 尽量放在 from mem0 import Memory 之前，减少 PostHog warning
os.environ["MEM0_TELEMETRY"] = "false"
os.environ["POSTHOG_DISABLED"] = "true"

from mem0 import Memory

config = {
    "llm": {
        "provider": "deepseek",
        "config": {
            "model": "deepseek-v4-flash",
            "api_key": "sk-6d61d0e79e72401d8a6edb8d21761cab",
            "temperature": 0.2,
            "max_tokens": 2000,
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
            "collection_name": "mem0_test_512",
            "path": "/tmp/qdrant_mem0_test_512",
            "embedding_model_dims": 512,
        }
    }
}

m = Memory.from_config(config)

USER_ID = "invest_user_001"
RUN_ID = "session_001"


def format_memories(search_results: Dict[str, Any]) -> str:
    results = search_results.get("results", [])
    if not results:
        return "无相关历史记忆。"

    lines = []
    for i, item in enumerate(results, 1):
        memory_text = item.get("memory") or item.get("data")
        score = item.get("score")
        lines.append(f"{i}. {memory_text}，相关度：{score}")
    return "\n".join(lines)


def call_answer_llm(user_input: str, memory_context: str) -> str:
    """
    这里直接调用 mem0 初始化时创建的 LLM。
    注意：这是“回答用户问题”的 LLM 调用；
    后面的 m.add() 内部还会再次调用 LLM 做“记忆抽取”。
    """

    messages = [
        {
            "role": "system",
            "content": (
                "你是一个金融投资问答助手。"
                "你需要结合用户长期记忆回答问题。"
                "如果记忆中没有相关信息，要明确说明无法从历史记忆中确认。"
                "回答中不要编造用户没有说过的信息。"
            )
        },
        {
            "role": "user",
            "content": f"""
以下是从 mem0 检索到的用户长期记忆：

{memory_context}

用户当前问题：

{user_input}

请结合以上记忆回答用户。
"""
        }
    ]

    return m.llm.generate_response(messages=messages)


def agent_chat(user_input: str) -> str:
    print("\n" + "=" * 80)
    print(f"用户输入：{user_input}")

    # 1. 回答前先 search：检索旧记忆
    search_results = m.search(
        user_input,
        filters={
            "user_id": USER_ID,
            # 如果你希望限制在当前 session，可以打开这一行
            # "run_id": RUN_ID,
        },
        top_k=5,
    )

    memory_context = format_memories(search_results)

    print("\n[1] 回答前 search 到的长期记忆：")
    print(memory_context)

    # 2. 调用 LLM 生成回答
    assistant_answer = call_answer_llm(user_input, memory_context)

    print("\n[2] LLM 生成的回答：")
    print(assistant_answer)

    # 3. 回答后 add：把本轮对话交给 mem0，让它判断是否需要沉淀新记忆
    add_result = m.add(
        [
            {"role": "user", "content": user_input},
            {"role": "assistant", "content": assistant_answer},
        ],
        user_id=USER_ID,
        run_id=RUN_ID,
    )

    print("\n[3] 回答后 add 的记忆写入结果：")
    print(add_result)

    return assistant_answer


def show_all_memories():
    print("\n" + "=" * 80)
    print("当前用户全部长期记忆：")

    all_memories = m.get_all(filters={"user_id": USER_ID})
    results = all_memories.get("results", all_memories)

    if not results:
        print("暂无记忆。")
        return

    for i, item in enumerate(results, 1):
        memory_text = item.get("memory") or item.get("data")
        print(f"{i}. {memory_text}")


def main():
    # 第 1 轮：用户表达风险偏好
    agent_chat("我比较保守，最大能接受本金亏损10%。")
    show_all_memories()

    # 第 2 轮：用户表达投资风格
    agent_chat("我更关注中长期收益，不太喜欢短线频繁交易。")
    show_all_memories()

    # 第 3 轮：用户询问历史偏好，此时应该通过 search 召回前两轮记忆
    agent_chat("我之前大概能接受多大的亏损？我的投资风格是什么？")
    show_all_memories()


if __name__ == "__main__":
    try:
        main()
    finally:
        if hasattr(m, "vector_store") and hasattr(m.vector_store, "client"):
            m.vector_store.client.close()
            