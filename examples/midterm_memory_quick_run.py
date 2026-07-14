import hashlib
import json
import os
import shutil
from pathlib import Path
from unittest.mock import patch

from mem0 import Memory


class SimpleEmbedding:
    def __init__(self, dims=10):
        self.dims = dims

    def embed(self, text, memory_action=None):
        text = (text or "").lower()
        vector = [0.01] * self.dims
        buckets = [
            ("preference", ["投资偏好", "保守", "亏损", "风险", "中长期", "短线", "风格"]),
            ("industry", ["行业", "新能源", "新能源车", "产业链"]),
            ("fund", ["基金", "债券基金", "配置", "比例"]),
            ("loss", ["10%", "亏损", "接受多大"]),
            ("question", ["之前", "关注过", "什么", "多大"]),
        ]
        for index, (_, terms) in enumerate(buckets):
            if any(term in text for term in terms):
                vector[index] += 1.0
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        for index in range(len(buckets), self.dims):
            vector[index] += digest[index] / 2550.0
        norm = sum(value * value for value in vector) ** 0.5
        return [value / norm for value in vector]

    def embed_batch(self, texts, memory_action="add"):
        return [self.embed(text, memory_action) for text in texts]


class SimpleLLM:
    def generate_response(self, messages, response_format=None, **kwargs):
        system = messages[0]["content"] if messages else ""
        user_prompt = messages[-1]["content"] if messages else ""
        text = user_prompt.lower()

        if "Summarize one evicted" in system:
            return json.dumps(self._page_summary(user_prompt), ensure_ascii=False)
        if "Merge an existing mid-term session summary" in system:
            return json.dumps(self._session_merge(user_prompt), ensure_ascii=False)

        memory = self._fact_memory(text)
        return json.dumps({"memory": [{"text": memory}]} if memory else {"memory": []}, ensure_ascii=False)

    @staticmethod
    def _page_summary(text):
        if "最大亏损" in text or "10%" in text or "风险" in text:
            return {
                "summary": "用户的投资偏好偏保守，最大可接受亏损约为10%。",
                "keywords": ["投资偏好", "风险", "保守", "亏损", "10%"],
            }
        if "中长期" in text or "短线" in text:
            return {
                "summary": "用户偏好中长期投资，不喜欢短线交易。",
                "keywords": ["投资偏好", "中长期", "短线", "风格"],
            }
        if "新能源" in text:
            return {
                "summary": "用户关注新能源车产业链相关行业。",
                "keywords": ["行业", "新能源车", "产业链"],
            }
        if "债券基金" in text or "基金配置" in text:
            return {
                "summary": "用户偏好较高比例的债券基金配置。",
                "keywords": ["基金", "债券基金", "配置"],
            }
        return {"summary": text[:120], "keywords": ["投资"]}

    @staticmethod
    def _fact_memory(text):
        if "最大亏损" in text or "10%" in text or "风险" in text:
            return "用户风险偏好保守，最大可接受亏损为10%。"
        if "中长期" in text or "短线" in text:
            return "用户偏好中长期投资，不喜欢短线交易。"
        if "新能源" in text:
            return "用户关注新能源车产业链。"
        if "债券基金" in text or "基金配置" in text:
            return "用户偏好较高比例配置债券基金。"
        return None

    @staticmethod
    def _session_merge(text):
        payload = json.loads(text)
        existing = payload.get("existing_session", {})
        new_page = payload.get("new_page", {})
        existing_summary = existing.get("summary", "")
        page_summary = new_page.get("summary", "")
        summary = existing_summary
        if page_summary and page_summary not in summary:
            summary = f"{summary} {page_summary}".strip()

        keywords = []
        for keyword in existing.get("keywords", []) + new_page.get("keywords", []):
            if keyword not in keywords:
                keywords.append(keyword)
        return {"summary": summary[:1000], "keywords": keywords[:12]}


def main():
    base_path = Path("/tmp/mem0_midterm_quick_run")
    qdrant_path = base_path / "qdrant"
    history_path = base_path / "history.db"
    shutil.rmtree(base_path, ignore_errors=True)
    os.makedirs(base_path, exist_ok=True)

    config = {
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": "mem0_midterm_quick_run",
                "path": str(qdrant_path),
                "embedding_model_dims": 10,
            },
        },
        "embedder": {"provider": "openai", "config": {"embedding_dims": 10}},
        "llm": {"provider": "openai", "config": {}},
        "history_db_path": str(history_path),
        "midterm": {
            "enabled": True,
            "short_term_capacity": 4,
            "session_similarity_threshold": 0.50,
            "top_k_sessions": 5,
            "top_k_pages": 5,
        },
    }

    turns = [
        (
            "我比较保守，投资里最大亏损最好控制在10%。",
            "我会按保守风险偏好来考虑，并把最大回撤控制在10%左右。",
        ),
        (
            "我更偏中长期投资，不喜欢短线频繁交易。",
            "明白，后续建议会偏中长期，减少短线交易假设。",
        ),
        (
            "我最近关注新能源车产业链。",
            "新能源车产业链可以继续拆成整车、电池、材料和充电环节。",
        ),
        (
            "基金配置上我希望债券基金比例高一点。",
            "可以提高债券基金比例，让组合波动更低。",
        ),
        (
            "我之前能接受多大亏损？",
            "你之前说最大亏损最好控制在10%。",
        ),
        (
            "我之前关注过什么行业？",
            "你之前关注过新能源车产业链。",
        ),
    ]

    with (
        patch("mem0.memory.main.MEM0_TELEMETRY", False),
        patch("mem0.memory.main.EmbedderFactory.create", return_value=SimpleEmbedding()),
        patch("mem0.memory.main.LlmFactory.create", return_value=SimpleLLM()),
    ):
        memory = Memory.from_config(config)
        for user_message, assistant_message in turns:
            memory.add(
                [
                    {"role": "user", "content": user_message},
                    {"role": "assistant", "content": assistant_message},
                ],
                user_id="investor-1",
            )

        filters = {"user_id": "investor-1"}
        pages = memory.midterm_memory.list_pages(filters=filters, top_k=20)
        sessions = memory.midterm_memory.list_sessions(filters=filters, top_k=20)

        print(f"midterm_pages={len(pages)}")
        print(f"midterm_sessions={len(sessions)}")
        for session in sessions:
            payload = session.payload
            print(f"session {session.id}: pages={len(payload.get('page_ids', []))}, summary={payload.get('summary')}")

        assert len(pages) >= 4, "evicted QA pairs should become midterm pages"
        assert len(sessions) >= 3, "distinct topics should create separate midterm sessions"

        page_payloads = [page.payload for page in pages]
        risk_page = next(page for page in page_payloads if "最大亏损" in page.get("user_input", ""))
        style_page = next(page for page in page_payloads if "中长期" in page.get("user_input", ""))
        industry_page = next(page for page in page_payloads if "新能源" in page.get("user_input", ""))
        assert risk_page["session_id"] == style_page["session_id"], (
            "similar preference pages should share a session"
        )
        assert industry_page["session_id"] != risk_page["session_id"], (
            "industry topic should create a different session"
        )
        assert all(page.get("user_input") for page in page_payloads)
        assert any(page.get("assistant_response") for page in page_payloads)

        before_assistant_only = len(memory.midterm_memory.list_pages(filters=filters, top_k=20))
        memory.midterm_updater.process_evicted_messages(
            [{"role": "assistant", "content": "assistant-only should be ignored"}],
            filters,
        )
        after_assistant_only = len(memory.midterm_memory.list_pages(filters=filters, top_k=20))
        assert after_assistant_only == before_assistant_only, "assistant-only should not create a page"

        search_result = memory.search("我之前能接受多大亏损？", filters=filters, top_k=5)
        sources = {item.get("source") for item in search_result["results"]}
        print(f"search_sources={sorted(source for source in sources if source)}")
        assert "long_term" in sources
        assert "mid_term_page" in sources
        assert "mid_term_session" in sources

        for item in search_result["results"]:
            print(f"{item.get('source')}: {item.get('memory')}")

        memory.reset()
        assert memory.midterm_memory.list_pages(filters=filters, top_k=20) == []
        assert memory.midterm_memory.list_sessions(filters=filters, top_k=20) == []

    disabled_path = Path("/tmp/mem0_midterm_quick_run_disabled")
    shutil.rmtree(disabled_path, ignore_errors=True)
    os.makedirs(disabled_path, exist_ok=True)
    disabled_config = {
        **config,
        "history_db_path": str(disabled_path / "history.db"),
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": "mem0_midterm_quick_run_disabled",
                "path": str(disabled_path / "qdrant"),
                "embedding_model_dims": 10,
            },
        },
        "midterm": {"enabled": False},
    }
    with (
        patch("mem0.memory.main.MEM0_TELEMETRY", False),
        patch("mem0.memory.main.EmbedderFactory.create", return_value=SimpleEmbedding()),
        patch("mem0.memory.main.LlmFactory.create", return_value=SimpleLLM()),
    ):
        disabled_memory = Memory.from_config(disabled_config)
        disabled_memory.add("用户偏好保守投资。", user_id="investor-disabled", infer=False)
        disabled_search = disabled_memory.search(
            "保守投资",
            filters={"user_id": "investor-disabled"},
            top_k=3,
        )
        assert disabled_search["results"], "disabled midterm search should still return long-term results"
        assert "source" not in disabled_search["results"][0], "midterm disabled should preserve search shape"
        assert disabled_memory._midterm_updater is None
        assert disabled_memory._midterm_retriever is None


if __name__ == "__main__":
    main()
