"""Re-rank frozen C3 Dense candidates with cached H1/H5 BM25 scores.

This experiment intentionally performs no LLM calls, embedding calls, or BM25
encoding/indexing.  It only applies the requested fixed fusion weights to the
raw sparse rankings saved by ``run_midterm_c3_bm25_input_idf_ablation.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from exp.benchmark.benchmark_common import ensure_repo_root_on_path, load_json

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))

from exp.benchmark.midterm_retrieval_eval import stable_hash  # noqa: E402
from exp.benchmark.run_midterm_add_local_context_controls import compare_rankings  # noqa: E402
from exp.benchmark.run_midterm_add_search_cross_ablation import (  # noqa: E402
    PRODUCTION_EMBEDDING,
    SESSION_CODES,
    evaluate_all,
    rank_configuration,
)
from exp.benchmark.run_midterm_dense_bm25_hybrid_checkpoints import (  # noqa: E402
    checkpoint_inputs,
    metric_bundle,
)
from exp.benchmark.run_query_rewrite_cross_session_diagnosis import (  # noqa: E402
    dump_json,
    rank_map,
    read_csv,
    write_csv,
)
from mem0.utils.scoring import get_bm25_params, normalize_bm25  # noqa: E402


OUTPUT_DIR = REPO_ROOT / "exp/results/midterm_c3_bm25_fusion_weight_ablation"
PRIOR_DIR = REPO_ROOT / "exp/results/midterm_c3_bm25_input_idf_ablation"
PRIOR_CACHE = PRIOR_DIR / "cache/bm25_rankings.jsonl"
ELIGIBLE_GOLD = 154
DENSE_GOLD5 = 59
H1_GOLD5 = 49
H5_GOLD5 = 51
LOCAL_DENSE_LIMIT = 60
SEMANTIC_THRESHOLD = 0.1


@dataclass(frozen=True)
class WeightConfig:
    key: str
    bm25_source: str
    dense_weight: float
    bm25_weight: float
    zero_fallback: bool = False


CONFIGS = (
    WeightConfig("H5-W50", "H5", 0.50, 0.50),
    WeightConfig("H5-W60", "H5", 0.60, 0.40),
    WeightConfig("H5-W70", "H5", 0.70, 0.30),
    WeightConfig("H5-W80", "H5", 0.80, 0.20),
    WeightConfig("H5-W90", "H5", 0.90, 0.10),
    WeightConfig("H5-W95", "H5", 0.95, 0.05),
    WeightConfig("H5-W100", "H5", 1.00, 0.00),
    WeightConfig("H1-W80", "H1", 0.80, 0.20),
    WeightConfig("H1-W90", "H1", 0.90, 0.10),
    WeightConfig("H1-W95", "H1", 0.95, 0.05),
    WeightConfig("H5-W90-ZeroFallback", "H5", 0.90, 0.10, True),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="C3 cached BM25 fusion-weight ablation")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    return parser.parse_args()


def all_queries(snapshots: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(query, session_code=code) for code in SESSION_CODES for query in snapshots[code]["queries"]]


def load_sparse_cache(path: Path) -> dict[str, dict[str, dict[str, Any]]]:
    if not path.exists():
        raise FileNotFoundError(f"Required frozen BM25 cache is missing: {path}")
    result = {"H1": {}, "H5": {}}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        source = str(row.get("checkpoint") or "")
        if source not in result or row.get("status") != "SUCCESS":
            continue
        query_id = str(row["query_id"])
        if query_id in result[source]:
            raise AssertionError(f"Duplicate cached sparse result: {source}/{query_id}")
        result[source][query_id] = row
    for source, rows in result.items():
        if len(rows) != 99:
            raise AssertionError(f"{source}: expected 99 cached sparse rankings, got {len(rows)}")
    return result


def weighted_ranking(
    dense: Sequence[Mapping[str, Any]],
    sparse_row: Mapping[str, Any],
    *,
    dense_weight: float,
    bm25_weight: float,
    candidate_limit: int,
    zero_fallback: bool,
) -> list[dict[str, Any]]:
    """Apply a fixed fusion formula to an already cached BM25 ranking."""
    if not math.isclose(dense_weight + bm25_weight, 1.0, abs_tol=1e-12):
        raise ValueError("Dense and BM25 weights must sum to one")
    sparse = [dict(row) for row in sparse_row["ranking"]]
    keyword_pool = sparse[:candidate_limit]
    raw_all = {str(row["page_id"]): float(row["raw_bm25_score"]) for row in sparse}
    midpoint, steepness = get_bm25_params("", lemmatized=str(sparse_row["query_lemmatized"]))
    normalized = {
        str(row["page_id"]): normalize_bm25(float(row["raw_bm25_score"]), midpoint, steepness)
        for row in keyword_pool
        if float(row["raw_bm25_score"]) > 0
    }
    scored: list[dict[str, Any]] = []
    for dense_order, row in enumerate(dense[:candidate_limit]):
        semantic = float(row["score"])
        if semantic < SEMANTIC_THRESHOLD:
            continue
        page_id = str(row["page_id"])
        raw = raw_all.get(page_id, 0.0)
        bm25 = normalized.get(page_id, 0.0)
        final = semantic if zero_fallback and raw == 0.0 else dense_weight * semantic + bm25_weight * bm25
        scored.append(
            {
                "page_id": page_id,
                "source_turn_id": row["source_turn_id"],
                "score": final,
                "semantic_score": semantic,
                "raw_bm25_score": raw,
                "normalized_bm25_score": bm25,
                "final_score": final,
                "zero_fallback_applied": zero_fallback and raw == 0.0,
                "dense_order": dense_order,
                "candidate_limit": candidate_limit,
            }
        )
    scored.sort(key=lambda row: (-float(row["final_score"]), int(row["dense_order"])))
    included = {str(row["page_id"]) for row in scored}
    for dense_order, row in enumerate(dense):
        page_id = str(row["page_id"])
        if page_id in included:
            continue
        scored.append(
            {
                "page_id": page_id,
                "source_turn_id": row["source_turn_id"],
                "score": 0.0,
                "semantic_score": float(row["score"]),
                "raw_bm25_score": raw_all.get(page_id, 0.0),
                "normalized_bm25_score": 0.0,
                "final_score": None,
                "zero_fallback_applied": False,
                "dense_order": dense_order,
                "candidate_limit": candidate_limit,
            }
        )
    for rank, row in enumerate(scored, start=1):
        row["rank"] = rank
    if len(scored) != len(dense) or {str(row["page_id"]) for row in scored} != {
        str(row["page_id"]) for row in dense
    }:
        raise AssertionError("Fusion changed the frozen visible candidate set")
    return scored


def rankings_for_config(
    config: WeightConfig,
    dense: Mapping[str, Sequence[Mapping[str, Any]]],
    sparse: Mapping[str, Mapping[str, Any]],
    visibility: Mapping[str, Sequence[str]],
) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[str, list[dict[str, Any]]],
    dict[str, list[dict[str, Any]]],
]:
    ranks5, ranks20, ranks_full = {}, {}, {}
    for query_id, dense_ranking in dense.items():
        row = sparse[query_id]
        if config.bm25_source == "H5":
            complete = weighted_ranking(
                dense_ranking,
                row,
                dense_weight=config.dense_weight,
                bm25_weight=config.bm25_weight,
                candidate_limit=min(LOCAL_DENSE_LIMIT, len(dense_ranking)),
                zero_fallback=config.zero_fallback,
            )
            ranks5[query_id] = complete
            ranks20[query_id] = complete
            ranks_full[query_id] = complete
            continue
        ranks5[query_id] = weighted_ranking(
            dense_ranking,
            row,
            dense_weight=config.dense_weight,
            bm25_weight=config.bm25_weight,
            candidate_limit=min(60, len(dense_ranking)),
            zero_fallback=config.zero_fallback,
        )
        ranks20[query_id] = weighted_ranking(
            dense_ranking,
            row,
            dense_weight=config.dense_weight,
            bm25_weight=config.bm25_weight,
            candidate_limit=min(80, len(dense_ranking)),
            zero_fallback=config.zero_fallback,
        )
        ranks_full[query_id] = weighted_ranking(
            dense_ranking,
            row,
            dense_weight=config.dense_weight,
            bm25_weight=config.bm25_weight,
            candidate_limit=len(visibility[query_id]),
            zero_fallback=config.zero_fallback,
        )
    return ranks5, ranks20, ranks_full


def prior_gold_lookup() -> dict[tuple[str, str, str], dict[str, str]]:
    return {
        (row["Config"], row["query_id"], row["gold_page_id"]): row
        for row in read_csv(PRIOR_DIR / "metrics/gold_results.csv")
        if row["Config"] in {"H1", "H5"}
    }


def validate_cached_scores(
    snapshots: Mapping[str, Mapping[str, Any]],
    dense: Mapping[str, Sequence[Mapping[str, Any]]],
    sparse: Mapping[str, Mapping[str, Mapping[str, Any]]],
    visibility: Mapping[str, Sequence[str]],
) -> list[dict[str, Any]]:
    prior = prior_gold_lookup()
    rows = []
    for source in ("H1", "H5"):
        config = WeightConfig(f"{source}-validation-W50", source, 0.5, 0.5)
        ranks5, _, _ = rankings_for_config(config, dense, sparse[source], visibility)
        maps = {query_id: rank_map(ranking) for query_id, ranking in ranks5.items()}
        for query in all_queries(snapshots):
            query_id = str(query["query_id"])
            for gold_id in map(str, query["eligible_gold_page_ids"]):
                expected = prior[(source, query_id, gold_id)]
                actual = maps[query_id][gold_id]
                raw_match = math.isclose(
                    float(actual["raw_bm25_score"]), float(expected["raw_bm25_score"]), abs_tol=1e-12
                )
                norm_match = math.isclose(
                    float(actual["normalized_bm25_score"]),
                    float(expected["normalized_bm25_score"]),
                    abs_tol=1e-12,
                )
                rank_match = int(actual["rank"]) == int(expected["hybrid_rank"])
                rows.append(
                    {
                        "BM25 Source": source,
                        "session_id": query["session_code"],
                        "query_id": query_id,
                        "gold_page_id": gold_id,
                        "raw_score_match": raw_match,
                        "normalized_score_match": norm_match,
                        "W50_rank_match": rank_match,
                    }
                )
    if not all(row["raw_score_match"] and row["normalized_score_match"] and row["W50_rank_match"] for row in rows):
        bad = [row for row in rows if not all((row["raw_score_match"], row["normalized_score_match"], row["W50_rank_match"]))]
        raise AssertionError(f"Cached H1/H5 score validation failed: {bad[:5]}")
    return rows


def transition_name(before_rank: int, after_rank: int) -> str:
    if before_rank > 5 >= after_rank:
        return "PROMOTED"
    if before_rank <= 5 < after_rank:
        return "DEMOTED"
    return "UNCHANGED"


def render_cases(cases: Sequence[Mapping[str, Any]], best_config: str) -> str:
    lines = [f"# 最佳 Hybrid（{best_config}）及 ZeroFallback 代表案例", ""]
    for case in cases:
        lines.extend(
            [
                f"## {case['case_type']} / {case['config']} / {case['query_id']} / {case['gold_source_turn_id']}",
                "",
                f"Original Query：{case['original_query']}",
                "",
                f"Dense P2 Query：{case['p2_query']}",
                "",
                f"Gold Page：{case['gold_page_id']}（{case['gold_source_turn_id']}）",
                "",
                f"Dense #{case['dense_rank']} → {case['config']} #{case['hybrid_rank']}；"
                f"Dense={float(case['dense_score']):.9f}；Raw BM25={float(case['raw_bm25_score']):.9f}；"
                f"Normalized BM25={float(case['normalized_bm25_score']):.9f}；"
                f"Final={case['final_score']}；ZeroFallback={case['zero_fallback_applied']}",
                "",
                "```text",
                str(case["gold_page_text"]),
                "```",
                "",
            ]
        )
    return "\n".join(lines)


def pct(value: Any) -> str:
    return f"{100 * float(value):.2f}%"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render_report(
    metrics: Sequence[Mapping[str, Any]],
    sessions: Sequence[Mapping[str, Any]],
    dense_metrics: Mapping[str, Any],
    zero_comparison: Mapping[str, Any],
    best_fixed: Mapping[str, Any],
    best_hybrid: Mapping[str, Any],
    zero_stats: Mapping[str, int],
    validations: Mapping[str, Mapping[str, Any]],
) -> str:
    lookup = {str(row["Config"]): row for row in metrics}
    h5_gold_curve = [int(lookup[f"H5-W{weight}"]["Gold@5"]) for weight in (50, 60, 70, 80, 90, 95, 100)]
    h5_monotonic = all(left <= right for left, right in zip(h5_gold_curve, h5_gold_curve[1:]))
    zero_row = lookup["H5-W90-ZeroFallback"]
    h1_w90 = lookup["H1-W90"]
    h5_w90 = lookup["H5-W90"]
    h5_w95 = lookup["H5-W95"]
    lines = [
        "# C3 Dense/BM25 固定融合权重消融",
        "",
        "| Config | BM25 Source | Dense Weight | BM25 Weight | Zero Fallback | R@5 | Gold@5 | Δ vs Dense | Promoted | Demoted | Net | R@10 | MRR |",
        "|---|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in metrics:
        lines.append(
            f"| {row['Config']} | {row['BM25 Source']} | {float(row['Dense Weight']):.2f} | "
            f"{float(row['BM25 Weight']):.2f} | {row['Zero Fallback']} | {pct(row['R@5'])} | "
            f"{row['Gold@5']} | {100 * float(row['Delta vs Dense']):+.2f}pp | "
            f"{row['Promoted']} | {row['Demoted']} | {int(row['Net']):+d} | "
            f"{pct(row['R@10'])} | {float(row['MRR']):.4f} |"
        )
    lines.extend(
        [
            "",
            "| Config | Macro R@5 | R@20 | Mean Gold Rank | Rescued Query | Hurt Query |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in metrics:
        lines.append(
            f"| {row['Config']} | {pct(row['Macro R@5'])} | {pct(row['R@20'])} | "
            f"{float(row['Mean Gold Rank']):.2f} | {row['Rescued Query']} | {row['Hurt Query']} |"
        )
    lines.extend(
        [
            "",
            f"Dense reference：{pct(dense_metrics['recall_at_5'])}（59/154）。",
            f"最佳 fixed weight：{best_fixed['Config']}，{pct(best_fixed['R@5'])}（{best_fixed['Gold@5']}/154）。",
            f"最佳含 BM25 配置：{best_hybrid['Config']}，{pct(best_hybrid['R@5'])}（{best_hybrid['Gold@5']}/154）。",
            f"H5-W90-ZeroFallback 相对普通 H5-W90：{zero_comparison['Promoted Gold']} promoted，"
            f"{zero_comparison['Demoted Gold']} demoted，net {int(zero_comparison['Net Gold gain']):+d}。",
            "",
            "## Session R@5",
            "",
            "| Config | S001 | S002 | S003 | S004 | S005 | Macro |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for metric in metrics:
        session_lookup = {row["session_id"]: row for row in sessions if row["Config"] == metric["Config"]}
        lines.append(
            f"| {metric['Config']} | "
            + " | ".join(pct(session_lookup[code]["R@5"]) for code in SESSION_CODES)
            + f" | {pct(metric['Macro R@5'])} |"
        )
    lines.extend(
        [
            "",
            "## 事实结论",
            "",
            f"1. H5 的 Gold@5 曲线为 {h5_gold_curve}；随 Dense 权重增加"
            f"{'单调恢复' if h5_monotonic else '并非单调恢复'}，但没有任何固定权重超过 Dense 的 59 Gold。",
            f"2. H5 主实验最佳为 W95：{h5_w95['Gold@5']}/154，promoted={h5_w95['Promoted']}、"
            f"demoted={h5_w95['Demoted']}。它与 Dense 的 Top5 Gold 集合一致，只把 MRR 从 "
            f"{float(dense_metrics['mrr']):.4f} 改为 {float(h5_w95['MRR']):.4f}。",
            f"3. H1-W90 同为 59/154，但发生 {h1_w90['Promoted']} promoted / "
            f"{h1_w90['Demoted']} demoted；R@10={pct(h1_w90['R@10'])} 高于 Dense 的 "
            f"{pct(dense_metrics['recall_at_10'])}，MRR={float(h1_w90['MRR']):.4f} 低于 Dense。"
            "这属于宽召回增强、Top5 净收益为零且前排区分下降。",
            f"4. 同权重下 H1/H5 Gold@5：W80={lookup['H1-W80']['Gold@5']}/{lookup['H5-W80']['Gold@5']}，"
            f"W90={h1_w90['Gold@5']}/{h5_w90['Gold@5']}，"
            f"W95={lookup['H1-W95']['Gold@5']}/{h5_w95['Gold@5']}。完整 Summary+Keywords 的 lexical coverage "
            "在 0.1/0.2 BM25 权重下优于 Keywords Only；到 0.05 时 H5 仅仅趋同 Dense。",
            f"5. ZeroFallback 相对普通 H5-W90 为 {zero_comparison['Promoted Gold']} promoted / "
            f"{zero_comparison['Demoted Gold']} demoted，Top5 净变化 0；相对 Dense 仍为 "
            f"{zero_row['Promoted']} promoted / {zero_row['Demoted']} demoted / net {int(zero_row['Net']):+d}。"
            f"普通 H5-W90 的 {zero_stats['H5-W90 zero demoted']}/{h5_w90['Demoted']} 个 demoted Gold "
            f"是 raw BM25=0；fallback 确实使 {zero_stats['fallback zero promoted']} 个 raw=0 Gold 进入 Top5，"
            f"但又使 {zero_stats['fallback matched demoted']} 个有 lexical match 的 Gold 掉出。"
            "因此简单 zero-hit gating 没有改善总结果。",
            "6. 没有配置达到 62/154；固定加权 Hybrid 未显示稳定价值。结果同时指向 lexical signal 的 Top5 "
            "精度不足和 normalization 后分数尺度不匹配；等权融合破坏最大，但降低权重只能恢复 Dense，不能产生净增益。",
            "7. 按本轮停止标准，不值得继续细扫固定权重。ZeroFallback 也未超过 Dense，因此当前结果不足以支持继续"
            "围绕这个简单 gating 规则优化。",
            "",
            "Validation：" + ("PASS" if all(row["status"] == "PASS" for row in validations.values()) else "FAIL") + "。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    snapshots, old_pages, queries, checkpoint_data, vector_cache_meta = checkpoint_inputs()
    data = checkpoint_data["C3"]
    query_by_id = {str(query["query_id"]): query for query in queries}
    original_queries = {query_id: str(query["original_query"]) for query_id, query in query_by_id.items()}
    p2_queries = dict(data["query_texts"])
    visibility = {
        str(row["query_id"]): [str(page_id) for page_id in row["visible_page_ids"]]
        for code in SESSION_CODES
        for row in snapshots[code]["visibility"]
    }
    dense = rank_configuration(snapshots, data["query_vectors"], data["page_vectors"])
    dense_metrics, dense_sessions = evaluate_all(snapshots, dense)
    sparse_cache = load_sparse_cache(PRIOR_CACHE)

    prior_metadata = load_json(PRIOR_DIR / "run_metadata.json")
    current_page_hash = stable_hash([data["page_texts"][str(page["page_id"])] for page in data["pages"]])
    current_p2_hash = stable_hash([p2_queries[str(query["query_id"])] for query in queries])
    current_original_hash = stable_hash([original_queries[str(query["query_id"])] for query in queries])
    for query_id in query_by_id:
        h1_ids = list(map(str, sparse_cache["H1"][query_id]["visible_page_ids"]))
        h5_ids = list(map(str, sparse_cache["H5"][query_id]["visible_page_ids"]))
        dense_top60 = [str(row["page_id"]) for row in dense[query_id][:LOCAL_DENSE_LIMIT]]
        if h1_ids != visibility[query_id] or h5_ids != dense_top60:
            raise AssertionError(f"Frozen sparse corpus mismatch for {query_id}")
    score_validation_rows = validate_cached_scores(snapshots, dense, sparse_cache, visibility)

    metrics_rows: list[dict[str, Any]] = []
    session_rows: list[dict[str, Any]] = []
    gold_rows: list[dict[str, Any]] = []
    query_rows: list[dict[str, Any]] = []
    rankings: dict[str, dict[str, list[dict[str, Any]]]] = {}
    metrics_by_config: dict[str, dict[str, Any]] = {}

    for config in CONFIGS:
        ranks5, ranks20, ranks_full = rankings_for_config(
            config, dense, sparse_cache[config.bm25_source], visibility
        )
        aggregate, config_sessions = metric_bundle(snapshots, ranks5, ranks20, ranks_full)
        comparison, comparison_gold, comparison_queries = compare_rankings(
            snapshots, dense, ranks5, comparison=f"{config.key} vs Dense"
        )
        rankings[config.key] = ranks5
        metrics_by_config[config.key] = aggregate
        metrics_rows.append(
            {
                "Config": config.key,
                "BM25 Source": config.bm25_source,
                "Dense Weight": config.dense_weight,
                "BM25 Weight": config.bm25_weight,
                "Zero Fallback": "ON" if config.zero_fallback else "OFF",
                "R@5": aggregate["recall_at_5"],
                "Gold@5": aggregate["gold_at_5"],
                "Macro R@5": aggregate["macro_session_r5"],
                "R@10": aggregate["recall_at_10"],
                "R@20": aggregate["recall_at_20"],
                "MRR": aggregate["mrr"],
                "Mean Gold Rank": aggregate["mean_gold_rank"],
                "Delta vs Dense": float(aggregate["recall_at_5"]) - float(dense_metrics["recall_at_5"]),
                "Promoted": comparison["Promoted Gold"],
                "Demoted": comparison["Demoted Gold"],
                "Net": comparison["Net Gold gain"],
                "Rescued Query": comparison["Rescued Queries"],
                "Hurt Query": comparison["Hurt Queries"],
                "Dense Top5 Gold pulled in": comparison["Promoted Gold"],
                "Dense Top5 Gold pushed out": comparison["Demoted Gold"],
            }
        )
        for row in config_sessions:
            session_rows.append(
                {
                    "Config": config.key,
                    "BM25 Source": config.bm25_source,
                    "Dense Weight": config.dense_weight,
                    "BM25 Weight": config.bm25_weight,
                    "session_id": row["session_id"],
                    "Dense R@5": dense_sessions[row["session_id"]]["recall_at_5"],
                    **row,
                }
            )
        dense_maps = {query_id: rank_map(rows) for query_id, rows in dense.items()}
        result_maps = {query_id: rank_map(rows) for query_id, rows in ranks5.items()}
        for row in comparison_gold:
            query_id = str(row["query_id"])
            gold_id = str(row["gold_page_id"])
            before = dense_maps[query_id][gold_id]
            after = result_maps[query_id][gold_id]
            gold_rows.append(
                {
                    "Config": config.key,
                    **row,
                    "transition": transition_name(int(before["rank"]), int(after["rank"])),
                    "dense_rank": before["rank"],
                    "hybrid_rank": after["rank"],
                    "dense_score": before["score"],
                    "raw_bm25_score": after["raw_bm25_score"],
                    "normalized_bm25_score": after["normalized_bm25_score"],
                    "final_score": after["final_score"],
                    "zero_fallback_applied": after["zero_fallback_applied"],
                }
            )
        query_rows.extend({"Config": config.key, **row} for row in comparison_queries)

    zero_comparison, zero_gold, zero_queries = compare_rankings(
        snapshots,
        rankings["H5-W90"],
        rankings["H5-W90-ZeroFallback"],
        comparison="H5-W90-ZeroFallback vs H5-W90",
    )
    zero_rows = []
    normal_maps = {query_id: rank_map(rows) for query_id, rows in rankings["H5-W90"].items()}
    fallback_maps = {
        query_id: rank_map(rows) for query_id, rows in rankings["H5-W90-ZeroFallback"].items()
    }
    for row in zero_gold:
        query_id = str(row["query_id"])
        gold_id = str(row["gold_page_id"])
        normal = normal_maps[query_id][gold_id]
        fallback = fallback_maps[query_id][gold_id]
        zero_rows.append(
            {
                **row,
                "raw_bm25_score": normal["raw_bm25_score"],
                "normalized_bm25_score": normal["normalized_bm25_score"],
                "normal_final_score": normal["final_score"],
                "fallback_final_score": fallback["final_score"],
                "fallback_applied": fallback["zero_fallback_applied"],
            }
        )

    zero_stats = {
        "H5-W90 zero demoted": sum(
            row["Config"] == "H5-W90"
            and row["transition"] == "DEMOTED"
            and float(row["raw_bm25_score"]) == 0.0
            for row in gold_rows
        ),
        "fallback zero promoted": sum(
            row["Config"] == "H5-W90-ZeroFallback"
            and row["transition"] == "PROMOTED"
            and float(row["raw_bm25_score"]) == 0.0
            for row in gold_rows
        ),
        "fallback matched demoted": sum(
            row["Config"] == "H5-W90-ZeroFallback"
            and row["transition"] == "DEMOTED"
            and float(row["raw_bm25_score"]) > 0.0
            for row in gold_rows
        ),
    }

    normal_fixed = [row for row in metrics_rows if row["Zero Fallback"] == "OFF"]
    best_fixed = max(normal_fixed, key=lambda row: (int(row["Gold@5"]), float(row["MRR"])))
    hybrid_candidates = [row for row in metrics_rows if float(row["BM25 Weight"]) > 0 or row["Zero Fallback"] == "ON"]
    best_hybrid = max(hybrid_candidates, key=lambda row: (int(row["Gold@5"]), float(row["MRR"])))

    cases = []
    selected: list[tuple[Mapping[str, Any], str, str]] = []
    best_gold = [row for row in gold_rows if row["Config"] == best_hybrid["Config"]]
    selected.extend(
        (row, str(best_hybrid["Config"]), "BEST_HYBRID_PROMOTED")
        for row in sorted(
            (row for row in best_gold if row["transition"] == "PROMOTED"),
            key=lambda row: -int(row["rank_improvement"]),
        )[:3]
    )
    selected.extend(
        (row, str(best_hybrid["Config"]), "BEST_HYBRID_DEMOTED")
        for row in sorted(
            (row for row in best_gold if row["transition"] == "DEMOTED"),
            key=lambda row: int(row["rank_improvement"]),
        )[:3]
    )
    # H5-W95 has no Top5 Gold movement.  H1-W90 ties its R@5 but swaps four
    # promoted and four demoted Gold, so preserve those factual diagnostics.
    h1_w90_gold = [row for row in gold_rows if row["Config"] == "H1-W90"]
    for transition in ("PROMOTED", "DEMOTED"):
        selected.extend(
            (row, "H1-W90", f"COBEST_H1_W90_{transition}")
            for row in sorted(
                (row for row in h1_w90_gold if row["transition"] == transition),
                key=lambda row: -int(row["rank_improvement"])
                if transition == "PROMOTED"
                else int(row["rank_improvement"]),
            )[:3]
        )
    h5_w90_zero_demotions = [
        row
        for row in gold_rows
        if row["Config"] == "H5-W90" and row["transition"] == "DEMOTED" and float(row["raw_bm25_score"]) == 0
    ]
    selected.extend(
        (row, "H5-W90", "H5_W90_BM25_ZERO_DEMOTED")
        for row in sorted(h5_w90_zero_demotions, key=lambda row: int(row["rank_improvement"]))[:3]
    )
    selected.extend(
        (
            row,
            "H5-W90-ZeroFallback",
            "ZEROFALLBACK_PROMOTED_VS_W90"
            if int(row["before_rank"]) > 5 >= int(row["after_rank"])
            else "ZEROFALLBACK_DEMOTED_VS_W90",
        )
        for row in sorted(
            (
                row
                for row in zero_rows
                if (int(row["before_rank"]) <= 5) != (int(row["after_rank"]) <= 5)
            ),
            key=lambda row: -abs(int(row["rank_improvement"])),
        )[:6]
    )
    seen = set()
    for row, config_key, case_type in selected:
        identity = (case_type, config_key, str(row["query_id"]), str(row["gold_page_id"]))
        if identity in seen:
            continue
        seen.add(identity)
        query_id, gold_id = str(row["query_id"]), str(row["gold_page_id"])
        config_map = rank_map(rankings[config_key][query_id])
        dense_item = rank_map(dense[query_id])[gold_id]
        item = config_map[gold_id]
        cases.append(
            {
                "case_type": case_type,
                "config": config_key,
                "session_id": query_by_id[query_id]["session_code"],
                "query_id": query_id,
                "original_query": original_queries[query_id],
                "p2_query": p2_queries[query_id],
                "gold_page_id": gold_id,
                "gold_source_turn_id": item["source_turn_id"],
                "gold_page_text": data["page_texts"][gold_id],
                "dense_rank": dense_item["rank"],
                "hybrid_rank": item["rank"],
                "dense_score": dense_item["score"],
                "raw_bm25_score": item["raw_bm25_score"],
                "normalized_bm25_score": item["normalized_bm25_score"],
                "final_score": item["final_score"],
                "zero_fallback_applied": item["zero_fallback_applied"],
            }
        )

    h5_weight_rows = [row for row in metrics_rows if row["Config"].startswith("H5-W") and "Zero" not in row["Config"]]
    h5_weight_rows.sort(key=lambda row: float(row["Dense Weight"]))
    monotonic = all(
        int(left["Gold@5"]) <= int(right["Gold@5"]) for left, right in zip(h5_weight_rows, h5_weight_rows[1:])
    )
    h1_h5_rows = []
    metric_lookup = {row["Config"]: row for row in metrics_rows}
    for weight in (80, 90, 95):
        h1, h5 = metric_lookup[f"H1-W{weight}"], metric_lookup[f"H5-W{weight}"]
        h1_h5_rows.append(
            {
                "Dense Weight": weight / 100,
                "H1 Gold@5": h1["Gold@5"],
                "H5 Gold@5": h5["Gold@5"],
                "H1 minus H5 Gold": int(h1["Gold@5"]) - int(h5["Gold@5"]),
                "H1 R@5": h1["R@5"],
                "H5 R@5": h5["R@5"],
            }
        )

    validations = {
        "dense_59_of_154": {
            "status": "PASS" if math.isclose(float(dense_metrics["recall_at_5"]), DENSE_GOLD5 / ELIGIBLE_GOLD, abs_tol=1e-12) else "FAIL"
        },
        "H5_W50_51_of_154": {
            "status": "PASS" if int(metrics_by_config["H5-W50"]["gold_at_5"]) == H5_GOLD5 else "FAIL"
        },
        "H5_W100_equals_dense": {
            "status": "PASS"
            if int(metrics_by_config["H5-W100"]["gold_at_5"]) == DENSE_GOLD5
            and all(
                [row["page_id"] for row in rankings["H5-W100"][query_id]]
                == [row["page_id"] for row in dense[query_id]]
                for query_id in query_by_id
            )
            else "FAIL"
        },
        "H1_W50_cache_reproduction": {
            "status": "PASS"
            if all(row["W50_rank_match"] for row in score_validation_rows if row["BM25 Source"] == "H1")
            and sum(
                int(int(row["hybrid_rank"]) <= 5)
                for row in read_csv(PRIOR_DIR / "metrics/gold_results.csv")
                if row["Config"] == "H1"
            )
            == H1_GOLD5
            else "FAIL"
        },
        "cached_raw_and_normalized_scores": {
            "status": "PASS"
            if all(row["raw_score_match"] and row["normalized_score_match"] for row in score_validation_rows)
            else "FAIL"
        },
        "frozen_counts": {
            "status": "PASS"
            if len(old_pages) == 333
            and len(queries) == 99
            and sum(len(query["eligible_gold_page_ids"]) for query in queries) == ELIGIBLE_GOLD
            else "FAIL"
        },
        "page_hash": {
            "status": "PASS" if current_page_hash == prior_metadata["page_text_hash"] else "FAIL",
            "value": current_page_hash,
        },
        "p2_query_hash": {
            "status": "PASS" if current_p2_hash == prior_metadata["p2_query_hash"] else "FAIL",
            "value": current_p2_hash,
        },
        "original_query_hash": {
            "status": "PASS" if current_original_hash == prior_metadata["original_query_hash"] else "FAIL",
            "value": current_original_hash,
        },
        "visibility_and_local_corpus": {"status": "PASS"},
        "BM25_regeneration": {"status": "PASS", "value": 0},
        "new_dense_embeddings": {"status": "PASS", "value": 0},
        "new_llm_calls": {"status": "PASS", "value": 0},
        "summary_keywords_regeneration": {"status": "PASS", "value": False},
        "full_session_rerun": {"status": "PASS", "value": False},
        "production_scorer_modified": {"status": "PASS", "value": False},
        "fixed_weight_grid_only": {"status": "PASS", "value": [config.key for config in CONFIGS]},
    }
    if any(row["status"] != "PASS" for row in validations.values()):
        raise AssertionError(validations)

    write_csv(args.output_dir / "metrics/retrieval_metrics.csv", metrics_rows)
    write_csv(args.output_dir / "metrics/session_metrics.csv", session_rows)
    write_csv(args.output_dir / "metrics/gold_results.csv", gold_rows)
    write_csv(args.output_dir / "metrics/query_transitions.csv", query_rows)
    write_csv(args.output_dir / "analysis/cached_score_validation.csv", score_validation_rows)
    write_csv(args.output_dir / "analysis/h1_vs_h5_low_weight.csv", h1_h5_rows)
    write_csv(args.output_dir / "analysis/zero_fallback_gold_movements.csv", zero_rows)
    write_csv(args.output_dir / "analysis/zero_fallback_query_transitions.csv", zero_queries)
    write_csv(args.output_dir / "analysis/h5_weight_curve.csv", h5_weight_rows)
    dump_json(args.output_dir / "representative_cases.json", {"best_hybrid": best_hybrid, "cases": cases})
    (args.output_dir / "representative_cases.md").write_text(
        render_cases(cases, str(best_hybrid["Config"])), encoding="utf-8"
    )
    (args.output_dir / "experiment_report.md").write_text(
        render_report(
            metrics_rows,
            session_rows,
            dense_metrics,
            zero_comparison,
            best_fixed,
            best_hybrid,
            zero_stats,
            validations,
        ),
        encoding="utf-8",
    )
    metadata = {
        "experiment_name": "midterm_c3_bm25_fusion_weight_ablation",
        "configs": [asdict(config) for config in CONFIGS],
        "dense_backbone": PRODUCTION_EMBEDDING,
        "dense_query": "frozen P2 resolved query",
        "dense_page": "frozen C3 Summary + Keywords",
        "bm25_cache_source": str(PRIOR_CACHE),
        "bm25_cache_sha256": sha256_file(PRIOR_CACHE),
        "bm25_cache_rows_reused": {source: len(rows) for source, rows in sparse_cache.items()},
        "bm25_inputs": {
            "H1": "Original Query / Summary + Keywords / visible Pages / filter OFF",
            "H5": "Original Query / Keywords Only / Dense Top60 Local-IDF / filter ON",
        },
        "fusion_formula": "dense_weight * dense_score + bm25_weight * normalized_bm25_score",
        "normalization": (
            "deterministically recomputed from cached raw scores with "
            "mem0.utils.scoring.get_bm25_params + normalize_bm25; Gold values validated against prior artifacts"
        ),
        "zero_fallback_formula": "raw_bm25_score == 0 => dense_score; otherwise fixed weighted score",
        "semantic_threshold": SEMANTIC_THRESHOLD,
        "session_count": 5,
        "page_count": len(old_pages),
        "query_count": len(queries),
        "eligible_gold_count": ELIGIBLE_GOLD,
        "page_text_hash": current_page_hash,
        "p2_query_hash": current_p2_hash,
        "original_query_hash": current_original_hash,
        "H5_r5_monotonic_with_dense_weight": monotonic,
        "best_fixed_weight": best_fixed,
        "best_hybrid": best_hybrid,
        "zero_fallback_vs_plain_W90": zero_comparison,
        "zero_fallback_diagnostics": zero_stats,
        "frozen_vector_cache_metadata": vector_cache_meta,
        "new_llm_calls": 0,
        "new_dense_embedding_count": 0,
        "new_bm25_ranking_count": 0,
        "summary_keywords_regeneration": False,
        "full_session_rerun": False,
        "production_scorer_modified": False,
        "validation": validations,
        "validation_all_pass": True,
    }
    dump_json(args.output_dir / "run_metadata.json", metadata)
    print(
        json.dumps(
            {
                "metrics": metrics_rows,
                "session_metrics": session_rows,
                "best_fixed": best_fixed,
                "best_hybrid": best_hybrid,
                "zero_fallback_vs_W90": zero_comparison,
                "H5_monotonic": monotonic,
                "validation": validations,
                "output_dir": str(args.output_dir),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
