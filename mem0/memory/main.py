import asyncio
import hashlib
import inspect
import json
import logging
import os
import threading
import time
import uuid
import warnings
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import date, datetime
from typing import Any, Dict, Optional

from pydantic import ValidationError

from mem0.configs.base import BackgroundTaskConfig, FineGrainedLongTermConfig, MemoryConfig, MemoryItem
from mem0.configs.enums import MemoryType
from mem0.configs.prompts import (
    ADDITIVE_EXTRACTION_PROMPT,
    AGENT_ANSWER_PROMPT,
    AGENT_CONTEXT_SUFFIX,
    AGENTIC_RETRIEVAL_PROMPT,
    PROCEDURAL_MEMORY_SYSTEM_PROMPT,
    generate_additive_extraction_prompt,
)
from mem0.configs.query_prompts import QUERY_REFERENCE_RESOLUTION_PROMPT
from mem0.exceptions import LLMError
from mem0.exceptions import ValidationError as Mem0ValidationError
from mem0.memory.agentic_retrieval import AgenticMemoryRunner, AsyncAgenticMemoryRunner
from mem0.memory.background_worker import BackgroundWorkerManager
from mem0.memory.base import MemoryBase
from mem0.memory.fine_grained_longterm import FineGrainedLongTermRetriever
from mem0.memory.midterm import MidTermMemory
from mem0.memory.midterm_retriever import MidTermRetriever
from mem0.memory.midterm_updater import MidTermUpdater
from mem0.memory.notices import (
    PERFORMANCE_SLOW_QUERY_THRESHOLD_SECONDS,
    detect_decay_usage_from_delete,
    detect_decay_usage_from_delete_all,
    detect_scale_threshold_from_add_result,
    detect_scale_threshold_from_top_k,
    detect_temporal_usage_from_metadata,
    detect_temporal_usage_from_search,
    display_decay_usage_notice,
    display_decay_usage_notice_async,
    display_first_run_notice,
    display_first_run_notice_async,
    display_performance_slow_query_notice,
    display_performance_slow_query_notice_async,
    display_scale_threshold_notice,
    display_scale_threshold_notice_async,
    display_temporal_usage_notice,
    display_temporal_usage_notice_async,
    get_decay_feature_error_message,
    get_decay_feature_error_message_async,
    get_temporal_feature_error_message,
    get_temporal_feature_error_message_async,
)
from mem0.memory.process_lock import ProcessInstanceLock
from mem0.memory.profile_manager import ProfileManager
from mem0.memory.profile_schema import ProfileUpdatePlan
from mem0.memory.profile_updater import ProfileUpdater
from mem0.memory.profile_validator import (
    normalize_memory_identifier,
    normalize_profile_user_id,
    select_profile_user_messages,
)
from mem0.memory.promoted_longterm import PromotedLongTermMemory, promotion_source_version
from mem0.memory.query_resolver import QueryResolver
from mem0.memory.retrieval_tools import AsyncMemoryToolExecutor, MemoryToolExecutor
from mem0.memory.setup import mem0_dir, setup_config
from mem0.memory.storage import MIGRATION_STAGE_TERMINAL_STATUSES, SQLiteManager
from mem0.memory.telemetry import MEM0_TELEMETRY, capture_event
from mem0.memory.utils import (
    extract_json,
    parse_messages,
    parse_vision_messages,
    process_telemetry_filters,
    remove_code_blocks,
)
from mem0.reranker.concurrency import RerankerConcurrencyGuard
from mem0.utils.bounded_timeout import BoundedTimeoutExecutor
from mem0.utils.entity_extraction import extract_entities, extract_entities_batch
from mem0.utils.factory import EmbedderFactory, LlmFactory, RerankerFactory, VectorStoreFactory
from mem0.utils.lemmatization import lemmatize_for_bm25
from mem0.utils.timestamps import beijing_now, beijing_now_iso, normalize_iso_timestamp_to_beijing
from mem0.vector_stores.base import VectorStoreBase

# Suppress SWIG deprecation warnings globally
warnings.filterwarnings("ignore", category=DeprecationWarning, message=".*SwigPy.*")
warnings.filterwarnings("ignore", category=DeprecationWarning, message=".*swigvarlink.*")

# Initialize logger early for util functions
logger = logging.getLogger(__name__)


@asynccontextmanager
async def _acquire_thread_lock_async(lock: threading.Lock):
    """Acquire a thread lock without leaking it when the waiting coroutine is cancelled."""
    acquire_task = asyncio.create_task(asyncio.to_thread(lock.acquire))
    acquired = False
    try:
        try:
            acquired = await asyncio.shield(acquire_task)
        except asyncio.CancelledError:
            while not acquire_task.done():
                try:
                    await asyncio.shield(acquire_task)
                except asyncio.CancelledError:
                    continue
            acquired = acquire_task.result()
            if acquired:
                lock.release()
                acquired = False
            raise
        try:
            yield
        finally:
            if acquired:
                lock.release()
                acquired = False
    finally:
        if acquire_task.done() and not acquire_task.cancelled():
            acquire_task.exception()


def _parse_extracted_memories(response: Any, *, strict: bool) -> list[dict]:
    """Parse and validate the long-term extraction response."""
    try:
        if isinstance(response, dict):
            parsed = response
        elif isinstance(response, str):
            cleaned_response = remove_code_blocks(response)
            if not cleaned_response.strip():
                raise ValueError("response is empty")
            try:
                parsed = json.loads(cleaned_response, strict=False)
            except json.JSONDecodeError:
                parsed = json.loads(extract_json(cleaned_response), strict=False)
        else:
            raise TypeError(f"expected a JSON object response, got {type(response).__name__}")

        if not isinstance(parsed, dict):
            raise TypeError("JSON root must be an object")
        if "memory" not in parsed:
            raise ValueError("JSON object is missing the 'memory' field")

        extracted_memories = parsed["memory"]
        if not isinstance(extracted_memories, list):
            raise TypeError("'memory' must be a list")

        for index, memory in enumerate(extracted_memories):
            if not isinstance(memory, dict):
                raise TypeError(f"memory[{index}] must be an object")
            text = memory.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"memory[{index}].text must be a non-empty string")
        return extracted_memories
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        message = f"Long-term memory response parsing failed: {exc}"
        if strict:
            raise LLMError(message) from exc
        logger.error("Error parsing extraction response: %s", message)
        return []


def _normalize_memory_text(text: str) -> str:
    """Normalize insignificant whitespace without changing stored memory text."""
    return " ".join(text.strip().split())


def _longterm_memory_hash(text: str) -> str:
    normalized_text = _normalize_memory_text(text)
    return hashlib.md5(normalized_text.encode("utf-8")).hexdigest()


def _longterm_memory_id(source_job_id: str, text: str) -> str:
    memory_hash = _longterm_memory_hash(text)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"mem0:longterm:{source_job_id}:{memory_hash}"))


def _longterm_payload_hash(payload: Dict[str, Any]) -> Optional[str]:
    data = payload.get("data")
    if isinstance(data, str) and _normalize_memory_text(data):
        return _longterm_memory_hash(data)
    stored_hash = payload.get("hash")
    return stored_hash if isinstance(stored_hash, str) and stored_hash else None


def _existing_longterm_hashes(
    existing_results: Any,
    source_job_id: Optional[str],
) -> tuple[set[str], Dict[str, Any]]:
    existing_hashes: set[str] = set()
    existing_source_hashes: Dict[str, Any] = {}
    for existing_memory in existing_results:
        payload = getattr(existing_memory, "payload", None) or {}
        memory_hash = _longterm_payload_hash(payload)
        if not memory_hash:
            continue
        existing_hashes.add(memory_hash)
        if source_job_id and payload.get("source_job_id") == source_job_id:
            existing_source_hashes.setdefault(memory_hash, existing_memory)
    return existing_hashes, existing_source_hashes


def _validated_existing_longterm_payload(
    existing_memory: Any,
    *,
    source_job_id: str,
    memory_id: str,
    expected_hash: str,
) -> Dict[str, Any]:
    payload = getattr(existing_memory, "payload", None) or {}
    existing_hash = payload.get("hash")
    if existing_hash != expected_hash:
        logger.error(
            "Long-term deterministic ID hash mismatch for source_job_id=%s memory_id=%s "
            "expected_hash=%s existing_hash=%s",
            source_job_id,
            memory_id,
            expected_hash,
            existing_hash,
        )
        raise RuntimeError(
            f"Long-term deterministic ID hash mismatch for source_job_id={source_job_id} memory_id={memory_id}"
        )
    return payload


def _embedding_is_available(embedding: Any) -> bool:
    if embedding is None:
        return False
    try:
        return len(embedding) > 0
    except TypeError:
        return False


def _missing_embeddings(memory_texts: list[str], embedding_map: Dict[str, Any]) -> list[str]:
    return [text for text in memory_texts if not _embedding_is_available(embedding_map.get(text))]


def _vector_store_list_rows(listed):
    if isinstance(listed, (list, tuple)) and listed and isinstance(listed[0], list):
        return listed[0]
    if isinstance(listed, (list, tuple)):
        return listed
    return []


def _update_vector_store_payload(store, memory_id: str, payload: Dict[str, Any]) -> None:
    update = getattr(store, "update", None)
    if callable(update):
        update(vector_id=memory_id, vector=None, payload=payload)
        return
    # Small in-memory stores used by the SDK tests expose their rows directly.
    rows = getattr(store, "rows", None)
    if isinstance(rows, dict) and memory_id in rows:
        rows[memory_id]["payload"] = dict(payload)
        return
    raise RuntimeError("Vector store does not support payload updates")


def _new_entity_payload(entity_text, entity_type, linked_memory_ids, filters):
    """Build a timestamped payload for a newly created entity."""
    now = beijing_now_iso()
    return {
        "data": entity_text,
        "entity_type": entity_type,
        "linked_memory_ids": sorted(linked_memory_ids),
        **filters,
        "created_at": now,
        "updated_at": now,
    }


def _longterm_entity_filters(filters: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Use user/agent isolation for fine-grained LongTerm entities, never run isolation."""
    return {key: value for key, value in (filters or {}).items() if key in ("user_id", "agent_id") and value}


def _update_entity_payload(payload, linked_memory_ids):
    """Copy an entity payload, preserving creation time and refreshing update time."""
    now = beijing_now_iso()
    return {
        **payload,
        "linked_memory_ids": sorted(linked_memory_ids),
        "created_at": normalize_iso_timestamp_to_beijing(payload.get("created_at")) or now,
        "updated_at": now,
    }


def _configured_bm25_language(config: MemoryConfig) -> str | None:
    """Return an explicit language only for Qdrant's configurable BM25 route."""
    if config.vector_store.provider != "qdrant":
        return None
    return getattr(config.vector_store.config, "bm25_language", "en")


def _additive_midterm_context(memory, query, filters, *, exclude_source_job_id=None):
    """Return the session summary and related page context declared by the additive prompt."""
    if not memory._midterm_enabled():
        return "", []

    try:
        results = memory.midterm_retriever.search(
            query,
            filters,
            exclude_source_job_id=exclude_source_job_id,
        )
    except Exception as exc:
        logger.warning("Mid-term context retrieval for long-term extraction failed: %s", exc)
        return "", []

    session_summaries = []
    related_memories = []
    for result in results:
        source = result.get("source")
        summary = str(result.get("summary") or result.get("memory") or "").strip()
        if source == "mid_term_session":
            if summary and summary not in session_summaries:
                session_summaries.append(summary)
            continue

        related_memory = {
            key: result.get(key)
            for key in ("source", "summary", "raw_dialogue", "created_at", "source_job_id")
            if result.get(key) not in (None, "")
        }
        if not related_memory and summary:
            related_memory["summary"] = summary
        if related_memory:
            related_memories.append(related_memory)

    return "\n".join(session_summaries), related_memories


# Fields that hold runtime auth/connection objects and must be preserved.
# These are non-serializable objects (e.g. AWSV4SignerAuth, RequestsHttpConnection)
# needed by clients like OpenSearch — not sensitive strings to redact.
_RUNTIME_FIELDS = frozenset(
    {
        "http_auth",
        "auth",
        "connection_class",
        "ssl_context",
    }
)

# Fields that are known to contain sensitive secrets and must be redacted.
_SENSITIVE_FIELDS_EXACT = frozenset(
    {
        "api_key",
        "secret_key",
        "private_key",
        "access_key",
        "password",
        "credentials",
        "credential",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "auth_token",
        "session_token",
        "client_secret",
        "auth_client_secret",
        "azure_client_secret",
        "service_account_json",
        "aws_session_token",
    }
)

# Suffixes that indicate a field likely holds a secret value.
_SENSITIVE_SUFFIXES = (
    "_password",
    "_secret",
    "_token",
    "_credential",
    "_credentials",
)

# Entity parameters that must be passed via filters, not top-level kwargs
ENTITY_PARAMS = frozenset({"user_id", "agent_id", "run_id"})


def _reject_top_level_entity_params(kwargs: Dict[str, Any], method_name: str) -> None:
    """Reject top-level entity parameters - must use filters instead."""
    invalid_keys = ENTITY_PARAMS & set(kwargs.keys())
    if invalid_keys:
        raise ValueError(
            f"Top-level entity parameters {invalid_keys} are not supported in {method_name}(). "
            f"Use filters={{'user_id': '...'}} instead."
        )


def _validate_and_trim_entity_id(value: Optional[str], name: str) -> Optional[str]:
    """
    Validates and normalizes an entity ID.
    - Trims leading/trailing whitespace
    - Rejects empty or whitespace-only strings
    - Rejects strings containing internal whitespace

    Args:
        value: The entity ID value to validate
        name: The parameter name (for error messages)

    Returns:
        The trimmed entity ID, or None if input is None

    Raises:
        ValueError: If entity ID is invalid
    """
    return normalize_memory_identifier(value, name)


def _validate_search_params(threshold: Optional[float] = None, top_k: Optional[int] = None) -> None:
    """
    Validates search parameters.

    Args:
        threshold: Similarity threshold (must be between 0 and 1)
        top_k: Number of results to return (must be non-negative integer)

    Raises:
        ValueError: If threshold or top_k are invalid
    """
    if threshold is not None:
        if not isinstance(threshold, (int, float)):
            raise ValueError("threshold must be a valid number")
        if threshold < 0 or threshold > 1:
            raise ValueError(f"Invalid threshold: {threshold}. Must be between 0 and 1 (inclusive).")
    if top_k is not None:
        if not isinstance(top_k, int) or isinstance(top_k, bool):
            raise ValueError("top_k must be a valid integer")
        if top_k < 0:
            raise ValueError(f"Invalid top_k: {top_k}. Must be a non-negative integer.")


def _validate_and_trim_search_query(query: str) -> str:
    """
    Validates and normalizes a search query before embedding/vector search.

    Raises:
        ValueError: If query is not a string or is empty/whitespace-only.
    """
    if not isinstance(query, str):
        raise ValueError("Invalid query: must be a non-empty string.")
    trimmed = query.strip()
    if not trimmed:
        raise ValueError("Invalid query: cannot be empty or whitespace-only.")
    return trimmed


def _is_sensitive_field(field_name: str) -> bool:
    """Check if a field should be redacted for telemetry safety.

    Uses a layered approach:
    1. Runtime fields (allowlist) — always preserved, highest priority.
    2. Exact deny list — known secret field names.
    3. Suffix deny list — catches patterns like db_password, auth_secret, etc.
    """
    name = field_name.lower().strip()
    if name in _RUNTIME_FIELDS:
        return False
    if name in _SENSITIVE_FIELDS_EXACT:
        return True
    return any(name.endswith(suffix) for suffix in _SENSITIVE_SUFFIXES)


def _safe_deepcopy_config(config):
    """Safely deepcopy config, falling back to dict-based cloning for non-serializable objects."""
    try:
        return deepcopy(config)
    except Exception as e:
        logger.debug(f"Deepcopy failed, using dict-based cloning: {e}")

        config_class = type(config)

        if hasattr(config, "model_dump"):
            try:
                clone_dict = config.model_dump()
            except Exception:
                clone_dict = dict(config.__dict__)
        else:
            clone_dict = dict(config.__dict__)

        # Restore runtime fields, redact sensitive ones
        for field_name in list(clone_dict.keys()):
            if field_name in _RUNTIME_FIELDS and hasattr(config, field_name):
                clone_dict[field_name] = getattr(config, field_name)
            elif _is_sensitive_field(field_name):
                clone_dict[field_name] = None

        try:
            return config_class(**clone_dict)
        except Exception:
            logger.debug("Config reconstruction failed, returning shallow dict clone")
            return type("Config", (), clone_dict)()


def _build_telemetry_vector_store_config(config: MemoryConfig):
    """Build an isolated telemetry vector-store config using the source config type."""
    source_config = config.vector_store.config
    if hasattr(source_config, "model_dump"):
        telemetry_config_dict = source_config.model_dump()
    else:
        telemetry_config_dict = {}
        common_attributes = ("host", "port", "path", "api_key", "index_name", "dimension", "metric")
        for attribute in (*common_attributes, *_RUNTIME_FIELDS):
            if hasattr(source_config, attribute):
                telemetry_config_dict[attribute] = getattr(source_config, attribute)

    telemetry_config_dict["collection_name"] = "mem0migrations"
    provider = config.vector_store.provider
    if provider in {"faiss", "qdrant"}:
        telemetry_path = os.path.join(mem0_dir, f"migrations_{provider}")
        os.makedirs(telemetry_path, exist_ok=True)
        telemetry_config_dict["path"] = telemetry_path

    return type(source_config)(**telemetry_config_dict)


def _build_filters_and_metadata(
    *,  # Enforce keyword-only arguments
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    run_id: Optional[str] = None,
    actor_id: Optional[str] = None,  # For query-time filtering
    input_metadata: Optional[Dict[str, Any]] = None,
    input_filters: Optional[Dict[str, Any]] = None,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Constructs metadata for storage and filters for querying based on session and actor identifiers.

    This helper supports multiple session identifiers (`user_id`, `agent_id`, and/or `run_id`)
    for flexible session scoping and optionally narrows queries to a specific `actor_id`. It returns two dicts:

    1. `base_metadata_template`: Used as a template for metadata when storing new memories.
       It includes all provided session identifier(s) and any `input_metadata`.
    2. `effective_query_filters`: Used for querying existing memories. It includes all
       provided session identifier(s), any `input_filters`, and a resolved actor
       identifier for targeted filtering if specified by any actor-related inputs.

    Actor filtering precedence: explicit `actor_id` arg → `filters["actor_id"]`
    This resolved actor ID is used for querying but is not added to `base_metadata_template`,
    as the actor for storage is typically derived from message content at a later stage.

    Args:
        user_id (Optional[str]): User identifier, for session scoping.
        agent_id (Optional[str]): Agent identifier, for session scoping.
        run_id (Optional[str]): Run identifier, for session scoping.
        actor_id (Optional[str]): Explicit actor identifier, used as a potential source for
            actor-specific filtering. See actor resolution precedence in the main description.
        input_metadata (Optional[Dict[str, Any]]): Base dictionary to be augmented with
            session identifiers for the storage metadata template. Defaults to an empty dict.
        input_filters (Optional[Dict[str, Any]]): Base dictionary to be augmented with
            session and actor identifiers for query filters. Defaults to an empty dict.

    Returns:
        tuple[Dict[str, Any], Dict[str, Any]]: A tuple containing:
            - base_metadata_template (Dict[str, Any]): Metadata template for storing memories,
              scoped to the provided session(s).
            - effective_query_filters (Dict[str, Any]): Filters for querying memories,
              scoped to the provided session(s) and potentially a resolved actor.
    """

    base_metadata_template = deepcopy(input_metadata) if input_metadata else {}
    effective_query_filters = deepcopy(input_filters) if input_filters else {}

    # ---------- validate and add all provided session ids ----------
    session_ids_provided = []

    # Validate and trim entity IDs
    user_id = _validate_and_trim_entity_id(user_id, "user_id")
    agent_id = _validate_and_trim_entity_id(agent_id, "agent_id")
    run_id = _validate_and_trim_entity_id(run_id, "run_id")

    if user_id:
        base_metadata_template["user_id"] = user_id
        effective_query_filters["user_id"] = user_id
        session_ids_provided.append("user_id")

    if agent_id:
        base_metadata_template["agent_id"] = agent_id
        effective_query_filters["agent_id"] = agent_id
        session_ids_provided.append("agent_id")

    if run_id:
        base_metadata_template["run_id"] = run_id
        effective_query_filters["run_id"] = run_id
        session_ids_provided.append("run_id")

    if not session_ids_provided:
        raise Mem0ValidationError(
            message="At least one of 'user_id', 'agent_id', or 'run_id' must be provided.",
            error_code="VALIDATION_001",
            details={"provided_ids": {"user_id": user_id, "agent_id": agent_id, "run_id": run_id}},
            suggestion="Please provide at least one identifier to scope the memory operation.",
        )

    # ---------- optional actor filter ----------
    resolved_actor_id = actor_id or effective_query_filters.get("actor_id")
    if resolved_actor_id:
        effective_query_filters["actor_id"] = resolved_actor_id

    return base_metadata_template, effective_query_filters


def _build_session_scope(filters):
    """Build deterministic session scope string from entity IDs."""
    parts = []
    for key in sorted(["user_id", "agent_id", "run_id"]):
        val = filters.get(key)
        if val:
            parts.append(f"{key}={val}")
    return "&".join(parts)


def _completed_qa_longterm_inputs(completed_qa, metadata):
    """Build the shared per-turn LongTerm inputs used by direct extraction."""
    return [
        (messages, {**deepcopy(metadata), "source_turn_index": int(turn_index)})
        for turn_index, messages in completed_qa
    ]


def _memory_add_request_hash(
    *,
    messages: Any,
    filters: Dict[str, Any],
    metadata: Dict[str, Any],
    infer: bool,
    memory_type: Optional[str],
    prompt: Optional[str],
) -> str:
    """Hash the business inputs guarded by a ``Memory.add`` idempotency key."""
    payload = {
        "operation_type": "memory.add",
        "messages": messages,
        "filters": filters,
        "metadata": metadata,
        "infer": bool(infer),
        "memory_type": memory_type,
        "prompt": prompt,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validate_and_trim_idempotency_key(idempotency_key: Optional[str]) -> Optional[str]:
    """Apply the persisted add idempotency-key contract shared by both APIs."""
    if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key.strip()):
        raise ValueError("idempotency_key must be a non-empty string")
    return idempotency_key.strip() if idempotency_key is not None else None


def _validate_memory_type(memory_type: Optional[str]) -> None:
    """Validate the OSS memory type without letting sync and async errors drift."""
    if memory_type is not None and memory_type != MemoryType.PROCEDURAL.value:
        raise Mem0ValidationError(
            message=(
                f"Invalid 'memory_type'. Please pass {MemoryType.PROCEDURAL.value} to create procedural memories."
            ),
            error_code="VALIDATION_002",
            details={"provided_type": memory_type, "valid_type": MemoryType.PROCEDURAL.value},
            suggestion=f"Use '{MemoryType.PROCEDURAL.value}' to create procedural memories.",
        )


def _build_procedural_memory_messages(messages: list, prompt: Optional[str]) -> list:
    """Build the procedural-memory prompt identically for sync and async callers."""
    return [
        {"role": "system", "content": prompt or PROCEDURAL_MEMORY_SYSTEM_PROMPT},
        *messages,
        {"role": "user", "content": "Create procedural memory of the above conversation."},
    ]


def _normalize_context_request(query: str, user_id: str, session_id: str) -> tuple[str, str, str]:
    """Normalize identifiers and query used by unified context retrieval."""
    normalized_user_id = normalize_profile_user_id(user_id)
    normalized_session_id = normalize_memory_identifier(session_id, "session_id", allow_none=False)
    normalized_query = _validate_and_trim_search_query(query)
    return normalized_query, normalized_user_id, normalized_session_id


def _assemble_retrieved_context(
    *,
    query: str,
    user_id: str,
    session_id: str,
    search_result: Any,
    profile_result: Dict[str, Any],
    short_term_messages: list[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build the stable context payload shared by sync and async APIs."""
    retrieved_memories = (
        search_result["results"] if isinstance(search_result, dict) and "results" in search_result else search_result
    )
    messages = []
    for message in short_term_messages:
        context_message = {
            "role": message.get("role"),
            "content": message.get("content"),
            "created_at": message.get("created_at"),
        }
        name = message.get("name")
        if isinstance(name, str) and name.strip():
            context_message["name"] = name
        messages.append(context_message)
        turn_index = message.get("turn_index")
        if turn_index is not None and int(turn_index) > 0:
            context_message["turn_index"] = int(turn_index)

    return {
        "user_id": user_id,
        "session_id": session_id,
        "query": query,
        "profile": profile_result["profile"],
        "short_term_messages": messages,
        "retrieved_memories": retrieved_memories,
    }


def _filter_shortterm_duplicate_longterm(context: Dict[str, Any]) -> None:
    """Hide same-run fine-grained facts while their source QA is still visible."""
    visible = {
        int(message["turn_index"])
        for message in context.get("short_term_messages") or []
        if message.get("turn_index") is not None
    }
    if not visible:
        return
    current_run_id = str(context.get("session_id") or "")
    filtered = []
    for memory in context.get("retrieved_memories") or []:
        if not isinstance(memory, dict) or memory.get("source") != "long_term":
            filtered.append(memory)
            continue
        metadata = memory.get("metadata") if isinstance(memory.get("metadata"), dict) else {}
        run_id = memory.get("run_id") or metadata.get("run_id")
        source_turn_index = memory.get("source_turn_index") or metadata.get("source_turn_index")
        try:
            duplicate = str(run_id or "") == current_run_id and int(source_turn_index) in visible
        except (TypeError, ValueError):
            duplicate = False
        if not duplicate:
            filtered.append(memory)
    context["retrieved_memories"] = filtered


def _strip_shortterm_internal_fields(context: Dict[str, Any]) -> None:
    for message in context.get("short_term_messages") or []:
        message.pop("turn_index", None)


def _serialize_prompt_value(value: Any, empty_value: Any) -> str:
    return json.dumps(
        value if value is not None else empty_value,
        ensure_ascii=False,
        indent=2,
        default=str,
    )


def _memory_created_at_sort_key(memory: Dict[str, Any]) -> tuple[bool, str]:
    created_at = normalize_iso_timestamp_to_beijing(memory.get("created_at"))
    return not bool(created_at), str(created_at or "")


def _project_retrieved_memories(
    retrieved_context: Dict[str, Any],
) -> tuple[list[Dict[str, Any]], list[Dict[str, Any]]]:
    """Project retrieved memories to the fields shared by answer and agentic prompts."""
    mid_term_memories = []
    long_term_memories = []
    for memory in retrieved_context.get("retrieved_memories") or []:
        if not isinstance(memory, dict):
            continue

        source = str(memory.get("source") or "")
        if source in {"mid_term_page", "midterm"}:
            content = memory.get("raw_dialogue")
            if content:
                mid_term_memories.append(
                    {
                        "score": memory.get("score"),
                        "created_at": memory.get("created_at"),
                        "content": content,
                    }
                )
        elif not source.startswith("mid_term"):
            content = memory.get("memory")
            if content:
                long_term_memories.append(
                    {
                        "score": memory.get("score"),
                        "created_at": memory.get("created_at"),
                        "content": content,
                    }
                )

    mid_term_memories.sort(key=_memory_created_at_sort_key)
    long_term_memories.sort(key=_memory_created_at_sort_key)
    return mid_term_memories, long_term_memories


def _build_answer_prompt_messages(
    retrieved_context: Dict[str, Any],
    reference_information: Any = None,
    agentic_memory_supplement: str = "",
    *,
    agentic_answer: Optional[str] = None,
) -> list[Dict[str, str]]:
    """Project retrieved context to the minimal fields needed by the answer model."""
    mid_term_memories, long_term_memories = _project_retrieved_memories(retrieved_context)
    # ``agentic_answer`` is a compatibility alias for callers using the old
    # parameter name. Its value now has supplement-only semantics.
    if not agentic_memory_supplement and agentic_answer:
        agentic_memory_supplement = agentic_answer

    prompt = AGENT_ANSWER_PROMPT.format(
        current_time=beijing_now_iso(),
        user_query=retrieved_context["query"],
        short_term_memory=_serialize_prompt_value(retrieved_context.get("short_term_messages"), []),
        mid_term_memory=_serialize_prompt_value(mid_term_memories, []),
        long_term_memory=_serialize_prompt_value(long_term_memories, []),
        user_profile=_serialize_prompt_value(retrieved_context.get("profile"), {}),
        reference_information=_serialize_prompt_value(reference_information, []),
        agentic_memory_supplement=agentic_memory_supplement,
    )
    return [{"role": "system", "content": prompt}]


def build_answer_prompt_messages_from_context(
    retrieved_context: Dict[str, Any],
    reference_information: Any = None,
    agentic_memory_supplement: str = "",
    *,
    agentic_answer: Optional[str] = None,
) -> list[Dict[str, str]]:
    """Build final answer messages from a context retrieved by the core memory flow."""
    return _build_answer_prompt_messages(
        retrieved_context,
        reference_information,
        agentic_memory_supplement=agentic_memory_supplement,
        agentic_answer=agentic_answer,
    )


def _build_agentic_prompt_messages(
    retrieved_context: Dict[str, Any],
    reference_information: Any = None,
) -> list[Dict[str, str]]:
    """Build the Agentic prompt from an already retrieved, complete context."""
    mid_term_memories, long_term_memories = _project_retrieved_memories(retrieved_context)
    prompt = AGENTIC_RETRIEVAL_PROMPT.format(
        current_time=beijing_now_iso(),
        user_query=retrieved_context["query"],
        short_term_memory=_serialize_prompt_value(retrieved_context.get("short_term_messages"), []),
        mid_term_memory=_serialize_prompt_value(mid_term_memories, []),
        long_term_memory=_serialize_prompt_value(long_term_memories, []),
        user_profile=_serialize_prompt_value(retrieved_context.get("profile"), {}),
        reference_information=_serialize_prompt_value(reference_information, []),
    )
    # The query is already present in the structured system prompt. Repeating
    # it as a user message over-emphasizes answering in this non-answer node.
    return [{"role": "system", "content": prompt}]


def _agentic_supplement_or_empty(result: Any) -> str:
    """Return a validated memory supplement, or an empty string when unavailable."""
    if not isinstance(result, dict):
        logger.warning("Agentic retrieval returned an invalid result type: %s", type(result).__name__)
        return ""

    status = result.get("status")
    if status not in {"not_needed", "supplemented", "no_relevant_memory", "degraded"}:
        logger.warning("Agentic retrieval returned an invalid status: %s", status)
        return ""

    iterations = result.get("iterations")
    if not isinstance(iterations, int) or iterations not in {1, 2}:
        logger.warning("Agentic retrieval returned an invalid iteration count")
        return ""

    tool_call_count = result.get("tool_call_count")
    if not isinstance(tool_call_count, int) or tool_call_count not in {0, 1}:
        logger.warning("Agentic retrieval returned an invalid tool call count")
        return ""

    tool_trace = result.get("tool_trace") or []
    if not isinstance(tool_trace, list):
        logger.warning("Agentic retrieval returned an invalid tool trace")
        return ""
    if len(tool_trace) != tool_call_count:
        logger.warning("Agentic retrieval returned an inconsistent tool trace")
        return ""
    for trace_item in tool_trace:
        if not isinstance(trace_item, dict):
            logger.warning("Agentic retrieval returned an invalid tool trace item")
            return ""
    if status == "not_needed" and tool_call_count != 0:
        logger.warning("Agentic retrieval returned an inconsistent not_needed result")
        return ""
    if status in {"supplemented", "no_relevant_memory"} and tool_call_count != 1:
        logger.warning("Agentic retrieval returned an inconsistent searched result")
        return ""

    supplement = result.get("supplement")
    if not isinstance(supplement, str):
        logger.warning("Agentic retrieval returned an invalid supplement")
        return ""
    answer_alias = result.get("answer")
    if answer_alias is not None and answer_alias != supplement:
        logger.warning("Agentic retrieval returned inconsistent supplement compatibility fields")
        return ""

    if status != "supplemented":
        if supplement.strip():
            logger.warning("Agentic retrieval returned text for a non-supplemented status; ignoring it")
        return ""

    for trace_item in tool_trace:
        summary = trace_item.get("result_summary")
        error_count = summary.get("error_count") if isinstance(summary, dict) else None
        if (
            not isinstance(summary, dict)
            or summary.get("ok") is not True
            or (error_count is not None and (not isinstance(error_count, int) or error_count > 0))
        ):
            logger.warning("Agentic retrieval tool call failed; memory supplement will be ignored")
            return ""

    if not supplement.strip():
        logger.warning("Agentic retrieval returned an empty supplemented result")
        return ""
    return supplement.strip()


def _agentic_answer_or_empty(result: Any) -> str:
    """Compatibility alias for the former candidate-answer normalizer."""
    return _agentic_supplement_or_empty(result)


def _entity_collection_name(provider: str, collection_name: str) -> str:
    separator = "-" if provider == "s3_vectors" else "_"
    return f"{collection_name}{separator}entities"


def _normalize_expiration_date(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError as exc:
            raise ValueError("expiration_date must be a valid date in YYYY-MM-DD format.") from exc
    raise ValueError("expiration_date must be a date string in YYYY-MM-DD format.")


def _payload_is_expired(payload: Optional[Dict[str, Any]]) -> bool:
    if not payload:
        return False
    expiration_date = payload.get("expiration_date")
    if not expiration_date:
        return False
    try:
        return date.fromisoformat(str(expiration_date)) < beijing_now().date()
    except ValueError:
        return False


def _effective_longterm_threshold(memory: Any, requested: Optional[float]) -> float:
    """Keep the public threshold as an optional stricter bound on configured RAG gating."""
    memory_config = getattr(memory, "config", None)
    fine_config = getattr(memory_config, "fine_grained_longterm", None)
    nested = getattr(fine_config, "rag_threshold", None)
    legacy = getattr(memory_config, "longterm_rag_threshold", None)
    configured = max(
        float(nested) if nested is not None else 0.1,
        float(legacy) if legacy is not None else 0.1,
    )
    configured_threshold = 0.1 if configured is None else float(configured)
    if requested is None:
        return configured_threshold
    return max(float(requested), configured_threshold)


def _configured_query_rewrite_prompt(memory: Any) -> str:
    config = getattr(memory, "config", None)
    prompt = getattr(config, "query_rewrite_prompt", None)
    return prompt if isinstance(prompt, str) and prompt.strip() else QUERY_REFERENCE_RESOLUTION_PROMPT


def _configured_fine_grained_extraction_prompt(memory: Any) -> str:
    config = getattr(memory, "config", None)
    fine_config = getattr(config, "fine_grained_longterm", None)
    prompt = getattr(fine_config, "extraction_prompt", None)
    return prompt if isinstance(prompt, str) and prompt.strip() else ADDITIVE_EXTRACTION_PROMPT


def _configured_fine_grained_extraction_request_options(memory: Any) -> Dict[str, Any]:
    config = getattr(memory, "config", None)
    fine_config = getattr(config, "fine_grained_longterm", None)
    options = getattr(fine_config, "extraction_request_options", None)
    return dict(options) if isinstance(options, dict) else {}


def _fine_grained_longterm_config(memory: Any) -> FineGrainedLongTermConfig:
    config = getattr(memory, "config", None)
    fine_config = getattr(config, "fine_grained_longterm", None)
    if isinstance(fine_config, FineGrainedLongTermConfig):
        return fine_config

    def legacy_number(name: str, default: int | float) -> int | float:
        value = getattr(config, name, default)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default

    return FineGrainedLongTermConfig(
        top_k=int(legacy_number("longterm_top_k", 20)),
        rag_threshold=float(legacy_number("longterm_rag_threshold", 0.1)),
        candidate_pool_multiplier=int(legacy_number("longterm_candidate_pool_multiplier", 4)),
        other_session_weight=float(legacy_number("longterm_other_session_weight", 0.7)),
        entity_similarity_threshold=float(legacy_number("entity_similarity_threshold", 0.5)),
        extraction_prompt=_configured_fine_grained_extraction_prompt(memory),
    )


setup_config()
logger = logging.getLogger(__name__)

_UNSET = object()
_PROJECT_UPDATE_UNSUPPORTED_ERROR = "Project updates are not supported by the OSS Memory SDK."


class _OSSProject:
    def update(
        self,
        custom_instructions: Optional[str] = None,
        custom_categories: Optional[list] = None,
        retrieval_criteria: Optional[list] = None,
        multilingual: Optional[bool] = None,
        decay: Optional[bool] = None,
    ):
        if decay is True:
            raise ValueError(get_decay_feature_error_message("sync", "project.update", "decay"))
        raise ValueError(_PROJECT_UPDATE_UNSUPPORTED_ERROR)


class _AsyncOSSProject:
    async def update(
        self,
        custom_instructions: Optional[str] = None,
        custom_categories: Optional[list] = None,
        retrieval_criteria: Optional[list] = None,
        multilingual: Optional[bool] = None,
        decay: Optional[bool] = None,
    ):
        if decay is True:
            raise ValueError(await get_decay_feature_error_message_async("async", "project.update", "decay"))
        raise ValueError(_PROJECT_UPDATE_UNSUPPORTED_ERROR)


class _BackgroundMemoryMixin:
    db: SQLiteManager

    def _cross_session_longterm_enabled(self) -> bool:
        return self._midterm_enabled()

    def _promoted_longterm_enabled(self) -> bool:
        promoted = getattr(getattr(self, "config", None), "promoted_longterm", None)
        return self._midterm_enabled() and bool(promoted is None or promoted.enabled)

    @property
    def fine_grained_longterm_retriever(self):
        if getattr(self, "_fine_grained_longterm_retriever", None) is None:
            with getattr(self, "_component_init_lock", threading.RLock()):
                if getattr(self, "_fine_grained_longterm_retriever", None) is None:
                    fine_config = _fine_grained_longterm_config(self)
                    self._fine_grained_longterm_retriever = FineGrainedLongTermRetriever(
                        vector_store=getattr(self, "vector_store", None),
                        embedding_model=self.embedding_model,
                        entity_store_provider=lambda: self.entity_store,
                        config=fine_config,
                        reranker=getattr(self, "reranker", None),
                        stage_output_is_visible=lambda payload: self._stage_output_is_visible(payload, "longterm"),
                        payload_is_expired=_payload_is_expired,
                        entity_extractor=lambda query: self._run_entity_extraction(extract_entities, query),
                        entity_boost_provider=lambda entities, filters: self._compute_entity_boosts(entities, filters),
                        entity_boost_provider_async=lambda entities, filters: self._compute_entity_boosts_async(
                            entities, filters
                        ),
                        bm25_preprocessor=lemmatize_for_bm25,
                        bm25_language=getattr(self, "_bm25_language", None),
                    )
        return self._fine_grained_longterm_retriever

    @property
    def promoted_longterm(self):
        if getattr(self, "_cross_session_longterm", None) is None:
            with self._component_init_lock:
                if getattr(self, "_cross_session_longterm", None) is None:
                    self._cross_session_longterm = PromotedLongTermMemory(
                        provider=self.config.vector_store.provider,
                        base_vector_config=self.config.vector_store.config,
                        base_collection_name=self.collection_name,
                        embedding_model=self.embedding_model,
                        config=self.config,
                        primary_vector_store=self.vector_store,
                        vector_store_timeout_seconds=getattr(
                            self.config,
                            "vector_store_timeout_seconds",
                            15.0,
                        ),
                    )
        return self._cross_session_longterm

    @property
    def cross_session_longterm(self):
        """Compatibility alias for promoted_longterm."""
        return self.promoted_longterm

    def _confirm_context_valid_recalls(
        self,
        retrieved_memories: Any,
        *,
        current_turn_index: Optional[int] = None,
        exclude_midterm_page_ids: Optional[set[str]] = None,
    ) -> set[str]:
        """Confirm only memories that are actually projected into model context."""
        if not isinstance(retrieved_memories, list):
            return set()
        excluded = exclude_midterm_page_ids or set()
        page_ids = {
            str(item.get("id"))
            for item in retrieved_memories
            if isinstance(item, dict)
            and item.get("source") in {"mid_term_page", "midterm"}
            and item.get("id") not in (None, "")
            and item.get("raw_dialogue")
            and str(item.get("id")) not in excluded
        }
        cross_session_ids = {
            str(item.get("id"))
            for item in retrieved_memories
            if isinstance(item, dict)
            and item.get("source") == PromotedLongTermMemory.SOURCE
            and item.get("id") not in (None, "")
            and item.get("memory")
        }

        if page_ids:
            if current_turn_index is None:
                raise ValueError("current_turn_index is required to confirm Mid-term recalls")
            self._confirm_valid_midterm_page_ids(page_ids, current_turn_index=current_turn_index)

        if cross_session_ids and self._promoted_longterm_enabled():
            try:
                self.promoted_longterm.record_valid_recalls(sorted(cross_session_ids))
            except Exception:
                logger.warning("Failed to record cross-session long-term recalls", exc_info=True)
        return page_ids

    def _confirm_valid_midterm_page_ids(self, page_ids: Any, *, current_turn_index: int) -> None:
        normalized_page_ids = sorted({str(page_id) for page_id in (page_ids or []) if page_id not in (None, "")})
        if not normalized_page_ids or not self._midterm_enabled():
            return
        try:
            updated_sessions = self.midterm_memory.record_valid_recalls(
                normalized_page_ids,
                recall_turn_index=current_turn_index,
            )
            if not self._promoted_longterm_enabled():
                return
            for session in updated_sessions:
                if int(session.get("valid_recall_count", 0) or 0) < int(self.config.midterm.promotion_min_recall_count):
                    continue
                if float(session.get("H_segment", 0.0) or 0.0) < float(self.config.midterm.promotion_heat_threshold):
                    continue
                try:
                    self._enqueue_promotion_job(session)
                except Exception:
                    logger.warning(
                        "Cross-session long-term promotion enqueue failed for session_id=%s",
                        session.get("id"),
                        exc_info=True,
                    )
        except Exception:
            logger.warning("Failed to record valid mid-term recalls", exc_info=True)

    def _enqueue_promotion_job(self, session: Dict[str, Any]) -> Optional[str]:
        user_id = session.get("user_id")
        session_id = session.get("id")
        if user_id in (None, "") or session_id in (None, ""):
            return None
        source_version = promotion_source_version(session)
        job = self.db.ensure_promotion_job(
            user_id=str(user_id),
            source_midterm_session_id=str(session_id),
            source_run_id=session.get("run_id"),
            source_version=source_version,
        )
        self._ensure_background_workers().wake_promotion()
        return str(job["job_id"])

    def _normalize_agentic_supplement_result(self, result: Any) -> str:
        """Normalize an Agentic result for use as optional historical context."""
        return _agentic_supplement_or_empty(result)

    def _normalize_agentic_answer_result(self, result: Any) -> str:
        """Compatibility alias for integrations using the former method name."""
        return self._normalize_agentic_supplement_result(result)

    def _background_config(self) -> BackgroundTaskConfig:
        configured = getattr(getattr(self, "config", None), "background", None)
        if configured is None:
            # Lightweight test doubles and legacy hand-built config objects do not
            # have enough lifecycle state to safely own worker threads.
            return BackgroundTaskConfig(enabled=False)
        return configured

    def _initialize_entity_extraction_executor(self) -> None:
        if getattr(self, "_entity_extraction_executor", None) is not None:
            return
        config = self._background_config()
        self._entity_extraction_executor = BoundedTimeoutExecutor(
            max_workers=config.entity_extraction_worker_count,
            max_pending=config.entity_extraction_pending_capacity,
            thread_name_prefix="mem0-entity-extraction",
        )

    def _run_entity_extraction(self, function, *args):
        self._initialize_entity_extraction_executor()
        config = getattr(self, "config", None)
        return self._entity_extraction_executor.run(
            function,
            *args,
            timeout_seconds=getattr(config, "entity_extraction_timeout_seconds", 60.0),
            operation_name="Entity extraction",
        )

    def _shutdown_entity_extraction_executor(self) -> None:
        executor = getattr(self, "_entity_extraction_executor", None)
        if executor is not None:
            executor.shutdown(wait=True)

    def _create_background_worker_manager(self) -> BackgroundWorkerManager:
        """Create the worker manager used by this memory runtime.

        Subclasses may override this factory to provide a compatible worker
        lifecycle without changing the default production behavior.
        """
        return BackgroundWorkerManager(
            self.db,
            self._background_config(),
            process_midterm=self._background_process_midterm,
            process_longterm=self._background_process_longterm,
            process_profile=self._background_process_profile,
            process_promotion=self._background_process_promotion,
            process_midterm_async=getattr(self, "_background_process_midterm_async", None),
            process_longterm_async=getattr(self, "_background_process_longterm_async", None),
            process_profile_async=getattr(self, "_background_process_profile_async", None),
            process_promotion_async=getattr(self, "_background_process_promotion_async", None),
            commit_migration_outputs=self._commit_migration_stage_outputs,
            commit_migration_outputs_async=getattr(self, "_commit_migration_stage_outputs_async", None),
            discard_migration_outputs=self._discard_migration_stage_outputs,
            startup_cleanup=self._cleanup_orphan_staging_outputs,
            process_longterm_extraction=self._background_process_longterm_extraction,
            process_longterm_extraction_async=getattr(self, "_background_process_longterm_extraction_async", None),
            commit_longterm_extraction_outputs=self._commit_longterm_extraction_outputs,
            discard_longterm_extraction_outputs=self._discard_longterm_extraction_outputs,
        )

    def _acquire_process_instance_lock(self) -> None:
        self._process_instance_lock = ProcessInstanceLock(
            self.config.history_db_path,
            enabled=self.config.enforce_single_process,
        )
        self._process_instance_lock.acquire()
        if self._process_instance_lock.acquired:
            logger.info("single process lock acquired history_db_path=%s", self.config.history_db_path)

    def _release_process_instance_lock(self) -> None:
        process_lock = getattr(self, "_process_instance_lock", None)
        if process_lock is None or not process_lock.acquired:
            return
        process_lock.release()
        logger.info("process lock released history_db_path=%s", self.config.history_db_path)

    def _cleanup_orphan_staging_outputs(self) -> Dict[str, Any]:
        result = {
            "midterm_discarded": 0,
            "longterm_discarded": 0,
            "cleanup_errors": [],
        }
        terminal_statuses = MIGRATION_STAGE_TERMINAL_STATUSES

        if (
            self._midterm_enabled()
            and hasattr(self, "vector_store")
            and getattr(self.config, "vector_store", None) is not None
        ):
            ephemeral_midterm = False
            midterm_memory = None
            try:
                midterm_memory = getattr(self, "_midterm_memory", None)
                if midterm_memory is None:
                    ephemeral_midterm = True
                    midterm_memory = MidTermMemory(
                        provider=self.config.vector_store.provider,
                        base_vector_config=self.config.vector_store.config,
                        base_collection_name=self.collection_name,
                        embedding_model=self.embedding_model,
                        config=self.config.midterm,
                        current_turn_index_provider=lambda filters: self.db.current_turn_index(
                            _build_session_scope(filters)
                        ),
                        primary_vector_store=self.vector_store,
                        output_is_visible=lambda payload: self._stage_output_is_visible(payload, "midterm"),
                        vector_store_timeout_seconds=getattr(
                            self.config,
                            "vector_store_timeout_seconds",
                            15.0,
                        ),
                    )
                midterm_updater = getattr(self, "_midterm_updater", None)
                if midterm_updater is None:
                    midterm_updater = MidTermUpdater(midterm_memory, self.llm, self.config.midterm)
                pages = midterm_memory.list_pages(top_k=10000, include_uncommitted=True)
                cleanup_groups = set()
                for row in pages:
                    payload = dict(getattr(row, "payload", None) or {})
                    source_job_id = payload.get("source_job_id")
                    if payload.get("output_state") != "staging" or not source_job_id:
                        continue
                    job = self.db.get_background_job(source_job_id, "migration")
                    if not job or job.get("midterm_status") not in terminal_statuses:
                        continue
                    cleanup_groups.add((source_job_id, payload.get("output_lease_token")))
                for source_job_id, lease_token in cleanup_groups:
                    before = {
                        str(row.id)
                        for row in midterm_memory.list_pages(top_k=10000, include_uncommitted=True)
                        if (getattr(row, "payload", None) or {}).get("source_job_id") == source_job_id
                        and (getattr(row, "payload", None) or {}).get("output_state") == "staging"
                        and (getattr(row, "payload", None) or {}).get("output_lease_token") == lease_token
                    }
                    cleanup_error = midterm_updater.discard_source_job_outputs(source_job_id, lease_token)
                    if cleanup_error:
                        result["cleanup_errors"].append(f"midterm {source_job_id}: {cleanup_error}")
                    for page_id in before:
                        page = midterm_memory.get_page(page_id)
                        payload = dict(getattr(page, "payload", None) or {}) if page else {}
                        if payload.get("output_state") != "discarded":
                            continue
                        payload["cleanup_reason"] = "orphan staging after terminal stage"
                        try:
                            midterm_memory.update_page(page_id, payload, reembed=False)
                        except Exception as exc:
                            result["cleanup_errors"].append(f"midterm page {page_id}: {exc}")
                    after = {
                        str(row.id)
                        for row in midterm_memory.list_pages(top_k=10000, include_uncommitted=True)
                        if (getattr(row, "payload", None) or {}).get("output_state") == "staging"
                    }
                    result["midterm_discarded"] += len(before - after)

                for row in midterm_memory.list_sessions(top_k=10000, include_uncommitted=True):
                    payload = dict(getattr(row, "payload", None) or {})
                    source_job_id = payload.get("last_output_job_id") or payload.get("source_job_id")
                    if payload.get("output_state") != "staging" or not source_job_id:
                        continue
                    job = self.db.get_background_job(source_job_id, "migration")
                    if not job or job.get("midterm_status") not in terminal_statuses:
                        continue
                    payload.update(
                        {
                            "output_state": "discarded",
                            "output_lease_token": None,
                            "cleanup_reason": "orphan staging after terminal stage",
                        }
                    )
                    try:
                        midterm_memory.update_session(str(row.id), payload, reembed=False)
                    except Exception as exc:
                        result["cleanup_errors"].append(f"midterm session {row.id}: {exc}")
            except Exception as exc:
                result["cleanup_errors"].append(f"midterm scan: {exc}")
                logger.warning("Failed to clean orphan midterm staging outputs", exc_info=True)
            finally:
                if ephemeral_midterm and midterm_memory is not None:
                    primary_client = getattr(self.vector_store, "client", None)
                    for store in (midterm_memory.pages_store, midterm_memory.sessions_store):
                        close = getattr(store, "close", None)
                        client = getattr(store, "client", None)
                        if callable(close):
                            try:
                                close()
                            except Exception:
                                logger.debug("Failed to close temporary midterm cleanup store", exc_info=True)
                        elif client is not None and client is not primary_client:
                            client_close = getattr(client, "close", None)
                            if callable(client_close):
                                try:
                                    client_close()
                                except Exception:
                                    logger.debug(
                                        "Failed to close temporary midterm cleanup client",
                                        exc_info=True,
                                    )

        try:
            if not hasattr(self, "vector_store"):
                return result
            list_method = getattr(self.vector_store, "list", None)
            if callable(list_method):
                longterm_rows = _vector_store_list_rows(list_method(filters=None, top_k=10000))
            else:
                rows = getattr(self.vector_store, "rows", {})
                longterm_rows = [
                    type("VectorRow", (), {"id": memory_id, "payload": dict(row.get("payload") or {})})()
                    for memory_id, row in rows.items()
                ]
            for row in longterm_rows:
                payload = dict(getattr(row, "payload", None) or {})
                source_job_id = payload.get("source_job_id")
                if (
                    payload.get("source_stage") != "longterm"
                    or payload.get("output_state") != "staging"
                    or not source_job_id
                ):
                    continue
                if payload.get("source_job_type") == "longterm_extraction":
                    job = self.db.get_background_job(source_job_id, "longterm_extraction")
                    if not job or job.get("status") not in {"succeeded", "discarded"}:
                        continue
                else:
                    job = self.db.get_background_job(source_job_id, "migration")
                    if not job or job.get("longterm_status") not in terminal_statuses:
                        continue
                memory_id = str(row.id)
                payload.update(
                    {
                        "output_state": "discarded",
                        "output_lease_token": None,
                        "cleanup_reason": "orphan staging after terminal stage",
                    }
                )
                try:
                    _update_vector_store_payload(self.vector_store, memory_id, payload)
                    result["longterm_discarded"] += 1
                except Exception as exc:
                    result["cleanup_errors"].append(f"longterm hide {memory_id}: {exc}")
                    continue
                try:
                    self.db.delete_history_for_memory_ids([memory_id])
                except Exception as exc:
                    result["cleanup_errors"].append(f"longterm history {memory_id}: {exc}")
                try:
                    self._strict_remove_stage_entity_links(memory_id, job.get("filters") or {})
                except Exception as exc:
                    result["cleanup_errors"].append(f"longterm entity {memory_id}: {exc}")
        except Exception as exc:
            result["cleanup_errors"].append(f"longterm scan: {exc}")
            logger.warning("Failed to clean orphan longterm staging outputs", exc_info=True)

        for cleanup_error in result["cleanup_errors"]:
            logger.warning("Orphan staging cleanup error cleanup_error=%s", cleanup_error)
        return result

    def _stage_output_is_visible(self, payload: Dict[str, Any], stage: str) -> bool:
        source_job_id = payload.get("last_output_job_id") or payload.get("source_job_id")
        if not source_job_id:
            return True
        if payload.get("output_state") != "committed":
            return False
        db = getattr(self, "db", None)
        if db is None or getattr(db, "connection", None) is None:
            return False
        try:
            if payload.get("source_job_type") == "longterm_extraction":
                job = db.get_background_job(source_job_id, "longterm_extraction")
                return bool(job and job.get("status") == "succeeded")
            job = db.get_background_job(source_job_id, "migration")
        except Exception:
            return False
        return bool(job and job.get(f"{stage}_status") in {"succeeded", "succeeded_degraded"})

    def _get_profile_user_thread_lock(self, user_id: str) -> threading.Lock:
        normalized_user_id = normalize_profile_user_id(user_id)
        guard = getattr(self, "_profile_user_locks_guard", None)
        if guard is None or not hasattr(guard, "__enter__"):
            self._profile_user_locks_guard = threading.Lock()
            self._profile_user_locks = {}
        with self._profile_user_locks_guard:
            lock = self._profile_user_locks.get(normalized_user_id)
            if lock is None:
                lock = threading.Lock()
                self._profile_user_locks[normalized_user_id] = lock
            return lock

    def _clear_profile_user_thread_locks(self) -> None:
        guard = getattr(self, "_profile_user_locks_guard", None)
        if guard is None or not hasattr(guard, "__enter__"):
            self._profile_user_locks_guard = threading.Lock()
            self._profile_user_locks = {}
            return
        with guard:
            self._profile_user_locks.clear()

    def _clear_profile_runtime_state(self) -> None:
        """Drop profile objects and per-user locks tied to the current database runtime."""
        self._profile_manager = None
        self._profile_updater = None
        self._clear_profile_user_thread_locks()

    def _initialize_background_workers(self) -> None:
        if not hasattr(self, "_background_lifecycle_lock"):
            self._background_lifecycle_lock = threading.RLock()
        self._initialize_entity_extraction_executor()
        self._closed = False
        self._background_worker = self._create_background_worker_manager()
        self._background_worker.start()

    def _ensure_background_workers(self) -> BackgroundWorkerManager:
        worker = getattr(self, "_background_worker", None)
        if worker is None:
            self._initialize_background_workers()
            worker = self._background_worker
        return worker

    def _background_process_midterm(self, job, messages, degraded: bool) -> None:
        if not self._midterm_enabled():
            return

        def lease_is_current():
            return self.db.migration_stage_lease_is_current(
                job["job_id"],
                "midterm",
                job["midterm_lease_token"],
            )

        if not lease_is_current():
            raise RuntimeError("stale migration stage lease")

        result = self._process_midterm_evictions(
            messages,
            job["filters"],
            source_job_id=job["job_id"],
            lease_token=job["midterm_lease_token"],
            lease_is_current=lease_is_current,
            degraded=degraded,
            raise_on_error=True,
        )

        if asyncio.iscoroutine(result):
            asyncio.run(result)

    def _background_process_longterm(self, job, messages, degraded: bool) -> None:
        def lease_is_current():
            return self.db.migration_stage_lease_is_current(
                job["job_id"],
                "longterm",
                job["longterm_lease_token"],
            )

        if not lease_is_current():
            raise RuntimeError("stale migration stage lease")
        if degraded:
            self._store_longterm_fallback(job, messages, lease_is_current=lease_is_current)
            return
        result = self._process_evicted_long_term_memories(
            messages,
            job["metadata"],
            job["filters"],
            infer=job["infer"],
            prompt=job.get("prompt"),
            source_job_id=job["job_id"],
            lease_token=job["longterm_lease_token"],
            lease_is_current=lease_is_current,
        )
        if asyncio.iscoroutine(result):
            asyncio.run(result)

    def _background_process_longterm_extraction(self, job) -> None:
        def lease_is_current():
            return self.db.longterm_extraction_job_lease_is_current(job["job_id"], job["lease_token"])

        if not lease_is_current():
            raise RuntimeError("stale fine-grained LongTerm extraction lease")
        metadata = {
            **job["metadata"],
            "source_job_type": "longterm_extraction",
            "source_turn_index": int(job["turn_index"]),
        }
        if job.get("source_operation_key"):
            metadata["source_operation_key"] = job["source_operation_key"]
        result = self._process_evicted_long_term_memories(
            job["messages"],
            metadata,
            job["filters"],
            infer=job["infer"],
            prompt=job.get("prompt"),
            source_job_id=job["job_id"],
            lease_token=job["lease_token"],
            lease_is_current=lease_is_current,
        )
        if asyncio.iscoroutine(result):
            asyncio.run(result)

    def _commit_longterm_extraction_outputs(self, job: Dict[str, Any], lease_token: str) -> None:
        extraction_job = {**job, "job_type": "longterm_extraction"}
        self._commit_migration_stage_outputs(extraction_job, "longterm", lease_token, False)

    def _discard_longterm_extraction_outputs(
        self,
        job: Dict[str, Any],
        lease_token: str,
    ) -> Optional[str]:
        return self._discard_migration_stage_outputs(
            {**job, "job_type": "longterm_extraction"},
            "longterm",
            lease_token,
        )

    def _background_process_promotion(self, job) -> Optional[str]:
        if not self._promoted_longterm_enabled():
            return "promoted long-term memory is disabled"
        session_id = str(job["source_midterm_session_id"])
        session = self.midterm_memory.get_session(session_id)
        if not session:
            return "source mid-term session no longer exists"
        session_payload = dict(getattr(session, "payload", None) or {})
        if str(session_payload.get("user_id") or "") != str(job["user_id"]):
            return "source mid-term session user does not match promotion job"
        if promotion_source_version(session_payload) != str(job["source_version"]):
            return "source mid-term session version has changed"
        if int(session_payload.get("valid_recall_count", 0) or 0) < int(self.config.midterm.promotion_min_recall_count):
            return "source mid-term session no longer meets recall threshold"
        if float(session_payload.get("H_segment", 0.0) or 0.0) < float(self.config.midterm.promotion_heat_threshold):
            return "source mid-term session no longer meets heat threshold"

        def lease_is_current() -> bool:
            return self.db.promotion_job_lease_is_current(job["job_id"], job["lease_token"])

        if not lease_is_current():
            raise RuntimeError("stale promotion job lease")
        promoted = self.promoted_longterm.promote_session(
            session_id,
            self.midterm_memory,
            expected_source_version=str(job["source_version"]),
            lease_is_current=lease_is_current,
        )
        if promoted is None:
            return "source mid-term session is not promotable"
        return None

    def _store_longterm_fallback(self, job, messages, *, lease_is_current=None) -> None:
        job_id = job["job_id"]
        memory_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"mem0:longterm-fallback:{job_id}"))
        try:
            existing = self.vector_store.get(vector_id=memory_id)
        except Exception:
            existing = None
        if lease_is_current is not None and not lease_is_current():
            raise RuntimeError("stale migration stage lease")
        if existing is not None:
            payload = dict(getattr(existing, "payload", None) or {})
            payload.update(
                {
                    "source_job_id": job_id,
                    "source_stage": "longterm",
                    "output_state": "staging",
                    "output_lease_token": job.get("longterm_lease_token"),
                    "degraded": True,
                    "needs_reprocessing": True,
                }
            )
            _update_vector_store_payload(self.vector_store, memory_id, payload)
            return

        raw_dialogue = parse_messages(messages)
        metadata = deepcopy(job["metadata"])
        metadata.update(
            {
                "source_job_id": job_id,
                "source_stage": "longterm",
                "output_state": "staging",
                "output_lease_token": job.get("longterm_lease_token"),
                "memory_type": "raw_fallback",
                "needs_reprocessing": True,
                "degraded": True,
            }
        )
        embedding = self.embedding_model.embed(raw_dialogue, "add")
        result = self._create_memory(
            raw_dialogue,
            {raw_dialogue: embedding},
            metadata,
            memory_id=memory_id,
        )
        if asyncio.iscoroutine(result):
            asyncio.run(result)

    def _ensure_longterm_history(
        self,
        memory_id: str,
        memory_text: str,
        created_at: Optional[str],
    ) -> None:
        if self.db.get_history(memory_id):
            return
        self.db.add_history(
            memory_id,
            None,
            memory_text,
            "ADD",
            created_at=created_at,
        )

    def _longterm_source_rows(self, source_job_id: str):
        list_method = getattr(self.vector_store, "list", None)
        if callable(list_method):
            listed = list_method(filters={"source_job_id": source_job_id}, top_k=10000)
        else:
            rows = getattr(self.vector_store, "rows", {})
            listed = [
                type("VectorRow", (), {"id": memory_id, "payload": dict(row.get("payload") or {})})()
                for memory_id, row in rows.items()
            ]
        return [
            row
            for row in _vector_store_list_rows(listed)
            if (getattr(row, "payload", None) or {}).get("source_job_id") == source_job_id
        ]

    def _longterm_staging_payload_owned_by_lease(
        self,
        memory_id: str,
        lease_token: str,
    ) -> Optional[Dict[str, Any]]:
        row = self.vector_store.get(vector_id=memory_id)
        if row is None:
            return None
        payload = dict(getattr(row, "payload", None) or {})
        if payload.get("output_state") != "staging":
            return None
        if payload.get("output_lease_token") != lease_token:
            return None
        return payload

    @staticmethod
    def _run_maybe_async(result):
        if asyncio.iscoroutine(result):
            return asyncio.run(result)
        return result

    def _strict_remove_stage_entity_links(self, memory_id: str, filters: Dict[str, Any]) -> None:
        if getattr(self, "_entity_store", None) is None:
            return
        search_filters = _longterm_entity_filters(filters)
        rows = _vector_store_list_rows(self.entity_store.list(filters=search_filters, top_k=10000))
        for row in rows:
            payload = dict(getattr(row, "payload", None) or {})
            linked = payload.get("linked_memory_ids") or []
            if memory_id not in linked:
                continue
            remaining = [linked_id for linked_id in linked if linked_id != memory_id]
            if not remaining:
                self.entity_store.delete(vector_id=row.id)
                continue
            entity_text = payload.get("data")
            vector = self.embedding_model.embed(entity_text, "update") if entity_text else None
            self.entity_store.update(
                vector_id=row.id,
                vector=vector,
                payload=_update_entity_payload(payload, remaining),
            )

    def _commit_migration_stage_outputs(
        self,
        job: Dict[str, Any],
        stage: str,
        lease_token: str,
        degraded: bool,
    ) -> None:
        def lease_is_current():
            if job.get("job_type") == "longterm_extraction":
                return self.db.longterm_extraction_job_lease_is_current(job["job_id"], lease_token)
            return self.db.migration_stage_lease_is_current(
                job["job_id"],
                stage,
                lease_token,
            )

        if not lease_is_current():
            raise RuntimeError("stale migration stage lease")
        if stage == "midterm":
            if not self._midterm_enabled():
                return
            self.midterm_updater.commit_source_job_outputs(
                job["job_id"],
                lease_token,
                degraded=degraded,
                lease_is_current=lease_is_current,
            )
            return

        if not hasattr(self, "vector_store"):
            return
        pending_rows = []
        prepared_memory_ids = []
        try:
            for row in self._longterm_source_rows(job["job_id"]):
                if not lease_is_current():
                    raise RuntimeError("stale migration stage lease")
                memory_id = str(row.id)
                payload = dict(getattr(row, "payload", None) or {})
                if payload.get("output_state") == "committed":
                    continue
                if payload.get("output_state") != "staging" or payload.get("output_lease_token") != lease_token:
                    raise RuntimeError("staging output is owned by another lease")
                pending_rows.append((memory_id, payload))
                if not lease_is_current():
                    raise RuntimeError("stale migration stage lease")
                self._ensure_longterm_history(
                    memory_id,
                    payload.get("data", ""),
                    payload.get("created_at"),
                )
                prepared_memory_ids.append(memory_id)
                if hasattr(self, "_component_init_lock"):
                    self._run_maybe_async(
                        self._link_entities_for_memory(memory_id, payload.get("data", ""), job["filters"])
                    )
                if not lease_is_current():
                    raise RuntimeError("stale migration stage lease")
            if not lease_is_current():
                raise RuntimeError("stale migration stage lease")
            for memory_id, _payload in pending_rows:
                if not lease_is_current():
                    raise RuntimeError("stale migration stage lease")
                current_payload = self._longterm_staging_payload_owned_by_lease(memory_id, lease_token)
                if current_payload is None:
                    raise RuntimeError("staging output is no longer owned by this lease")
                _update_vector_store_payload(
                    self.vector_store,
                    memory_id,
                    {
                        **current_payload,
                        "output_state": "committed",
                        "output_lease_token": None,
                        "degraded": degraded,
                        "needs_reprocessing": degraded,
                    },
                )
            if not lease_is_current():
                raise RuntimeError("stale migration stage lease")
        except Exception:
            owned_memory_ids = [
                memory_id
                for memory_id in prepared_memory_ids
                if self._longterm_staging_payload_owned_by_lease(memory_id, lease_token) is not None
            ]
            if owned_memory_ids:
                self.db.delete_history_for_memory_ids(owned_memory_ids)
            for memory_id in owned_memory_ids:
                try:
                    self._strict_remove_stage_entity_links(memory_id, job["filters"])
                except Exception:
                    logger.warning("Failed to roll back stage entity links memory_id=%s", memory_id, exc_info=True)
            raise

    def _discard_migration_stage_outputs(
        self,
        job: Dict[str, Any],
        stage: str,
        lease_token: str,
    ) -> Optional[str]:
        if stage == "midterm":
            if not self._midterm_enabled():
                return None
            return self.midterm_updater.discard_source_job_outputs(job["job_id"], lease_token)

        if not hasattr(self, "vector_store"):
            return None
        cleanup_errors = []
        owned_staging_rows = []
        for row in self._longterm_source_rows(job["job_id"]):
            memory_id = str(row.id)
            payload = dict(getattr(row, "payload", None) or {})
            if payload.get("output_state") != "staging":
                continue
            if payload.get("output_lease_token") != lease_token:
                continue
            owned_staging_rows.append((memory_id, payload))

        discarded_memory_ids = []
        for memory_id, _payload in owned_staging_rows:
            payload = self._longterm_staging_payload_owned_by_lease(memory_id, lease_token)
            if payload is None:
                continue
            try:
                _update_vector_store_payload(
                    self.vector_store,
                    memory_id,
                    {
                        **payload,
                        "output_state": "discarded",
                        "output_lease_token": None,
                        "discarded_by_lease_token": lease_token,
                    },
                )
                discarded_memory_ids.append(memory_id)
            except Exception as exc:
                cleanup_errors.append(f"hide {memory_id}: {exc}")

        cleanup_memory_ids = []
        for memory_id in discarded_memory_ids:
            current = self.vector_store.get(vector_id=memory_id)
            payload = dict(getattr(current, "payload", None) or {}) if current else {}
            if payload.get("output_state") == "discarded" and payload.get("discarded_by_lease_token") == lease_token:
                cleanup_memory_ids.append(memory_id)
        if cleanup_memory_ids:
            try:
                self.db.delete_history_for_memory_ids(cleanup_memory_ids)
            except Exception as exc:
                cleanup_errors.append(f"history: {exc}")
        for memory_id in cleanup_memory_ids:
            current = self.vector_store.get(vector_id=memory_id)
            payload = dict(getattr(current, "payload", None) or {}) if current else {}
            if payload.get("output_state") != "discarded" or payload.get("discarded_by_lease_token") != lease_token:
                continue
            try:
                self._strict_remove_stage_entity_links(memory_id, job["filters"])
            except Exception as exc:
                cleanup_errors.append(f"entity {memory_id}: {exc}")
        return "; ".join(cleanup_errors) or None

    def _background_process_profile(self, job) -> bool:
        lock = self._get_profile_user_thread_lock(job["user_id"])
        with lock:
            user_messages = select_profile_user_messages(
                job["messages"],
                self.config.profile.max_input_user_messages,
            )
            plan = None
            if user_messages:
                current_profile = self.profile_manager.get_profile(job["user_id"])
                attribute_catalog = self.profile_manager.list_attributes()
                plan = self.profile_updater.generate_update_plan(
                    current_profile=current_profile,
                    attribute_catalog=attribute_catalog,
                    messages=user_messages,
                )
            validated_plan = self.profile_manager.validate_update_plan(
                plan or ProfileUpdatePlan(operations=[]),
            )
            return self.db.apply_profile_plan_and_finish_job(
                job["job_id"],
                job["lease_token"],
                job["user_id"],
                validated_plan,
                max_value_json_bytes=self.config.profile.max_value_json_bytes,
            )

    def _profile_job_user_id_after_add(self, user_id) -> Optional[str]:
        profile_config = getattr(self.config, "profile", None)
        if (
            profile_config is None
            or profile_config.enabled is not True
            or profile_config.update_on_add is not True
            or not user_id
        ):
            return None
        return normalize_profile_user_id(user_id)

    def _create_profile_job_after_add(self, user_id, messages) -> Optional[str]:
        normalized_user_id = self._profile_job_user_id_after_add(user_id)
        if normalized_user_id is None:
            return None
        return self.db.create_profile_update_job(normalized_user_id, messages)

    def _enqueue_profile_job_after_add(self, user_id, messages) -> Optional[str]:
        if not hasattr(self, "_background_lifecycle_lock"):
            self._background_lifecycle_lock = threading.RLock()
        with self._background_lifecycle_lock:
            if getattr(self, "_closed", False):
                raise RuntimeError("Cannot add memories after Memory.close()")
            profile_job_id = self._create_profile_job_after_add(user_id, messages)
            if profile_job_id:
                self._ensure_background_workers().wake_profile()
            return profile_job_id

    def _save_and_enqueue_background_jobs(
        self,
        messages,
        session_scope,
        processed_metadata,
        effective_filters,
        *,
        normalized_user_id,
        infer,
        prompt,
        idempotency_key=None,
        request_hash=None,
    ) -> tuple[Optional[str], Optional[str]]:
        if not hasattr(self, "_background_lifecycle_lock"):
            self._background_lifecycle_lock = threading.RLock()

        with self._background_lifecycle_lock:
            if getattr(self, "_closed", False):
                raise RuntimeError("Cannot add memories after Memory.close()")
            worker = self._ensure_background_workers()
            if idempotency_key is not None:
                result = self.db.save_background_add_idempotently(
                    messages,
                    session_scope,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    max_messages=self._short_term_capacity(),
                    filters=effective_filters,
                    metadata=processed_metadata,
                    infer=infer,
                    prompt=prompt,
                    profile_user_id=self._profile_job_user_id_after_add(normalized_user_id),
                )
                background = result["background"]
                migration_job_id = background["migration_job_id"]
                profile_job_id = background["profile_job_id"]
                longterm_job_ids = [
                    job["job_id"]
                    for job in self.db.list_longterm_extraction_jobs(session_scope=session_scope)
                    if job.get("source_operation_key") == idempotency_key
                ]
            else:
                migration_job_id, profile_job_id, longterm_job_ids = self.db.save_messages_and_create_background_jobs(
                    messages,
                    session_scope,
                    max_messages=self._short_term_capacity(),
                    filters=effective_filters,
                    metadata=processed_metadata,
                    infer=infer,
                    prompt=prompt,
                    profile_user_id=self._profile_job_user_id_after_add(normalized_user_id),
                    create_longterm_jobs=True,
                    return_longterm_job_ids=True,
                )
            if migration_job_id:
                worker.wake_migration()
            if profile_job_id:
                worker.wake_profile()
            if longterm_job_ids:
                worker.wake_longterm()
            return migration_job_id, profile_job_id

    def flush_background_tasks(self, timeout: Optional[float] = None) -> bool:
        return self._ensure_background_workers().flush(timeout)

    def get_background_job(self, job_id: str, job_type: str = "migration"):
        return self.db.get_background_job(job_id, job_type)

    def _stop_background_workers(self, timeout: Optional[float]) -> bool:
        worker = getattr(self, "_background_worker", None)
        if worker is None:
            return True
        return worker.stop(wait=True, timeout=timeout)

    def _pause_background_workers_for_reset(self) -> None:
        if not hasattr(self, "_background_lifecycle_lock"):
            self._background_lifecycle_lock = threading.RLock()
        with self._background_lifecycle_lock:
            self._closed = True
            worker = getattr(self, "_background_worker", None)
        if worker is not None and not worker.stop(wait=True, timeout=None):
            raise RuntimeError("Cannot reset while background workers are still running")
        with self._background_lifecycle_lock:
            if getattr(self, "_background_worker", None) is worker:
                self._background_worker = None

    def _close_background_workers_and_db(self) -> bool:
        if not hasattr(self, "_background_lifecycle_lock"):
            self._background_lifecycle_lock = threading.RLock()
        with self._background_lifecycle_lock:
            self._closed = True
            timeout = self._background_config().shutdown_timeout_seconds
            worker = getattr(self, "_background_worker", None)
            db = getattr(self, "db", None)
        if db is None:
            self._shutdown_entity_extraction_executor()
            self._clear_profile_runtime_state()
            self._release_process_instance_lock()
            return True
        logger.info("worker shutdown started timeout_seconds=%s", timeout)
        if worker is not None and not worker.stop(wait=True, timeout=timeout):
            logger.warning(
                "worker shutdown timed out timeout_seconds=%.1f; database and process lock remain open",
                timeout,
            )
            return False
        logger.info("all workers stopped")
        self._shutdown_entity_extraction_executor()
        logger.info("entity extraction executor stopped")
        db.close()
        with self._background_lifecycle_lock:
            self.db = None
            if getattr(self, "_background_worker", None) is worker:
                self._background_worker = None
        self._clear_profile_runtime_state()
        logger.info(
            "database closed history_db_path=%s",
            getattr(getattr(self, "config", None), "history_db_path", getattr(db, "db_path", None)),
        )
        self._close_external_resources()
        self._release_process_instance_lock()
        return True

    def _close_external_resources(self) -> None:
        resources = [
            getattr(self, "_telemetry_vector_store", None),
            getattr(self, "_midterm_memory", None),
            getattr(self, "_cross_session_longterm", None),
            getattr(self, "_entity_store", None),
            getattr(self, "reranker", None),
            getattr(self, "llm", None),
            getattr(self, "embedding_model", None),
            getattr(self, "vector_store", None),
        ]
        midterm = getattr(self, "_midterm_memory", None)
        if midterm is not None:
            resources.extend(
                [
                    getattr(midterm, "pages_store", None),
                    getattr(midterm, "sessions_store", None),
                ]
            )
        cross_session = getattr(self, "_cross_session_longterm", None)
        if cross_session is not None:
            resources.append(getattr(cross_session, "store", None))
        closed_objects = set()
        for resource in resources:
            if resource is None or id(resource) in closed_objects:
                continue
            closed_objects.add(id(resource))
            close = getattr(resource, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    logger.warning("Failed to close resource type=%s", type(resource).__name__, exc_info=True)
                continue
            client = getattr(resource, "client", None)
            if client is None or id(client) in closed_objects:
                continue
            closed_objects.add(id(client))
            client_close = getattr(client, "close", None)
            if callable(client_close):
                try:
                    client_close()
                except Exception:
                    logger.warning(
                        "Failed to close provider client type=%s",
                        type(client).__name__,
                        exc_info=True,
                    )

    def _get_context_messages(self, session_scope: str, active_limit: int):
        reader = getattr(self.db, "get_context_messages", None)
        if reader is None:
            return self.db.get_last_messages(session_scope, limit=active_limit)
        background_config = self._background_config()
        return reader(
            session_scope,
            active_limit=active_limit,
            include_pending=background_config.include_pending_in_context,
            max_pending=background_config.max_pending_context_messages,
            include_failed=background_config.include_failed_in_context,
        )


class Memory(_BackgroundMemoryMixin, MemoryBase):
    def __init__(self, config: MemoryConfig = MemoryConfig()):
        self.config = config
        self._acquire_process_instance_lock()
        self._bm25_language = _configured_bm25_language(config)

        self.embedding_model = EmbedderFactory.create(
            self.config.embedder.provider,
            self.config.embedder.config,
            self.config.vector_store.config,
            timeout_seconds=self.config.embedding_timeout_seconds,
        )
        self.vector_store = VectorStoreFactory.create(
            self.config.vector_store.provider,
            self.config.vector_store.config,
            timeout_seconds=self.config.vector_store_timeout_seconds,
        )
        self.llm = LlmFactory.create(
            self.config.llm.provider,
            self.config.llm.config,
            timeout_seconds=self.config.llm_timeout_seconds,
        )
        self.db = SQLiteManager(self.config.history_db_path)
        self.collection_name = self.config.vector_store.config.collection_name
        self.api_version = self.config.version
        self.custom_instructions = self.config.custom_instructions

        # Initialize reranker if configured
        self.reranker = None
        if config.reranker:
            self.reranker = RerankerConcurrencyGuard(
                RerankerFactory.create(
                    config.reranker.provider,
                    config.reranker.config,
                    timeout_seconds=self.config.reranker_timeout_seconds,
                ),
                max_concurrency=config.reranker.max_concurrency,
            )

        # Entity store is initialized lazily on first use
        self._entity_store = None
        self._midterm_memory = None
        self._midterm_updater = None
        self._midterm_retriever = None
        self._fine_grained_longterm_retriever = None
        self._cross_session_longterm = None
        self._profile_manager = None
        self._profile_updater = None
        self._component_init_lock = threading.RLock()
        self._profile_user_locks = {}
        self._profile_user_locks_guard = threading.Lock()

        if MEM0_TELEMETRY:
            telemetry_config = _build_telemetry_vector_store_config(self.config)
            self._telemetry_vector_store = VectorStoreFactory.create(
                self.config.vector_store.provider,
                telemetry_config,
                timeout_seconds=self.config.vector_store_timeout_seconds,
            )
        if getattr(type(self.vector_store), "keyword_search", None) is VectorStoreBase.keyword_search:
            logger.warning(
                "The '%s' vector store does not support keyword search. "
                "Hybrid (BM25) scoring will be disabled and search will use "
                "semantic similarity only. To enable hybrid search, switch to a "
                "store with keyword_search support (e.g. qdrant, elasticsearch, pgvector).",
                self.config.vector_store.provider,
            )

        self._initialize_background_workers()
        capture_event("mem0.init", self, {"sync_type": "sync"})

    @property
    def project(self):
        return _OSSProject()

    @property
    def entity_store(self):
        """Lazily initialize entity store on first use."""
        if self._entity_store is None:
            with self._component_init_lock:
                if self._entity_store is None:
                    entity_config = _safe_deepcopy_config(self.config.vector_store.config)
                    entity_collection = _entity_collection_name(
                        self.config.vector_store.provider,
                        self.collection_name,
                    )
                    # Set collection name on the cloned config
                    if hasattr(entity_config, "collection_name"):
                        entity_config.collection_name = entity_collection
                    elif isinstance(entity_config, dict):
                        entity_config["collection_name"] = entity_collection
                    # For Qdrant, share the existing client to avoid RocksDB lock contention
                    # when using embedded mode (path=...). QdrantConfig.client takes precedence
                    # over host/port/path.
                    if self.config.vector_store.provider == "qdrant" and hasattr(self.vector_store, "client"):
                        if hasattr(entity_config, "client"):
                            entity_config.client = self.vector_store.client
                        elif isinstance(entity_config, dict):
                            entity_config["client"] = self.vector_store.client
                    self._entity_store = VectorStoreFactory.create(
                        self.config.vector_store.provider,
                        entity_config,
                        timeout_seconds=getattr(self.config, "vector_store_timeout_seconds", 15.0),
                    )
        return self._entity_store

    def _midterm_enabled(self):
        return bool(getattr(getattr(self, "config", None), "midterm", None) and self.config.midterm.enabled)

    @property
    def midterm_memory(self):
        if self._midterm_memory is None:
            with self._component_init_lock:
                if self._midterm_memory is None:
                    self._midterm_memory = MidTermMemory(
                        provider=self.config.vector_store.provider,
                        base_vector_config=self.config.vector_store.config,
                        base_collection_name=self.collection_name,
                        embedding_model=self.embedding_model,
                        config=self.config.midterm,
                        current_turn_index_provider=lambda filters: self.db.current_turn_index(
                            _build_session_scope(filters)
                        ),
                        primary_vector_store=self.vector_store,
                        output_is_visible=lambda payload: self._stage_output_is_visible(payload, "midterm"),
                        vector_store_timeout_seconds=getattr(
                            self.config,
                            "vector_store_timeout_seconds",
                            15.0,
                        ),
                    )
        return self._midterm_memory

    @property
    def midterm_updater(self):
        if self._midterm_updater is None:
            with self._component_init_lock:
                if self._midterm_updater is None:
                    self._midterm_updater = MidTermUpdater(
                        self.midterm_memory,
                        self.llm,
                        self.config.midterm,
                        following_qa_provider=lambda filters, after_turn_index, qa_limit: (
                            self.db.get_following_qa_messages(
                                _build_session_scope(filters),
                                after_turn_index,
                                qa_limit,
                            )
                        ),
                    )
        return self._midterm_updater

    @property
    def midterm_retriever(self):
        if self._midterm_retriever is None:
            with self._component_init_lock:
                if self._midterm_retriever is None:
                    self._midterm_retriever = MidTermRetriever(
                        self.midterm_memory,
                        self.config.midterm,
                        reranker=getattr(self, "reranker", None),
                    )
        return self._midterm_retriever

    @property
    def profile_manager(self):
        """Lazily initialize profile storage and assembly without touching the LLM."""
        if self._profile_manager is None:
            with self._component_init_lock:
                if self._profile_manager is None:
                    self._profile_manager = ProfileManager(self.db, self.config.profile)
        return self._profile_manager

    @property
    def profile_updater(self):
        """Lazily initialize the LLM-backed profile plan generator."""
        if self._profile_updater is None:
            with self._component_init_lock:
                if self._profile_updater is None:
                    self._profile_updater = ProfileUpdater(self.llm, self.config.profile)
        return self._profile_updater

    def get_profile(self, user_id: str, include_metadata: Optional[bool] = None):
        """Return a user's current profile shared across all runs."""
        normalized_user_id = normalize_profile_user_id(user_id)
        return self.profile_manager.get_profile(normalized_user_id, include_metadata=include_metadata)

    def _retrieve_context(
        self,
        query: str,
        *,
        user_id: str,
        session_id: str,
        top_k: Optional[int] = None,
        threshold: Optional[float] = None,
        rerank: bool = False,
        explain: bool = False,
        include_profile_metadata: bool = False,
    ) -> Dict[str, Any]:
        """Retrieve session memories, short-term messages, and the user's cross-session profile."""
        context = self._retrieve_base_context(
            query,
            user_id=user_id,
            session_id=session_id,
            include_profile_metadata=include_profile_metadata,
        )
        filters = {
            "user_id": context["user_id"],
            "run_id": context["session_id"],
        }
        resolver_llm = getattr(self, "llm", None)
        retrieval_query = (
            QueryResolver(resolver_llm, prompt=_configured_query_rewrite_prompt(self)).resolve(
                context["query"],
                context.get("short_term_messages"),
            )
            if resolver_llm is not None
            else context["query"]
        )
        context["retrieval_query"] = retrieval_query
        top_k = 20 if top_k is None else top_k
        search_result = self.search(
            retrieval_query,
            top_k=top_k,
            threshold=threshold,
            rerank=rerank,
            explain=explain,
            filters=filters,
        )
        context["retrieved_memories"] = (
            search_result["results"]
            if isinstance(search_result, dict) and "results" in search_result
            else search_result
        )
        _filter_shortterm_duplicate_longterm(context)
        _strip_shortterm_internal_fields(context)
        has_midterm_page = any(
            isinstance(item, dict) and item.get("source") in {"mid_term_page", "midterm"} and item.get("raw_dialogue")
            for item in (context["retrieved_memories"] or [])
        )
        self._confirm_context_valid_recalls(
            context["retrieved_memories"],
            current_turn_index=(
                self.db.current_turn_index(_build_session_scope(filters)) if has_midterm_page else None
            ),
        )
        return context

    def _retrieve_base_context(
        self,
        query: str,
        *,
        user_id: str,
        session_id: str,
        include_profile_metadata: bool = False,
    ) -> Dict[str, Any]:
        """Retrieve only recent session messages and the cross-session user profile."""
        normalized_query, normalized_user_id, normalized_session_id = _normalize_context_request(
            query,
            user_id,
            session_id,
        )
        filters = {
            "user_id": normalized_user_id,
            "run_id": normalized_session_id,
        }
        session_scope = _build_session_scope(filters)
        profile_result = self.get_profile(
            normalized_user_id,
            include_metadata=include_profile_metadata,
        )
        short_term_limit = self._short_term_capacity()
        if getattr(self.db, "get_context_messages", None) is None:
            short_term_messages = self.db.get_last_messages(session_scope, limit=short_term_limit)
        else:
            short_term_messages = self._get_context_messages(session_scope, short_term_limit)

        return _assemble_retrieved_context(
            query=normalized_query,
            user_id=normalized_user_id,
            session_id=normalized_session_id,
            search_result=[],
            profile_result=profile_result,
            short_term_messages=short_term_messages,
        )

    def build_agent_answer_messages(
        self,
        query: str,
        *,
        user_id: str,
        session_id: str,
        top_k: int = 20,
        threshold: Optional[float] = None,
        rerank: bool = False,
        explain: bool = False,
        include_profile_metadata: bool = False,
        reference_information: Any = None,
        agentic_generation_kwargs: Optional[Dict[str, Any]] = None,
    ) -> list[Dict[str, str]]:
        """Build messages for an external LLM from the query and layered memory context."""
        retrieved_context = self._retrieve_context(
            query,
            user_id=user_id,
            session_id=session_id,
            top_k=top_k,
            threshold=threshold,
            rerank=rerank,
            explain=explain,
            include_profile_metadata=include_profile_metadata,
        )

        agentic_memory_supplement = ""
        agentic_config = getattr(getattr(self, "config", None), "agentic_retrieval", None)
        if agentic_config is not None and agentic_config.enabled:
            try:
                result = self._run_agentic_retrieval_from_context(
                    retrieved_context,
                    reference_information=reference_information,
                    generation_kwargs=agentic_generation_kwargs,
                )
                agentic_memory_supplement = self._normalize_agentic_supplement_result(result)
            except Exception:
                logger.warning(
                    "Agentic retrieval failed while building answer messages; using the retrieved context only",
                    exc_info=True,
                )

        return build_answer_prompt_messages_from_context(
            retrieved_context,
            reference_information,
            agentic_memory_supplement=agentic_memory_supplement,
        )

    def _run_agentic_retrieval_from_context(
        self,
        retrieved_context: Dict[str, Any],
        *,
        reference_information: Any = None,
        generation_kwargs: Optional[Dict[str, Any]] = None,
        record_midterm_visits: bool = True,
    ) -> Dict[str, Any]:
        """Run Agentic retrieval using an already retrieved complete context."""
        messages = _build_agentic_prompt_messages(retrieved_context, reference_information)
        already_recalled_page_ids = {
            str(item.get("id"))
            for item in retrieved_context.get("retrieved_memories", [])
            if isinstance(item, dict)
            and item.get("source") in {"mid_term_page", "midterm"}
            and item.get("id") not in (None, "")
            and item.get("raw_dialogue")
        }
        return self._run_agentic_retrieval_messages(
            messages,
            user_id=retrieved_context["user_id"],
            session_id=retrieved_context["session_id"],
            generation_kwargs=generation_kwargs,
            record_midterm_visits=record_midterm_visits,
            exclude_midterm_page_ids=already_recalled_page_ids,
        )

    def run_agentic_retrieval(
        self,
        query: str,
        *,
        user_id: str,
        session_id: str,
        include_profile_metadata: bool = False,
        reference_information: Any = None,
        generation_kwargs: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Run the optional two-call mid-term retrieval flow without writing messages."""
        if not self.config.agentic_retrieval.enabled:
            raise ValueError("Agentic retrieval is disabled; set agentic_retrieval.enabled=True")
        context = self._retrieve_context(
            query,
            user_id=user_id,
            session_id=session_id,
            include_profile_metadata=include_profile_metadata,
        )
        return self._run_agentic_retrieval_from_context(
            context,
            reference_information=reference_information,
            generation_kwargs=generation_kwargs,
        )

    def _run_agentic_retrieval_messages(
        self,
        messages: list[Dict[str, Any]],
        *,
        user_id: str,
        session_id: str,
        generation_kwargs: Optional[Dict[str, Any]] = None,
        record_midterm_visits: bool = True,
        exclude_midterm_page_ids: Optional[set[str]] = None,
    ) -> Dict[str, Any]:
        executor = self._create_agentic_tool_executor(
            user_id=user_id,
            session_id=session_id,
            record_midterm_visits=record_midterm_visits,
            exclude_midterm_page_ids=exclude_midterm_page_ids,
        )
        runner = AgenticMemoryRunner(
            self.llm,
            executor,
            self.config.agentic_retrieval,
            generation_kwargs=generation_kwargs,
        )
        return runner.run(messages)

    def _create_agentic_tool_executor(
        self,
        *,
        user_id: str,
        session_id: str,
        record_midterm_visits: bool,
        exclude_midterm_page_ids: Optional[set[str]] = None,
    ) -> MemoryToolExecutor:
        """Create the core Agentic tool executor, allowing scoped runtime decoration."""
        return MemoryToolExecutor(
            self,
            user_id=user_id,
            run_id=session_id,
            config=self.config.agentic_retrieval,
            record_midterm_visits=record_midterm_visits,
            exclude_midterm_page_ids=exclude_midterm_page_ids,
        )

    def update_profile(self, user_id: str, messages):
        """Explicitly extract and apply profile updates from user messages."""
        normalized_user_id = normalize_profile_user_id(user_id)
        profile_config = getattr(self.config, "profile", None)
        if profile_config is None or profile_config.enabled is not True:
            raise ValueError(
                "User profile updates are disabled; set profile.enabled=True before calling update_profile"
            )
        lock = self._get_profile_user_thread_lock(normalized_user_id)
        with lock:
            plan = self._generate_profile_update_plan(normalized_user_id, messages)
            if plan is None:
                return self.profile_manager.get_profile(normalized_user_id)
            return self.profile_manager.apply_update_plan(normalized_user_id, plan)

    def delete_profile_value(self, user_id: str, attribute_key: str):
        """Delete one current profile value for a user."""
        normalized_user_id = normalize_profile_user_id(user_id)
        lock = self._get_profile_user_thread_lock(normalized_user_id)
        with lock:
            return self.profile_manager.delete_value(normalized_user_id, attribute_key)

    def delete_profile(self, user_id: str):
        """Delete all current profile values for a user."""
        normalized_user_id = normalize_profile_user_id(user_id)
        lock = self._get_profile_user_thread_lock(normalized_user_id)
        with lock:
            return self.profile_manager.delete_profile(normalized_user_id)

    def create_profile_attribute(self, definition):
        """Create a manually managed profile attribute definition."""
        return self.profile_manager.create_attribute(definition)

    def _generate_profile_update_plan(self, user_id, messages):
        normalized_user_id = normalize_profile_user_id(user_id)
        user_messages = select_profile_user_messages(messages, self.config.profile.max_input_user_messages)
        if not user_messages:
            return None
        current_profile = self.profile_manager.get_profile(normalized_user_id)
        attribute_catalog = self.profile_manager.list_attributes()
        return self.profile_updater.generate_update_plan(
            current_profile=current_profile,
            attribute_catalog=attribute_catalog,
            messages=user_messages,
        )

    def _update_profile_after_add(self, user_id, messages):
        profile_config = getattr(self.config, "profile", None)
        if (
            profile_config is None
            or profile_config.enabled is not True
            or profile_config.update_on_add is not True
            or not user_id
        ):
            return

        try:
            normalized_user_id = normalize_profile_user_id(user_id)
            lock = self._get_profile_user_thread_lock(normalized_user_id)
            with lock:
                plan = self._generate_profile_update_plan(normalized_user_id, messages)
                if plan is None:
                    return
                self.profile_manager.apply_update_plan(normalized_user_id, plan)
        except Exception as exc:
            logger.warning("Automatic profile update failed for user %s: %s", user_id, exc)

    def _short_term_capacity(self):
        capacity = max(int(self.config.midterm.short_term_capacity), 0)
        if capacity % 2 != 0:
            capacity += 1
            self.config.midterm.short_term_capacity = capacity
            logger.warning(
                "midterm short_term_capacity should be even; using %s to preserve QA pairs.",
                capacity,
            )
        return capacity

    def _expand_evicted_messages_for_qa_pairs(self, evicted_messages, session_scope):
        if not evicted_messages or evicted_messages[-1].get("role") != "user":
            return evicted_messages or []

        retained_messages = self.db.get_messages(session_scope, limit=1)
        if not retained_messages or retained_messages[0].get("role") != "assistant":
            return evicted_messages

        assistant_message = retained_messages[0]
        self.db.delete_messages([assistant_message["id"]])
        return [*evicted_messages, assistant_message]

    def _save_short_term_messages(self, messages, session_scope):
        evicted_messages = (
            self.db.save_messages(
                messages,
                session_scope,
                max_messages=self._short_term_capacity(),
                return_evicted=True,
            )
            or []
        )
        return self._expand_evicted_messages_for_qa_pairs(evicted_messages, session_scope)

    def _save_short_term_messages_with_completed_qa(self, messages, session_scope):
        evicted_messages, completed_qa = self.db.save_messages_and_get_completed_qa(
            messages,
            session_scope,
            max_messages=self._short_term_capacity(),
        )
        expanded = self._expand_evicted_messages_for_qa_pairs(evicted_messages, session_scope)
        return expanded, completed_qa

    def _process_midterm_evictions(
        self,
        evicted_messages,
        filters,
        *,
        source_job_id=None,
        lease_token=None,
        lease_is_current=None,
        degraded=False,
        raise_on_error=False,
    ):
        if not self._midterm_enabled() or not evicted_messages:
            return []

        try:
            return self.midterm_updater.process_evicted_messages(
                evicted_messages,
                filters,
                source_job_id=source_job_id,
                lease_token=lease_token,
                lease_is_current=lease_is_current,
                degraded=degraded,
            )
        except Exception as e:
            if raise_on_error:
                raise
            logger.warning(f"Mid-term memory update failed: {e}")
            return []

    def _with_midterm_search_results(self, query, filters, long_term_memories):
        if not self._midterm_enabled():
            return long_term_memories

        results = [{**memory, "source": "long_term"} for memory in long_term_memories]
        try:
            results.extend(self.midterm_retriever.search(query, filters))
        except Exception as e:
            logger.warning(f"Mid-term memory search failed: {e}")
        user_id = (filters or {}).get("user_id")
        if user_id and self._promoted_longterm_enabled():
            try:
                results.extend(
                    self.promoted_longterm.search(
                        query,
                        user_id=user_id,
                        top_k=int(self.config.promoted_longterm.top_k),
                        threshold=float(self.config.promoted_longterm.rag_threshold),
                    )
                )
            except Exception as e:
                logger.warning(f"Cross-session long-term memory search failed: {e}")
        return results

    def _reset_midterm_state(self):
        if self._midterm_memory is not None or self._midterm_enabled():
            try:
                self.midterm_memory.reset()
            except Exception as e:
                logger.warning(f"Failed to reset mid-term memory: {e}")
        self._midterm_memory = None
        self._midterm_updater = None
        self._midterm_retriever = None
        self._fine_grained_longterm_retriever = None
        cross_session_longterm = getattr(self, "_cross_session_longterm", None)
        if cross_session_longterm is not None:
            try:
                cross_session_longterm.reset()
            except Exception as e:
                logger.warning(f"Failed to reset cross-session long-term memory: {e}")
        self._cross_session_longterm = None

    @staticmethod
    def _normalize_entity_text(value: str) -> str:
        return " ".join(value.strip().lower().split())

    def _existing_entities_by_text(self, filters):
        """Return existing entity rows keyed by normalized payload data."""
        try:
            listed = self.entity_store.list(filters=filters, top_k=10000)
        except Exception as e:
            logger.debug(f"Exact entity lookup failed, falling back to semantic dedup: {e}")
            return {}

        rows_by_text = {}
        for row in _vector_store_list_rows(listed):
            payload = getattr(row, "payload", None) or {}
            text = payload.get("data")
            if not isinstance(text, str):
                continue
            normalized = self._normalize_entity_text(text)
            if normalized and normalized not in rows_by_text:
                rows_by_text[normalized] = row
        return rows_by_text

    def _upsert_entity(self, entity_text, entity_type, memory_id, filters):
        """Upsert an entity into the entity store, linking it to a memory."""
        try:
            entity_embedding = self.embedding_model.embed(entity_text, "add")
            search_filters = _longterm_entity_filters(filters)
            exact_match = self._existing_entities_by_text(search_filters).get(self._normalize_entity_text(entity_text))

            existing = []
            if exact_match is None:
                existing = self.entity_store.search(
                    query=entity_text,
                    vectors=entity_embedding,
                    top_k=1,
                    filters=search_filters,
                )

            semantic_match = existing[0] if existing and existing[0].score >= 0.95 else None
            match = exact_match or semantic_match
            if match:
                # Update existing entity's linked_memory_ids
                payload = match.payload or {}
                linked_ids = payload.get("linked_memory_ids", [])
                if memory_id not in linked_ids:
                    payload = _update_entity_payload(payload, [*linked_ids, memory_id])
                    self.entity_store.update(
                        vector_id=match.id,
                        vector=None,
                        payload=payload,
                    )
            else:
                # Create new entity
                entity_id = str(uuid.uuid4())
                entity_payload = _new_entity_payload(
                    entity_text,
                    entity_type,
                    [memory_id],
                    search_filters,
                )
                self.entity_store.insert(
                    vectors=[entity_embedding],
                    ids=[entity_id],
                    payloads=[entity_payload],
                )
        except Exception as e:
            logger.warning(f"Entity upsert failed for '{entity_text}': {e}")

    def _remove_memory_from_entity_store(self, memory_id, filters):
        """Strip `memory_id` from every entity record scoped to `filters`.

        For each entity whose `linked_memory_ids` contains `memory_id`:
          - remove the id; if the list becomes empty, delete the entity record.
          - otherwise re-embed the entity text and update the payload
            (the vector store's update() requires a vector).

        No-op if the entity store has never been initialized in this process.
        Errors on individual entities are swallowed at debug level; outer
        failures are swallowed at warning level so the primary delete/update
        path is never broken by entity cleanup.
        """
        if self._entity_store is None:
            return
        search_filters = _longterm_entity_filters(filters)
        try:
            listed = self.entity_store.list(filters=search_filters, top_k=10000)
            rows = listed[0] if isinstance(listed, (list, tuple)) and listed and isinstance(listed[0], list) else listed
            for row in rows or []:
                try:
                    payload = getattr(row, "payload", None) or {}
                    linked = payload.get("linked_memory_ids", [])
                    if not isinstance(linked, list) or memory_id not in linked:
                        continue
                    remaining = [mid for mid in linked if mid != memory_id]
                    if not remaining:
                        try:
                            self.entity_store.delete(vector_id=row.id)
                        except Exception as e:
                            logger.debug(f"Entity delete failed for id={row.id}: {e}")
                    else:
                        entity_text = payload.get("data")
                        if not isinstance(entity_text, str) or not entity_text:
                            logger.debug(f"Entity id={row.id} missing 'data'; skipping update during cleanup")
                            continue
                        try:
                            vec = self.embedding_model.embed(entity_text, "update")
                        except Exception as e:
                            logger.debug(f"Entity re-embed failed for '{entity_text}': {e}")
                            continue
                        new_payload = _update_entity_payload(payload, remaining)
                        try:
                            self.entity_store.update(
                                vector_id=row.id,
                                vector=vec,
                                payload=new_payload,
                            )
                        except Exception as e:
                            logger.debug(f"Entity update failed for id={row.id}: {e}")
                except Exception as e:
                    logger.debug(f"Entity cleanup error: {e}")
        except Exception as e:
            logger.warning(f"Entity store cleanup failed for memory_id={memory_id}: {e}")

    def _link_entities_for_memory(self, memory_id, text, filters):
        """Extract entities from `text` and link them to `memory_id` in the
        entity store, scoped to `filters`. Simpler single-memory variant of
        Phase 7 in add(): per-entity search-then-update-or-insert via the
        existing `_upsert_entity` helper. Non-fatal on any failure.
        """
        try:
            entities = self._run_entity_extraction(extract_entities, text)
            if not entities:
                return
            seen = set()
            for entity_type, entity_text in entities:
                key = self._normalize_entity_text(entity_text)
                if not key or key in seen:
                    continue
                seen.add(key)
                try:
                    self._upsert_entity(entity_text, entity_type, memory_id, filters)
                except Exception as e:
                    logger.debug(f"Entity link failed for '{entity_text}': {e}")
        except Exception as e:
            logger.warning(f"Entity linking failed for memory_id={memory_id}: {e}")

    @classmethod
    def from_config(cls, config_dict: Dict[str, Any]):
        try:
            config = MemoryConfig(**config_dict)
        except ValidationError as e:
            logger.error(f"Configuration validation error: {e}")
            raise
        return cls(config)

    def _should_use_agent_memory_extraction(self, messages, metadata):
        """Determine whether to use agent memory extraction based on the logic:
        - If agent_id is present and messages contain assistant role -> True
        - Otherwise -> False

        Args:
            messages: List of message dictionaries
            metadata: Metadata containing user_id, agent_id, etc.

        Returns:
            bool: True if should use agent memory extraction, False for user memory extraction
        """
        # Check if agent_id is present in metadata
        has_agent_id = metadata.get("agent_id") is not None

        # Check if there are assistant role messages
        has_assistant_messages = any(msg.get("role") == "assistant" for msg in messages)

        # Use agent memory extraction if agent_id is present and there are assistant messages
        return has_agent_id and has_assistant_messages

    def add(
        self,
        messages,
        *,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        run_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        timestamp: Optional[Any] = None,
        expiration_date: Optional[Any] = None,
        infer: bool = True,
        memory_type: Optional[str] = None,
        prompt: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ):
        """
        Create a new memory.

        Adds new memories scoped to a single session id (e.g. `user_id`, `agent_id`, or `run_id`). One of those ids is required.

        Args:
            messages (str or List[Dict[str, str]]): The message content or list of messages
                (e.g., `[{"role": "user", "content": "Hello"}, {"role": "assistant", "content": "Hi"}]`)
                to be processed and stored.
            user_id (str, optional): ID of the user creating the memory. Defaults to None.
            agent_id (str, optional): ID of the agent creating the memory. Defaults to None.
            run_id (str, optional): ID of the run creating the memory. Defaults to None.
            metadata (dict, optional): Metadata to store with the memory. Defaults to None.
            timestamp (Any, optional): Platform-only temporal parameter. Not supported in OSS.
            expiration_date (Any, optional): Date in YYYY-MM-DD format. Expired memories are hidden
                from search and get_all unless show_expired is True.
            infer (bool, optional): Controls Fine-grained LongTerm extraction for each complete QA.
                If True (default), an LLM extracts facts from each complete QA. If False, the
                complete QA's non-system messages are stored directly. This behavior is independent
                of ShortTerm eviction.
            memory_type (str, optional): Specifies the type of memory. Currently, only
                `MemoryType.PROCEDURAL.value` ("procedural_memory") is explicitly handled for
                creating procedural memories (typically requires 'agent_id'). Otherwise, memories
                are treated as general conversational/factual memories.
            prompt (str, optional): Prompt to use for the memory creation. Defaults to None.
            idempotency_key (str, optional): Persisted key that makes a background add
                safe to retry with the same business inputs. Defaults to None.


        Returns:
            dict: A submission result. When background processing is enabled, ``results`` is empty
                  and ``background`` contains the durable migration/profile job IDs (or ``None``
                  when no job was needed). A job ID means enqueued, not completed.

        Raises:
            Mem0ValidationError: If input validation fails (invalid memory_type, messages format, etc.).
            VectorStoreError: If vector store operations fail.
            EmbeddingError: If embedding generation fails.
            LLMError: If LLM operations fail.
            DatabaseError: If database operations fail.
        """
        if timestamp is not None:
            raise ValueError(get_temporal_feature_error_message("sync", "add", "timestamp"))
        idempotency_key = _validate_and_trim_idempotency_key(idempotency_key)

        normalized_expiration_date = _normalize_expiration_date(expiration_date)
        temporal_usage_notice = detect_temporal_usage_from_metadata(metadata)
        processed_metadata, effective_filters = _build_filters_and_metadata(
            user_id=user_id,
            agent_id=agent_id,
            run_id=run_id,
            input_metadata=metadata,
        )
        normalized_user_id = effective_filters.get("user_id")
        if normalized_expiration_date is not None:
            processed_metadata["expiration_date"] = normalized_expiration_date

        _validate_memory_type(memory_type)

        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]

        elif isinstance(messages, dict):
            messages = [messages]

        elif not isinstance(messages, list):
            raise Mem0ValidationError(
                message="messages must be str, dict, or list[dict]",
                error_code="VALIDATION_003",
                details={"provided_type": type(messages).__name__, "valid_types": ["str", "dict", "list[dict]"]},
                suggestion="Convert your input to a string, dictionary, or list of dictionaries.",
            )

        if agent_id is not None and memory_type == MemoryType.PROCEDURAL.value:
            if idempotency_key is not None:
                raise ValueError("idempotency_key is not supported for procedural memory adds")
            results = self._create_procedural_memory(messages, metadata=processed_metadata, prompt=prompt)
            if self._background_config().enabled:
                profile_job_id = self._enqueue_profile_job_after_add(normalized_user_id, messages)
            else:
                self._update_profile_after_add(normalized_user_id, messages)
                profile_job_id = None
            results = {
                **results,
                "background": {
                    "migration_job_id": None,
                    "profile_job_id": profile_job_id,
                },
            }
            scale_threshold_notice = detect_scale_threshold_from_add_result(self, results)
            if temporal_usage_notice:
                display_temporal_usage_notice(self, "sync", "add", *temporal_usage_notice)
            elif scale_threshold_notice:
                display_scale_threshold_notice(self, "sync", "add", *scale_threshold_notice)
            else:
                display_first_run_notice(self, "sync", "add")
            return results

        if self.config.llm.config.get("enable_vision"):
            messages = parse_vision_messages(messages, self.llm, self.config.llm.config.get("vision_details"))
        else:
            messages = parse_vision_messages(messages)

        # Persist ShortTerm before either direct extraction or durable background job submission.
        session_scope = _build_session_scope(effective_filters)
        if not self._background_config().enabled:
            if idempotency_key is not None:
                raise ValueError("idempotency_key requires background task persistence")
            evicted_messages, completed_qa = self._save_short_term_messages_with_completed_qa(
                messages,
                session_scope,
            )
            vector_store_result = []
            if evicted_messages:
                self._process_midterm_evictions(evicted_messages, effective_filters)
            for qa_messages, qa_metadata in _completed_qa_longterm_inputs(completed_qa, processed_metadata):
                vector_store_result.extend(
                    self._process_evicted_long_term_memories(
                        qa_messages,
                        qa_metadata,
                        effective_filters,
                        infer=infer,
                        prompt=prompt,
                    )
                )
            self._update_profile_after_add(normalized_user_id, messages)
            scale_threshold_notice = detect_scale_threshold_from_add_result(self, vector_store_result)
            if temporal_usage_notice:
                display_temporal_usage_notice(self, "sync", "add", *temporal_usage_notice)
            elif scale_threshold_notice:
                display_scale_threshold_notice(self, "sync", "add", *scale_threshold_notice)
            else:
                display_first_run_notice(self, "sync", "add")
            return {
                "results": vector_store_result,
                "background": {
                    "migration_job_id": None,
                    "profile_job_id": None,
                },
            }

        request_hash = (
            _memory_add_request_hash(
                messages=messages,
                filters=effective_filters,
                metadata=processed_metadata,
                infer=infer,
                memory_type=memory_type,
                prompt=prompt,
            )
            if idempotency_key is not None
            else None
        )
        migration_job_id, profile_job_id = self._save_and_enqueue_background_jobs(
            messages,
            session_scope,
            processed_metadata,
            effective_filters,
            normalized_user_id=normalized_user_id,
            infer=infer,
            prompt=prompt,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        vector_store_result = []

        scale_threshold_notice = detect_scale_threshold_from_add_result(self, vector_store_result)
        if temporal_usage_notice:
            display_temporal_usage_notice(self, "sync", "add", *temporal_usage_notice)
        elif scale_threshold_notice:
            display_scale_threshold_notice(self, "sync", "add", *scale_threshold_notice)
        else:
            display_first_run_notice(self, "sync", "add")
        return {
            "results": vector_store_result,
            "background": {
                "migration_job_id": migration_job_id,
                "profile_job_id": profile_job_id,
            },
        }

    def _process_evicted_long_term_memories(
        self,
        evicted_messages,
        metadata,
        filters,
        *,
        infer=True,
        prompt=None,
        source_job_id=None,
        lease_token=None,
        lease_is_current=None,
    ):
        if not infer:
            returned_memories = []
            for index, message_dict in enumerate(evicted_messages):
                if lease_is_current is not None and not lease_is_current():
                    raise RuntimeError("stale migration stage lease")
                if (
                    not isinstance(message_dict, dict)
                    or message_dict.get("role") is None
                    or message_dict.get("content") is None
                ):
                    logger.warning(f"Skipping invalid message format: {message_dict}")
                    continue

                if message_dict["role"] == "system":
                    continue

                per_msg_meta = deepcopy(metadata)
                per_msg_meta["role"] = message_dict["role"]
                if source_job_id:
                    per_msg_meta.update(
                        {
                            "source_job_id": source_job_id,
                            "source_stage": "longterm",
                            "output_state": "staging",
                            "output_lease_token": lease_token,
                        }
                    )

                actor_name = message_dict.get("name")
                if actor_name:
                    per_msg_meta["actor_id"] = actor_name

                msg_content = message_dict["content"]
                memory_id = (
                    str(uuid.uuid5(uuid.NAMESPACE_URL, f"mem0:longterm:{source_job_id}:{index}"))
                    if source_job_id
                    else None
                )
                vector_store = getattr(self, "vector_store", None)
                existing_memory = (
                    vector_store.get(vector_id=memory_id) if memory_id and vector_store is not None else None
                )
                if existing_memory is not None:
                    existing_payload = dict(getattr(existing_memory, "payload", None) or {})
                    existing_payload.update(
                        {
                            "source_job_id": source_job_id,
                            "output_state": "staging",
                            "output_lease_token": lease_token,
                            "source_stage": "longterm",
                        }
                    )
                    _update_vector_store_payload(self.vector_store, memory_id, existing_payload)
                    returned_memories.append(
                        {
                            "id": memory_id,
                            "memory": msg_content,
                            "event": "ADD",
                            "actor_id": actor_name if actor_name else None,
                            "role": message_dict["role"],
                        }
                    )
                    continue
                msg_embeddings = self.embedding_model.embed(msg_content, "add")
                mem_id = self._create_memory(
                    msg_content,
                    {msg_content: msg_embeddings},
                    per_msg_meta,
                    memory_id=memory_id,
                )

                returned_memories.append(
                    {
                        "id": mem_id,
                        "memory": msg_content,
                        "event": "ADD",
                        "actor_id": actor_name if actor_name else None,
                        "role": message_dict["role"],
                    }
                )
            return returned_memories

        # === V3 PHASED BATCH PIPELINE ===

        # Phase 0: Context gathering
        session_scope = _build_session_scope(filters)
        short_term_context = self.db.get_last_messages(session_scope, limit=self._short_term_capacity())
        parsed_evicted_messages = parse_messages(evicted_messages)
        session_summary, existing_related_memories = _additive_midterm_context(
            self,
            parsed_evicted_messages,
            filters,
            exclude_source_job_id=source_job_id,
        )

        # Phase 1: Existing memory retrieval remains source-run scoped for write deduplication.
        search_filters = {k: v for k, v in filters.items() if k in ("user_id", "agent_id", "run_id") and v}
        query_embedding = self.embedding_model.embed(parsed_evicted_messages, "search")
        raw_existing_results = self.vector_store.search(
            query=parsed_evicted_messages,
            vectors=query_embedding,
            top_k=10,
            filters=search_filters,
        )

        existing_results = [
            mem
            for mem in raw_existing_results
            if self._stage_output_is_visible(getattr(mem, "payload", None) or {}, "longterm")
        ]
        existing_long_term_memories = [
            {"id": str(mem.id), "text": mem.payload.get("data", "")} for mem in existing_results
        ]

        # Phase 2: LLM extraction (single call)
        is_agent_scoped = bool(filters.get("agent_id")) and not filters.get("user_id")
        system_prompt = _configured_fine_grained_extraction_prompt(self)
        if is_agent_scoped:
            system_prompt += AGENT_CONTEXT_SUFFIX

        custom_instr = prompt or self.custom_instructions

        user_prompt = generate_additive_extraction_prompt(
            new_messages=evicted_messages,
            session_summary=session_summary,
            existing_long_term_memories=existing_long_term_memories,
            existing_related_memories=existing_related_memories,
            short_term_context=short_term_context,
            custom_instructions=custom_instr,
        )

        try:
            response = self.llm.generate_response(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                **_configured_fine_grained_extraction_request_options(self),
            )
        except Exception as e:
            # Re-raise so callers can implement provider fallback / retry.
            # The original silent ``return []`` made upstream callers unable to
            # distinguish "LLM unavailable" (429/5xx/timeout) from "LLM
            # extracted no facts" -- both surfaced as an empty list.
            logger.error(f"LLM extraction failed: {e}")
            raise LLMError(f"LLM extraction failed: {e}") from e

        extracted_memories = _parse_extracted_memories(response, strict=source_job_id is not None)

        if not extracted_memories:
            return []
        if lease_is_current is not None and not lease_is_current():
            raise RuntimeError("stale migration stage lease")

        # Phase 3: Determine which validated facts actually require a new write.
        existing_hashes, existing_source_hashes = _existing_longterm_hashes(raw_existing_results, source_job_id)

        pending_records = []  # (memory_id, text, memory_hash, extracted_memory)
        seen_hashes = set()
        for mem in extracted_memories:
            text = mem["text"]
            mem_hash = _longterm_memory_hash(text)
            memory_id = _longterm_memory_id(source_job_id, text) if source_job_id else str(uuid.uuid4())
            existing_memory = self.vector_store.get(vector_id=memory_id) if source_job_id else None
            if existing_memory is not None:
                existing_payload = _validated_existing_longterm_payload(
                    existing_memory,
                    source_job_id=source_job_id,
                    memory_id=memory_id,
                    expected_hash=mem_hash,
                )
                existing_payload.update(
                    {
                        "source_job_id": source_job_id,
                        "source_stage": "longterm",
                        "output_state": "staging",
                        "output_lease_token": lease_token,
                    }
                )
                _update_vector_store_payload(self.vector_store, memory_id, existing_payload)
                seen_hashes.add(mem_hash)
                continue
            existing_job_memory = existing_source_hashes.get(mem_hash)
            if existing_job_memory is not None:
                existing_job_payload = getattr(existing_job_memory, "payload", None) or {}
                existing_job_memory_id = str(existing_job_memory.id)
                existing_job_payload = dict(existing_job_payload)
                existing_job_payload.update(
                    {
                        "source_job_id": source_job_id,
                        "source_stage": "longterm",
                        "output_state": "staging",
                        "output_lease_token": lease_token,
                    }
                )
                _update_vector_store_payload(
                    self.vector_store,
                    existing_job_memory_id,
                    existing_job_payload,
                )
                seen_hashes.add(mem_hash)
                continue
            if mem_hash in existing_hashes or mem_hash in seen_hashes:
                logger.debug(f"Skipping duplicate memory (hash match): {text[:50]}")
                continue
            seen_hashes.add(mem_hash)
            pending_records.append((memory_id, text, mem_hash, mem))

        if not pending_records:
            return []

        # Phase 4: Embed only facts that survived deterministic-ID and hash deduplication.
        mem_texts = [record[1] for record in pending_records]
        embed_map = {}
        try:
            mem_embeddings_list = self.embedding_model.embed_batch(mem_texts, "add")
            for text, embedding in zip(mem_texts, mem_embeddings_list):
                if _embedding_is_available(embedding):
                    embed_map[text] = embedding
        except Exception as exc:
            logger.warning("Batch long-term memory embedding failed; retrying individually: %s", exc)

        for text in _missing_embeddings(mem_texts, embed_map):
            try:
                embedding = self.embedding_model.embed(text, "add")
                if _embedding_is_available(embedding):
                    embed_map[text] = embedding
            except Exception as exc:
                logger.warning("Failed to embed long-term memory text: %s", exc)

        missing_embeddings = _missing_embeddings(mem_texts, embed_map)
        if source_job_id and missing_embeddings:
            raise RuntimeError(f"Failed to embed {len(missing_embeddings)} extracted memories")

        # Phase 5: Build complete records. Non-background callers retain skip-on-failure compatibility.
        records = []
        for memory_id, text, mem_hash, mem in pending_records:
            if text not in embed_map:
                continue
            text_lemmatized = lemmatize_for_bm25(text, language=getattr(self, "_bm25_language", None))
            mem_metadata = deepcopy(metadata)
            mem_metadata["data"] = text
            mem_metadata["text_lemmatized"] = text_lemmatized
            mem_metadata["hash"] = mem_hash
            mem_metadata["created_at"] = (
                normalize_iso_timestamp_to_beijing(mem_metadata.get("created_at")) or beijing_now_iso()
            )
            mem_metadata["updated_at"] = mem_metadata["created_at"]
            if source_job_id:
                mem_metadata.update(
                    {
                        "source_job_id": source_job_id,
                        "source_stage": "longterm",
                        "output_state": "staging",
                        "output_lease_token": lease_token,
                    }
                )
            if mem.get("attributed_to"):
                mem_metadata["attributed_to"] = mem["attributed_to"]

            records.append((memory_id, text, embed_map[text], mem_metadata))

        if not records:
            return []
        if lease_is_current is not None and not lease_is_current():
            raise RuntimeError("stale migration stage lease")

        # Phase 6: Batch persist
        all_vectors = [r[2] for r in records]
        all_ids = [r[0] for r in records]
        all_payloads = [r[3] for r in records]

        insertion_errors = []
        persisted_records = []
        try:
            self.vector_store.insert(
                vectors=all_vectors,
                ids=all_ids,
                payloads=all_payloads,
            )
            persisted_records = records
        except Exception:
            # Fallback: insert one by one
            for record in records:
                mid, _, vec, pay = record
                try:
                    self.vector_store.insert(vectors=[vec], ids=[mid], payloads=[pay])
                    persisted_records.append(record)
                except Exception as e:
                    logger.error(f"Failed to insert memory {mid}: {e}")
                    insertion_errors.append(e)

        # Batch history
        history_records = [
            {
                "memory_id": r[0],
                "old_memory": None,
                "new_memory": r[1],
                "event": "ADD",
                "created_at": r[3].get("created_at"),
                "is_deleted": 0,
            }
            for r in persisted_records
        ]
        if not source_job_id:
            try:
                self.db.batch_add_history(history_records)
            except Exception:
                # Fallback: add one by one
                for hr in history_records:
                    try:
                        self.db.add_history(
                            hr["memory_id"],
                            None,
                            hr["new_memory"],
                            "ADD",
                            created_at=hr.get("created_at"),
                        )
                    except Exception as e:
                        logger.error(f"Failed to add history for {hr['memory_id']}: {e}")

        if insertion_errors and source_job_id:
            raise RuntimeError(f"Failed to insert {len(insertion_errors)} long-term memories") from insertion_errors[0]

        if source_job_id:
            return [{"id": r[0], "memory": r[1], "event": "ADD"} for r in records]

        # Phase 7: Batch entity linking
        entity_search_filters = _longterm_entity_filters(filters)
        try:
            all_texts = [r[1] for r in records]
            all_entities = self._run_entity_extraction(extract_entities_batch, all_texts)

            # 7a: Global dedup — collect unique entities across all memories
            global_entities = {}  # normalized_key -> (entity_type, entity_text, set of memory_ids)
            for idx, (memory_id, text, embedding, payload) in enumerate(records):
                entities = all_entities[idx] if idx < len(all_entities) else []
                for entity_type, entity_text in entities:
                    key = self._normalize_entity_text(entity_text)
                    if key in global_entities:
                        global_entities[key][2].add(memory_id)
                    else:
                        global_entities[key] = [entity_type, entity_text, {memory_id}]

            if global_entities:
                ordered_keys = list(global_entities.keys())
                entity_texts = [global_entities[k][1] for k in ordered_keys]

                # 7b: Single batch embed for all unique entities
                try:
                    entity_embeddings = self.embedding_model.embed_batch(entity_texts, "add")
                except Exception:
                    # Fallback: embed individually, use None for failures
                    entity_embeddings = []
                    for t in entity_texts:
                        try:
                            entity_embeddings.append(self.embedding_model.embed(t, "add"))
                        except Exception:
                            entity_embeddings.append(None)

                if len(entity_embeddings) != len(ordered_keys):
                    logger.warning(
                        "embed_batch returned %d vectors for %d entity texts — "
                        "padding/truncating to avoid dropping entity links",
                        len(entity_embeddings),
                        len(ordered_keys),
                    )
                    entity_embeddings = list(entity_embeddings[: len(ordered_keys)])
                    entity_embeddings += [None] * (len(ordered_keys) - len(entity_embeddings))

                # Filter out entities with failed embeddings
                valid = [(i, k) for i, k in enumerate(ordered_keys) if entity_embeddings[i] is not None]
                if valid:
                    valid_indices, valid_keys = zip(*valid)
                    valid_vectors = [entity_embeddings[i] for i in valid_indices]
                    exact_matches = self._existing_entities_by_text(entity_search_filters)

                    # 7c: Batch search for existing entities
                    valid_texts = [global_entities[k][1] for k in valid_keys]
                    existing_matches = self.entity_store.search_batch(
                        queries=valid_texts,
                        vectors_list=valid_vectors,
                        top_k=1,
                        filters=entity_search_filters,
                    )

                    # 7d: Separate into inserts vs updates
                    to_insert_vectors, to_insert_ids, to_insert_payloads = [], [], []
                    for j, key in enumerate(valid_keys):
                        entity_type, entity_text, memory_ids = global_entities[key]
                        matches = existing_matches[j] if j < len(existing_matches) else []
                        exact_match = exact_matches.get(key)

                        semantic_match = matches[0] if matches and matches[0].score >= 0.95 else None
                        match = exact_match or semantic_match
                        if match:
                            # Update existing entity
                            payload = match.payload or {}
                            linked = set(payload.get("linked_memory_ids", []))
                            linked |= memory_ids
                            payload = _update_entity_payload(payload, linked)
                            try:
                                self.entity_store.update(
                                    vector_id=match.id,
                                    vector=None,
                                    payload=payload,
                                )
                            except Exception as e:
                                logger.debug(f"Entity update failed for '{entity_text}': {e}")
                        else:
                            # New entity — collect for batch insert
                            to_insert_vectors.append(valid_vectors[j])
                            to_insert_ids.append(str(uuid.uuid4()))
                            to_insert_payloads.append(
                                _new_entity_payload(entity_text, entity_type, memory_ids, entity_search_filters)
                            )

                    # 7e: Single batch insert for all new entities
                    if to_insert_vectors:
                        try:
                            self.entity_store.insert(
                                vectors=to_insert_vectors,
                                ids=to_insert_ids,
                                payloads=to_insert_payloads,
                            )
                        except Exception as e:
                            logger.warning(f"Batch entity insert failed: {e}")
        except Exception as e:
            logger.warning(f"Batch entity linking failed: {e}")

        returned_memories = [{"id": r[0], "memory": r[1], "event": "ADD"} for r in records]

        keys, encoded_ids = process_telemetry_filters(filters)
        capture_event(
            "mem0.add",
            self,
            {"version": self.api_version, "keys": keys, "encoded_ids": encoded_ids, "sync_type": "sync"},
        )
        return returned_memories

    def _add_to_vector_store(self, messages, metadata, filters, infer, prompt=None):
        """Compatibility alias treating ``messages`` as an already-evicted long-term batch."""
        return self._process_evicted_long_term_memories(
            messages,
            metadata,
            filters,
            infer=infer,
            prompt=prompt,
        )

    def get(self, memory_id):
        """
        Retrieve a memory by ID.

        Args:
            memory_id (str): ID of the memory to retrieve.

        Returns:
            dict: Retrieved memory.
        """
        capture_event("mem0.get", self, {"memory_id": memory_id, "sync_type": "sync"})
        memory = self.vector_store.get(vector_id=memory_id)
        if not memory or not self._stage_output_is_visible(
            getattr(memory, "payload", None) or {},
            "longterm",
        ):
            display_first_run_notice(self, "sync", "get")
            return None

        promoted_payload_keys = [
            "user_id",
            "agent_id",
            "run_id",
            "actor_id",
            "role",
            "attributed_to",
            "expiration_date",
        ]

        core_and_promoted_keys = {
            "data",
            "hash",
            "created_at",
            "updated_at",
            "id",
            "text_lemmatized",
            "attributed_to",
            *promoted_payload_keys,
        }

        result_item = MemoryItem(
            id=memory.id,
            memory=memory.payload.get("data", ""),
            hash=memory.payload.get("hash"),
            created_at=memory.payload.get("created_at"),
            updated_at=memory.payload.get("updated_at"),
        ).model_dump()

        for key in promoted_payload_keys:
            if key in memory.payload:
                result_item[key] = memory.payload[key]

        additional_metadata = {k: v for k, v in memory.payload.items() if k not in core_and_promoted_keys}
        if additional_metadata:
            result_item["metadata"] = additional_metadata

        display_first_run_notice(self, "sync", "get")
        return result_item

    def get_all(
        self,
        *,
        filters: Optional[Dict[str, Any]] = None,
        top_k: int = 20,
        show_expired: bool = False,
        **kwargs,
    ):
        """
        List all memories.

        Args:
            filters (dict): Filter dict containing entity IDs and optional metadata filters.
                Must contain at least one of: user_id, agent_id, run_id.
                Example: filters={"user_id": "u1", "agent_id": "a1"}
            top_k (int, optional): The maximum number of memories to return. Defaults to 20.
            show_expired (bool, optional): Include expired memories. Defaults to False.

        Returns:
            dict: A dictionary containing a list of memories under the "results" key.
                  Example for v1.1+: `{"results": [{"id": "...", "memory": "...", ...}]}`

        Raises:
            ValueError: If filters doesn't contain at least one of user_id, agent_id, run_id,
                or if top_k is invalid.
        """
        # Reject top-level entity params - must use filters instead
        _reject_top_level_entity_params(kwargs, "get_all")

        # Validate top_k
        _validate_search_params(top_k=top_k)

        # Validate and trim entity IDs in filters
        effective_filters = dict(filters) if filters else {}
        if "user_id" in effective_filters:
            effective_filters["user_id"] = _validate_and_trim_entity_id(effective_filters["user_id"], "user_id")
        if "agent_id" in effective_filters:
            effective_filters["agent_id"] = _validate_and_trim_entity_id(effective_filters["agent_id"], "agent_id")
        if "run_id" in effective_filters:
            effective_filters["run_id"] = _validate_and_trim_entity_id(effective_filters["run_id"], "run_id")

        # Validate filters contains at least one entity ID
        if not any(key in effective_filters for key in ("user_id", "agent_id", "run_id")):
            raise ValueError(
                "filters must contain at least one of: user_id, agent_id, run_id. Example: filters={'user_id': 'u1'}"
            )

        limit = top_k
        fetch_limit = limit if show_expired else max(limit * 4, 60)
        scale_threshold_notice = detect_scale_threshold_from_top_k(top_k)

        keys, encoded_ids = process_telemetry_filters(effective_filters)
        capture_event(
            "mem0.get_all", self, {"limit": limit, "keys": keys, "encoded_ids": encoded_ids, "sync_type": "sync"}
        )

        all_memories_result = self._get_all_from_vector_store(effective_filters, fetch_limit, show_expired, limit)

        if scale_threshold_notice:
            display_scale_threshold_notice(self, "sync", "get_all", *scale_threshold_notice)
        else:
            display_first_run_notice(self, "sync", "get_all")
        return {"results": all_memories_result}

    def _get_all_from_vector_store(self, filters, limit, show_expired=False, output_limit=None):
        memories_result = self.vector_store.list(filters=filters, top_k=limit)

        # Handle different vector store return formats by inspecting first element
        if isinstance(memories_result, (tuple, list)) and len(memories_result) > 0:
            first_element = memories_result[0]

            # If first element is a container, unwrap one level
            if isinstance(first_element, (list, tuple)):
                actual_memories = first_element
            else:
                # First element is a memory object, structure is already flat
                actual_memories = memories_result
        else:
            actual_memories = memories_result

        promoted_payload_keys = [
            "user_id",
            "agent_id",
            "run_id",
            "actor_id",
            "role",
            "attributed_to",
            "expiration_date",
        ]
        core_and_promoted_keys = {
            "data",
            "hash",
            "created_at",
            "updated_at",
            "id",
            "text_lemmatized",
            "attributed_to",
            *promoted_payload_keys,
        }

        formatted_memories = []
        for mem in actual_memories:
            if not self._stage_output_is_visible(getattr(mem, "payload", None) or {}, "longterm"):
                continue
            if not show_expired and _payload_is_expired(mem.payload):
                continue
            memory_item_dict = MemoryItem(
                id=mem.id,
                memory=mem.payload.get("data", ""),
                hash=mem.payload.get("hash"),
                created_at=mem.payload.get("created_at"),
                updated_at=mem.payload.get("updated_at"),
            ).model_dump(exclude={"score"})

            for key in promoted_payload_keys:
                if key in mem.payload:
                    memory_item_dict[key] = mem.payload[key]

            additional_metadata = {k: v for k, v in mem.payload.items() if k not in core_and_promoted_keys}
            if additional_metadata:
                memory_item_dict["metadata"] = additional_metadata

            formatted_memories.append(memory_item_dict)
            if output_limit is not None and len(formatted_memories) >= output_limit:
                break

        return formatted_memories

    def search(
        self,
        query: str,
        *,
        top_k: Optional[int] = None,
        filters: Optional[Dict[str, Any]] = None,
        threshold: Optional[float] = None,
        rerank: bool = False,
        explain: bool = False,
        reference_date: Optional[Any] = None,
        show_expired: bool = False,
        **kwargs,
    ):
        """
        Searches for memories based on a query.

        Args:
            query (str): Query to search for.
            top_k (int, optional): Maximum number of results to return. Defaults to 20.
            filters (dict): Filter dict containing entity IDs and optional metadata filters.
                Must contain at least one of: user_id, agent_id, run_id.
                Example: filters={"user_id": "u1", "agent_id": "a1"}

                Enhanced metadata filtering with operators:
                - {"key": "value"} - exact match
                - {"key": {"eq": "value"}} - equals
                - {"key": {"ne": "value"}} - not equals
                - {"key": {"in": ["val1", "val2"]}} - in list
                - {"key": {"nin": ["val1", "val2"]}} - not in list
                - {"key": {"gt": 10}} - greater than
                - {"key": {"gte": 10}} - greater than or equal
                - {"key": {"lt": 10}} - less than
                - {"key": {"lte": 10}} - less than or equal
                - {"key": {"contains": "text"}} - contains text
                - {"key": {"icontains": "text"}} - case-insensitive contains
                - {"key": "*"} - wildcard match (any value)
                - {"AND": [filter1, filter2]} - logical AND
                - {"OR": [filter1, filter2]} - logical OR
                - {"NOT": [filter1]} - logical NOT
            threshold (float, optional): Minimum score for a memory to be included. Defaults to 0.1.
            rerank (bool, optional): Whether to rerank results. Defaults to False.
            explain (bool, optional): Whether to include score_details for each result. Defaults to False.
            reference_date (Any, optional): Platform-only temporal parameter. Not supported in OSS.
            show_expired (bool, optional): Include expired memories. Defaults to False.

        Returns:
            dict: A dictionary containing the search results under a "results" key.
                  Example for v1.1+: `{"results": [{"id": "...", "memory": "...", "score": 0.8, ...}]}`

        Raises:
            ValueError: If filters doesn't contain at least one of user_id, agent_id, run_id,
                or if threshold/top_k values are invalid.
        """
        if reference_date is not None:
            raise ValueError(get_temporal_feature_error_message("sync", "search", "reference_date"))

        # Reject top-level entity params - must use filters instead
        _reject_top_level_entity_params(kwargs, "search")

        if top_k is None:
            top_k = int(_fine_grained_longterm_config(self).top_k)
        # Validate search parameters (before applying defaults)
        _validate_search_params(threshold=threshold, top_k=top_k)
        threshold = _effective_longterm_threshold(self, threshold)
        query = _validate_and_trim_search_query(query)
        temporal_usage_notice = detect_temporal_usage_from_search(query, filters)

        # Validate and trim entity IDs in filters
        effective_filters = filters.copy() if filters else {}
        if "user_id" in effective_filters:
            effective_filters["user_id"] = _validate_and_trim_entity_id(effective_filters["user_id"], "user_id")
        if "agent_id" in effective_filters:
            effective_filters["agent_id"] = _validate_and_trim_entity_id(effective_filters["agent_id"], "agent_id")
        if "run_id" in effective_filters:
            effective_filters["run_id"] = _validate_and_trim_entity_id(effective_filters["run_id"], "run_id")
        if not any(key in effective_filters for key in ("user_id", "agent_id", "run_id")):
            raise ValueError(
                "filters must contain at least one of: user_id, agent_id, run_id. Example: filters={'user_id': 'u1'}"
            )

        limit = top_k
        scale_threshold_notice = detect_scale_threshold_from_top_k(top_k)

        # Apply enhanced metadata filtering if advanced operators are detected
        if self._has_advanced_operators(effective_filters):
            processed_filters = self._process_metadata_filters(effective_filters)
            # Remove logical/operator keys that have been reprocessed
            for logical_key in ("AND", "OR", "NOT"):
                effective_filters.pop(logical_key, None)
            for fk in list(effective_filters.keys()):
                if fk not in ("AND", "OR", "NOT", "user_id", "agent_id", "run_id") and isinstance(
                    effective_filters.get(fk), dict
                ):
                    effective_filters.pop(fk, None)
            effective_filters.update(processed_filters)

        keys, encoded_ids = process_telemetry_filters(effective_filters)
        # TODO: 为什么没有系统时间的记录？
        capture_event(
            "mem0.search",
            self,
            {
                "limit": limit,
                "version": self.api_version,
                "keys": keys,
                "encoded_ids": encoded_ids,
                "sync_type": "sync",
                "threshold": threshold,
                "explain": explain,
                "advanced_filters": bool(filters and self._has_advanced_operators(filters)),
            },
        )

        search_start = time.perf_counter()
        original_memories = self._search_vector_store(
            query, effective_filters, limit, threshold, explain=explain, show_expired=show_expired
        )
        search_elapsed_seconds = time.perf_counter() - search_start

        # Apply reranking if enabled and reranker is available
        configured_fine_reranker = getattr(
            getattr(getattr(self, "config", None), "fine_grained_longterm", None),
            "reranker",
            None,
        )
        if (
            rerank
            and self.reranker
            and original_memories
            and getattr(configured_fine_reranker, "method", "none") == "none"
        ):
            try:
                reranked_memories = self.reranker.rerank(query, original_memories, limit)
                original_memories = reranked_memories
            except Exception as e:
                logger.warning(f"Reranking failed, using original results: {e}")

        original_memories = self._with_midterm_search_results(query, effective_filters, original_memories)

        if temporal_usage_notice:
            display_temporal_usage_notice(self, "sync", "search", *temporal_usage_notice)
        elif scale_threshold_notice:
            display_scale_threshold_notice(self, "sync", "search", *scale_threshold_notice)
        elif search_elapsed_seconds > PERFORMANCE_SLOW_QUERY_THRESHOLD_SECONDS:
            display_performance_slow_query_notice(
                self,
                "sync",
                "search",
                search_elapsed_seconds,
                top_k,
                len(original_memories),
            )
        else:
            display_first_run_notice(self, "sync", "search")

        # TODO: 返回结果只有中期和长期的memory，没有短期的
        return {"results": original_memories}

    def _process_metadata_filters(self, metadata_filters: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process enhanced metadata filters and convert them to vector store compatible format.

        Args:
            metadata_filters: Enhanced metadata filters with operators

        Returns:
            Dict of processed filters compatible with vector store
        """
        processed_filters = {}

        def process_condition(key: str, condition: Any) -> Dict[str, Any]:
            if not isinstance(condition, dict):
                # Simple equality: {"key": "value"}
                if condition == "*":
                    # Wildcard: match everything for this field (implementation depends on vector store)
                    return {key: "*"}
                return {key: condition}

            result = {}
            for operator, value in condition.items():
                # Map platform operators to universal format that can be translated by each vector store
                operator_map = {
                    "eq": "eq",
                    "ne": "ne",
                    "gt": "gt",
                    "gte": "gte",
                    "lt": "lt",
                    "lte": "lte",
                    "in": "in",
                    "nin": "nin",
                    "contains": "contains",
                    "icontains": "icontains",
                }

                if operator in operator_map:
                    result.setdefault(key, {})[operator_map[operator]] = value
                else:
                    raise ValueError(f"Unsupported metadata filter operator: {operator}")
            return result

        def merge_filters(target: Dict[str, Any], source: Dict[str, Any]) -> None:
            """Merge source into target, deep-merging nested operator dicts for the same key."""
            for key, value in source.items():
                if key in target and isinstance(target[key], dict) and isinstance(value, dict):
                    target[key].update(value)
                else:
                    target[key] = value

        for key, value in metadata_filters.items():
            if key == "AND":
                # Logical AND: combine multiple conditions
                if not isinstance(value, list):
                    raise ValueError("AND operator requires a list of conditions")
                for condition in value:
                    for sub_key, sub_value in condition.items():
                        merge_filters(processed_filters, process_condition(sub_key, sub_value))
            elif key == "OR":
                # Logical OR: Pass through to vector store for implementation-specific handling
                if not isinstance(value, list) or not value:
                    raise ValueError("OR operator requires a non-empty list of conditions")
                # Store OR conditions in a way that vector stores can interpret
                processed_filters["$or"] = []
                for condition in value:
                    or_condition = {}
                    for sub_key, sub_value in condition.items():
                        merge_filters(or_condition, process_condition(sub_key, sub_value))
                    processed_filters["$or"].append(or_condition)
            elif key == "NOT":
                # Logical NOT: Pass through to vector store for implementation-specific handling
                if not isinstance(value, list) or not value:
                    raise ValueError("NOT operator requires a non-empty list of conditions")
                processed_filters["$not"] = []
                for condition in value:
                    not_condition = {}
                    for sub_key, sub_value in condition.items():
                        merge_filters(not_condition, process_condition(sub_key, sub_value))
                    processed_filters["$not"].append(not_condition)
            else:
                merge_filters(processed_filters, process_condition(key, value))

        return processed_filters

    def _has_advanced_operators(self, filters: Dict[str, Any]) -> bool:
        """
        Check if filters contain advanced operators that need special processing.

        Args:
            filters: Dictionary of filters to check

        Returns:
            bool: True if advanced operators are detected
        """
        if not isinstance(filters, dict):
            return False

        for key, value in filters.items():
            # Check for platform-style logical operators
            if key in ["AND", "OR", "NOT"]:
                return True
            # Check for comparison operators (without $ prefix for universal compatibility)
            if isinstance(value, dict):
                for op in value.keys():
                    if op in ["eq", "ne", "gt", "gte", "lt", "lte", "in", "nin", "contains", "icontains"]:
                        return True
            # Check for wildcard values
            if value == "*":
                return True
        return False

    def _search_vector_store(self, query, filters, limit, threshold=0.1, explain=False, show_expired=False):
        return self.fine_grained_longterm_retriever.search(
            query,
            filters,
            top_k=limit,
            threshold=threshold,
            explain=explain,
            show_expired=show_expired,
        )

    def _compute_entity_boosts(self, query_entities, filters):
        """Compatibility entry point backed by the production FineGrainedLongTerm retriever."""
        return self.fine_grained_longterm_retriever._entity_boosts(query_entities, filters)

    def update(
        self,
        memory_id,
        data: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        expiration_date: Any = _UNSET,
    ):
        """
        Update a memory by ID.

        Args:
            memory_id (str): ID of the memory to update.
            data (str, optional): New content to update the memory with.
            metadata (dict, optional): Metadata to update with the memory. Defaults to None.
            expiration_date (Any, optional): Date in YYYY-MM-DD format, or None to clear it.

        Returns:
            dict: Success message indicating the memory was updated.

        Example:
            >>> m.update(memory_id="mem_123", data="Likes to play tennis on weekends")
            {'message': 'Memory updated successfully!'}
        """
        capture_event("mem0.update", self, {"memory_id": memory_id, "sync_type": "sync"})

        if data is None and metadata is None and expiration_date is _UNSET:
            raise ValueError("At least one of data, metadata, or expiration_date must be provided.")

        update_metadata = deepcopy(metadata) if metadata is not None else None
        if expiration_date is not _UNSET:
            update_metadata = update_metadata or {}
            update_metadata["expiration_date"] = _normalize_expiration_date(expiration_date)

        existing_embeddings = {}
        if data is not None:
            existing_embeddings[data] = self.embedding_model.embed(data, "update")

        self._update_memory(memory_id, data, existing_embeddings, update_metadata)
        display_first_run_notice(self, "sync", "update")
        return {"message": "Memory updated successfully!"}

    def delete(self, memory_id):
        """
        Delete a memory by ID.

        Args:
            memory_id (str): ID of the memory to delete.
        """
        capture_event("mem0.delete", self, {"memory_id": memory_id, "sync_type": "sync"})

        existing_memory = self.vector_store.get(vector_id=memory_id)
        if existing_memory is None:
            raise ValueError(f"Memory with id {memory_id} not found")

        self._delete_memory(memory_id, existing_memory)
        decay_usage_notice = detect_decay_usage_from_delete()
        if decay_usage_notice:
            display_decay_usage_notice(self, "sync", "delete", *decay_usage_notice)
        else:
            display_first_run_notice(self, "sync", "delete")
        return {"message": "Memory deleted successfully!"}

    def delete_all(self, user_id: Optional[str] = None, agent_id: Optional[str] = None, run_id: Optional[str] = None):
        """
        Delete all memories.

        Args:
            user_id (str, optional): ID of the user to delete memories for. Defaults to None.
            agent_id (str, optional): ID of the agent to delete memories for. Defaults to None.
            run_id (str, optional): ID of the run to delete memories for. Defaults to None.
        """
        user_id = _validate_and_trim_entity_id(user_id, "user_id")
        agent_id = _validate_and_trim_entity_id(agent_id, "agent_id")
        run_id = _validate_and_trim_entity_id(run_id, "run_id")

        filters: Dict[str, Any] = {}
        if user_id:
            filters["user_id"] = user_id
        if agent_id:
            filters["agent_id"] = agent_id
        if run_id:
            filters["run_id"] = run_id

        if not filters:
            raise ValueError(
                "At least one filter is required to delete all memories. If you want to delete all memories, use the `reset()` method."
            )

        keys, encoded_ids = process_telemetry_filters(filters)
        capture_event("mem0.delete_all", self, {"keys": keys, "encoded_ids": encoded_ids, "sync_type": "sync"})
        # delete all vector memories and reset the collections
        memories = self.vector_store.list(filters=filters)[0]
        for memory in memories:
            self._delete_memory(memory.id)

        logger.info(f"Deleted {len(memories)} memories")

        decay_usage_notice = detect_decay_usage_from_delete_all(len(memories))
        if decay_usage_notice:
            display_decay_usage_notice(self, "sync", "delete_all", *decay_usage_notice)
        else:
            display_first_run_notice(self, "sync", "delete_all")
        return {"message": "Memories deleted successfully!"}

    def history(self, memory_id):
        """
        Get the history of changes for a memory by ID.

        Args:
            memory_id (str): ID of the memory to get history for.

        Returns:
            list: List of changes for the memory.
        """
        capture_event("mem0.history", self, {"memory_id": memory_id, "sync_type": "sync"})
        memory = self.vector_store.get(vector_id=memory_id)
        if memory is not None and not self._stage_output_is_visible(
            getattr(memory, "payload", None) or {},
            "longterm",
        ):
            return []
        history = self.db.get_history(memory_id)
        display_first_run_notice(self, "sync", "history")
        return history

    def _create_memory(self, data, existing_embeddings, metadata=None, memory_id=None):
        logger.debug(f"Creating memory with {data=}")
        if data in existing_embeddings:
            embeddings = existing_embeddings[data]
        else:
            embeddings = self.embedding_model.embed(data, memory_action="add")
        memory_id = memory_id or str(uuid.uuid4())
        new_metadata = deepcopy(metadata) if metadata is not None else {}
        new_metadata["data"] = data
        new_metadata["hash"] = hashlib.md5(data.encode()).hexdigest()
        new_metadata["created_at"] = (
            normalize_iso_timestamp_to_beijing(new_metadata.get("created_at")) or beijing_now_iso()
        )
        new_metadata["updated_at"] = new_metadata["created_at"]
        new_metadata["text_lemmatized"] = lemmatize_for_bm25(data, language=getattr(self, "_bm25_language", None))

        self.vector_store.insert(
            vectors=[embeddings],
            ids=[memory_id],
            payloads=[new_metadata],
        )
        if new_metadata.get("output_state") != "staging":
            self.db.add_history(
                memory_id,
                None,
                data,
                "ADD",
                created_at=new_metadata.get("created_at"),
                updated_at=new_metadata.get("updated_at"),
                actor_id=new_metadata.get("actor_id"),
                role=new_metadata.get("role"),
            )
        return memory_id

    def _create_procedural_memory(self, messages, metadata=None, prompt=None):
        """
        Create a procedural memory

        Args:
            messages (list): List of messages to create a procedural memory from.
            metadata (dict): Metadata to create a procedural memory from.
            prompt (str, optional): Prompt to use for the procedural memory creation. Defaults to None.
        """
        logger.info("Creating procedural memory")

        parsed_messages = _build_procedural_memory_messages(messages, prompt)

        try:
            procedural_memory = self.llm.generate_response(messages=parsed_messages)
            procedural_memory = remove_code_blocks(procedural_memory)
        except Exception as e:
            logger.error(f"Error generating procedural memory summary: {e}")
            raise

        if metadata is None:
            raise ValueError("Metadata cannot be done for procedural memory.")

        metadata = {**metadata, "memory_type": MemoryType.PROCEDURAL.value}
        embeddings = self.embedding_model.embed(procedural_memory, memory_action="add")
        memory_id = self._create_memory(procedural_memory, {procedural_memory: embeddings}, metadata=metadata)
        capture_event("mem0._create_procedural_memory", self, {"memory_id": memory_id, "sync_type": "sync"})

        result = {"results": [{"id": memory_id, "memory": procedural_memory, "event": "ADD"}]}

        return result

    def _update_memory(self, memory_id, data, existing_embeddings, metadata=None):
        logger.info(f"Updating memory with {data=}")

        try:
            existing_memory = self.vector_store.get(vector_id=memory_id)
        except Exception:
            # Backing-store failure, not a bad memory_id: re-raise the original so the REST layer maps it to 5xx, not 4xx.
            logger.error(f"Error getting memory with ID {memory_id} during update.")
            raise

        if existing_memory is None:
            raise ValueError(f"Memory with id {memory_id} not found. Please provide a valid 'memory_id'")

        prev_value = existing_memory.payload.get("data")
        if data is None:
            data = prev_value
        if not isinstance(data, str):
            raise ValueError(f"Memory with id {memory_id} does not have text content to update")
        text_changed = data != prev_value

        new_metadata = deepcopy(existing_memory.payload)
        if metadata is not None:
            new_metadata.update(metadata)

        new_metadata["data"] = data
        new_metadata["hash"] = hashlib.md5(data.encode()).hexdigest()
        new_metadata["text_lemmatized"] = lemmatize_for_bm25(data, language=getattr(self, "_bm25_language", None))
        new_metadata["created_at"] = normalize_iso_timestamp_to_beijing(existing_memory.payload.get("created_at"))
        new_metadata["updated_at"] = beijing_now_iso()

        # actor_id is immutable after creation (issue #4490)
        if "actor_id" in existing_memory.payload:
            new_metadata["actor_id"] = existing_memory.payload["actor_id"]

        if data in existing_embeddings:
            embeddings = existing_embeddings[data]
        else:
            embeddings = self.embedding_model.embed(data, "update")

        self.vector_store.update(
            vector_id=memory_id,
            vector=embeddings,
            payload=new_metadata,
        )
        logger.info(f"Updating memory with ID {memory_id=} with {data=}")

        self.db.add_history(
            memory_id,
            prev_value,
            data,
            "UPDATE",
            created_at=new_metadata["created_at"],
            updated_at=new_metadata["updated_at"],
            actor_id=new_metadata.get("actor_id"),
            role=new_metadata.get("role"),
        )

        # Entity-store cleanup: strip this memory's id from old-text entities,
        # then re-extract entities from the new text and link them back.
        session_filters = {k: new_metadata[k] for k in ("user_id", "agent_id", "run_id") if new_metadata.get(k)}
        if text_changed:
            self._remove_memory_from_entity_store(memory_id, session_filters)
            self._link_entities_for_memory(memory_id, data, session_filters)

        return memory_id

    def _delete_memory(self, memory_id, existing_memory=None):
        logger.info(f"Deleting memory with {memory_id=}")
        if existing_memory is None:
            existing_memory = self.vector_store.get(vector_id=memory_id)
            if existing_memory is None:
                raise ValueError(f"Memory with id {memory_id} not found. Please provide a valid 'memory_id'")
        prev_value = existing_memory.payload.get("data", "")
        created_at = normalize_iso_timestamp_to_beijing(existing_memory.payload.get("created_at"))
        updated_at = beijing_now_iso()
        payload = existing_memory.payload or {}
        session_filters = {k: payload[k] for k in ("user_id", "agent_id", "run_id") if payload.get(k)}
        self.vector_store.delete(vector_id=memory_id)
        self.db.add_history(
            memory_id,
            prev_value,
            None,
            "DELETE",
            created_at=created_at,
            updated_at=updated_at,
            actor_id=existing_memory.payload.get("actor_id"),
            role=existing_memory.payload.get("role"),
            is_deleted=1,
        )

        # Entity-store cleanup: strip this memory's id from any entity records
        # that linked to it. Non-fatal — the helper swallows errors.
        self._remove_memory_from_entity_store(memory_id, session_filters)

        return memory_id

    def reset(self):
        """
        Reset the memory store by:
            Deletes the vector store collection
            Resets the database
            Recreates the vector store with a new client
        """
        logger.warning("Resetting all memories")

        self._pause_background_workers_for_reset()
        self._reset_midterm_state()
        self.db.reset()
        self.db.close()
        self.db = SQLiteManager(self.config.history_db_path)
        self._clear_profile_runtime_state()

        if hasattr(self.vector_store, "reset"):
            self.vector_store = VectorStoreFactory.reset(self.vector_store)
        else:
            logger.warning("Vector store does not support reset. Skipping.")
            self.vector_store.delete_col()
            self.vector_store = VectorStoreFactory.create(
                self.config.vector_store.provider,
                self.config.vector_store.config,
                timeout_seconds=self.config.vector_store_timeout_seconds,
            )
        # Reset entity store if initialized
        if self._entity_store is not None:
            try:
                self._entity_store.reset()
            except Exception as e:
                logger.warning(f"Failed to reset entity store: {e}")
            self._entity_store = None

        self._initialize_background_workers()
        capture_event("mem0.reset", self, {"sync_type": "sync"})
        display_first_run_notice(self, "sync", "reset")

    def close(self) -> bool:
        """Release resources held by this Memory instance (SQLite connections, etc.)."""
        return self._close_background_workers_and_db()

    def chat(self, query):
        raise NotImplementedError("Chat function not implemented yet.")


class AsyncMemory(_BackgroundMemoryMixin, MemoryBase):
    """Asynchronous memory API with cross-session user profile support."""

    def __init__(self, config: MemoryConfig = MemoryConfig()):
        self.config = config
        self._acquire_process_instance_lock()
        self._bm25_language = _configured_bm25_language(config)

        self.embedding_model = EmbedderFactory.create(
            self.config.embedder.provider,
            self.config.embedder.config,
            self.config.vector_store.config,
            timeout_seconds=self.config.embedding_timeout_seconds,
        )
        self.vector_store = VectorStoreFactory.create(
            self.config.vector_store.provider,
            self.config.vector_store.config,
            timeout_seconds=self.config.vector_store_timeout_seconds,
        )
        self.llm = LlmFactory.create(
            self.config.llm.provider,
            self.config.llm.config,
            timeout_seconds=self.config.llm_timeout_seconds,
        )
        self.db = SQLiteManager(self.config.history_db_path)
        self.collection_name = self.config.vector_store.config.collection_name
        self.api_version = self.config.version
        self.custom_instructions = self.config.custom_instructions
        self._entity_store = None
        self._midterm_memory = None
        self._midterm_updater = None
        self._midterm_retriever = None
        self._fine_grained_longterm_retriever = None
        self._cross_session_longterm = None
        self._profile_manager = None
        self._profile_updater = None
        self._component_init_lock = threading.RLock()
        self._profile_user_locks = {}
        self._profile_user_locks_guard = threading.Lock()

        # Initialize reranker if configured
        self.reranker = None
        if config.reranker:
            self.reranker = RerankerConcurrencyGuard(
                RerankerFactory.create(
                    config.reranker.provider,
                    config.reranker.config,
                    timeout_seconds=self.config.reranker_timeout_seconds,
                ),
                max_concurrency=config.reranker.max_concurrency,
            )

        if MEM0_TELEMETRY:
            telemetry_config = _build_telemetry_vector_store_config(self.config)
            self._telemetry_vector_store = VectorStoreFactory.create(
                self.config.vector_store.provider,
                telemetry_config,
                timeout_seconds=self.config.vector_store_timeout_seconds,
            )

        if getattr(type(self.vector_store), "keyword_search", None) is VectorStoreBase.keyword_search:
            logger.warning(
                "The '%s' vector store does not support keyword search. "
                "Hybrid (BM25) scoring will be disabled and search will use "
                "semantic similarity only. To enable hybrid search, switch to a "
                "store with keyword_search support (e.g. qdrant, elasticsearch, pgvector).",
                self.config.vector_store.provider,
            )

        self._initialize_background_workers()
        capture_event("mem0.init", self, {"sync_type": "async"})

    @property
    def project(self):
        return _AsyncOSSProject()

    @property
    def entity_store(self):
        """Lazily initialize entity store on first use."""
        if self._entity_store is None:
            with self._component_init_lock:
                if self._entity_store is None:
                    entity_config = _safe_deepcopy_config(self.config.vector_store.config)
                    entity_collection = _entity_collection_name(
                        self.config.vector_store.provider,
                        self.collection_name,
                    )
                    if hasattr(entity_config, "collection_name"):
                        entity_config.collection_name = entity_collection
                    elif isinstance(entity_config, dict):
                        entity_config["collection_name"] = entity_collection
                    # For Qdrant, share the existing client to avoid RocksDB lock contention
                    # when using embedded mode (path=...). QdrantConfig.client takes precedence
                    # over host/port/path.
                    if self.config.vector_store.provider == "qdrant" and hasattr(self.vector_store, "client"):
                        if hasattr(entity_config, "client"):
                            entity_config.client = self.vector_store.client
                        elif isinstance(entity_config, dict):
                            entity_config["client"] = self.vector_store.client
                    self._entity_store = VectorStoreFactory.create(
                        self.config.vector_store.provider,
                        entity_config,
                        timeout_seconds=getattr(self.config, "vector_store_timeout_seconds", 15.0),
                    )
        return self._entity_store

    def _midterm_enabled(self):
        return bool(getattr(getattr(self, "config", None), "midterm", None) and self.config.midterm.enabled)

    @property
    def midterm_memory(self):
        if self._midterm_memory is None:
            with self._component_init_lock:
                if self._midterm_memory is None:
                    self._midterm_memory = MidTermMemory(
                        provider=self.config.vector_store.provider,
                        base_vector_config=self.config.vector_store.config,
                        base_collection_name=self.collection_name,
                        embedding_model=self.embedding_model,
                        config=self.config.midterm,
                        current_turn_index_provider=lambda filters: self.db.current_turn_index(
                            _build_session_scope(filters)
                        ),
                        primary_vector_store=self.vector_store,
                        output_is_visible=lambda payload: self._stage_output_is_visible(payload, "midterm"),
                        vector_store_timeout_seconds=getattr(
                            self.config,
                            "vector_store_timeout_seconds",
                            15.0,
                        ),
                    )
        return self._midterm_memory

    @property
    def midterm_updater(self):
        if self._midterm_updater is None:
            with self._component_init_lock:
                if self._midterm_updater is None:
                    self._midterm_updater = MidTermUpdater(
                        self.midterm_memory,
                        self.llm,
                        self.config.midterm,
                        following_qa_provider=lambda filters, after_turn_index, qa_limit: (
                            self.db.get_following_qa_messages(
                                _build_session_scope(filters),
                                after_turn_index,
                                qa_limit,
                            )
                        ),
                    )
        return self._midterm_updater

    @property
    def midterm_retriever(self):
        if self._midterm_retriever is None:
            with self._component_init_lock:
                if self._midterm_retriever is None:
                    self._midterm_retriever = MidTermRetriever(
                        self.midterm_memory,
                        self.config.midterm,
                        reranker=getattr(self, "reranker", None),
                    )
        return self._midterm_retriever

    @property
    def profile_manager(self):
        """Lazily initialize profile storage without touching the LLM."""
        if self._profile_manager is None:
            with self._component_init_lock:
                if self._profile_manager is None:
                    self._profile_manager = ProfileManager(self.db, self.config.profile)
        return self._profile_manager

    @property
    def profile_updater(self):
        """Lazily initialize the LLM-backed profile plan generator."""
        if self._profile_updater is None:
            with self._component_init_lock:
                if self._profile_updater is None:
                    self._profile_updater = ProfileUpdater(self.llm, self.config.profile)
        return self._profile_updater

    async def _background_process_midterm_async(self, job, messages, degraded: bool) -> None:
        if not self._midterm_enabled():
            return

        def lease_is_current():
            return self.db.migration_stage_lease_is_current(
                job["job_id"],
                "midterm",
                job["midterm_lease_token"],
            )

        if not lease_is_current():
            raise RuntimeError("stale migration stage lease")
        await self._process_midterm_evictions_async(
            messages,
            job["filters"],
            source_job_id=job["job_id"],
            lease_token=job["midterm_lease_token"],
            lease_is_current=lease_is_current,
            degraded=degraded,
            raise_on_error=True,
        )

    async def _background_process_longterm_async(self, job, messages, degraded: bool) -> None:
        def lease_is_current():
            return self.db.migration_stage_lease_is_current(
                job["job_id"],
                "longterm",
                job["longterm_lease_token"],
            )

        if not lease_is_current():
            raise RuntimeError("stale migration stage lease")
        if degraded:
            await asyncio.to_thread(
                self._store_longterm_fallback,
                job,
                messages,
                lease_is_current=lease_is_current,
            )
            return
        await self._process_evicted_long_term_memories(
            messages,
            job["metadata"],
            job["filters"],
            infer=job["infer"],
            prompt=job.get("prompt"),
            source_job_id=job["job_id"],
            lease_token=job["longterm_lease_token"],
            lease_is_current=lease_is_current,
        )

    async def _background_process_longterm_extraction_async(self, job) -> None:
        def lease_is_current():
            return self.db.longterm_extraction_job_lease_is_current(job["job_id"], job["lease_token"])

        if not lease_is_current():
            raise RuntimeError("stale fine-grained LongTerm extraction lease")
        metadata = {
            **job["metadata"],
            "source_job_type": "longterm_extraction",
            "source_turn_index": int(job["turn_index"]),
        }
        if job.get("source_operation_key"):
            metadata["source_operation_key"] = job["source_operation_key"]
        await self._process_evicted_long_term_memories(
            job["messages"],
            metadata,
            job["filters"],
            infer=job["infer"],
            prompt=job.get("prompt"),
            source_job_id=job["job_id"],
            lease_token=job["lease_token"],
            lease_is_current=lease_is_current,
        )

    async def _background_process_promotion_async(self, job) -> Optional[str]:
        return await asyncio.to_thread(self._background_process_promotion, job)

    async def _background_process_profile_async(self, job) -> bool:
        lock = self._get_profile_user_thread_lock(job["user_id"])
        async with _acquire_thread_lock_async(lock):
            plan = await self._generate_profile_update_plan(job["user_id"], job["messages"])
            validated_plan = self.profile_manager.validate_update_plan(
                plan or ProfileUpdatePlan(operations=[]),
            )
            return await asyncio.to_thread(
                self.db.apply_profile_plan_and_finish_job,
                job["job_id"],
                job["lease_token"],
                job["user_id"],
                validated_plan,
                max_value_json_bytes=self.config.profile.max_value_json_bytes,
            )

    async def _commit_migration_stage_outputs_async(
        self,
        job: Dict[str, Any],
        stage: str,
        lease_token: str,
        degraded: bool,
    ) -> None:
        def lease_is_current():
            return self.db.migration_stage_lease_is_current(
                job["job_id"],
                stage,
                lease_token,
            )

        if not lease_is_current():
            raise RuntimeError("stale migration stage lease")
        if stage == "midterm":
            if not self._midterm_enabled():
                return
            await self.midterm_updater.commit_source_job_outputs_async(
                job["job_id"],
                lease_token,
                degraded=degraded,
                lease_is_current=lease_is_current,
            )
            return
        await asyncio.to_thread(
            self._commit_migration_stage_outputs,
            job,
            stage,
            lease_token,
            degraded,
        )

    async def get_profile(self, user_id: str, include_metadata: Optional[bool] = None):
        """Return a user's current profile without blocking the event loop."""
        normalized_user_id = normalize_profile_user_id(user_id)
        return await asyncio.to_thread(
            self.profile_manager.get_profile,
            normalized_user_id,
            include_metadata,
        )

    async def _retrieve_context(
        self,
        query: str,
        *,
        user_id: str,
        session_id: str,
        top_k: Optional[int] = None,
        threshold: Optional[float] = None,
        rerank: bool = False,
        explain: bool = False,
        include_profile_metadata: bool = False,
    ) -> Dict[str, Any]:
        """Asynchronously retrieve session memories, messages, and the cross-session profile."""
        normalized_query, normalized_user_id, normalized_session_id = _normalize_context_request(
            query,
            user_id,
            session_id,
        )
        context = await self._retrieve_base_context(
            normalized_query,
            user_id=normalized_user_id,
            session_id=normalized_session_id,
            include_profile_metadata=include_profile_metadata,
        )
        resolver_llm = getattr(self, "llm", None)
        retrieval_query = (
            await QueryResolver(resolver_llm, prompt=_configured_query_rewrite_prompt(self)).resolve_async(
                context["query"],
                context.get("short_term_messages"),
            )
            if resolver_llm is not None
            else context["query"]
        )
        context["retrieval_query"] = retrieval_query
        top_k = 20 if top_k is None else top_k
        search_result = await self.search(
            retrieval_query,
            top_k=top_k,
            threshold=threshold,
            rerank=rerank,
            explain=explain,
            filters={"user_id": normalized_user_id, "run_id": normalized_session_id},
        )
        context["retrieved_memories"] = (
            search_result["results"]
            if isinstance(search_result, dict) and "results" in search_result
            else search_result
        )
        _filter_shortterm_duplicate_longterm(context)
        _strip_shortterm_internal_fields(context)
        if any(
            isinstance(item, dict)
            and (
                (item.get("source") in {"mid_term_page", "midterm"} and item.get("raw_dialogue"))
                or (item.get("source") == PromotedLongTermMemory.SOURCE and item.get("memory"))
            )
            for item in (context["retrieved_memories"] or [])
        ):
            has_midterm_page = any(
                isinstance(item, dict)
                and item.get("source") in {"mid_term_page", "midterm"}
                and item.get("raw_dialogue")
                for item in (context["retrieved_memories"] or [])
            )
            await asyncio.to_thread(
                self._confirm_context_valid_recalls,
                context["retrieved_memories"],
                current_turn_index=(
                    self.db.current_turn_index(
                        _build_session_scope({"user_id": normalized_user_id, "run_id": normalized_session_id})
                    )
                    if has_midterm_page
                    else None
                ),
            )
        return context

    async def _retrieve_base_context(
        self,
        query: str,
        *,
        user_id: str,
        session_id: str,
        include_profile_metadata: bool = False,
    ) -> Dict[str, Any]:
        """Asynchronously retrieve only short-term messages and the user profile."""
        normalized_query, normalized_user_id, normalized_session_id = _normalize_context_request(
            query,
            user_id,
            session_id,
        )
        filters = {
            "user_id": normalized_user_id,
            "run_id": normalized_session_id,
        }
        session_scope = _build_session_scope(filters)
        short_term_limit = self._short_term_capacity()
        context_reader = (
            self._get_context_messages
            if getattr(self.db, "get_context_messages", None) is not None
            else self.db.get_last_messages
        )

        profile_result, short_term_messages = await asyncio.gather(
            self.get_profile(
                normalized_user_id,
                include_metadata=include_profile_metadata,
            ),
            asyncio.to_thread(
                context_reader,
                session_scope,
                short_term_limit,
            ),
        )

        return _assemble_retrieved_context(
            query=normalized_query,
            user_id=normalized_user_id,
            session_id=normalized_session_id,
            search_result=[],
            profile_result=profile_result,
            short_term_messages=short_term_messages,
        )

    async def build_agent_answer_messages(
        self,
        query: str,
        *,
        user_id: str,
        session_id: str,
        top_k: int = 20,
        threshold: Optional[float] = None,
        rerank: bool = False,
        explain: bool = False,
        include_profile_metadata: bool = False,
        reference_information: Any = None,
        agentic_generation_kwargs: Optional[Dict[str, Any]] = None,
    ) -> list[Dict[str, str]]:
        """Asynchronously build messages for an external LLM from layered memory context."""
        retrieved_context = await self._retrieve_context(
            query,
            user_id=user_id,
            session_id=session_id,
            top_k=top_k,
            threshold=threshold,
            rerank=rerank,
            explain=explain,
            include_profile_metadata=include_profile_metadata,
        )

        agentic_memory_supplement = ""
        agentic_config = getattr(getattr(self, "config", None), "agentic_retrieval", None)
        if agentic_config is not None and agentic_config.enabled:
            try:
                result = await self._run_agentic_retrieval_from_context(
                    retrieved_context,
                    reference_information=reference_information,
                    generation_kwargs=agentic_generation_kwargs,
                )
                agentic_memory_supplement = self._normalize_agentic_supplement_result(result)
            except Exception:
                logger.warning(
                    "Async Agentic retrieval failed while building answer messages; using the retrieved context only",
                    exc_info=True,
                )

        return build_answer_prompt_messages_from_context(
            retrieved_context,
            reference_information,
            agentic_memory_supplement=agentic_memory_supplement,
        )

    async def _run_agentic_retrieval_from_context(
        self,
        retrieved_context: Dict[str, Any],
        *,
        reference_information: Any = None,
        generation_kwargs: Optional[Dict[str, Any]] = None,
        record_midterm_visits: bool = True,
    ) -> Dict[str, Any]:
        """Run async Agentic retrieval using an already retrieved complete context."""
        messages = _build_agentic_prompt_messages(retrieved_context, reference_information)
        already_recalled_page_ids = {
            str(item.get("id"))
            for item in retrieved_context.get("retrieved_memories", [])
            if isinstance(item, dict)
            and item.get("source") in {"mid_term_page", "midterm"}
            and item.get("id") not in (None, "")
            and item.get("raw_dialogue")
        }
        return await self._run_agentic_retrieval_messages(
            messages,
            user_id=retrieved_context["user_id"],
            session_id=retrieved_context["session_id"],
            generation_kwargs=generation_kwargs,
            record_midterm_visits=record_midterm_visits,
            exclude_midterm_page_ids=already_recalled_page_ids,
        )

    async def _run_agentic_retrieval_messages(
        self,
        messages: list[Dict[str, Any]],
        *,
        user_id: str,
        session_id: str,
        generation_kwargs: Optional[Dict[str, Any]] = None,
        record_midterm_visits: bool = True,
        exclude_midterm_page_ids: Optional[set[str]] = None,
    ) -> Dict[str, Any]:
        """Run the async Agentic tool loop for prebuilt messages."""
        executor = self._create_agentic_tool_executor(
            user_id=user_id,
            session_id=session_id,
            record_midterm_visits=record_midterm_visits,
            exclude_midterm_page_ids=exclude_midterm_page_ids,
        )
        runner = AsyncAgenticMemoryRunner(
            self.llm,
            executor,
            self.config.agentic_retrieval,
            generation_kwargs=generation_kwargs,
        )
        return await runner.run(messages)

    def _create_agentic_tool_executor(
        self,
        *,
        user_id: str,
        session_id: str,
        record_midterm_visits: bool,
        exclude_midterm_page_ids: Optional[set[str]] = None,
    ) -> AsyncMemoryToolExecutor:
        """Create the async core Agentic tool executor."""
        return AsyncMemoryToolExecutor(
            self,
            user_id=user_id,
            run_id=session_id,
            config=self.config.agentic_retrieval,
            record_midterm_visits=record_midterm_visits,
            exclude_midterm_page_ids=exclude_midterm_page_ids,
        )

    async def run_agentic_retrieval(
        self,
        query: str,
        *,
        user_id: str,
        session_id: str,
        include_profile_metadata: bool = False,
        reference_information: Any = None,
        generation_kwargs: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Run the optional async two-call mid-term flow without writing messages."""
        if not self.config.agentic_retrieval.enabled:
            raise ValueError("Agentic retrieval is disabled; set agentic_retrieval.enabled=True")
        context = await self._retrieve_context(
            query,
            user_id=user_id,
            session_id=session_id,
            include_profile_metadata=include_profile_metadata,
        )
        return await self._run_agentic_retrieval_from_context(
            context,
            reference_information=reference_information,
            generation_kwargs=generation_kwargs,
        )

    async def update_profile(self, user_id: str, messages):
        """Explicitly extract and apply profile updates for a user."""
        normalized_user_id = normalize_profile_user_id(user_id)
        profile_config = getattr(self.config, "profile", None)
        if profile_config is None or profile_config.enabled is not True:
            raise ValueError(
                "User profile updates are disabled; set profile.enabled=True before calling update_profile"
            )

        user_lock = self._get_profile_user_thread_lock(normalized_user_id)
        async with _acquire_thread_lock_async(user_lock):
            plan = await self._generate_profile_update_plan(normalized_user_id, messages)
            if plan is None:
                return await asyncio.to_thread(self.profile_manager.get_profile, normalized_user_id)
            return await asyncio.to_thread(self.profile_manager.apply_update_plan, normalized_user_id, plan)

    async def delete_profile_value(self, user_id: str, attribute_key: str):
        """Delete one current profile value for a user."""
        normalized_user_id = normalize_profile_user_id(user_id)
        user_lock = self._get_profile_user_thread_lock(normalized_user_id)
        async with _acquire_thread_lock_async(user_lock):
            return await asyncio.to_thread(self.profile_manager.delete_value, normalized_user_id, attribute_key)

    async def delete_profile(self, user_id: str):
        """Delete all current profile values for a user."""
        normalized_user_id = normalize_profile_user_id(user_id)
        user_lock = self._get_profile_user_thread_lock(normalized_user_id)
        async with _acquire_thread_lock_async(user_lock):
            return await asyncio.to_thread(self.profile_manager.delete_profile, normalized_user_id)

    async def create_profile_attribute(self, definition):
        """Create a dynamically managed profile attribute definition."""
        return await asyncio.to_thread(self.profile_manager.create_attribute, definition)

    async def _generate_profile_update_plan(self, user_id, messages):
        normalized_user_id = normalize_profile_user_id(user_id)
        user_messages = select_profile_user_messages(messages, self.config.profile.max_input_user_messages)
        if not user_messages:
            return None
        current_profile = await asyncio.to_thread(self.profile_manager.get_profile, normalized_user_id)
        attribute_catalog = await asyncio.to_thread(self.profile_manager.list_attributes)
        request = {
            "current_profile": current_profile,
            "attribute_catalog": attribute_catalog,
            "messages": user_messages,
        }
        async_generate = getattr(self.profile_updater, "generate_update_plan_async", None)
        if inspect.iscoroutinefunction(async_generate):
            return await async_generate(**request)
        return await asyncio.to_thread(self.profile_updater.generate_update_plan, **request)

    async def _update_profile_after_add(self, user_id, messages):
        profile_config = getattr(self.config, "profile", None)
        if (
            profile_config is None
            or profile_config.enabled is not True
            or profile_config.update_on_add is not True
            or not user_id
        ):
            return

        try:
            normalized_user_id = normalize_profile_user_id(user_id)
            user_lock = self._get_profile_user_thread_lock(normalized_user_id)
            async with _acquire_thread_lock_async(user_lock):
                plan = await self._generate_profile_update_plan(normalized_user_id, messages)
                if plan is None:
                    return
                await asyncio.to_thread(self.profile_manager.apply_update_plan, normalized_user_id, plan)
        except Exception as exc:
            logger.warning("Automatic profile update failed for user %s: %s", user_id, exc)

    def _short_term_capacity(self):
        capacity = max(int(self.config.midterm.short_term_capacity), 0)
        if capacity % 2 != 0:
            capacity += 1
            self.config.midterm.short_term_capacity = capacity
            logger.warning(
                "midterm short_term_capacity should be even; using %s to preserve QA pairs.",
                capacity,
            )
        return capacity

    async def _expand_evicted_messages_for_qa_pairs(self, evicted_messages, session_scope):
        if not evicted_messages or evicted_messages[-1].get("role") != "user":
            return evicted_messages or []

        retained_messages = await asyncio.to_thread(self.db.get_messages, session_scope, 1)
        if not retained_messages or retained_messages[0].get("role") != "assistant":
            return evicted_messages

        assistant_message = retained_messages[0]
        await asyncio.to_thread(self.db.delete_messages, [assistant_message["id"]])
        return [*evicted_messages, assistant_message]

    async def _save_short_term_messages(self, messages, session_scope):
        evicted_messages = (
            await asyncio.to_thread(
                self.db.save_messages,
                messages,
                session_scope,
                self._short_term_capacity(),
                True,
            )
            or []
        )
        return await self._expand_evicted_messages_for_qa_pairs(evicted_messages, session_scope)

    async def _save_short_term_messages_with_completed_qa(self, messages, session_scope):
        evicted_messages, completed_qa = await asyncio.to_thread(
            self.db.save_messages_and_get_completed_qa,
            messages,
            session_scope,
            self._short_term_capacity(),
        )
        expanded = await self._expand_evicted_messages_for_qa_pairs(evicted_messages, session_scope)
        return expanded, completed_qa

    def _process_midterm_evictions(
        self,
        evicted_messages,
        filters,
        *,
        source_job_id=None,
        lease_token=None,
        lease_is_current=None,
        degraded=False,
        raise_on_error=False,
    ):
        if not self._midterm_enabled() or not evicted_messages:
            return []
        try:
            return self.midterm_updater.process_evicted_messages(
                evicted_messages,
                filters,
                source_job_id=source_job_id,
                lease_token=lease_token,
                lease_is_current=lease_is_current,
                degraded=degraded,
            )
        except Exception as e:
            if raise_on_error:
                raise
            logger.warning(f"Mid-term memory update failed: {e}")
            return []

    async def _process_midterm_evictions_async(
        self,
        evicted_messages,
        filters,
        *,
        source_job_id=None,
        lease_token=None,
        lease_is_current=None,
        degraded=False,
        raise_on_error=False,
    ):
        if not self._midterm_enabled() or not evicted_messages:
            return []
        try:
            return await self.midterm_updater.process_evicted_messages_async(
                evicted_messages,
                filters,
                source_job_id=source_job_id,
                lease_token=lease_token,
                lease_is_current=lease_is_current,
                degraded=degraded,
            )
        except Exception as exc:
            if raise_on_error:
                raise
            logger.warning("Mid-term memory update failed: %s", exc)
            return []

    def _with_midterm_search_results(self, query, filters, long_term_memories):
        if not self._midterm_enabled():
            return long_term_memories

        results = [{**memory, "source": "long_term"} for memory in long_term_memories]
        try:
            results.extend(self.midterm_retriever.search(query, filters))
        except Exception as e:
            logger.warning(f"Mid-term memory search failed: {e}")
        user_id = (filters or {}).get("user_id")
        if user_id and self._promoted_longterm_enabled():
            try:
                results.extend(
                    self.promoted_longterm.search(
                        query,
                        user_id=user_id,
                        top_k=int(self.config.promoted_longterm.top_k),
                        threshold=float(self.config.promoted_longterm.rag_threshold),
                    )
                )
            except Exception as e:
                logger.warning(f"Cross-session long-term memory search failed: {e}")
        return results

    def _reset_midterm_state(self):
        if self._midterm_memory is not None or self._midterm_enabled():
            try:
                self.midterm_memory.reset()
            except Exception as e:
                logger.warning(f"Failed to reset mid-term memory: {e}")
        self._midterm_memory = None
        self._midterm_updater = None
        self._midterm_retriever = None
        self._fine_grained_longterm_retriever = None
        cross_session_longterm = getattr(self, "_cross_session_longterm", None)
        if cross_session_longterm is not None:
            try:
                cross_session_longterm.reset()
            except Exception as e:
                logger.warning(f"Failed to reset cross-session long-term memory: {e}")
        self._cross_session_longterm = None

    @staticmethod
    def _normalize_entity_text(value: str) -> str:
        return " ".join(value.strip().lower().split())

    def _existing_entities_by_text(self, filters):
        """Return existing entity rows keyed by normalized payload data."""
        try:
            listed = self.entity_store.list(filters=filters, top_k=10000)
        except Exception as e:
            logger.debug(f"Exact entity lookup failed, falling back to semantic dedup: {e}")
            return {}

        rows_by_text = {}
        for row in _vector_store_list_rows(listed):
            payload = getattr(row, "payload", None) or {}
            text = payload.get("data")
            if not isinstance(text, str):
                continue
            normalized = self._normalize_entity_text(text)
            if normalized and normalized not in rows_by_text:
                rows_by_text[normalized] = row
        return rows_by_text

    async def _upsert_entity_async(self, entity_text, entity_type, memory_id, filters):
        """Async variant of `_upsert_entity` — per-entity search-then-update-or-insert."""
        try:
            entity_embedding = await asyncio.to_thread(self.embedding_model.embed, entity_text, "add")
            search_filters = _longterm_entity_filters(filters)
            exact_match = (await asyncio.to_thread(self._existing_entities_by_text, search_filters)).get(
                self._normalize_entity_text(entity_text)
            )

            existing = []
            if exact_match is None:
                existing = await asyncio.to_thread(
                    self.entity_store.search,
                    query=entity_text,
                    vectors=entity_embedding,
                    top_k=1,
                    filters=search_filters,
                )

            semantic_match = existing[0] if existing and existing[0].score >= 0.95 else None
            match = exact_match or semantic_match
            if match:
                payload = match.payload or {}
                linked_ids = payload.get("linked_memory_ids", [])
                if memory_id not in linked_ids:
                    payload = _update_entity_payload(payload, [*linked_ids, memory_id])
                    await asyncio.to_thread(
                        self.entity_store.update,
                        vector_id=match.id,
                        vector=None,
                        payload=payload,
                    )
            else:
                entity_id = str(uuid.uuid4())
                entity_payload = _new_entity_payload(
                    entity_text,
                    entity_type,
                    [memory_id],
                    search_filters,
                )
                await asyncio.to_thread(
                    self.entity_store.insert,
                    vectors=[entity_embedding],
                    ids=[entity_id],
                    payloads=[entity_payload],
                )
        except Exception as e:
            logger.warning(f"Entity upsert failed for '{entity_text}' (async): {e}")

    async def _bulk_clear_entity_store(self, filters):
        """Delete all entity records matching the given scope filters.

        Used by delete_all to avoid the race condition that occurs when
        concurrent _delete_memory coroutines each try to read-modify-write
        the same entity rows' linked_memory_ids lists.
        """
        if self._entity_store is None:
            return
        search_filters = _longterm_entity_filters(filters)
        try:
            listed = await asyncio.to_thread(self.entity_store.list, filters=search_filters, top_k=10000)
            rows = listed[0] if isinstance(listed, (list, tuple)) and listed and isinstance(listed[0], list) else listed
            for row in rows or []:
                try:
                    await asyncio.to_thread(self.entity_store.delete, vector_id=row.id)
                except Exception as e:
                    logger.debug(f"Bulk entity delete failed for id={row.id}: {e}")
        except Exception as e:
            logger.warning(f"Bulk entity store cleanup failed: {e}")

    async def _remove_memory_from_entity_store(self, memory_id, filters):
        """Async variant of `Memory._remove_memory_from_entity_store`."""
        if self._entity_store is None:
            return
        search_filters = _longterm_entity_filters(filters)
        try:
            listed = await asyncio.to_thread(self.entity_store.list, filters=search_filters, top_k=10000)
            rows = listed[0] if isinstance(listed, (list, tuple)) and listed and isinstance(listed[0], list) else listed
            for row in rows or []:
                try:
                    payload = getattr(row, "payload", None) or {}
                    linked = payload.get("linked_memory_ids", [])
                    if not isinstance(linked, list) or memory_id not in linked:
                        continue
                    remaining = [mid for mid in linked if mid != memory_id]
                    if not remaining:
                        try:
                            await asyncio.to_thread(self.entity_store.delete, vector_id=row.id)
                        except Exception as e:
                            logger.debug(f"Entity delete failed for id={row.id} (async): {e}")
                    else:
                        entity_text = payload.get("data")
                        if not isinstance(entity_text, str) or not entity_text:
                            logger.debug(f"Entity id={row.id} missing 'data'; skipping update during cleanup (async)")
                            continue
                        try:
                            vec = await asyncio.to_thread(self.embedding_model.embed, entity_text, "update")
                        except Exception as e:
                            logger.debug(f"Entity re-embed failed for '{entity_text}' (async): {e}")
                            continue
                        new_payload = _update_entity_payload(payload, remaining)
                        try:
                            await asyncio.to_thread(
                                self.entity_store.update,
                                vector_id=row.id,
                                vector=vec,
                                payload=new_payload,
                            )
                        except Exception as e:
                            logger.debug(f"Entity update failed for id={row.id} (async): {e}")
                except Exception as e:
                    logger.debug(f"Entity cleanup error (async): {e}")
        except Exception as e:
            logger.warning(f"Entity store cleanup failed for memory_id={memory_id} (async): {e}")

    async def _link_entities_for_memory(self, memory_id, text, filters):
        """Async variant of `Memory._link_entities_for_memory`."""
        try:
            entities = await asyncio.to_thread(self._run_entity_extraction, extract_entities, text)
            if not entities:
                return
            seen = set()
            for entity_type, entity_text in entities:
                key = self._normalize_entity_text(entity_text)
                if not key or key in seen:
                    continue
                seen.add(key)
                try:
                    await self._upsert_entity_async(entity_text, entity_type, memory_id, filters)
                except Exception as e:
                    logger.debug(f"Entity link failed for '{entity_text}' (async): {e}")
        except Exception as e:
            logger.warning(f"Entity linking failed for memory_id={memory_id} (async): {e}")

    @classmethod
    def from_config(cls, config_dict: Dict[str, Any]):
        try:
            config = MemoryConfig(**config_dict)
        except ValidationError as e:
            logger.error(f"Configuration validation error: {e}")
            raise
        return cls(config)

    def _should_use_agent_memory_extraction(self, messages, metadata):
        """Determine whether to use agent memory extraction based on the logic:
        - If agent_id is present and messages contain assistant role -> True
        - Otherwise -> False

        Args:
            messages: List of message dictionaries
            metadata: Metadata containing user_id, agent_id, etc.

        Returns:
            bool: True if should use agent memory extraction, False for user memory extraction
        """
        # Check if agent_id is present in metadata
        has_agent_id = metadata.get("agent_id") is not None

        # Check if there are assistant role messages
        has_assistant_messages = any(msg.get("role") == "assistant" for msg in messages)

        # Use agent memory extraction if agent_id is present and there are assistant messages
        return has_agent_id and has_assistant_messages

    async def add(
        self,
        messages,
        *,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        run_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        timestamp: Optional[Any] = None,
        expiration_date: Optional[Any] = None,
        infer: bool = True,
        memory_type: Optional[str] = None,
        prompt: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        llm=None,
    ):
        """
        Create a new memory asynchronously.

        Args:
            messages (str or List[Dict[str, str]]): Messages to store in the memory.
            user_id (str, optional): ID of the user creating the memory.
            agent_id (str, optional): ID of the agent creating the memory. Defaults to None.
            run_id (str, optional): ID of the run creating the memory. Defaults to None.
            metadata (dict, optional): Metadata to store with the memory. Defaults to None.
            timestamp (Any, optional): Platform-only temporal parameter. Not supported in OSS.
            expiration_date (Any, optional): Date in YYYY-MM-DD format. Expired memories are hidden
                from search and get_all unless show_expired is True.
            infer (bool, optional): Controls Fine-grained LongTerm extraction for each complete QA.
                If True (default), an LLM extracts facts from each complete QA. If False, the
                complete QA's non-system messages are stored directly. This behavior is independent
                of ShortTerm eviction.
            memory_type (str, optional): Type of memory to create. Defaults to None.
                                         Pass "procedural_memory" to create procedural memories.
            prompt (str, optional): Prompt to use for the memory creation. Defaults to None.
            idempotency_key (str, optional): Persisted key that makes a background add
                safe to retry with the same business inputs. Defaults to None.
            llm (BaseChatModel, optional): LLM class to use for generating procedural memories. Defaults to None. Useful when user is using LangChain ChatModel.
        Returns:
            dict: The same submission result as :meth:`Memory.add`; background job IDs indicate
                  durable enqueue only and do not imply that extraction has completed.
        """
        if timestamp is not None:
            raise ValueError(await get_temporal_feature_error_message_async("async", "add", "timestamp"))
        idempotency_key = _validate_and_trim_idempotency_key(idempotency_key)

        normalized_expiration_date = _normalize_expiration_date(expiration_date)
        temporal_usage_notice = detect_temporal_usage_from_metadata(metadata)
        processed_metadata, effective_filters = _build_filters_and_metadata(
            user_id=user_id, agent_id=agent_id, run_id=run_id, input_metadata=metadata
        )
        normalized_user_id = effective_filters.get("user_id")
        if normalized_expiration_date is not None:
            processed_metadata["expiration_date"] = normalized_expiration_date

        _validate_memory_type(memory_type)

        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]

        elif isinstance(messages, dict):
            messages = [messages]

        elif not isinstance(messages, list):
            raise Mem0ValidationError(
                message="messages must be str, dict, or list[dict]",
                error_code="VALIDATION_003",
                details={"provided_type": type(messages).__name__, "valid_types": ["str", "dict", "list[dict]"]},
                suggestion="Convert your input to a string, dictionary, or list of dictionaries.",
            )

        if agent_id is not None and memory_type == MemoryType.PROCEDURAL.value:
            if idempotency_key is not None:
                raise ValueError("idempotency_key is not supported for procedural memory adds")
            results = await self._create_procedural_memory(
                messages, metadata=processed_metadata, prompt=prompt, llm=llm
            )
            if self._background_config().enabled:
                profile_job_id = await asyncio.to_thread(
                    self._enqueue_profile_job_after_add,
                    normalized_user_id,
                    messages,
                )
            else:
                await self._update_profile_after_add(normalized_user_id, messages)
                profile_job_id = None
            results = {
                **results,
                "background": {
                    "migration_job_id": None,
                    "profile_job_id": profile_job_id,
                },
            }
            scale_threshold_notice = await asyncio.to_thread(detect_scale_threshold_from_add_result, self, results)
            if temporal_usage_notice:
                await display_temporal_usage_notice_async(self, "async", "add", *temporal_usage_notice)
            elif scale_threshold_notice:
                await display_scale_threshold_notice_async(self, "async", "add", *scale_threshold_notice)
            else:
                await display_first_run_notice_async(self, "async", "add")
            return results

        if self.config.llm.config.get("enable_vision"):
            messages = await asyncio.to_thread(
                parse_vision_messages,
                messages,
                self.llm,
                self.config.llm.config.get("vision_details"),
            )
        else:
            messages = await asyncio.to_thread(parse_vision_messages, messages)

        # Persist ShortTerm before either direct extraction or durable background job submission.
        session_scope = _build_session_scope(effective_filters)
        if not self._background_config().enabled:
            if idempotency_key is not None:
                raise ValueError("idempotency_key requires background task persistence")
            evicted_messages, completed_qa = await self._save_short_term_messages_with_completed_qa(
                messages,
                session_scope,
            )
            vector_store_result = []
            if evicted_messages:
                await asyncio.to_thread(self._process_midterm_evictions, evicted_messages, effective_filters)
            for qa_messages, qa_metadata in _completed_qa_longterm_inputs(completed_qa, processed_metadata):
                vector_store_result.extend(
                    await self._process_evicted_long_term_memories(
                        qa_messages,
                        qa_metadata,
                        effective_filters,
                        infer=infer,
                        prompt=prompt,
                    )
                )
            await self._update_profile_after_add(normalized_user_id, messages)
            scale_threshold_notice = await asyncio.to_thread(
                detect_scale_threshold_from_add_result, self, vector_store_result
            )
            if temporal_usage_notice:
                await display_temporal_usage_notice_async(self, "async", "add", *temporal_usage_notice)
            elif scale_threshold_notice:
                await display_scale_threshold_notice_async(self, "async", "add", *scale_threshold_notice)
            else:
                await display_first_run_notice_async(self, "async", "add")
            return {
                "results": vector_store_result,
                "background": {
                    "migration_job_id": None,
                    "profile_job_id": None,
                },
            }

        request_hash = (
            _memory_add_request_hash(
                messages=messages,
                filters=effective_filters,
                metadata=processed_metadata,
                infer=infer,
                memory_type=memory_type,
                prompt=prompt,
            )
            if idempotency_key is not None
            else None
        )
        migration_job_id, profile_job_id = await asyncio.to_thread(
            self._save_and_enqueue_background_jobs,
            messages,
            session_scope,
            processed_metadata,
            effective_filters,
            normalized_user_id=normalized_user_id,
            infer=infer,
            prompt=prompt,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        vector_store_result = []

        scale_threshold_notice = await asyncio.to_thread(
            detect_scale_threshold_from_add_result, self, vector_store_result
        )
        if temporal_usage_notice:
            await display_temporal_usage_notice_async(self, "async", "add", *temporal_usage_notice)
        elif scale_threshold_notice:
            await display_scale_threshold_notice_async(self, "async", "add", *scale_threshold_notice)
        else:
            await display_first_run_notice_async(self, "async", "add")
        return {
            "results": vector_store_result,
            "background": {
                "migration_job_id": migration_job_id,
                "profile_job_id": profile_job_id,
            },
        }

    async def _process_evicted_long_term_memories(
        self,
        evicted_messages: list,
        metadata: dict,
        effective_filters: dict,
        *,
        infer: bool = True,
        prompt: Optional[str] = None,
        source_job_id: Optional[str] = None,
        lease_token: Optional[str] = None,
        lease_is_current=None,
    ):
        if not infer:
            returned_memories = []
            for index, message_dict in enumerate(evicted_messages):
                if lease_is_current is not None and not lease_is_current():
                    raise RuntimeError("stale migration stage lease")
                if (
                    not isinstance(message_dict, dict)
                    or message_dict.get("role") is None
                    or message_dict.get("content") is None
                ):
                    logger.warning(f"Skipping invalid message format (async): {message_dict}")
                    continue

                if message_dict["role"] == "system":
                    continue

                per_msg_meta = deepcopy(metadata)
                per_msg_meta["role"] = message_dict["role"]
                if source_job_id:
                    per_msg_meta.update(
                        {
                            "source_job_id": source_job_id,
                            "source_stage": "longterm",
                            "output_state": "staging",
                            "output_lease_token": lease_token,
                        }
                    )

                actor_name = message_dict.get("name")
                if actor_name:
                    per_msg_meta["actor_id"] = actor_name

                msg_content = message_dict["content"]
                memory_id = (
                    str(uuid.uuid5(uuid.NAMESPACE_URL, f"mem0:longterm:{source_job_id}:{index}"))
                    if source_job_id
                    else None
                )
                vector_store = getattr(self, "vector_store", None)
                existing_memory = (
                    await asyncio.to_thread(vector_store.get, vector_id=memory_id)
                    if memory_id and vector_store is not None
                    else None
                )
                if existing_memory is not None:
                    existing_payload = dict(getattr(existing_memory, "payload", None) or {})
                    await asyncio.to_thread(
                        _update_vector_store_payload,
                        self.vector_store,
                        memory_id,
                        {
                            **existing_payload,
                            "source_job_id": source_job_id,
                            "source_stage": "longterm",
                            "output_state": "staging",
                            "output_lease_token": lease_token,
                        },
                    )
                    returned_memories.append(
                        {
                            "id": memory_id,
                            "memory": msg_content,
                            "event": "ADD",
                            "actor_id": actor_name if actor_name else None,
                            "role": message_dict["role"],
                        }
                    )
                    continue
                msg_embeddings = await asyncio.to_thread(self.embedding_model.embed, msg_content, "add")
                mem_id = await self._create_memory(
                    msg_content,
                    {msg_content: msg_embeddings},
                    per_msg_meta,
                    memory_id=memory_id,
                )

                returned_memories.append(
                    {
                        "id": mem_id,
                        "memory": msg_content,
                        "event": "ADD",
                        "actor_id": actor_name if actor_name else None,
                        "role": message_dict["role"],
                    }
                )
            return returned_memories

        # === V3 PHASED BATCH PIPELINE (async) ===

        # Phase 0: Context gathering
        session_scope = _build_session_scope(effective_filters)
        short_term_context = await asyncio.to_thread(
            self.db.get_last_messages,
            session_scope,
            self._short_term_capacity(),
        )
        parsed_evicted_messages = parse_messages(evicted_messages)
        session_summary, existing_related_memories = await asyncio.to_thread(
            _additive_midterm_context,
            self,
            parsed_evicted_messages,
            effective_filters,
            exclude_source_job_id=source_job_id,
        )

        # Phase 1: Existing memory retrieval
        search_filters = {k: v for k, v in effective_filters.items() if k in ("user_id", "agent_id", "run_id") and v}
        query_embedding = await asyncio.to_thread(self.embedding_model.embed, parsed_evicted_messages, "search")
        raw_existing_results = await asyncio.to_thread(
            self.vector_store.search,
            query=parsed_evicted_messages,
            vectors=query_embedding,
            top_k=10,
            filters=search_filters,
        )

        existing_results = [
            mem
            for mem in raw_existing_results
            if self._stage_output_is_visible(getattr(mem, "payload", None) or {}, "longterm")
        ]
        existing_long_term_memories = [
            {"id": str(mem.id), "text": mem.payload.get("data", "")} for mem in existing_results
        ]

        # Phase 2: LLM extraction (single call)
        is_agent_scoped = bool(effective_filters.get("agent_id")) and not effective_filters.get("user_id")
        system_prompt = _configured_fine_grained_extraction_prompt(self)
        if is_agent_scoped:
            system_prompt += AGENT_CONTEXT_SUFFIX

        custom_instr = prompt or self.custom_instructions

        user_prompt = generate_additive_extraction_prompt(
            new_messages=evicted_messages,
            session_summary=session_summary,
            existing_long_term_memories=existing_long_term_memories,
            existing_related_memories=existing_related_memories,
            short_term_context=short_term_context,
            custom_instructions=custom_instr,
        )

        try:
            request = {
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "response_format": {"type": "json_object"},
                **_configured_fine_grained_extraction_request_options(self),
            }
            async_generate = getattr(self.llm, "generate_response_async", None)
            if inspect.iscoroutinefunction(async_generate):
                response = await async_generate(**request)
            else:
                response = await asyncio.to_thread(self.llm.generate_response, **request)
        except Exception as e:
            # Re-raise so callers can implement provider fallback / retry
            # (see sync counterpart for rationale).
            logger.error(f"LLM extraction failed (async): {e}")
            raise LLMError(f"LLM extraction failed: {e}") from e

        extracted_memories = _parse_extracted_memories(response, strict=source_job_id is not None)

        if not extracted_memories:
            return []
        if lease_is_current is not None and not lease_is_current():
            raise RuntimeError("stale migration stage lease")

        # Phase 3: Determine which validated facts actually require a new write.
        existing_hashes, existing_source_hashes = _existing_longterm_hashes(raw_existing_results, source_job_id)

        pending_records = []
        seen_hashes = set()
        for mem in extracted_memories:
            text = mem["text"]
            mem_hash = _longterm_memory_hash(text)
            memory_id = _longterm_memory_id(source_job_id, text) if source_job_id else str(uuid.uuid4())
            existing_memory = (
                await asyncio.to_thread(self.vector_store.get, vector_id=memory_id) if source_job_id else None
            )
            if existing_memory is not None:
                existing_payload = _validated_existing_longterm_payload(
                    existing_memory,
                    source_job_id=source_job_id,
                    memory_id=memory_id,
                    expected_hash=mem_hash,
                )
                existing_payload.update(
                    {
                        "source_job_id": source_job_id,
                        "source_stage": "longterm",
                        "output_state": "staging",
                        "output_lease_token": lease_token,
                    }
                )
                await asyncio.to_thread(
                    _update_vector_store_payload,
                    self.vector_store,
                    memory_id,
                    existing_payload,
                )
                seen_hashes.add(mem_hash)
                continue
            existing_job_memory = existing_source_hashes.get(mem_hash)
            if existing_job_memory is not None:
                existing_job_payload = getattr(existing_job_memory, "payload", None) or {}
                existing_job_memory_id = str(existing_job_memory.id)
                existing_job_payload = {
                    **existing_job_payload,
                    "source_job_id": source_job_id,
                    "source_stage": "longterm",
                    "output_state": "staging",
                    "output_lease_token": lease_token,
                }
                await asyncio.to_thread(
                    _update_vector_store_payload,
                    self.vector_store,
                    existing_job_memory_id,
                    existing_job_payload,
                )
                seen_hashes.add(mem_hash)
                continue
            if mem_hash in existing_hashes or mem_hash in seen_hashes:
                logger.debug(f"Skipping duplicate memory (hash match, async): {text[:50]}")
                continue
            seen_hashes.add(mem_hash)
            pending_records.append((memory_id, text, mem_hash, mem))

        if not pending_records:
            return []

        # Phase 4: Embed only facts that survived deterministic-ID and hash deduplication.
        mem_texts = [record[1] for record in pending_records]
        embed_map = {}
        try:
            mem_embeddings_list = await asyncio.to_thread(self.embedding_model.embed_batch, mem_texts, "add")
            for text, embedding in zip(mem_texts, mem_embeddings_list):
                if _embedding_is_available(embedding):
                    embed_map[text] = embedding
        except Exception as exc:
            logger.warning("Batch long-term memory embedding failed (async); retrying individually: %s", exc)

        for text in _missing_embeddings(mem_texts, embed_map):
            try:
                embedding = await asyncio.to_thread(self.embedding_model.embed, text, "add")
                if _embedding_is_available(embedding):
                    embed_map[text] = embedding
            except Exception as exc:
                logger.warning("Failed to embed long-term memory text (async): %s", exc)

        missing_embeddings = _missing_embeddings(mem_texts, embed_map)
        if source_job_id and missing_embeddings:
            raise RuntimeError(f"Failed to embed {len(missing_embeddings)} extracted memories")

        # Phase 5: Build complete records. Non-background callers retain skip-on-failure compatibility.
        records = []
        for memory_id, text, mem_hash, mem in pending_records:
            if text not in embed_map:
                continue
            text_lemmatized = lemmatize_for_bm25(text, language=getattr(self, "_bm25_language", None))
            mem_metadata = deepcopy(metadata)
            mem_metadata["data"] = text
            mem_metadata["text_lemmatized"] = text_lemmatized
            mem_metadata["hash"] = mem_hash
            mem_metadata["created_at"] = (
                normalize_iso_timestamp_to_beijing(mem_metadata.get("created_at")) or beijing_now_iso()
            )
            mem_metadata["updated_at"] = mem_metadata["created_at"]
            if source_job_id:
                mem_metadata.update(
                    {
                        "source_job_id": source_job_id,
                        "source_stage": "longterm",
                        "output_state": "staging",
                        "output_lease_token": lease_token,
                    }
                )
            if mem.get("attributed_to"):
                mem_metadata["attributed_to"] = mem["attributed_to"]

            records.append((memory_id, text, embed_map[text], mem_metadata))

        if not records:
            return []
        if lease_is_current is not None and not lease_is_current():
            raise RuntimeError("stale migration stage lease")

        # Phase 6: Batch persist
        all_vectors = [r[2] for r in records]
        all_ids = [r[0] for r in records]
        all_payloads = [r[3] for r in records]

        insertion_errors = []
        persisted_records = []
        try:
            await asyncio.to_thread(
                self.vector_store.insert,
                vectors=all_vectors,
                ids=all_ids,
                payloads=all_payloads,
            )
            persisted_records = records
        except Exception:
            for record in records:
                mid, _, vec, pay = record
                try:
                    await asyncio.to_thread(self.vector_store.insert, vectors=[vec], ids=[mid], payloads=[pay])
                    persisted_records.append(record)
                except Exception as e:
                    logger.error(f"Failed to insert memory {mid} (async): {e}")
                    insertion_errors.append(e)

        # Batch history
        history_records = [
            {
                "memory_id": r[0],
                "old_memory": None,
                "new_memory": r[1],
                "event": "ADD",
                "created_at": r[3].get("created_at"),
                "is_deleted": 0,
            }
            for r in persisted_records
        ]
        if not source_job_id:
            try:
                await asyncio.to_thread(self.db.batch_add_history, history_records)
            except Exception:
                for hr in history_records:
                    try:
                        await asyncio.to_thread(
                            self.db.add_history,
                            hr["memory_id"],
                            None,
                            hr["new_memory"],
                            "ADD",
                            created_at=hr.get("created_at"),
                        )
                    except Exception as e:
                        logger.error(f"Failed to add history for {hr['memory_id']} (async): {e}")

        if insertion_errors and source_job_id:
            raise RuntimeError(f"Failed to insert {len(insertion_errors)} long-term memories") from insertion_errors[0]

        if source_job_id:
            return [{"id": r[0], "memory": r[1], "event": "ADD"} for r in records]

        # Phase 7: Batch entity linking
        entity_search_filters = _longterm_entity_filters(effective_filters)
        try:
            all_texts = [r[1] for r in records]
            all_entities = await asyncio.to_thread(
                self._run_entity_extraction,
                extract_entities_batch,
                all_texts,
            )

            # 7a: Global dedup
            global_entities = {}
            for idx, (memory_id, text, embedding, payload) in enumerate(records):
                entities = all_entities[idx] if idx < len(all_entities) else []
                for entity_type, entity_text in entities:
                    key = self._normalize_entity_text(entity_text)
                    if key in global_entities:
                        global_entities[key][2].add(memory_id)
                    else:
                        global_entities[key] = [entity_type, entity_text, {memory_id}]

            if global_entities:
                ordered_keys = list(global_entities.keys())
                entity_texts = [global_entities[k][1] for k in ordered_keys]

                # 7b: Batch embed entities
                try:
                    entity_embeddings = await asyncio.to_thread(self.embedding_model.embed_batch, entity_texts, "add")
                except Exception:
                    entity_embeddings = []
                    for t in entity_texts:
                        try:
                            entity_embeddings.append(await asyncio.to_thread(self.embedding_model.embed, t, "add"))
                        except Exception:
                            entity_embeddings.append(None)

                if len(entity_embeddings) != len(ordered_keys):
                    logger.warning(
                        "embed_batch returned %d vectors for %d entity texts — "
                        "padding/truncating to avoid dropping entity links",
                        len(entity_embeddings),
                        len(ordered_keys),
                    )
                    entity_embeddings = list(entity_embeddings[: len(ordered_keys)])
                    entity_embeddings += [None] * (len(ordered_keys) - len(entity_embeddings))

                valid = [(i, k) for i, k in enumerate(ordered_keys) if entity_embeddings[i] is not None]
                if valid:
                    valid_indices, valid_keys = zip(*valid)
                    valid_vectors = [entity_embeddings[i] for i in valid_indices]
                    exact_matches = await asyncio.to_thread(self._existing_entities_by_text, entity_search_filters)

                    # 7c: Batch search for existing entities
                    valid_texts = [global_entities[k][1] for k in valid_keys]
                    existing_matches = await asyncio.to_thread(
                        self.entity_store.search_batch,
                        queries=valid_texts,
                        vectors_list=valid_vectors,
                        top_k=1,
                        filters=entity_search_filters,
                    )

                    # 7d: Separate into inserts vs updates
                    to_insert_vectors, to_insert_ids, to_insert_payloads = [], [], []
                    for j, key in enumerate(valid_keys):
                        entity_type, entity_text, memory_ids = global_entities[key]
                        matches = existing_matches[j] if j < len(existing_matches) else []
                        exact_match = exact_matches.get(key)

                        semantic_match = matches[0] if matches and matches[0].score >= 0.95 else None
                        match = exact_match or semantic_match
                        if match:
                            payload = match.payload or {}
                            linked = set(payload.get("linked_memory_ids", []))
                            linked |= memory_ids
                            payload = _update_entity_payload(payload, linked)
                            try:
                                await asyncio.to_thread(
                                    self.entity_store.update,
                                    vector_id=match.id,
                                    vector=None,
                                    payload=payload,
                                )
                            except Exception as e:
                                logger.debug(f"Entity update failed for '{entity_text}' (async): {e}")
                        else:
                            to_insert_vectors.append(valid_vectors[j])
                            to_insert_ids.append(str(uuid.uuid4()))
                            to_insert_payloads.append(
                                _new_entity_payload(entity_text, entity_type, memory_ids, entity_search_filters)
                            )

                    # 7e: Batch insert new entities
                    if to_insert_vectors:
                        try:
                            await asyncio.to_thread(
                                self.entity_store.insert,
                                vectors=to_insert_vectors,
                                ids=to_insert_ids,
                                payloads=to_insert_payloads,
                            )
                        except Exception as e:
                            logger.warning(f"Batch entity insert failed (async): {e}")
        except Exception as e:
            logger.warning(f"Batch entity linking failed (async): {e}")

        returned_memories = [{"id": r[0], "memory": r[1], "event": "ADD"} for r in records]

        keys, encoded_ids = process_telemetry_filters(effective_filters)
        capture_event(
            "mem0.add",
            self,
            {"version": self.api_version, "keys": keys, "encoded_ids": encoded_ids, "sync_type": "async"},
        )
        return returned_memories

    async def _add_to_vector_store(
        self,
        messages: list,
        metadata: dict,
        effective_filters: dict,
        infer: bool,
        prompt: Optional[str] = None,
    ):
        """Compatibility alias treating ``messages`` as an already-evicted long-term batch."""
        return await self._process_evicted_long_term_memories(
            messages,
            metadata,
            effective_filters,
            infer=infer,
            prompt=prompt,
        )

    async def get(self, memory_id):
        """
        Retrieve a memory by ID asynchronously.

        Args:
            memory_id (str): ID of the memory to retrieve.

        Returns:
            dict: Retrieved memory.
        """
        capture_event("mem0.get", self, {"memory_id": memory_id, "sync_type": "async"})
        memory = await asyncio.to_thread(self.vector_store.get, vector_id=memory_id)
        if not memory or not self._stage_output_is_visible(
            getattr(memory, "payload", None) or {},
            "longterm",
        ):
            await display_first_run_notice_async(self, "async", "get")
            return None

        promoted_payload_keys = [
            "user_id",
            "agent_id",
            "run_id",
            "actor_id",
            "role",
            "attributed_to",
            "expiration_date",
        ]

        core_and_promoted_keys = {
            "data",
            "hash",
            "created_at",
            "updated_at",
            "id",
            "text_lemmatized",
            "attributed_to",
            *promoted_payload_keys,
        }

        result_item = MemoryItem(
            id=memory.id,
            memory=memory.payload.get("data", ""),
            hash=memory.payload.get("hash"),
            created_at=memory.payload.get("created_at"),
            updated_at=memory.payload.get("updated_at"),
        ).model_dump()

        for key in promoted_payload_keys:
            if key in memory.payload:
                result_item[key] = memory.payload[key]

        additional_metadata = {k: v for k, v in memory.payload.items() if k not in core_and_promoted_keys}
        if additional_metadata:
            result_item["metadata"] = additional_metadata

        await display_first_run_notice_async(self, "async", "get")
        return result_item

    async def get_all(
        self,
        *,
        filters: Optional[Dict[str, Any]] = None,
        top_k: int = 20,
        show_expired: bool = False,
        **kwargs,
    ):
        """
        List all memories.

        Args:
            filters (dict): Filter dict containing entity IDs and optional metadata filters.
                Must contain at least one of: user_id, agent_id, run_id.
                Example: filters={"user_id": "u1", "agent_id": "a1"}
            top_k (int, optional): The maximum number of memories to return. Defaults to 20.
            show_expired (bool, optional): Include expired memories. Defaults to False.

        Returns:
            dict: A dictionary containing a list of memories under the "results" key.
                  Example for v1.1+: `{"results": [{"id": "...", "memory": "...", ...}]}`

        Raises:
            ValueError: If filters doesn't contain at least one of user_id, agent_id, run_id,
                or if top_k is invalid.
        """
        # Reject top-level entity params - must use filters instead
        _reject_top_level_entity_params(kwargs, "get_all")

        # Validate top_k
        _validate_search_params(top_k=top_k)

        # Validate and trim entity IDs in filters
        effective_filters = dict(filters) if filters else {}
        if "user_id" in effective_filters:
            effective_filters["user_id"] = _validate_and_trim_entity_id(effective_filters["user_id"], "user_id")
        if "agent_id" in effective_filters:
            effective_filters["agent_id"] = _validate_and_trim_entity_id(effective_filters["agent_id"], "agent_id")
        if "run_id" in effective_filters:
            effective_filters["run_id"] = _validate_and_trim_entity_id(effective_filters["run_id"], "run_id")

        # Validate filters contains at least one entity ID
        if not any(key in effective_filters for key in ("user_id", "agent_id", "run_id")):
            raise ValueError(
                "filters must contain at least one of: user_id, agent_id, run_id. Example: filters={'user_id': 'u1'}"
            )

        limit = top_k
        fetch_limit = limit if show_expired else max(limit * 4, 60)
        scale_threshold_notice = detect_scale_threshold_from_top_k(top_k)

        keys, encoded_ids = process_telemetry_filters(effective_filters)
        capture_event(
            "mem0.get_all", self, {"limit": limit, "keys": keys, "encoded_ids": encoded_ids, "sync_type": "async"}
        )

        all_memories_result = await self._get_all_from_vector_store(effective_filters, fetch_limit, show_expired, limit)

        if scale_threshold_notice:
            await display_scale_threshold_notice_async(self, "async", "get_all", *scale_threshold_notice)
        else:
            await display_first_run_notice_async(self, "async", "get_all")
        return {"results": all_memories_result}

    async def _get_all_from_vector_store(self, filters, limit, show_expired=False, output_limit=None):
        memories_result = await asyncio.to_thread(self.vector_store.list, filters=filters, top_k=limit)

        # Handle different vector store return formats by inspecting first element
        if isinstance(memories_result, (tuple, list)) and len(memories_result) > 0:
            first_element = memories_result[0]

            # If first element is a container, unwrap one level
            if isinstance(first_element, (list, tuple)):
                actual_memories = first_element
            else:
                # First element is a memory object, structure is already flat
                actual_memories = memories_result
        else:
            actual_memories = memories_result

        promoted_payload_keys = [
            "user_id",
            "agent_id",
            "run_id",
            "actor_id",
            "role",
            "attributed_to",
            "expiration_date",
        ]
        core_and_promoted_keys = {
            "data",
            "hash",
            "created_at",
            "updated_at",
            "id",
            "text_lemmatized",
            "attributed_to",
            *promoted_payload_keys,
        }

        formatted_memories = []
        for mem in actual_memories:
            if not self._stage_output_is_visible(getattr(mem, "payload", None) or {}, "longterm"):
                continue
            if not show_expired and _payload_is_expired(mem.payload):
                continue
            memory_item_dict = MemoryItem(
                id=mem.id,
                memory=mem.payload.get("data", ""),
                hash=mem.payload.get("hash"),
                created_at=mem.payload.get("created_at"),
                updated_at=mem.payload.get("updated_at"),
            ).model_dump(exclude={"score"})

            for key in promoted_payload_keys:
                if key in mem.payload:
                    memory_item_dict[key] = mem.payload[key]

            additional_metadata = {k: v for k, v in mem.payload.items() if k not in core_and_promoted_keys}
            if additional_metadata:
                memory_item_dict["metadata"] = additional_metadata

            formatted_memories.append(memory_item_dict)
            if output_limit is not None and len(formatted_memories) >= output_limit:
                break

        return formatted_memories

    async def search(
        self,
        query: str,
        *,
        top_k: Optional[int] = None,
        filters: Optional[Dict[str, Any]] = None,
        threshold: Optional[float] = None,
        rerank: bool = False,
        explain: bool = False,
        reference_date: Optional[Any] = None,
        show_expired: bool = False,
        **kwargs,
    ):
        """
        Searches for memories based on a query.

        Args:
            query (str): Query to search for.
            top_k (int, optional): Maximum number of results to return. Defaults to 20.
            filters (dict): Filter dict containing entity IDs and optional metadata filters.
                Must contain at least one of: user_id, agent_id, run_id.
                Example: filters={"user_id": "u1", "agent_id": "a1"}

                Enhanced metadata filtering with operators:
                - {"key": "value"} - exact match
                - {"key": {"eq": "value"}} - equals
                - {"key": {"ne": "value"}} - not equals
                - {"key": {"in": ["val1", "val2"]}} - in list
                - {"key": {"nin": ["val1", "val2"]}} - not in list
                - {"key": {"gt": 10}} - greater than
                - {"key": {"gte": 10}} - greater than or equal
                - {"key": {"lt": 10}} - less than
                - {"key": {"lte": 10}} - less than or equal
                - {"key": {"contains": "text"}} - contains text
                - {"key": {"icontains": "text"}} - case-insensitive contains
                - {"key": "*"} - wildcard match (any value)
                - {"AND": [filter1, filter2]} - logical AND
                - {"OR": [filter1, filter2]} - logical OR
                - {"NOT": [filter1]} - logical NOT
            threshold (float, optional): Minimum score for a memory to be included. Defaults to 0.1.
            rerank (bool, optional): Whether to rerank results. Defaults to False.
            explain (bool, optional): Whether to include score_details for each result. Defaults to False.
            reference_date (Any, optional): Platform-only temporal parameter. Not supported in OSS.
            show_expired (bool, optional): Include expired memories. Defaults to False.

        Returns:
            dict: A dictionary containing the search results under a "results" key.
                  Example for v1.1+: `{"results": [{"id": "...", "memory": "...", "score": 0.8, ...}]}`

        Raises:
            ValueError: If filters doesn't contain at least one of user_id, agent_id, run_id,
                or if threshold/top_k values are invalid.
        """
        if reference_date is not None:
            raise ValueError(await get_temporal_feature_error_message_async("async", "search", "reference_date"))

        # Reject top-level entity params - must use filters instead
        _reject_top_level_entity_params(kwargs, "search")

        if top_k is None:
            top_k = int(_fine_grained_longterm_config(self).top_k)
        # Validate search parameters (before applying defaults)
        _validate_search_params(threshold=threshold, top_k=top_k)
        threshold = _effective_longterm_threshold(self, threshold)
        query = _validate_and_trim_search_query(query)
        temporal_usage_notice = detect_temporal_usage_from_search(query, filters)

        # Validate and trim entity IDs in filters
        effective_filters = filters.copy() if filters else {}
        if "user_id" in effective_filters:
            effective_filters["user_id"] = _validate_and_trim_entity_id(effective_filters["user_id"], "user_id")
        if "agent_id" in effective_filters:
            effective_filters["agent_id"] = _validate_and_trim_entity_id(effective_filters["agent_id"], "agent_id")
        if "run_id" in effective_filters:
            effective_filters["run_id"] = _validate_and_trim_entity_id(effective_filters["run_id"], "run_id")

        # Validate filters contains at least one entity ID
        if not any(key in effective_filters for key in ("user_id", "agent_id", "run_id")):
            raise ValueError(
                "filters must contain at least one of: user_id, agent_id, run_id. Example: filters={'user_id': 'u1'}"
            )

        limit = top_k
        scale_threshold_notice = detect_scale_threshold_from_top_k(top_k)

        # Apply enhanced metadata filtering if advanced operators are detected
        if self._has_advanced_operators(effective_filters):
            processed_filters = self._process_metadata_filters(effective_filters)
            # Remove logical/operator keys that have been reprocessed
            for logical_key in ("AND", "OR", "NOT"):
                effective_filters.pop(logical_key, None)
            for fk in list(effective_filters.keys()):
                if fk not in ("AND", "OR", "NOT", "user_id", "agent_id", "run_id") and isinstance(
                    effective_filters.get(fk), dict
                ):
                    effective_filters.pop(fk, None)
            effective_filters.update(processed_filters)

        keys, encoded_ids = process_telemetry_filters(effective_filters)
        capture_event(
            "mem0.search",
            self,
            {
                "limit": limit,
                "version": self.api_version,
                "keys": keys,
                "encoded_ids": encoded_ids,
                "sync_type": "async",
                "threshold": threshold,
                "explain": explain,
                "advanced_filters": bool(filters and self._has_advanced_operators(filters)),
            },
        )

        search_start = time.perf_counter()
        original_memories = await self._search_vector_store(
            query, effective_filters, limit, threshold, explain=explain, show_expired=show_expired
        )
        search_elapsed_seconds = time.perf_counter() - search_start

        # Apply reranking if enabled and reranker is available
        configured_fine_reranker = getattr(
            getattr(getattr(self, "config", None), "fine_grained_longterm", None),
            "reranker",
            None,
        )
        if (
            rerank
            and self.reranker
            and original_memories
            and getattr(configured_fine_reranker, "method", "none") == "none"
        ):
            try:
                rerank_async = getattr(self.reranker, "rerank_async", None)
                if callable(rerank_async):
                    reranked_memories = await rerank_async(query, original_memories, limit)
                else:
                    reranked_memories = await asyncio.to_thread(self.reranker.rerank, query, original_memories, limit)
                original_memories = reranked_memories
            except Exception as e:
                logger.warning(f"Reranking failed, using original results: {e}")

        original_memories = await asyncio.to_thread(
            self._with_midterm_search_results, query, effective_filters, original_memories
        )

        if temporal_usage_notice:
            await display_temporal_usage_notice_async(self, "async", "search", *temporal_usage_notice)
        elif scale_threshold_notice:
            await display_scale_threshold_notice_async(self, "async", "search", *scale_threshold_notice)
        elif search_elapsed_seconds > PERFORMANCE_SLOW_QUERY_THRESHOLD_SECONDS:
            await display_performance_slow_query_notice_async(
                self,
                "async",
                "search",
                search_elapsed_seconds,
                top_k,
                len(original_memories),
            )
        else:
            await display_first_run_notice_async(self, "async", "search")
        return {"results": original_memories}

    def _process_metadata_filters(self, metadata_filters: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process enhanced metadata filters and convert them to vector store compatible format.

        Args:
            metadata_filters: Enhanced metadata filters with operators

        Returns:
            Dict of processed filters compatible with vector store
        """
        processed_filters = {}

        def process_condition(key: str, condition: Any) -> Dict[str, Any]:
            if not isinstance(condition, dict):
                # Simple equality: {"key": "value"}
                if condition == "*":
                    # Wildcard: match everything for this field (implementation depends on vector store)
                    return {key: "*"}
                return {key: condition}

            result = {}
            for operator, value in condition.items():
                # Map platform operators to universal format that can be translated by each vector store
                operator_map = {
                    "eq": "eq",
                    "ne": "ne",
                    "gt": "gt",
                    "gte": "gte",
                    "lt": "lt",
                    "lte": "lte",
                    "in": "in",
                    "nin": "nin",
                    "contains": "contains",
                    "icontains": "icontains",
                }

                if operator in operator_map:
                    result.setdefault(key, {})[operator_map[operator]] = value
                else:
                    raise ValueError(f"Unsupported metadata filter operator: {operator}")
            return result

        def merge_filters(target: Dict[str, Any], source: Dict[str, Any]) -> None:
            """Merge source into target, deep-merging nested operator dicts for the same key."""
            for key, value in source.items():
                if key in target and isinstance(target[key], dict) and isinstance(value, dict):
                    target[key].update(value)
                else:
                    target[key] = value

        for key, value in metadata_filters.items():
            if key == "AND":
                # Logical AND: combine multiple conditions
                if not isinstance(value, list):
                    raise ValueError("AND operator requires a list of conditions")
                for condition in value:
                    for sub_key, sub_value in condition.items():
                        merge_filters(processed_filters, process_condition(sub_key, sub_value))
            elif key == "OR":
                # Logical OR: Pass through to vector store for implementation-specific handling
                if not isinstance(value, list) or not value:
                    raise ValueError("OR operator requires a non-empty list of conditions")
                # Store OR conditions in a way that vector stores can interpret
                processed_filters["$or"] = []
                for condition in value:
                    or_condition = {}
                    for sub_key, sub_value in condition.items():
                        merge_filters(or_condition, process_condition(sub_key, sub_value))
                    processed_filters["$or"].append(or_condition)
            elif key == "NOT":
                # Logical NOT: Pass through to vector store for implementation-specific handling
                if not isinstance(value, list) or not value:
                    raise ValueError("NOT operator requires a non-empty list of conditions")
                processed_filters["$not"] = []
                for condition in value:
                    not_condition = {}
                    for sub_key, sub_value in condition.items():
                        merge_filters(not_condition, process_condition(sub_key, sub_value))
                    processed_filters["$not"].append(not_condition)
            else:
                merge_filters(processed_filters, process_condition(key, value))

        return processed_filters

    def _has_advanced_operators(self, filters: Dict[str, Any]) -> bool:
        """
        Check if filters contain advanced operators that need special processing.

        Args:
            filters: Dictionary of filters to check

        Returns:
            bool: True if advanced operators are detected
        """
        if not isinstance(filters, dict):
            return False

        for key, value in filters.items():
            # Check for platform-style logical operators
            if key in ["AND", "OR", "NOT"]:
                return True
            # Check for comparison operators (without $ prefix for universal compatibility)
            if isinstance(value, dict):
                for op in value.keys():
                    if op in ["eq", "ne", "gt", "gte", "lt", "lte", "in", "nin", "contains", "icontains"]:
                        return True
            # Check for wildcard values
            if value == "*":
                return True
        return False

    async def _search_vector_store(self, query, filters, limit, threshold=0.1, explain=False, show_expired=False):
        return await self.fine_grained_longterm_retriever.search_async(
            query,
            filters,
            top_k=limit,
            threshold=threshold,
            explain=explain,
            show_expired=show_expired,
        )

    async def _compute_entity_boosts_async(self, query_entities, filters):
        """Compatibility entry point backed by the production FineGrainedLongTerm retriever."""
        return await self.fine_grained_longterm_retriever._entity_boosts_async(query_entities, filters)

    async def update(
        self,
        memory_id,
        data: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        expiration_date: Any = _UNSET,
    ):
        """
        Update a memory by ID asynchronously.

        Args:
            memory_id (str): ID of the memory to update.
            data (str, optional): New content to update the memory with.
            metadata (dict, optional): Metadata to update with the memory. Defaults to None.
            expiration_date (Any, optional): Date in YYYY-MM-DD format, or None to clear it.

        Returns:
            dict: Success message indicating the memory was updated.

        Example:
            >>> await m.update(memory_id="mem_123", data="Likes to play tennis on weekends")
            {'message': 'Memory updated successfully!'}
        """
        capture_event("mem0.update", self, {"memory_id": memory_id, "sync_type": "async"})

        if data is None and metadata is None and expiration_date is _UNSET:
            raise ValueError("At least one of data, metadata, or expiration_date must be provided.")

        update_metadata = deepcopy(metadata) if metadata is not None else None
        if expiration_date is not _UNSET:
            update_metadata = update_metadata or {}
            update_metadata["expiration_date"] = _normalize_expiration_date(expiration_date)

        existing_embeddings = {}
        if data is not None:
            embeddings = await asyncio.to_thread(self.embedding_model.embed, data, "update")
            existing_embeddings[data] = embeddings

        await self._update_memory(memory_id, data, existing_embeddings, update_metadata)
        await display_first_run_notice_async(self, "async", "update")
        return {"message": "Memory updated successfully!"}

    async def delete(self, memory_id):
        """
        Delete a memory by ID asynchronously.

        Args:
            memory_id (str): ID of the memory to delete.
        """
        capture_event("mem0.delete", self, {"memory_id": memory_id, "sync_type": "async"})

        existing_memory = await asyncio.to_thread(self.vector_store.get, vector_id=memory_id)
        if existing_memory is None:
            raise ValueError(f"Memory with id {memory_id} not found")

        await self._delete_memory(memory_id, existing_memory)
        decay_usage_notice = detect_decay_usage_from_delete()
        if decay_usage_notice:
            await display_decay_usage_notice_async(self, "async", "delete", *decay_usage_notice)
        else:
            await display_first_run_notice_async(self, "async", "delete")
        return {"message": "Memory deleted successfully!"}

    async def delete_all(self, user_id=None, agent_id=None, run_id=None):
        """
        Delete all memories asynchronously.

        Args:
            user_id (str, optional): ID of the user to delete memories for. Defaults to None.
            agent_id (str, optional): ID of the agent to delete memories for. Defaults to None.
            run_id (str, optional): ID of the run to delete memories for. Defaults to None.
        """
        user_id = _validate_and_trim_entity_id(user_id, "user_id")
        agent_id = _validate_and_trim_entity_id(agent_id, "agent_id")
        run_id = _validate_and_trim_entity_id(run_id, "run_id")

        filters = {}
        if user_id:
            filters["user_id"] = user_id
        if agent_id:
            filters["agent_id"] = agent_id
        if run_id:
            filters["run_id"] = run_id

        if not filters:
            raise ValueError(
                "At least one filter is required to delete all memories. If you want to delete all memories, use the `reset()` method."
            )

        keys, encoded_ids = process_telemetry_filters(filters)
        capture_event("mem0.delete_all", self, {"keys": keys, "encoded_ids": encoded_ids, "sync_type": "async"})
        memories = await asyncio.to_thread(self.vector_store.list, filters=filters)

        for memory in memories[0]:
            await self._delete_memory(memory.id)

        logger.info("Deleted %d memories", len(memories[0]))

        decay_usage_notice = detect_decay_usage_from_delete_all(len(memories[0]))
        if decay_usage_notice:
            await display_decay_usage_notice_async(self, "async", "delete_all", *decay_usage_notice)
        else:
            await display_first_run_notice_async(self, "async", "delete_all")
        return {"message": "Memories deleted successfully!"}

    async def history(self, memory_id):
        """
        Get the history of changes for a memory by ID asynchronously.

        Args:
            memory_id (str): ID of the memory to get history for.

        Returns:
            list: List of changes for the memory.
        """
        capture_event("mem0.history", self, {"memory_id": memory_id, "sync_type": "async"})
        memory = await asyncio.to_thread(self.vector_store.get, vector_id=memory_id)
        if memory is not None and not self._stage_output_is_visible(
            getattr(memory, "payload", None) or {},
            "longterm",
        ):
            return []
        history = await asyncio.to_thread(self.db.get_history, memory_id)
        await display_first_run_notice_async(self, "async", "history")
        return history

    async def _create_memory(self, data, existing_embeddings, metadata=None, memory_id=None):
        logger.debug(f"Creating memory with {data=}")
        if data in existing_embeddings:
            embeddings = existing_embeddings[data]
        else:
            embeddings = await asyncio.to_thread(self.embedding_model.embed, data, memory_action="add")

        memory_id = memory_id or str(uuid.uuid4())
        new_metadata = deepcopy(metadata) if metadata is not None else {}
        new_metadata["data"] = data
        new_metadata["hash"] = hashlib.md5(data.encode()).hexdigest()
        new_metadata["created_at"] = (
            normalize_iso_timestamp_to_beijing(new_metadata.get("created_at")) or beijing_now_iso()
        )
        new_metadata["updated_at"] = new_metadata["created_at"]
        new_metadata["text_lemmatized"] = lemmatize_for_bm25(data, language=getattr(self, "_bm25_language", None))

        await asyncio.to_thread(
            self.vector_store.insert,
            vectors=[embeddings],
            ids=[memory_id],
            payloads=[new_metadata],
        )

        if new_metadata.get("output_state") != "staging":
            await asyncio.to_thread(
                self.db.add_history,
                memory_id,
                None,
                data,
                "ADD",
                created_at=new_metadata.get("created_at"),
                updated_at=new_metadata.get("updated_at"),
                actor_id=new_metadata.get("actor_id"),
                role=new_metadata.get("role"),
            )

        return memory_id

    async def _create_procedural_memory(self, messages, metadata=None, llm=None, prompt=None):
        """
        Create a procedural memory asynchronously

        Args:
            messages (list): List of messages to create a procedural memory from.
            metadata (dict): Metadata to create a procedural memory from.
            llm (llm, optional): LLM to use for the procedural memory creation. Defaults to None.
            prompt (str, optional): Prompt to use for the procedural memory creation. Defaults to None.
        """
        logger.info("Creating procedural memory")

        parsed_messages = _build_procedural_memory_messages(messages, prompt)

        try:
            if llm is not None:
                try:
                    from langchain_core.messages.utils import convert_to_messages  # type: ignore
                except Exception:
                    logger.error(
                        "Import error while loading langchain-core. "
                        "Please install 'langchain-core' to use a custom LangChain procedural-memory model."
                    )
                    raise
                parsed_messages = convert_to_messages(parsed_messages)
                async_invoke = getattr(llm, "ainvoke", None)
                if inspect.iscoroutinefunction(async_invoke):
                    response = await async_invoke(input=parsed_messages)
                else:
                    response = await asyncio.to_thread(llm.invoke, input=parsed_messages)
                procedural_memory = remove_code_blocks(response.content)
            else:
                async_generate = None
                for method_name in ("generate_response_async", "agenerate_response"):
                    candidate = getattr(self.llm, method_name, None)
                    if inspect.iscoroutinefunction(candidate):
                        async_generate = candidate
                        break
                if async_generate is None:
                    procedural_memory = await asyncio.to_thread(
                        self.llm.generate_response,
                        messages=parsed_messages,
                    )
                else:
                    procedural_memory = await async_generate(messages=parsed_messages)
                procedural_memory = remove_code_blocks(procedural_memory)

        except Exception as e:
            logger.error(f"Error generating procedural memory summary: {e}")
            raise

        if metadata is None:
            raise ValueError("Metadata cannot be done for procedural memory.")

        metadata = {**metadata, "memory_type": MemoryType.PROCEDURAL.value}
        embeddings = await asyncio.to_thread(self.embedding_model.embed, procedural_memory, memory_action="add")
        memory_id = await self._create_memory(procedural_memory, {procedural_memory: embeddings}, metadata=metadata)
        capture_event("mem0._create_procedural_memory", self, {"memory_id": memory_id, "sync_type": "async"})

        result = {"results": [{"id": memory_id, "memory": procedural_memory, "event": "ADD"}]}

        return result

    async def _update_memory(self, memory_id, data, existing_embeddings, metadata=None):
        logger.info(f"Updating memory with {data=}")

        try:
            existing_memory = await asyncio.to_thread(self.vector_store.get, vector_id=memory_id)
        except Exception:
            # Backing-store failure, not a bad memory_id: re-raise the original so the REST layer maps it to 5xx, not 4xx.
            logger.error(f"Error getting memory with ID {memory_id} during update.")
            raise

        if existing_memory is None:
            raise ValueError(f"Memory with id {memory_id} not found. Please provide a valid 'memory_id'")

        prev_value = existing_memory.payload.get("data")
        if data is None:
            data = prev_value
        if not isinstance(data, str):
            raise ValueError(f"Memory with id {memory_id} does not have text content to update")
        text_changed = data != prev_value

        new_metadata = deepcopy(existing_memory.payload)
        if metadata is not None:
            new_metadata.update(metadata)

        new_metadata["data"] = data
        new_metadata["hash"] = hashlib.md5(data.encode()).hexdigest()
        new_metadata["text_lemmatized"] = lemmatize_for_bm25(data, language=getattr(self, "_bm25_language", None))
        new_metadata["created_at"] = normalize_iso_timestamp_to_beijing(existing_memory.payload.get("created_at"))
        new_metadata["updated_at"] = beijing_now_iso()

        # actor_id is immutable after creation (issue #4490)
        if "actor_id" in existing_memory.payload:
            new_metadata["actor_id"] = existing_memory.payload["actor_id"]

        if data in existing_embeddings:
            embeddings = existing_embeddings[data]
        else:
            embeddings = await asyncio.to_thread(self.embedding_model.embed, data, "update")

        await asyncio.to_thread(
            self.vector_store.update,
            vector_id=memory_id,
            vector=embeddings,
            payload=new_metadata,
        )
        logger.info(f"Updating memory with ID {memory_id=} with {data=}")

        await asyncio.to_thread(
            self.db.add_history,
            memory_id,
            prev_value,
            data,
            "UPDATE",
            created_at=new_metadata["created_at"],
            updated_at=new_metadata["updated_at"],
            actor_id=new_metadata.get("actor_id"),
            role=new_metadata.get("role"),
        )

        # Entity-store cleanup: strip this memory's id from old-text entities,
        # then re-extract entities from the new text and link them back.
        session_filters = {k: new_metadata[k] for k in ("user_id", "agent_id", "run_id") if new_metadata.get(k)}
        if text_changed:
            await self._remove_memory_from_entity_store(memory_id, session_filters)
            await self._link_entities_for_memory(memory_id, data, session_filters)

        return memory_id

    async def _delete_memory(self, memory_id, existing_memory=None, skip_entity_cleanup=False):
        logger.info(f"Deleting memory with {memory_id=}")
        if existing_memory is None:
            existing_memory = await asyncio.to_thread(self.vector_store.get, vector_id=memory_id)
            if existing_memory is None:
                raise ValueError(f"Memory with id {memory_id} not found. Please provide a valid 'memory_id'")
        prev_value = existing_memory.payload.get("data", "")
        created_at = normalize_iso_timestamp_to_beijing(existing_memory.payload.get("created_at"))
        updated_at = beijing_now_iso()
        payload = existing_memory.payload or {}
        session_filters = {k: payload[k] for k in ("user_id", "agent_id", "run_id") if payload.get(k)}

        await asyncio.to_thread(self.vector_store.delete, vector_id=memory_id)
        await asyncio.to_thread(
            self.db.add_history,
            memory_id,
            prev_value,
            None,
            "DELETE",
            created_at=created_at,
            updated_at=updated_at,
            actor_id=existing_memory.payload.get("actor_id"),
            role=existing_memory.payload.get("role"),
            is_deleted=1,
        )

        if not skip_entity_cleanup:
            await self._remove_memory_from_entity_store(memory_id, session_filters)

        return memory_id

    async def reset(self):
        """
        Reset the memory store asynchronously by:
            Deletes the vector store collection
            Resets the database
            Recreates the vector store with a new client
        """
        logger.warning("Resetting all memories")

        await asyncio.to_thread(self._pause_background_workers_for_reset)
        await asyncio.to_thread(self._reset_midterm_state)
        await asyncio.to_thread(self.db.reset)
        await asyncio.to_thread(self.db.close)
        self.db = await asyncio.to_thread(SQLiteManager, self.config.history_db_path)
        self._clear_profile_runtime_state()

        if hasattr(self.vector_store, "reset"):
            self.vector_store = await asyncio.to_thread(VectorStoreFactory.reset, self.vector_store)
        else:
            logger.warning("Vector store does not support reset. Skipping.")
            await asyncio.to_thread(self.vector_store.delete_col)
            self.vector_store = await asyncio.to_thread(
                VectorStoreFactory.create,
                self.config.vector_store.provider,
                self.config.vector_store.config,
                timeout_seconds=self.config.vector_store_timeout_seconds,
            )

        if self._entity_store is not None:
            try:
                await asyncio.to_thread(self._entity_store.reset)
            except Exception as e:
                logger.warning(f"Failed to reset entity store: {e}")
            self._entity_store = None

        await asyncio.to_thread(self._initialize_background_workers)
        capture_event("mem0.reset", self, {"sync_type": "async"})
        await display_first_run_notice_async(self, "async", "reset")

    async def flush_background_tasks(self, timeout: Optional[float] = None) -> bool:
        return await asyncio.to_thread(self._ensure_background_workers().flush, timeout)

    def close(self) -> bool:
        """Release resources held by this AsyncMemory instance."""
        return self._close_background_workers_and_db()

    async def chat(self, query):
        raise NotImplementedError("Chat function not implemented yet.")
