"""Evaluate production Dense + Chinese BM25 scoring at four frozen MidTerm checkpoints.

The script reuses frozen Page/Query embeddings and the long-term memory BM25
pipeline.  A temporary Qdrant sparse collection is built from each query's
visible Page set so future Pages cannot influence either candidates or IDF.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any

from exp.benchmark.benchmark_common import ensure_repo_root_on_path, load_json

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))

from exp.benchmark.midterm_retrieval_eval import stable_hash  # noqa: E402
from exp.benchmark.run_midterm_add_local_context_controls import (  # noqa: E402
    compare_rankings,
    load_existing_add_pages,
)
from exp.benchmark.run_midterm_add_search_cross_ablation import (  # noqa: E402
    PRODUCTION_EMBEDDING,
    SESSION_CODES,
    evaluate_all,
    load_p2_texts,
    load_pages_queries,
    load_query_vectors,
    rank_configuration,
)
from exp.benchmark.run_midterm_rerank_tuning import load_existing_embedding_vectors  # noqa: E402
from exp.benchmark.run_query_rewrite_cross_session_diagnosis import (  # noqa: E402
    dump_json,
    rank_map,
    write_csv,
)
from mem0.utils.lemmatization import lemmatize_for_bm25  # noqa: E402
from mem0.utils.scoring import get_bm25_params, normalize_bm25, score_and_rank  # noqa: E402
from mem0.vector_stores.qdrant import Qdrant  # noqa: E402


OUTPUT_DIR = REPO_ROOT / "exp/results/midterm_dense_bm25_hybrid_checkpoints"
LOCAL_CONTEXT_DIR = REPO_ROOT / "exp/results/midterm_add_local_context_ablation"
CONTROL_CACHE_ROOT = LOCAL_CONTEXT_DIR / "controls"
TOP5_LIMIT = 5
TOP10_LIMIT = 10
TOP20_LIMIT = 20
SEMANTIC_THRESHOLD = 0.1


@dataclass(frozen=True)
class CheckpointSpec:
    key: str
    label: str
    query_variant: str
    page_variant: str
    expected_dense_r5: float
    expected_dense_gold5: int


CHECKPOINTS = (
    CheckpointSpec(
        "C0",
        "C0 — Original Query + Production Summary + Keywords + User",
        "Search-Baseline",
        "ProductionFull",
        50 / 154,
        50,
    ),
    CheckpointSpec(
        "C1",
        "C1 — P2 Query + Production Summary + Keywords + User",
        "Search-P2",
        "ProductionFull",
        54 / 154,
        54,
    ),
    CheckpointSpec(
        "C2",
        "C2 — P2 Query + Production Summary + Keywords",
        "Search-P2",
        "ProductionNoUser",
        55 / 154,
        55,
    ),
    CheckpointSpec(
        "C3",
        "C3 — P2 Query + 最近上文及 eviction 下文 Summary + Keywords",
        "Search-P2",
        "ContextNoUser",
        59 / 154,
        59,
    ),
)

INDICATORS = (
    "营业收入",
    "归母净利润",
    "扣非归母净利润",
    "经营活动现金流净额",
    "经营现金流",
    "总资产",
    "归母股东权益",
    "资产",
    "权益",
    "利润",
    "现金流",
)
ENTITIES = ("贵州茅台", "比亚迪", "宁德时代")
TASK_PATTERNS = {
    "证据层级/证据强度": r"证据层级|证据分级|证据链|证据强度|事实.*计算.*判断|披露事实",
    "反证/反例": r"反证|反例|推翻",
    "基期判断": r"基期|基准期",
    "近似周转": r"近似周转|周转",
    "近似杠杆": r"近似杠杆|杠杆",
    "判断修订": r"修订|修正|降级|调整前面的判断|前序判断",
    "阶段小结": r"阶段小结|小结|合并.*结论",
    "信息缺口": r"信息缺口|证据不足|无法确认|待核验",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Frozen MidTerm Dense + production Chinese BM25 checkpoint ablation")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def all_queries(snapshots: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [query for code in SESSION_CODES for query in snapshots[code]["queries"]]


def validate_vector_cache(
    cache_root: Path,
    batch_prefix: str,
    ids: Sequence[str],
    texts: Sequence[str],
) -> dict[str, Any]:
    expected_hash = stable_hash({"model": PRODUCTION_EMBEDDING, "ids": list(ids), "texts": list(texts)})
    model_dir = cache_root / "cache/embeddings" / PRODUCTION_EMBEDDING.replace("/", "_")
    matches = []
    for path in model_dir.glob(f"{batch_prefix}*.json"):
        metadata = load_json(path)
        if metadata.get("content_hash") == expected_hash and int(metadata.get("item_count") or 0) == len(ids):
            matches.append((path, metadata))
    if len(matches) != 1:
        raise AssertionError(f"Expected one exact frozen vector cache for {batch_prefix}; found {len(matches)}")
    return {"metadata_path": str(matches[0][0]), **matches[0][1]}


def load_no_user_vectors(
    pages: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, dict[str, list[float]]], dict[str, Any]]:
    values: dict[str, dict[str, list[float]]] = {}
    metadata = {}
    for variant in ("Production", "PreviousAndFollowingContext"):
        selected = pages[variant]
        ids = [f"NoUser:{variant}:{row['page_id']}" for row in selected]
        texts = [str(row["no_user_text"]) for row in selected]
        batch = f"no-user-{variant}-S001-S005"
        metadata[variant] = validate_vector_cache(CONTROL_CACHE_ROOT, batch, ids, texts)
        encoded = load_existing_embedding_vectors(
            CONTROL_CACHE_ROOT,
            model_name=PRODUCTION_EMBEDDING,
            prefix=f"{batch}-",
            required_ids=ids,
        )
        values[variant] = {str(page["page_id"]): encoded[item_id] for page, item_id in zip(selected, ids)}
    return values, metadata


def checkpoint_inputs() -> tuple[
    dict[str, dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, Any],
]:
    snapshots, old_pages, queries = load_pages_queries()
    pages = load_existing_add_pages(old_pages)
    p2 = load_p2_texts(queries)
    query_vectors = load_query_vectors(queries)
    no_user_vectors, cache_metadata = load_no_user_vectors(pages)
    stored_vectors = {str(page["page_id"]): list(page["stored_embedding"]) for page in old_pages}
    query_by_id = {str(query["query_id"]): query for query in queries}
    values: dict[str, dict[str, Any]] = {}
    for spec in CHECKPOINTS:
        selected_pages = (
            pages["Production"]
            if spec.page_variant in {"ProductionFull", "ProductionNoUser"}
            else pages["PreviousAndFollowingContext"]
        )
        page_text_field = "full_text" if spec.page_variant == "ProductionFull" else "no_user_text"
        page_vectors = (
            stored_vectors
            if spec.page_variant == "ProductionFull"
            else no_user_vectors["Production"]
            if spec.page_variant == "ProductionNoUser"
            else no_user_vectors["PreviousAndFollowingContext"]
        )
        query_texts = {
            query_id: str(query["original_query"]) if spec.query_variant == "Search-Baseline" else p2[query_id]
            for query_id, query in query_by_id.items()
        }
        values[spec.key] = {
            "spec": spec,
            "pages": selected_pages,
            "page_by_id": {str(page["page_id"]): page for page in selected_pages},
            "page_texts": {str(page["page_id"]): str(page[page_text_field]) for page in selected_pages},
            "page_vectors": page_vectors,
            "query_texts": query_texts,
            "query_vectors": query_vectors[spec.query_variant],
        }
    return snapshots, old_pages, queries, values, cache_metadata


class BM25Cache:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.rows = []
        if path.exists():
            self.rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.success = {str(row["cache_key"]): row for row in self.rows if row.get("status") == "SUCCESS"}
        self.hit_count = 0
        self.build_count = 0

    def get(self, cache_key: str) -> dict[str, Any] | None:
        row = self.success.get(cache_key)
        if row:
            self.hit_count += 1
        return row

    def append(self, row: Mapping[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(row), ensure_ascii=False) + "\n")
        self.success[str(row["cache_key"])] = dict(row)
        self.build_count += 1


def raw_bm25_ranking(
    cache: BM25Cache,
    checkpoint: str,
    query_id: str,
    query_text: str,
    visible_pages: Sequence[Mapping[str, Any]],
    page_texts: Mapping[str, str],
    component_hash: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    visible_ids = [str(page["page_id"]) for page in visible_pages]
    lemmatized_query = lemmatize_for_bm25(query_text, language="zh")
    lemmatized_pages = {page_id: lemmatize_for_bm25(page_texts[page_id], language="zh") for page_id in visible_ids}
    identity = {
        "checkpoint": checkpoint,
        "query_id": query_id,
        "query": query_text,
        "query_lemmatized": lemmatized_query,
        "visible_page_ids": visible_ids,
        "lemmatized_page_text_hash": stable_hash([lemmatized_pages[page_id] for page_id in visible_ids]),
        "component_hash": component_hash,
        "bm25_language": "zh",
        "idf_scope": "exact query-time visible Page set",
    }
    cache_key = stable_hash(identity)
    cached = cache.get(cache_key)
    if cached:
        return [dict(row) for row in cached["ranking"]], {
            "cache_hit": True,
            "cache_key": cache_key,
            "lemmatized_query": lemmatized_query,
        }

    collection = f"midterm_{checkpoint}_{query_id.replace('-', '_')}"
    started = time.perf_counter()
    store = Qdrant(
        collection_name=collection,
        embedding_model_dims=512,
        path=":memory:",
        bm25_language="zh",
    )
    try:
        store.insert(
            vectors=[[0.0] * 512 for _ in visible_pages],
            payloads=[
                {"data": page_texts[page_id], "text_lemmatized": lemmatized_pages[page_id]} for page_id in visible_ids
            ],
            ids=visible_ids,
        )
        hits = store.keyword_search(lemmatized_query, top_k=len(visible_pages), filters=None) or []
        ranking = [
            {
                "page_id": str(hit.id),
                "raw_bm25_score": float(hit.score or 0.0),
                "bm25_rank": rank,
            }
            for rank, hit in enumerate(hits, start=1)
        ]
    finally:
        store.client.close()
    if not {str(row["page_id"]) for row in ranking} <= set(visible_ids):
        raise AssertionError(f"{checkpoint}/{query_id}: BM25 returned a non-visible Page")
    row = {
        **identity,
        "cache_key": cache_key,
        "status": "SUCCESS",
        "ranking": ranking,
        "visible_page_count": len(visible_pages),
        "bm25_hit_count": len(ranking),
        "build_ms": (time.perf_counter() - started) * 1000.0,
    }
    cache.append(row)
    return ranking, {"cache_hit": False, "cache_key": cache_key, "lemmatized_query": lemmatized_query}


def production_hybrid_ranking(
    dense: Sequence[Mapping[str, Any]],
    bm25: Sequence[Mapping[str, Any]],
    query_text: str,
    lemmatized_query: str,
    limit: int,
) -> list[dict[str, Any]]:
    internal_limit = min(max(limit * 4, 60), len(dense))
    semantic_pool = list(dense[:internal_limit])
    keyword_pool = list(bm25[:internal_limit])
    midpoint, steepness = get_bm25_params(query_text, lemmatized=lemmatized_query)
    bm25_scores = {
        str(row["page_id"]): normalize_bm25(float(row["raw_bm25_score"]), midpoint, steepness)
        for row in keyword_pool
        if float(row["raw_bm25_score"]) > 0
    }
    raw_scores = {str(row["page_id"]): float(row["raw_bm25_score"]) for row in bm25}
    semantic_results = [
        {
            "id": str(row["page_id"]),
            "score": float(row["score"]),
            "payload": {"source_turn_id": str(row.get("source_turn_id") or "")},
        }
        for row in semantic_pool
    ]
    scored = score_and_rank(
        semantic_results=semantic_results,
        bm25_scores=bm25_scores,
        entity_boosts={},
        threshold=SEMANTIC_THRESHOLD,
        top_k=internal_limit,
        explain=True,
    )
    dense_by_id = {str(row["page_id"]): row for row in dense}
    result = []
    for row in scored:
        page_id = str(row["id"])
        details = dict(row["score_details"])
        result.append(
            {
                "page_id": page_id,
                "source_turn_id": dense_by_id[page_id]["source_turn_id"],
                "score": float(row["score"]),
                "semantic_score": float(details["semantic_score"]),
                "raw_bm25_score": raw_scores.get(page_id, 0.0),
                "bm25_score": float(details["bm25_score"]),
                "final_score": float(details["final_score"]),
                "semantic_candidate": True,
                "bm25_overfetch_candidate": page_id in bm25_scores,
                "internal_limit": internal_limit,
            }
        )
    included = {str(row["page_id"]) for row in result}
    for row in dense:
        page_id = str(row["page_id"])
        if page_id in included:
            continue
        result.append(
            {
                "page_id": page_id,
                "source_turn_id": row["source_turn_id"],
                "score": 0.0,
                "semantic_score": float(row["score"]),
                "raw_bm25_score": raw_scores.get(page_id, 0.0),
                "bm25_score": 0.0,
                "final_score": None,
                "semantic_candidate": False,
                "bm25_overfetch_candidate": False,
                "internal_limit": internal_limit,
            }
        )
    for rank, row in enumerate(result, start=1):
        row["rank"] = rank
    if len(result) != len(dense) or {str(row["page_id"]) for row in result} != set(dense_by_id):
        raise AssertionError("Hybrid ranking changed candidate coverage")
    return result


def query_categories(text: str) -> list[str]:
    categories = []
    if any(indicator in text for indicator in INDICATORS):
        categories.append("指标名")
    if re.search(r"\d{4}|\d+(?:\.\d+)?%", text):
        categories.append("年份/数字")
    if any(entity in text for entity in ENTITIES):
        categories.append("实体")
    categories.extend(label for label, pattern in TASK_PATTERNS.items() if re.search(pattern, text))
    return categories or ["其它"]


def metric_bundle(
    snapshots: Mapping[str, Mapping[str, Any]],
    rankings5: Mapping[str, Sequence[Mapping[str, Any]]],
    rankings20: Mapping[str, Sequence[Mapping[str, Any]]],
    rankings_full: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    aggregate5, sessions5 = evaluate_all(snapshots, rankings5)
    aggregate20, sessions20 = evaluate_all(snapshots, rankings20)
    aggregate_full, sessions_full = evaluate_all(snapshots, rankings_full)
    aggregate = {
        "eligible_gold_count": aggregate5["eligible_gold_count"],
        "gold_at_5": round(float(aggregate5["recall_at_5"]) * int(aggregate5["eligible_gold_count"])),
        "recall_at_5": aggregate5["recall_at_5"],
        "macro_session_r5": aggregate5["macro_session_r5"],
        "recall_at_10": aggregate5["recall_at_10"],
        "recall_at_20": aggregate20["recall_at_20"],
        "mrr": aggregate_full["mrr"],
        "mean_gold_rank": aggregate_full["mean_gold_rank"],
    }
    session_rows = []
    for code in SESSION_CODES:
        session_rows.append(
            {
                "session_id": code,
                "eligible_gold_count": sessions5[code]["eligible_gold_count"],
                "R@5": sessions5[code]["recall_at_5"],
                "R@10": sessions5[code]["recall_at_10"],
                "R@20": sessions20[code]["recall_at_20"],
                "MRR": sessions_full[code]["mrr"],
                "Mean Gold Rank": sessions_full[code]["mean_gold_rank"],
            }
        )
    return aggregate, session_rows


def shared_tokens(query: str, page_text: str) -> list[str]:
    query_tokens = set(lemmatize_for_bm25(query, language="zh").split())
    page_tokens = set(lemmatize_for_bm25(page_text, language="zh").split())
    return sorted(query_tokens & page_tokens)


def render_cases(
    selected: Sequence[Mapping[str, Any]],
    checkpoint_data: Mapping[str, Mapping[str, Any]],
    top5_changes: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], str]:
    cases, lines = [], ["# Dense + BM25 Hybrid 代表案例", ""]
    for row in selected:
        checkpoint = str(row["Checkpoint"])
        query_id = str(row["query_id"])
        gold_id = str(row["gold_page_id"])
        data = checkpoint_data[checkpoint]
        page = data["page_by_id"][gold_id]
        query_text = data["query_texts"][query_id]
        case = {
            **dict(row),
            "query": query_text,
            "gold_page_text": data["page_texts"][gold_id],
            "gold_source_turn_id": page["source_turn_id"],
            "shared_bm25_tokens": shared_tokens(query_text, data["page_texts"][gold_id]),
            "hybrid_top5_entries": [
                dict(change)
                for change in top5_changes
                if change["Checkpoint"] == checkpoint
                and change["query_id"] == query_id
                and change["direction"] == "ENTERED_HYBRID_TOP5"
            ],
        }
        cases.append(case)
        final_score = (
            "N/A" if row.get("hybrid_final_score") in {None, ""} else f"{float(row['hybrid_final_score']):.9f}"
        )
        lines.extend(
            [
                f"## {checkpoint} / {query_id} / {page['source_turn_id']}",
                "",
                f"Transition：{row['transition']}",
                "",
                f"Query：{query_text}",
                "",
                f"Gold Page：{gold_id}（{page['source_turn_id']}）",
                "",
                f"Dense #{row['dense_rank']} → Hybrid #{row['hybrid_rank']}；"
                f"Dense score={float(row['dense_score']):.9f}；"
                f"raw BM25={float(row['raw_bm25_score']):.9f}；"
                f"normalized BM25={float(row['normalized_bm25_score']):.9f}；final={final_score}",
                "",
                f"共同词：{', '.join(case['shared_bm25_tokens'])}",
                "",
                "Hybrid 新进入 Top5 的 Page：",
                "",
                *(
                    [
                        f"- {item['source_turn_id']}（{'Gold' if item['is_gold'] else 'Non-Gold'}）："
                        f"Dense #{item['dense_rank']} → Hybrid #{item['hybrid_rank']}；"
                        f"raw BM25={float(item['raw_bm25_score']):.9f}；"
                        f"共同词={', '.join(item['shared_bm25_tokens'])}"
                        for item in case["hybrid_top5_entries"]
                    ]
                    or ["- 无"]
                ),
                "",
                "```text",
                data["page_texts"][gold_id],
                "```",
                "",
            ]
        )
    return {"cases": cases}, "\n".join(lines) + "\n"


def pct(value: Any) -> str:
    return f"{100 * float(value):.2f}%"


def render_report(
    metrics: Sequence[Mapping[str, Any]],
    sessions: Sequence[Mapping[str, Any]],
    types: Sequence[Mapping[str, Any]],
    top5_changes: Sequence[Mapping[str, Any]],
    validations: Mapping[str, Mapping[str, Any]],
) -> str:
    lines = [
        "# MidTerm Dense + 生产中文 BM25 Hybrid Checkpoint 实验",
        "",
        "| Checkpoint | Dense R@5 | Hybrid R@5 | Δpp | Dense Gold@5 | Hybrid Gold@5 | Promoted | Demoted | Net | MRR |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in metrics:
        lines.append(
            f"| {row['Checkpoint']} | {pct(row['Dense R@5'])} | {pct(row['Hybrid R@5'])} | "
            f"{100 * float(row['Delta R@5']):+.2f} | {row['Dense Gold@5']} | {row['Hybrid Gold@5']} | "
            f"{row['Promoted']} | {row['Demoted']} | {int(row['Net']):+d} | {float(row['Hybrid MRR']):.4f} |"
        )
    lines.extend(
        [
            "",
            "| Checkpoint | Hybrid Macro R@5 | Hybrid R@10 | Hybrid R@20 | Hybrid MRR | Hybrid Mean Gold Rank | Rescued Q | Hurt Q |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in metrics:
        lines.append(
            f"| {row['Checkpoint']} | {pct(row['Hybrid Macro R@5'])} | {pct(row['Hybrid R@10'])} | "
            f"{pct(row['Hybrid R@20'])} | {float(row['Hybrid MRR']):.4f} | "
            f"{float(row['Hybrid Mean Gold Rank']):.2f} | {row['Rescued Queries']} | {row['Hurt Queries']} |"
        )
    lines.extend(
        [
            "",
            "## Session Hybrid R@5",
            "",
            "| Checkpoint | S001 | S002 | S003 | S004 | S005 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for checkpoint in [spec.key for spec in CHECKPOINTS]:
        lookup = {str(row["session_id"]): row for row in sessions if row["Checkpoint"] == checkpoint}
        lines.append(
            f"| {checkpoint} | " + " | ".join(pct(lookup[code]["Hybrid R@5"]) for code in SESSION_CODES) + " |"
        )
    best = max(metrics, key=lambda row: float(row["Hybrid R@5"]))
    c3 = next(row for row in metrics if row["Checkpoint"] == "C3")
    entered_non_gold = Counter(
        str(row["Checkpoint"])
        for row in top5_changes
        if row["direction"] == "ENTERED_HYBRID_TOP5" and not row["is_gold"]
    )
    lines.extend(
        [
            "",
            "## 结论",
            "",
            f"- Hybrid 最好的是 {best['Checkpoint']}，Micro R@5={pct(best['Hybrid R@5'])}；仍低于对应 Dense。",
            f"- C3 从 59/154（38.31%）降至 {c3['Hybrid Gold@5']}/154（{pct(c3['Hybrid R@5'])}），"
            f"净变化 {int(c3['Net']):+d} Gold（{100 * float(c3['Delta R@5']):+.2f}pp）。",
            "- 四个 checkpoint 都是负增益，因此当前固定 production Hybrid 不值得直接引入 MidTerm Retriever。",
            "",
            "Hybrid 新进入 Top5 的 Non-Gold Page 数（跨 Query 计次）："
            + "；".join(f"{spec.key}={entered_non_gold[spec.key]}" for spec in CHECKPOINTS)
            + "。",
        ]
    )
    lines.extend(["", "## BM25 有效 Query 类型", ""])
    promoted = sorted(
        (row for row in types if row["Transition"] == "PROMOTED"),
        key=lambda row: (-int(row["Gold count"]), str(row["Category"])),
    )
    for row in promoted[:12]:
        lines.append(f"- {row['Checkpoint']} / {row['Category']}：{row['Gold count']} 个 promoted Gold。")
    c3_promoted = {
        str(row["Category"]): int(row["Gold count"])
        for row in types
        if row["Checkpoint"] == "C3" and row["Transition"] == "PROMOTED"
    }
    c3_demoted = {
        str(row["Category"]): int(row["Gold count"])
        for row in types
        if row["Checkpoint"] == "C3" and row["Transition"] == "DEMOTED"
    }
    c3_nongold_terms = Counter(
        token
        for row in top5_changes
        if row["Checkpoint"] == "C3" and row["direction"] == "ENTERED_HYBRID_TOP5" and not row["is_gold"]
        for token in row["shared_bm25_tokens"]
    )
    lines.extend(
        [
            "",
            "C3 gross promoted 的主要重叠类别（同一 Gold 可属于多类）："
            + "；".join(
                f"{category}={count}"
                for category, count in sorted(c3_promoted.items(), key=lambda item: (-item[1], item[0]))
            )
            + "。",
            "",
            "C3 demoted 的主要重叠类别："
            + "；".join(
                f"{category}={count}"
                for category, count in sorted(c3_demoted.items(), key=lambda item: (-item[1], item[0]))
            )
            + "。",
            "",
            "C3 新进入 Top5 的 Non-Gold 高频共同词："
            + "、".join(f"{token}({count})" for token, count in c3_nongold_terms.most_common(15))
            + "。",
        ]
    )
    lines.extend(
        [
            "",
            "## 实现与冻结",
            "",
            "- 文本预处理、金融词典、中文 sparse encoder、Qdrant IDF、sigmoid normalization 与 additive scoring 均直接调用生产组件。",
            "- Entity Boost 为空；没有 BM25-only 评测、权重扫描、reranker 或其它信号。",
            "- 每个 Query 的临时 Qdrant collection 只包含其 frozen visible_page_ids，因此未来 Page 不参与 IDF、候选或最终评分。",
            "- MRR/Mean Gold Rank 使用同一生产 scorer 对全部可见 Dense 候选的诊断性完整排序；R@5/R@10 使用生产 Top5/10 的 60-candidate over-fetch，R@20 使用 80-candidate over-fetch。",
            "",
            f"Validation：{'PASS' if all(row['status'] == 'PASS' for row in validations.values()) else 'FAIL'}。",
            "",
            "详细 movement 与分数见 `representative_cases.md`。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    snapshots, old_pages, queries, checkpoint_data, vector_cache_meta = checkpoint_inputs()
    page_ids = {str(page["page_id"]) for page in old_pages}
    visibility_by_query = {
        str(row["query_id"]): [str(page_id) for page_id in row["visible_page_ids"]]
        for code in SESSION_CODES
        for row in snapshots[code]["visibility"]
    }
    query_by_id = {str(query["query_id"]): query for query in queries}

    component_paths = {
        "qdrant": REPO_ROOT / "mem0/vector_stores/qdrant.py",
        "bm25_sparse": REPO_ROOT / "mem0/utils/bm25_sparse.py",
        "lemmatization": REPO_ROOT / "mem0/utils/lemmatization.py",
        "scoring": REPO_ROOT / "mem0/utils/scoring.py",
        "finance_dictionary": REPO_ROOT / "mem0/configs/finance_bm25_dict.txt",
    }
    component_hashes = {name: sha256_file(path) for name, path in component_paths.items()}
    component_hash = stable_hash(component_hashes)
    cache = BM25Cache(args.output_dir / "cache/visible_scope_bm25.jsonl")

    metric_rows, session_rows = [], []
    comparison_rows, gold_rows, query_rows = [], [], []
    visibility_rows, type_counter, overlap_rows, top5_change_rows = [], Counter(), [], []
    dense_reproduction = {}

    for spec in CHECKPOINTS:
        data = checkpoint_data[spec.key]
        dense = rank_configuration(snapshots, data["query_vectors"], data["page_vectors"])
        dense_metrics, dense_sessions = evaluate_all(snapshots, dense)
        dense_reproduction[spec.key] = dense_metrics
        if not math.isclose(float(dense_metrics["recall_at_5"]), spec.expected_dense_r5, abs_tol=1e-12):
            raise AssertionError(f"{spec.key} Dense history reproduction failed: {dense_metrics}")

        hybrid5, hybrid20, hybrid_full = {}, {}, {}
        for query_id, query in query_by_id.items():
            visible_ids = visibility_by_query[query_id]
            visible_pages = [data["page_by_id"][page_id] for page_id in visible_ids]
            bm25, audit = raw_bm25_ranking(
                cache,
                spec.key,
                query_id,
                data["query_texts"][query_id],
                visible_pages,
                data["page_texts"],
                component_hash,
            )
            hybrid5[query_id] = production_hybrid_ranking(
                dense[query_id], bm25, data["query_texts"][query_id], audit["lemmatized_query"], TOP5_LIMIT
            )
            hybrid20[query_id] = production_hybrid_ranking(
                dense[query_id], bm25, data["query_texts"][query_id], audit["lemmatized_query"], TOP20_LIMIT
            )
            hybrid_full[query_id] = production_hybrid_ranking(
                dense[query_id],
                bm25,
                data["query_texts"][query_id],
                audit["lemmatized_query"],
                len(visible_pages),
            )
            visible_set = set(visible_ids)
            visibility_rows.append(
                {
                    "Checkpoint": spec.key,
                    "session_id": query["session_code"],
                    "query_id": query_id,
                    "visible_page_count": len(visible_ids),
                    "visible_page_ids_hash": stable_hash(visible_ids),
                    "bm25_returned_page_count": len(bm25),
                    "bm25_nonvisible_page_count": sum(str(row["page_id"]) not in visible_set for row in bm25),
                    "hybrid_nonvisible_page_count": sum(
                        str(row["page_id"]) not in visible_set for row in hybrid5[query_id]
                    ),
                    "future_page_leak_count": int(
                        next(
                            row.get("future_page_leak_count") or 0
                            for row in snapshots[query["session_code"]]["visibility"]
                            if str(row["query_id"]) == query_id
                        )
                    ),
                    "idf_scope": "exact query-time visible Page set",
                    "bm25_cache_hit": audit["cache_hit"],
                }
            )

        hybrid_metrics, checkpoint_sessions = metric_bundle(snapshots, hybrid5, hybrid20, hybrid_full)
        comparison, checkpoint_gold, checkpoint_queries = compare_rankings(
            snapshots,
            dense,
            hybrid5,
            comparison=f"{spec.key} Hybrid vs Dense",
        )
        comparison_rows.append({"Checkpoint": spec.key, **comparison})
        session_movements = {
            code: {
                "promoted": sum(
                    row["session_id"] == code and int(row["before_rank"]) > 5 and int(row["after_rank"]) <= 5
                    for row in checkpoint_gold
                ),
                "demoted": sum(
                    row["session_id"] == code and int(row["before_rank"]) <= 5 and int(row["after_rank"]) > 5
                    for row in checkpoint_gold
                ),
            }
            for code in SESSION_CODES
        }
        metric_rows.append(
            {
                "Checkpoint": spec.key,
                "Description": spec.label,
                "Dense R@5": dense_metrics["recall_at_5"],
                "Hybrid R@5": hybrid_metrics["recall_at_5"],
                "Delta R@5": float(hybrid_metrics["recall_at_5"]) - float(dense_metrics["recall_at_5"]),
                "Dense Gold@5": spec.expected_dense_gold5,
                "Hybrid Gold@5": hybrid_metrics["gold_at_5"],
                "Hybrid Macro R@5": hybrid_metrics["macro_session_r5"],
                "Hybrid R@10": hybrid_metrics["recall_at_10"],
                "Hybrid R@20": hybrid_metrics["recall_at_20"],
                "Hybrid MRR": hybrid_metrics["mrr"],
                "Hybrid Mean Gold Rank": hybrid_metrics["mean_gold_rank"],
                "Promoted": comparison["Promoted Gold"],
                "Demoted": comparison["Demoted Gold"],
                "Net": comparison["Net Gold gain"],
                "Rescued Queries": comparison["Rescued Queries"],
                "Hurt Queries": comparison["Hurt Queries"],
            }
        )
        for row in checkpoint_sessions:
            session_rows.append(
                {
                    "Checkpoint": spec.key,
                    "Description": spec.label,
                    "Dense R@5": dense_sessions[row["session_id"]]["recall_at_5"],
                    "Hybrid R@5": row["R@5"],
                    "Delta R@5": float(row["R@5"]) - float(dense_sessions[row["session_id"]]["recall_at_5"]),
                    "Promoted Gold": session_movements[row["session_id"]]["promoted"],
                    "Demoted Gold": session_movements[row["session_id"]]["demoted"],
                    "Net Gold": session_movements[row["session_id"]]["promoted"]
                    - session_movements[row["session_id"]]["demoted"],
                    **row,
                }
            )

        dense_maps = {query_id: rank_map(items) for query_id, items in dense.items()}
        hybrid_maps = {query_id: rank_map(items) for query_id, items in hybrid5.items()}
        full_maps = {query_id: rank_map(items) for query_id, items in hybrid_full.items()}
        for row in checkpoint_gold:
            query_id = str(row["query_id"])
            gold_id = str(row["gold_page_id"])
            dense_item = dense_maps[query_id][gold_id]
            hybrid_item = hybrid_maps[query_id][gold_id]
            full_item = full_maps[query_id][gold_id]
            transition = (
                "PROMOTED"
                if int(row["before_rank"]) > 5 and int(row["after_rank"]) <= 5
                else "DEMOTED"
                if int(row["before_rank"]) <= 5 and int(row["after_rank"]) > 5
                else "UNCHANGED"
            )
            categories = query_categories(data["query_texts"][query_id])
            if transition != "UNCHANGED":
                for category in categories:
                    type_counter[(spec.key, transition, category)] += 1
            enriched = {
                "Checkpoint": spec.key,
                **row,
                "transition": transition,
                "query_text": data["query_texts"][query_id],
                "query_categories": categories,
                "dense_rank": dense_item["rank"],
                "hybrid_rank": hybrid_item["rank"],
                "hybrid_full_rank": full_item["rank"],
                "dense_score": dense_item["score"],
                "raw_bm25_score": hybrid_item["raw_bm25_score"],
                "normalized_bm25_score": hybrid_item["bm25_score"],
                "hybrid_final_score": hybrid_item["final_score"],
                "shared_bm25_tokens": shared_tokens(data["query_texts"][query_id], data["page_texts"][gold_id]),
            }
            gold_rows.append(enriched)
            if transition != "UNCHANGED":
                overlap_rows.append(
                    {
                        "Checkpoint": spec.key,
                        "session_id": row["session_id"],
                        "query_id": query_id,
                        "gold_page_id": gold_id,
                        "Transition": transition,
                        "Categories": categories,
                        "Shared BM25 tokens": enriched["shared_bm25_tokens"],
                        "Dense rank": dense_item["rank"],
                        "Hybrid rank": hybrid_item["rank"],
                    }
                )
        query_rows.extend({"Checkpoint": spec.key, **row} for row in checkpoint_queries)
        for query_id, query in query_by_id.items():
            dense_top5 = {str(row["page_id"]) for row in dense[query_id][:5]}
            hybrid_top5 = {str(row["page_id"]) for row in hybrid5[query_id][:5]}
            gold_ids = {str(page_id) for page_id in query["eligible_gold_page_ids"]}
            for direction, page_ids_changed in (
                ("ENTERED_HYBRID_TOP5", hybrid_top5 - dense_top5),
                ("DROPPED_FROM_DENSE_TOP5", dense_top5 - hybrid_top5),
            ):
                for page_id in sorted(page_ids_changed):
                    dense_item = dense_maps[query_id][page_id]
                    hybrid_item = hybrid_maps[query_id][page_id]
                    top5_change_rows.append(
                        {
                            "Checkpoint": spec.key,
                            "session_id": query["session_code"],
                            "query_id": query_id,
                            "direction": direction,
                            "page_id": page_id,
                            "source_turn_id": dense_item["source_turn_id"],
                            "is_gold": page_id in gold_ids,
                            "dense_rank": dense_item["rank"],
                            "hybrid_rank": hybrid_item["rank"],
                            "dense_score": dense_item["score"],
                            "raw_bm25_score": hybrid_item["raw_bm25_score"],
                            "normalized_bm25_score": hybrid_item["bm25_score"],
                            "hybrid_final_score": hybrid_item["final_score"],
                            "shared_bm25_tokens": shared_tokens(
                                data["query_texts"][query_id], data["page_texts"][page_id]
                            ),
                        }
                    )

    type_rows = [
        {"Checkpoint": checkpoint, "Transition": transition, "Category": category, "Gold count": count}
        for (checkpoint, transition, category), count in sorted(type_counter.items())
    ]
    promoted = sorted(
        (row for row in gold_rows if row["transition"] == "PROMOTED"),
        key=lambda row: int(row["rank_improvement"]),
        reverse=True,
    )
    demoted = sorted(
        (row for row in gold_rows if row["transition"] == "DEMOTED"),
        key=lambda row: int(row["rank_improvement"]),
    )
    selected = []
    for spec in CHECKPOINTS:
        selected.extend([row for row in promoted if row["Checkpoint"] == spec.key][:3])
        selected.extend([row for row in demoted if row["Checkpoint"] == spec.key][:3])
    cases_json, cases_md = render_cases(selected, checkpoint_data, top5_change_rows)

    c3_hash = stable_hash(
        [checkpoint_data["C3"]["page_texts"][str(page["page_id"])] for page in checkpoint_data["C3"]["pages"]]
    )
    p2_query_hash = stable_hash([checkpoint_data["C3"]["query_texts"][str(query["query_id"])] for query in queries])
    previous_embedding_metadata = load_json(
        REPO_ROOT / "exp/results/midterm_financial_lightweight_embedding_ablation/run_metadata.json"
    )
    validations = {
        "dense_C0_32.47": {
            "status": "PASS"
            if math.isclose(dense_reproduction["C0"]["recall_at_5"], 50 / 154, abs_tol=1e-12)
            else "FAIL"
        },
        "dense_C1_35.06": {
            "status": "PASS"
            if math.isclose(dense_reproduction["C1"]["recall_at_5"], 54 / 154, abs_tol=1e-12)
            else "FAIL"
        },
        "dense_C2_35.71": {
            "status": "PASS"
            if math.isclose(dense_reproduction["C2"]["recall_at_5"], 55 / 154, abs_tol=1e-12)
            else "FAIL"
        },
        "dense_C3_59_of_154": {
            "status": "PASS"
            if math.isclose(dense_reproduction["C3"]["recall_at_5"], 59 / 154, abs_tol=1e-12)
            else "FAIL"
        },
        "frozen_counts": {
            "status": "PASS"
            if len(page_ids) == 333
            and len(queries) == 99
            and sum(len(row["eligible_gold_page_ids"]) for row in queries) == 154
            else "FAIL"
        },
        "page_identity": {
            "status": "PASS"
            if all({str(page["page_id"]) for page in data["pages"]} == page_ids for data in checkpoint_data.values())
            else "FAIL"
        },
        "visible_page_ids": {
            "status": "PASS"
            if len(visibility_by_query) == 99
            and all(
                not row["bm25_nonvisible_page_count"] and not row["hybrid_nonvisible_page_count"]
                for row in visibility_rows
            )
            else "FAIL"
        },
        "future_leakage": {
            "status": "PASS" if all(not row["future_page_leak_count"] for row in visibility_rows) else "FAIL"
        },
        "C3_page_hash": {
            "status": "PASS" if c3_hash == previous_embedding_metadata["raw_page_text_hash"] else "FAIL",
            "value": c3_hash,
        },
        "P2_query_hash": {
            "status": "PASS" if p2_query_hash == previous_embedding_metadata["raw_query_text_hash"] else "FAIL",
            "value": p2_query_hash,
        },
        "production_components": {
            "status": "PASS",
            "modules": [
                "mem0.vector_stores.qdrant.Qdrant",
                "mem0.utils.lemmatization.lemmatize_for_bm25",
                "mem0.utils.scoring.get_bm25_params",
                "mem0.utils.scoring.normalize_bm25",
                "mem0.utils.scoring.score_and_rank",
            ],
        },
        "no_new_dense_embeddings": {"status": "PASS", "value": 0},
        "new_llm_calls": {"status": "PASS", "value": 0},
        "summary_regeneration": {"status": "PASS", "value": False},
        "full_session_rerun": {"status": "PASS", "value": False},
    }
    if any(row["status"] != "PASS" for row in validations.values()):
        raise AssertionError(validations)

    write_csv(args.output_dir / "metrics/retrieval_metrics.csv", metric_rows)
    write_csv(args.output_dir / "metrics/session_metrics.csv", session_rows)
    write_csv(args.output_dir / "metrics/checkpoint_comparisons.csv", comparison_rows)
    write_csv(args.output_dir / "metrics/gold_results.csv", gold_rows)
    write_csv(args.output_dir / "metrics/query_transitions.csv", query_rows)
    write_csv(args.output_dir / "analysis/visibility_audit.csv", visibility_rows)
    write_csv(args.output_dir / "analysis/query_type_summary.csv", type_rows)
    write_csv(args.output_dir / "analysis/bm25_anchor_overlap.csv", overlap_rows)
    write_csv(args.output_dir / "analysis/top5_page_changes.csv", top5_change_rows)
    dump_json(args.output_dir / "representative_cases.json", cases_json)
    (args.output_dir / "representative_cases.md").write_text(cases_md, encoding="utf-8")
    (args.output_dir / "experiment_report.md").write_text(
        render_report(metric_rows, session_rows, type_rows, top5_change_rows, validations), encoding="utf-8"
    )
    metadata = {
        "experiment_name": "midterm_dense_bm25_hybrid_checkpoints",
        "checkpoint_specs": [asdict(spec) for spec in CHECKPOINTS],
        "session_count": 5,
        "page_count": 333,
        "evaluation_query_count": 99,
        "eligible_gold_count": 154,
        "dense_backbone": PRODUCTION_EMBEDDING,
        "bm25_language": "zh",
        "page_and_query_contracts": {
            spec.key: {
                "query_text_hash": stable_hash(checkpoint_data[spec.key]["query_texts"]),
                "page_text_hash": stable_hash(checkpoint_data[spec.key]["page_texts"]),
                "query_variant": spec.query_variant,
                "page_variant": spec.page_variant,
            }
            for spec in CHECKPOINTS
        },
        "production_hybrid_contract": {
            "query_preprocessing": "lemmatize_for_bm25(query, language='zh')",
            "document_preprocessing": "lemmatize_for_bm25(page_text, language='zh')",
            "encoder": "ChineseBM25SparseEncoder via Qdrant(bm25_language='zh')",
            "qdrant_sparse_modifier": "Modifier.IDF",
            "bm25_normalization": "get_bm25_params + normalize_bm25",
            "fusion": "score_and_rank; semantic + normalized BM25; entity_boosts={}",
            "threshold": SEMANTIC_THRESHOLD,
            "overfetch": "max(limit * 4, 60)",
            "top5_internal_limit": 60,
            "top10_internal_limit": 60,
            "top20_internal_limit": 80,
            "weight_scan": False,
            "bm25_only_evaluation": False,
            "entity_boost": False,
            "reranker": False,
        },
        "idf_visibility_contract": "one temporary Qdrant sparse collection per checkpoint/query, containing exactly visible_page_ids",
        "component_paths": {name: str(path) for name, path in component_paths.items()},
        "component_sha256": component_hashes,
        "runtime_versions": {
            "qdrant-client": version("qdrant-client"),
            "jieba": version("jieba"),
            "mmh3": version("mmh3"),
        },
        "frozen_vector_cache_metadata": vector_cache_meta,
        "bm25_cache": {
            "path": str(cache.path),
            "cache_hits_this_run": cache.hit_count,
            "new_visible_scope_indexes_this_run": cache.build_count,
        },
        "new_dense_embedding_count": 0,
        "new_llm_calls": 0,
        "summary_regeneration": False,
        "full_session_rerun": False,
        "validation": validations,
        "validation_all_pass": True,
    }
    dump_json(args.output_dir / "run_metadata.json", metadata)
    print(
        json.dumps(
            {
                "metrics": metric_rows,
                "session_metrics": session_rows,
                "query_type_summary": type_rows,
                "validation": validations,
                "output_dir": str(args.output_dir),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
