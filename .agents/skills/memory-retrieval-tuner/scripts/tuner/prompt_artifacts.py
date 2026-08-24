from __future__ import annotations

import hashlib
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from mem0.configs.query_prompts import QUERY_REFERENCE_RESOLUTION_PROMPT
from mem0.memory.query_resolver import QueryResolver

from .artifact_registry import ArtifactRegistry
from .benchmark_support import load_json, redact_secrets
from .io_utils import sha256_file, stable_hash
from .models import Candidate, CandidateResult, Dataset, Turn
from .production_runtime import create_tuner_policy_llm

PROMPT_ARTIFACT_SCHEMA = 4
PRODUCTION_QUERY_PROMPT_HASH = hashlib.sha256(QUERY_REFERENCE_RESOLUTION_PROMPT.encode()).hexdigest()
PRODUCTION_QUERY_PROMPT_IDENTITY = f"production-p0-reference-resolution-v1:{PRODUCTION_QUERY_PROMPT_HASH}"
PRODUCTION_SHORTTERM_HISTORY_POLICY = "production_visible_prior_qa_turns_v1"


def _parent_candidate_identity(candidate: Candidate) -> str:
    config = dict(candidate.config)
    config.pop("manifest_paths", None)
    manifest_hashes = config.pop("manifest_sha256", {}) or {}
    config["manifest_content_sha256"] = sorted(str(value) for value in manifest_hashes.values())
    for path_key, hash_key in (
        ("derived_artifact_path", "derived_artifact_sha256"),
        ("query_artifact_path", "query_artifact_sha256"),
        ("embedding_model_path", "embedding_model_revision"),
        ("reranker_model_path", "reranker_model_revision"),
    ):
        if config.get(path_key):
            config.pop(path_key, None)
            config[f"{path_key}_identity"] = config.get(hash_key)
    for key in ("experiment_branch", "branch_cost_level", "parent_candidate_hash", "applied_branches"):
        config.pop(key, None)
    return stable_hash(config)


@dataclass(frozen=True)
class QueryPromptVariant:
    prompt_text: str
    prompt_hash: str
    parent_prompt_hash: str
    generation_round: int
    optimization_direction: str


@dataclass(frozen=True)
class QueryArtifactResult:
    path: Path
    sha256: str
    variant: str
    reused: bool
    llm_calls: int
    failed_calls: int
    identity: dict[str, Any]


@dataclass(frozen=True)
class ProductionShortTermContract:
    memory_config_path: Path
    memory_config: dict[str, Any]
    shortterm_capacity_messages: int
    shortterm_qa_turns: int
    history_policy: str
    production_config_hash: str
    production_memory_contract: str


_ROUND_DIRECTIONS = {
    1: (
        "explicit_coreference_resolution",
        "entity_metric_time_preservation",
        "bounded_context_with_noise_suppression",
    ),
    2: (
        "narrower_antecedent_selection",
        "relationship_and_comparison_preservation",
        "minimal_change_information_guard",
    ),
    3: (
        "failure_focused_reference_precision",
        "failure_focused_retrieval_terms",
        "strict_unsupported_detail_prevention",
    ),
}


_DIRECTION_LABELS = {
    "explicit_coreference_resolution": "显式指代消解",
    "entity_metric_time_preservation": "主体、指标与时间信息保留",
    "bounded_context_with_noise_suppression": "受控上下文补全与噪声抑制",
    "narrower_antecedent_selection": "更严格的指代对象选择",
    "relationship_and_comparison_preservation": "关系与比较结构保留",
    "minimal_change_information_guard": "最小改写与信息边界保护",
    "failure_focused_reference_precision": "针对失败样本提高指代精度",
    "failure_focused_retrieval_terms": "针对失败样本强化检索信息",
    "strict_unsupported_detail_prevention": "严格禁止无依据细节",
}


_DIRECTION_RULES = {
    "explicit_coreference_resolution": "明确消解代词、省略主语以及“前述/这个判断/这些指标”等历史引用。",
    "entity_metric_time_preservation": "完整保留问题中的主体、指标、时间、范围、比较关系和约束词。",
    "bounded_context_with_noise_suppression": "仅补充回答当前问题所必需的最近历史，过滤无关事实和旧任务。",
    "narrower_antecedent_selection": "优先选择最近且语义一致的指代对象；存在歧义时保持原问法，不猜测。",
    "relationship_and_comparison_preservation": "保留继续、比较、反证、修订等关系及比较双方，不压缩成宽泛主题。",
    "minimal_change_information_guard": "采用最小必要改写；原问题已经独立时原样返回。",
    "failure_focused_reference_precision": "针对仍未命中的指代型问题，补齐唯一必要的对象而不扩写背景。",
    "failure_focused_retrieval_terms": "针对深层候选仍弱的问题，保留可检索的实体、指标、数值类别和时间词。",
    "strict_unsupported_detail_prevention": "删除无法由输入问题及可见历史支持的新增细节，禁止代答或引入未来信息。",
}


_FAILURE_PROFILE_LABELS = {
    "missed_queries": "未命中问题数",
    "reference_markers": "指代表达数",
    "time_markers": "时间表达数",
    "comparison_markers": "比较表达数",
}


def _prompt_text(parent_prompt: str | None, direction: str, failure_profile: Mapping[str, int]) -> str:
    parent_rule = (
        "上一轮最佳 Prompt 如下，其约束继续生效；本轮只强化后面一个方向：\n"
        f"<parent_prompt>\n{parent_prompt}\n</parent_prompt>"
        if parent_prompt
        else (
            "这是从生产 P0 查询指代消解 Prompt 出发的第一轮受控改写。"
            f"\n<production_p0_prompt>\n{QUERY_REFERENCE_RESOLUTION_PROMPT}\n</production_p0_prompt>"
        )
    )
    profile = ", ".join(
        f"{_FAILURE_PROFILE_LABELS.get(key, key)}={value}" for key, value in sorted(failure_profile.items())
    ) or "无"
    return (
        '你是记忆检索查询的保守改写器。输出严格 JSON：{"resolved_query": "..."}。\n'
        "只允许使用 current_query 与 recent_history 中已经出现的信息；禁止使用答案标签、Gold、未来轮次，"
        "禁止回答问题。原问题已独立时应原样返回。\n"
        f"{parent_rule}\n"
        f"本轮唯一优化方向：{_DIRECTION_LABELS[direction]}。{_DIRECTION_RULES[direction]}\n"
        f"调参失败样本的无标签聚合特征：{profile}。\n"
        "控制改写幅度，保持原问题意图、语气、否定、时间范围和比较关系。"
    )


def _failure_profile(dataset: Dataset, result: CandidateResult, tune_sessions: Sequence[str]) -> dict[str, int]:
    tune = set(tune_sessions)
    missed = {
        str(row.get("query_id") or "").upper()
        for row in result.requirement_rows
        if str(row.get("session_id") or "") in tune and not bool(row.get("hit_at_k"))
    }
    questions = [
        turn.question
        for session_id, turns in dataset.sessions.items()
        if session_id in tune
        for turn in turns
        if turn.query_id.upper() in missed
    ]
    text = "\n".join(questions)
    return {
        "missed_queries": len(missed),
        "reference_markers": len(re.findall(r"这|该|前|刚才|上述|它|其|them|that|previous", text, re.I)),
        "time_markers": len(re.findall(r"年|月|季度|同比|环比|之前|之后|latest|year|quarter", text, re.I)),
        "comparison_markers": len(re.findall(r"比较|相比|差异|变化|反证|修订|versus|compare", text, re.I)),
    }


def controlled_query_prompt_variants(
    *,
    dataset: Dataset,
    anchor: Candidate,
    anchor_result: CandidateResult,
    tune_sessions: Sequence[str],
    generation_round: int,
    variants_per_round: int,
) -> list[QueryPromptVariant]:
    if generation_round not in _ROUND_DIRECTIONS:
        return []
    parent_text = str(anchor.config.get("query_prompt_text") or "") or QUERY_REFERENCE_RESOLUTION_PROMPT
    parent_hash = str(anchor.config.get("query_prompt_hash") or "") or PRODUCTION_QUERY_PROMPT_HASH
    profile = _failure_profile(dataset, anchor_result, tune_sessions)
    variants = []
    for direction in _ROUND_DIRECTIONS[generation_round][: max(0, min(3, variants_per_round))]:
        text = _prompt_text(parent_text, direction, profile)
        variants.append(
            QueryPromptVariant(
                prompt_text=text,
                prompt_hash=hashlib.sha256(text.encode()).hexdigest(),
                parent_prompt_hash=parent_hash,
                generation_round=generation_round,
                optimization_direction=direction,
            )
        )
    return variants


class QueryPromptArtifactGenerator:
    """Generate exact-dataset query artifacts with per-query resumability."""

    def __init__(self, registry: ArtifactRegistry, *, llm_factory: Any | None = None):
        self.registry = registry
        self._llm_factory = llm_factory

    @staticmethod
    def _production_shortterm_contract(candidate: Candidate) -> ProductionShortTermContract:
        manifests = [Path(str(value)) for value in candidate.config.get("manifest_paths") or []]
        if not manifests:
            raise ValueError("Query generation requires production MidTerm manifests")
        contracts: list[tuple[Path, dict[str, Any], str, int, int, str]] = []
        for manifest_path in manifests:
            manifest = load_json(manifest_path)
            raw_config_path = manifest.get("memory_config_path")
            if not raw_config_path:
                raise ValueError(f"production manifest is missing memory_config_path: {manifest_path}")
            config_path = Path(str(raw_config_path))
            if not config_path.is_absolute():
                config_path = (manifest_path.parent / config_path).resolve()
            if not config_path.exists():
                raise ValueError(f"production memory config does not exist: {config_path}")
            config_sha = sha256_file(config_path)
            declared_sha = str(manifest.get("memory_config_sha256") or "")
            if not declared_sha:
                raise ValueError(f"production manifest is missing memory_config_sha256: {manifest_path}")
            if declared_sha != config_sha:
                raise ValueError(f"production manifest memory_config_sha256 mismatch: {manifest_path}")
            memory_config = load_json(config_path)
            capacity = (memory_config.get("midterm") or {}).get("short_term_capacity")
            if capacity is None:
                raise ValueError("memory_config.midterm.short_term_capacity is required for Query history")
            try:
                capacity_messages = int(capacity)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "memory_config.midterm.short_term_capacity must be a positive even message count"
                ) from exc
            if capacity_messages <= 0 or capacity_messages % 2:
                raise ValueError("memory_config.midterm.short_term_capacity must be a positive even message count")
            qa_turns = capacity_messages // 2
            manifest_qa_turns = manifest.get("shortterm_qa_turns")
            if manifest_qa_turns is None:
                raise ValueError(f"production manifest is missing shortterm_qa_turns: {manifest_path}")
            if int(manifest_qa_turns) != qa_turns:
                raise ValueError(
                    f"production manifest/config ShortTerm mismatch: manifest={manifest_qa_turns} QA turns, "
                    f"config={qa_turns} QA turns"
                )
            for config_section in (manifest.get("production_config"), manifest.get("effective_memory_config")):
                if not isinstance(config_section, Mapping):
                    continue
                midterm = config_section.get("midterm") if "midterm" in config_section else config_section
                if not isinstance(midterm, Mapping) or midterm.get("short_term_capacity") is None:
                    continue
                if int(midterm["short_term_capacity"]) != capacity_messages:
                    raise ValueError(f"production manifest effective ShortTerm config mismatch: {manifest_path}")
            contracts.append(
                (
                    config_path,
                    memory_config,
                    config_sha,
                    capacity_messages,
                    qa_turns,
                    str(manifest.get("production_memory_contract") or "legacy-production-contract"),
                )
            )

        config_hashes = {item[2] for item in contracts}
        capacities = {(item[3], item[4]) for item in contracts}
        memory_contracts = {item[5] for item in contracts}
        if len(config_hashes) != 1 or len(capacities) != 1 or len(memory_contracts) != 1:
            raise ValueError("production MidTerm manifests disagree on memory config or ShortTerm window")
        config_path, memory_config, config_sha, capacity_messages, qa_turns, memory_contract = contracts[0]
        production_config_hash = stable_hash(
            {
                "memory_config_sha256": config_sha,
                "midterm": memory_config.get("midterm") or {},
            }
        )
        return ProductionShortTermContract(
            memory_config_path=config_path,
            memory_config=memory_config,
            shortterm_capacity_messages=capacity_messages,
            shortterm_qa_turns=qa_turns,
            history_policy=PRODUCTION_SHORTTERM_HISTORY_POLICY,
            production_config_hash=production_config_hash,
            production_memory_contract=memory_contract,
        )

    def _create_llm(self, config: Mapping[str, Any], llm_mode: str) -> Any:
        if self._llm_factory is not None:
            return self._llm_factory(config, llm_mode)
        return create_tuner_policy_llm(dict(config), llm_mode=llm_mode)

    def generate(
        self,
        *,
        dataset: Dataset,
        anchor: Candidate,
        variant: QueryPromptVariant,
        tune_sessions: Sequence[str],
        max_parallel_llm_calls: int,
    ) -> QueryArtifactResult:
        shortterm = self._production_shortterm_contract(anchor)
        memory_config = shortterm.memory_config
        llm_mode = str(load_json(Path(str(anchor.config["manifest_paths"][0]))).get("llm_mode") or "real")
        model_config = redact_secrets(memory_config.get("llm") or {})
        identity = {
            "schema": PROMPT_ARTIFACT_SCHEMA,
            "kind": "query_representation",
            "dataset_sha256": dataset.sha256,
            "parent_candidate_config_hash": _parent_candidate_identity(anchor),
            "parent_prompt_hash": variant.parent_prompt_hash,
            "prompt_hash": variant.prompt_hash,
            "prompt_generation_round": variant.generation_round,
            "optimization_direction": variant.optimization_direction,
            "model_config": model_config,
            "shortterm_capacity_messages": shortterm.shortterm_capacity_messages,
            "shortterm_qa_turns": shortterm.shortterm_qa_turns,
            "history_policy": shortterm.history_policy,
            "production_config_hash": shortterm.production_config_hash,
            "production_memory_contract": shortterm.production_memory_contract,
            "analysis_session_scope": sorted(tune_sessions),
            "query_representation": "bounded_reference_resolution",
        }
        artifact_path = self.registry.artifact_path(identity)
        if artifact_path.exists():
            value = load_json(artifact_path)
            if value.get("status") == "COMPLETE" and value.get("identity") == identity:
                payload = value.get("payload") or {}
                return QueryArtifactResult(
                    path=artifact_path,
                    sha256=sha256_file(artifact_path),
                    variant=str(payload["variant"]),
                    reused=True,
                    llm_calls=0,
                    failed_calls=int(payload.get("failed_calls") or 0),
                    identity=identity,
                )

        llm_lock = threading.Lock()
        llm_holder: list[Any] = []

        def get_llm() -> Any:
            if llm_holder:
                return llm_holder[0]
            with llm_lock:
                if not llm_holder:
                    llm_holder.append(self._create_llm(memory_config, llm_mode))
            return llm_holder[0]

        history_by_query: dict[str, list[dict[str, str]]] = {}
        turns_by_id: dict[str, Turn] = {}
        for turns in dataset.sessions.values():
            history: list[list[dict[str, str]]] = []
            for turn in turns:
                turns_by_id[turn.query_id] = turn
                history_by_query[turn.query_id] = [
                    message for qa_messages in history[-shortterm.shortterm_qa_turns :] for message in qa_messages
                ]
                history.append(
                    [
                        {"role": "user", "content": turn.question},
                        {"role": "assistant", "content": turn.answer},
                    ]
                )

        def produce() -> dict[str, Any]:
            calls = 0
            failed = 0

            def generate_one(turn: Turn) -> tuple[dict[str, Any], bool, int]:
                row_identity = {
                    **identity,
                    "kind": "query_representation_row",
                    "query_id": turn.query_id,
                    "original_query_hash": stable_hash(turn.question),
                    "history_hash": stable_hash(history_by_query[turn.query_id]),
                }

                def call() -> dict[str, Any]:
                    history = history_by_query[turn.query_id]
                    resolved = QueryResolver(
                        get_llm(),
                        prompt=variant.prompt_text,
                        request_options=memory_config.get("query_rewrite_request_options") or {},
                    ).resolve(
                        turn.question,
                        history,
                    )
                    return {
                        "resolved_query": resolved,
                        "llm_calls": 1 if history else 0,
                        "errors": [],
                        "failed": False,
                    }

                value, reused = self.registry.materialize_once(row_identity, call)
                payload = value.get("payload") or {}
                row = {
                    "query_id": turn.query_id,
                    "session_id": turn.session_id,
                    "original_query": turn.question,
                    "resolved_query": str(payload.get("resolved_query") or turn.question),
                    "variant": f"generated:{variant.prompt_hash}",
                    "prompt_hash": variant.prompt_hash,
                    "generation_round": variant.generation_round,
                    "optimization_direction": variant.optimization_direction,
                    "row_artifact_hash": stable_hash(row_identity),
                    "status": "FALLBACK_ORIGINAL" if payload.get("failed") else "SUCCESS",
                }
                return row, reused, 0 if reused else int(payload.get("llm_calls") or 0)

            rows: list[dict[str, Any]] = []
            with ThreadPoolExecutor(max_workers=max(1, max_parallel_llm_calls)) as executor:
                futures = [executor.submit(generate_one, turn) for turn in turns_by_id.values()]
                for future in as_completed(futures):
                    row, _, row_calls = future.result()
                    rows.append(row)
                    calls += row_calls
                    failed += int(row["status"] != "SUCCESS")
            rows.sort(key=lambda row: str(row["query_id"]))
            return {
                "schema": PROMPT_ARTIFACT_SCHEMA,
                "variant": f"row:variant:generated:{variant.prompt_hash}",
                "rows": rows,
                "prompt_text": variant.prompt_text,
                "prompt_hash": variant.prompt_hash,
                "parent_prompt_hash": variant.parent_prompt_hash,
                "generation_round": variant.generation_round,
                "optimization_direction": variant.optimization_direction,
                "model_config": model_config,
                "dataset_sha256": dataset.sha256,
                "shortterm_capacity_messages": shortterm.shortterm_capacity_messages,
                "shortterm_qa_turns": shortterm.shortterm_qa_turns,
                "history_policy": shortterm.history_policy,
                "production_config_hash": shortterm.production_config_hash,
                "production_memory_contract": shortterm.production_memory_contract,
                "analysis_session_ids": sorted(tune_sessions),
                "generated_session_ids": sorted(dataset.sessions),
                "llm_calls": calls,
                "failed_calls": failed,
            }

        value, reused = self.registry.materialize_once(identity, produce)
        payload = value.get("payload") or {}
        return QueryArtifactResult(
            path=artifact_path,
            sha256=sha256_file(artifact_path),
            variant=str(payload["variant"]),
            reused=reused,
            llm_calls=0 if reused else int(payload.get("llm_calls") or 0),
            failed_calls=int(payload.get("failed_calls") or 0),
            identity=identity,
        )
