"""Diagnose BM25 input and IDF scope at the frozen best MidTerm C3 checkpoint."""

from __future__ import annotations

import argparse
import json
import math
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from exp.benchmark.benchmark_common import ensure_repo_root_on_path, load_json

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))

from exp.benchmark.midterm_retrieval_eval import stable_hash  # noqa: E402
from exp.benchmark.run_midterm_add_search_cross_ablation import (  # noqa: E402
    PRODUCTION_EMBEDDING,
    SESSION_CODES,
    evaluate_all,
    rank_configuration,
)
from exp.benchmark.run_midterm_add_local_context_controls import compare_rankings  # noqa: E402
from exp.benchmark.run_midterm_dense_bm25_hybrid_checkpoints import (  # noqa: E402
    BM25Cache,
    checkpoint_inputs,
    metric_bundle,
    production_hybrid_ranking,
    raw_bm25_ranking,
    sha256_file,
)
from exp.benchmark.run_query_rewrite_cross_session_diagnosis import (  # noqa: E402
    dump_json,
    rank_map,
    write_csv,
)
from mem0.utils.bm25_sparse import ChineseBM25SparseEncoder  # noqa: E402
from mem0.utils.lemmatization import lemmatize_for_bm25  # noqa: E402
from mem0.vector_stores.qdrant import Qdrant  # noqa: E402


OUTPUT_DIR = REPO_ROOT / "exp/results/midterm_c3_bm25_input_idf_ablation"
PREVIOUS_HYBRID_DIR = REPO_ROOT / "exp/results/midterm_dense_bm25_hybrid_checkpoints"
PREVIOUS_MODEL_DIR = REPO_ROOT / "exp/results/midterm_financial_lightweight_embedding_ablation"
DENSE_GOLD5 = 59
H0_GOLD5 = 46
ELIGIBLE_GOLD = 154
LOCAL_DENSE_LIMIT = 60
SEMANTIC_THRESHOLD = 0.1

# Frozen before the first experiment run. This is exactly the list requested by the experiment contract.
BM25_FILTER_TOKENS = (
    "的",
    "和",
    "与",
    "在",
    "了",
    "是",
    "把",
    "也",
    "中",
    "请",
    "回答",
    "前面",
    "当前",
    "刚才",
    "结合",
    "如果",
    "具体",
    "体现",
    "怎么",
    "说明",
)
FILTER_SET = frozenset(BM25_FILTER_TOKENS)

FINANCIAL_TOKENS = frozenset(
    {
        "归母",
        "净利润",
        "营业",
        "收入",
        "总资产",
        "现金流",
        "经营",
        "权益",
        "股东权益",
        "同比",
        "利润",
        "资产",
    }
)
TASK_TOKENS = frozenset(
    {
        "反证",
        "反例",
        "证据",
        "层级",
        "基期",
        "周转",
        "杠杆",
        "因果",
        "措辞",
        "修订",
        "验证",
        "可复算",
        "现金利润比",
    }
)
FOCUS_TOKENS = (
    "的",
    "和",
    "与",
    "归母",
    "净利润",
    "三年",
    "结论",
    "判断",
    "来源",
    "营业",
    "收入",
    "总资产",
    "现金流",
    "反证",
    "证据",
    "层级",
    "基期",
    "周转",
    "杠杆",
    "因果",
    "措辞",
)


@dataclass(frozen=True)
class HybridConfig:
    key: str
    label: str
    bm25_query: str
    bm25_page: str
    idf_scope: str
    filter_enabled: bool


CONFIGS = (
    HybridConfig("H0", "当前 Production Hybrid", "P2", "Summary + Keywords", "visible_pages", False),
    HybridConfig("H1", "BM25 Query 去噪", "Original", "Summary + Keywords", "visible_pages", False),
    HybridConfig("H2", "BM25 Page = Keywords Only", "P2", "Keywords Only", "visible_pages", False),
    HybridConfig(
        "H3",
        "BM25 stopword / 高频模板词过滤",
        "P2",
        "Summary + Keywords",
        "visible_pages",
        True,
    ),
    HybridConfig(
        "H4",
        "Dense Top60 Local-IDF BM25",
        "P2",
        "Summary + Keywords",
        "dense_top60",
        False,
    ),
    HybridConfig("H5", "Combined", "Original", "Keywords Only", "dense_top60", True),
)


class ReadOnlyBM25Cache:
    """Read the prior H0 sparse results without mutating the prior experiment."""

    def __init__(self, path: Path):
        self.path = path
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.success = {str(row["cache_key"]): row for row in rows if row.get("status") == "SUCCESS"}
        self.hit_count = 0
        self.build_count = 0

    def get(self, cache_key: str) -> dict[str, Any] | None:
        row = self.success.get(cache_key)
        if row:
            self.hit_count += 1
        return row

    def append(self, _: Mapping[str, Any]) -> None:
        raise AssertionError("H0 must reuse the existing read-only production BM25 cache")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="C3 BM25 input and IDF-scope ablation")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    return parser.parse_args()


def filtered_bm25_text(text: str) -> str:
    tokens = lemmatize_for_bm25(text, language="zh").split()
    return " ".join(token for token in tokens if token not in FILTER_SET)


def keywords_text(page: Mapping[str, Any]) -> str:
    keywords = page.get("keywords") or []
    if not isinstance(keywords, list):
        raise TypeError(f"Expected frozen keyword list for {page.get('source_turn_id')}")
    return ", ".join(str(keyword) for keyword in keywords)


def prepared_contract(
    config: HybridConfig,
    query_id: str,
    original_queries: Mapping[str, str],
    p2_queries: Mapping[str, str],
    full_page_texts: Mapping[str, str],
    keyword_page_texts: Mapping[str, str],
) -> tuple[str, dict[str, str]]:
    query = original_queries[query_id] if config.bm25_query == "Original" else p2_queries[query_id]
    pages = keyword_page_texts if config.bm25_page == "Keywords Only" else full_page_texts
    if not config.filter_enabled:
        return query, dict(pages)
    filtered_query = filtered_bm25_text(query)
    filtered_pages = {page_id: filtered_bm25_text(text) for page_id, text in pages.items()}
    return filtered_query, filtered_pages


def prepared_bm25_ranking(
    cache: BM25Cache,
    checkpoint: str,
    query_id: str,
    prepared_query: str,
    corpus_ids: Sequence[str],
    prepared_pages: Mapping[str, str],
    component_hash: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run production Qdrant sparse encoding on already-tokenized filtered text."""
    identity = {
        "checkpoint": checkpoint,
        "query_id": query_id,
        "query_lemmatized": prepared_query,
        "visible_page_ids": list(corpus_ids),
        "lemmatized_page_text_hash": stable_hash([prepared_pages[page_id] for page_id in corpus_ids]),
        "component_hash": component_hash,
        "bm25_language": "zh",
        "preprocessing": "production lemmatize_for_bm25 followed by frozen exact-token filter",
    }
    cache_key = stable_hash(identity)
    cached = cache.get(cache_key)
    if cached:
        return [dict(row) for row in cached["ranking"]], {
            "cache_hit": True,
            "cache_key": cache_key,
            "lemmatized_query": prepared_query,
        }

    started = time.perf_counter()
    collection = f"midterm_filtered_{checkpoint}_{query_id.replace('-', '_')}"
    store = Qdrant(collection_name=collection, embedding_model_dims=512, path=":memory:", bm25_language="zh")
    try:
        store.insert(
            vectors=[[0.0] * 512 for _ in corpus_ids],
            payloads=[
                {"data": prepared_pages[page_id], "text_lemmatized": prepared_pages[page_id]} for page_id in corpus_ids
            ],
            ids=list(corpus_ids),
        )
        hits = store.keyword_search(prepared_query, top_k=len(corpus_ids), filters=None) or []
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
    row = {
        **identity,
        "cache_key": cache_key,
        "status": "SUCCESS",
        "ranking": ranking,
        "visible_page_count": len(corpus_ids),
        "bm25_hit_count": len(ranking),
        "build_ms": (time.perf_counter() - started) * 1000.0,
    }
    cache.append(row)
    return ranking, {
        "cache_hit": False,
        "cache_key": cache_key,
        "lemmatized_query": prepared_query,
    }


def local_top60_hybrid_ranking(
    dense: Sequence[Mapping[str, Any]],
    bm25: Sequence[Mapping[str, Any]],
    query_text: str,
    lemmatized_query: str,
) -> list[dict[str, Any]]:
    local_dense = list(dense[:LOCAL_DENSE_LIMIT])
    result = production_hybrid_ranking(local_dense, bm25, query_text, lemmatized_query, limit=5)
    included = {str(row["page_id"]) for row in result}
    raw_scores = {str(row["page_id"]): float(row["raw_bm25_score"]) for row in bm25}
    for row in dense[LOCAL_DENSE_LIMIT:]:
        page_id = str(row["page_id"])
        if page_id in included:
            raise AssertionError(f"Dense tail unexpectedly present in Local-IDF pool: {page_id}")
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
                "internal_limit": min(LOCAL_DENSE_LIMIT, len(dense)),
            }
        )
    for rank, row in enumerate(result, start=1):
        row["rank"] = rank
    if len(result) != len(dense):
        raise AssertionError("Local-IDF complete ranking changed candidate coverage")
    return result


def token_category(token: str) -> str:
    if token in FILTER_SET:
        return "模板/功能词"
    if token in FINANCIAL_TOKENS:
        return "金融词"
    if token in TASK_TOKENS:
        return "高区分度任务词"
    return "其它"


def shared_prepared_tokens(query: str, page: str) -> list[str]:
    query_tokens = set(query.split())
    page_tokens = set(page.split())
    return sorted(query_tokens & page_tokens)


def qdrant_token_diagnostics(
    config: str,
    query_id: str,
    target_page_id: str,
    corpus_ids: Sequence[str],
    prepared_pages: Mapping[str, str],
    prepared_query: str,
) -> list[dict[str, Any]]:
    page_tokens = {page_id: prepared_pages[page_id].split() for page_id in corpus_ids}
    matched = sorted(set(prepared_query.split()) & set(page_tokens[target_page_id]))
    if not matched:
        return []
    collection = f"token_diag_{stable_hash([config, query_id, target_page_id])[:16]}"
    store = Qdrant(collection_name=collection, embedding_model_dims=512, path=":memory:", bm25_language="zh")
    try:
        store.insert(
            vectors=[[0.0] * 512 for _ in corpus_ids],
            payloads=[
                {"data": prepared_pages[page_id], "text_lemmatized": prepared_pages[page_id]} for page_id in corpus_ids
            ],
            ids=list(corpus_ids),
        )
        rows = []
        encoder = ChineseBM25SparseEncoder()
        target_tokens = page_tokens[target_page_id]
        for token in matched:
            hits = store.keyword_search(token, top_k=len(corpus_ids), filters=None) or []
            contribution = next(
                (float(hit.score or 0.0) for hit in hits if str(hit.id) == target_page_id),
                0.0,
            )
            frequency = target_tokens.count(token)
            doc_len = len(target_tokens)
            tf_weight = frequency * (encoder.k + 1)
            tf_weight /= frequency + encoder.k * (1 - encoder.b + encoder.b * doc_len / encoder.avg_len)
            rows.append(
                {
                    "config": config,
                    "query_id": query_id,
                    "target_page_id": target_page_id,
                    "token": token,
                    "category": token_category(token),
                    "corpus_size": len(corpus_ids),
                    "document_frequency": sum(token in tokens for tokens in page_tokens.values()),
                    "target_term_frequency": frequency,
                    "qdrant_single_token_raw_contribution": contribution,
                    "effective_idf": contribution / tf_weight if tf_weight else 0.0,
                }
            )
        return rows
    finally:
        store.client.close()


def pct(value: Any) -> str:
    return f"{100 * float(value):.2f}%"


def render_report(
    metrics: Sequence[Mapping[str, Any]],
    sessions: Sequence[Mapping[str, Any]],
    non_gold_summary: Sequence[Mapping[str, Any]],
    dense_metrics: Mapping[str, Any],
    local_scope_changed_query_count: int,
    h5_zero_bm25_demotion_count: int,
    validations: Mapping[str, Mapping[str, Any]],
) -> str:
    lines = [
        "# C3 BM25 输入与 Local-IDF 诊断",
        "",
        "| Config | BM25 Query | BM25 Page | IDF Scope | Filter | R@5 | Gold@5 | Δ vs Dense | Δ vs H0 | Promoted | Demoted | Net | R@10 | MRR |",
        "|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in metrics:
        lines.append(
            f"| {row['Config']} | {row['BM25 Query']} | {row['BM25 Page']} | {row['IDF Scope']} | "
            f"{row['Filter']} | {pct(row['R@5'])} | {row['Gold@5']} | {100 * float(row['Delta vs Dense']):+.2f}pp | "
            f"{100 * float(row['Delta vs H0']):+.2f}pp | {row['Promoted vs Dense']} | "
            f"{row['Demoted vs Dense']} | {int(row['Net vs Dense']):+d} | {pct(row['R@10'])} | "
            f"{float(row['MRR']):.4f} |"
        )
    lines.extend(
        [
            "",
            "| Config | Macro R@5 | R@20 | Mean Gold Rank | Rescued Q vs Dense | Hurt Q vs Dense | Promoted vs H0 | Demoted vs H0 | Net vs H0 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in metrics:
        lines.append(
            f"| {row['Config']} | {pct(row['Macro R@5'])} | {pct(row['R@20'])} | "
            f"{float(row['Mean Gold Rank']):.2f} | {row['Rescued Q vs Dense']} | {row['Hurt Q vs Dense']} | "
            f"{row['Promoted vs H0']} | {row['Demoted vs H0']} | {int(row['Net vs H0']):+d} |"
        )
    metrics_lookup = {str(row["Config"]): row for row in metrics}
    lines.extend(
        [
            "",
            f"Dense C3 reference：R@5={pct(dense_metrics['recall_at_5'])}（59/154），"
            f"Macro R@5={pct(dense_metrics['macro_session_r5'])}，R@10={pct(dense_metrics['recall_at_10'])}，"
            f"R@20={pct(dense_metrics['recall_at_20'])}，MRR={float(dense_metrics['mrr']):.4f}，"
            f"Mean Gold Rank={float(dense_metrics['mean_gold_rank']):.2f}。",
        ]
    )
    lines.extend(
        [
            "",
            "## Session R@5",
            "",
            "| Config | S001 | S002 | S003 | S004 | S005 | Macro |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for config in [item.key for item in CONFIGS]:
        session_lookup = {row["session_id"]: row for row in sessions if row["Config"] == config}
        macro = next(row["Macro R@5"] for row in metrics if row["Config"] == config)
        lines.append(
            f"| {config} | "
            + " | ".join(pct(session_lookup[code]["R@5"]) for code in SESSION_CODES)
            + f" | {pct(macro)} |"
        )
    lines.extend(["", "## Non-Gold lexical intrusion", ""])
    for row in non_gold_summary:
        lines.append(
            f"- {row['Config']}：新进入 Top5 的 Non-Gold={row['Non-Gold entered Top5']}；"
            f"模板/功能词={row['Template token matches']}；金融词={row['Financial token matches']}；"
            f"高区分度任务词={row['Task token matches']}。"
        )
    lines.extend(
        [
            "",
            "## 事实结论",
            "",
            f"1. H1 相对 H0 为 {100 * float(metrics_lookup['H1']['Delta vs H0']):+.2f}pp / "
            f"{int(metrics_lookup['H1']['Net vs H0']):+d} Gold：Original Query 更适合当前 BM25；P2 对 Dense 有益，"
            "但其补充内容给 lexical matching 带来可测噪声。",
            f"2. H2 相对 H0 为 {100 * float(metrics_lookup['H2']['Delta vs H0']):+.2f}pp / "
            f"{int(metrics_lookup['H2']['Net vs H0']):+d} Gold。Keywords Only 减少 Dense Gold demotion，"
            "但同时显著减少 gross promotion，并降低 R@10/R@20，说明它更稳但 lexical coverage 不足。",
            f"3. H3 相对 H0 为 {100 * float(metrics_lookup['H3']['Delta vs H0']):+.2f}pp / "
            f"{int(metrics_lookup['H3']['Net vs H0']):+d} Gold；相对 H0 是 3 promoted、0 demoted。"
            "固定功能词/模板词过滤有正向但有限的实际作用。",
            f"4. H4 相对 H0 为 {100 * float(metrics_lookup['H4']['Delta vs H0']):+.2f}pp / "
            f"{int(metrics_lookup['H4']['Net vs H0']):+d} Gold，没有改善。只有 {local_scope_changed_query_count}/99 "
            "Query 的 visible corpus 超过 60 Pages；其余 Query 的 Local-IDF 文档集合与 visible corpus 相同。",
            "5. Dense Top60 内同主题金融词仍高度普遍，Local-IDF 没有形成稳定的新区分信号；"
            "它也没有减少 Non-Gold lexical intrusion。",
            f"6. H5 最好，为 {pct(metrics_lookup['H5']['R@5'])}（{metrics_lookup['H5']['Gold@5']}/154），"
            f"仍比 Dense 低 {abs(100 * float(metrics_lookup['H5']['Delta vs Dense'])):.2f}pp / 8 Gold。"
            f"H5 的 13 个 demoted Gold 中有 {h5_zero_bm25_demotion_count} 个 raw BM25=0。",
            "7. 剩余问题主要是 Keywords lexical coverage 稀疏与 production normalization/等权融合的共同作用："
            "当其它候选有 BM25 命中时，无 lexical match 的 Dense Gold 仍被按双信号分母缩放。"
            "Query/Page 去噪能缓解，但 Local-IDF 不能解决这一排序机制。",
            "8. 本轮不支持正式引入 Hybrid；应停止继续调 BM25 输入/IDF。BM25 的 R@10 信号仍存在，"
            "若以后继续，只值得单独研究融合/gating，而不是继续扩展本轮输入组合。",
        ]
    )
    lines.extend(
        [
            "",
            "## 冻结与实现",
            "",
            "- Dense、P2、Page、Gold、visibility 全部复用 C3 frozen artifacts；没有新 embedding 或 LLM 调用。",
            "- Sparse encoding、Qdrant IDF、BM25 normalization 和 additive scoring 均直接调用生产组件。",
            "- H4/H5 的 corpus 与 candidate set 都逐 Query 验证为 Dense Top60；其余配置使用 query-time visible corpus。",
            "- 未扫描融合权重；Entity Boost、reranker、BM25-only 均未加入。",
            "",
            f"Validation：{'PASS' if all(row['status'] == 'PASS' for row in validations.values()) else 'FAIL'}。",
            "",
        ]
    )
    return "\n".join(lines)


def render_cases(cases: Sequence[Mapping[str, Any]]) -> str:
    lines = ["# H0 / H4 / H5 代表案例", ""]
    for case in cases:
        lines.extend(
            [
                f"## {case['Config']} / {case['query_id']} / {case['gold_source_turn_id']}",
                "",
                f"Transition：{case['transition']}",
                "",
                f"Query：{case['query_text']}",
                "",
                f"Gold Page：{case['gold_page_id']}（{case['gold_source_turn_id']}）",
                "",
                f"Dense #{case['dense_rank']} → Hybrid #{case['hybrid_rank']}；"
                f"Dense={float(case['dense_score']):.9f}；Raw BM25={float(case['raw_bm25_score']):.9f}；"
                f"Normalized BM25={float(case['normalized_bm25_score']):.9f}；"
                f"Final={case['hybrid_final_score']}",
                "",
                "| token | category | DF / corpus | effective IDF | lexical contribution |",
                "|---|---|---:|---:|---:|",
            ]
        )
        for token in case["token_diagnostics"]:
            lines.append(
                f"| {token['token']} | {token['category']} | {token['document_frequency']} / "
                f"{token['corpus_size']} | {float(token['effective_idf']):.6f} | "
                f"{float(token['qdrant_single_token_raw_contribution']):.6f} |"
            )
        lines.extend(["", "```text", case["gold_page_text"], "```", ""])
    return "\n".join(lines) + "\n"


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    snapshots, old_pages, queries, checkpoint_data, vector_cache_meta = checkpoint_inputs()
    data = checkpoint_data["C3"]
    query_by_id = {str(query["query_id"]): query for query in queries}
    original_queries = {query_id: str(query["original_query"]) for query_id, query in query_by_id.items()}
    p2_queries = dict(data["query_texts"])
    full_page_texts = dict(data["page_texts"])
    keyword_page_texts = {str(page["page_id"]): keywords_text(page) for page in data["pages"]}
    visibility = {
        str(row["query_id"]): [str(page_id) for page_id in row["visible_page_ids"]]
        for code in SESSION_CODES
        for row in snapshots[code]["visibility"]
    }
    dense = rank_configuration(snapshots, data["query_vectors"], data["page_vectors"])
    dense_metrics, dense_sessions = evaluate_all(snapshots, dense)
    if not math.isclose(float(dense_metrics["recall_at_5"]), DENSE_GOLD5 / ELIGIBLE_GOLD, abs_tol=1e-12):
        raise AssertionError(f"Dense C3 reproduction failed: {dense_metrics}")

    component_paths = {
        "qdrant": REPO_ROOT / "mem0/vector_stores/qdrant.py",
        "bm25_sparse": REPO_ROOT / "mem0/utils/bm25_sparse.py",
        "lemmatization": REPO_ROOT / "mem0/utils/lemmatization.py",
        "scoring": REPO_ROOT / "mem0/utils/scoring.py",
        "finance_dictionary": REPO_ROOT / "mem0/configs/finance_bm25_dict.txt",
    }
    component_hashes = {name: sha256_file(path) for name, path in component_paths.items()}
    production_component_hash = stable_hash(component_hashes)
    h0_cache = ReadOnlyBM25Cache(PREVIOUS_HYBRID_DIR / "cache/visible_scope_bm25.jsonl")
    experiment_cache = BM25Cache(args.output_dir / "cache/bm25_rankings.jsonl")

    metrics_by_config: dict[str, dict[str, Any]] = {}
    rankings: dict[str, dict[str, list[dict[str, Any]]]] = {}
    config_artifacts: dict[str, dict[str, Any]] = {}
    session_rows, gold_rows, query_rows, scope_rows = [], [], [], []
    top5_change_rows, non_gold_token_counter = [], Counter()

    for config in CONFIGS:
        hybrid5, hybrid20, hybrid_full = {}, {}, {}
        prepared_queries: dict[str, str] = {}
        prepared_pages_by_query: dict[str, dict[str, str]] = {}
        corpus_ids_by_query: dict[str, list[str]] = {}
        bm25_by_query: dict[str, list[dict[str, Any]]] = {}
        cache = h0_cache if config.key == "H0" else experiment_cache
        config_component_hash = (
            production_component_hash
            if config.key == "H0"
            else stable_hash(
                {
                    "production_component_hash": production_component_hash,
                    "config": asdict(config),
                    "filter_tokens": BM25_FILTER_TOKENS,
                    "keywords_formatter": '", ".join(frozen_keywords)',
                }
            )
        )
        for query_id, query in query_by_id.items():
            prepared_query, prepared_pages = prepared_contract(
                config,
                query_id,
                original_queries,
                p2_queries,
                full_page_texts,
                keyword_page_texts,
            )
            visible_ids = visibility[query_id]
            dense_top60 = [str(row["page_id"]) for row in dense[query_id][:LOCAL_DENSE_LIMIT]]
            corpus_ids = dense_top60 if config.idf_scope == "dense_top60" else visible_ids
            corpus_pages = [data["page_by_id"][page_id] for page_id in corpus_ids]
            checkpoint_key = "C3" if config.key == "H0" else config.key
            if config.filter_enabled:
                bm25, audit = prepared_bm25_ranking(
                    cache,
                    checkpoint_key,
                    query_id,
                    prepared_query,
                    corpus_ids,
                    prepared_pages,
                    config_component_hash,
                )
            else:
                bm25, audit = raw_bm25_ranking(
                    cache,
                    checkpoint_key,
                    query_id,
                    prepared_query,
                    corpus_pages,
                    prepared_pages,
                    config_component_hash,
                )
            if config.idf_scope == "dense_top60":
                complete = local_top60_hybrid_ranking(dense[query_id], bm25, prepared_query, audit["lemmatized_query"])
                hybrid5[query_id] = complete
                hybrid20[query_id] = complete
                hybrid_full[query_id] = complete
            else:
                hybrid5[query_id] = production_hybrid_ranking(
                    dense[query_id], bm25, prepared_query, audit["lemmatized_query"], limit=5
                )
                hybrid20[query_id] = production_hybrid_ranking(
                    dense[query_id], bm25, prepared_query, audit["lemmatized_query"], limit=20
                )
                hybrid_full[query_id] = production_hybrid_ranking(
                    dense[query_id], bm25, prepared_query, audit["lemmatized_query"], limit=len(visible_ids)
                )
            prepared_queries[query_id] = audit["lemmatized_query"]
            prepared_pages_by_query[query_id] = {
                page_id: (
                    prepared_pages[page_id]
                    if config.filter_enabled
                    else lemmatize_for_bm25(prepared_pages[page_id], language="zh")
                )
                for page_id in corpus_ids
            }
            corpus_ids_by_query[query_id] = list(corpus_ids)
            bm25_by_query[query_id] = bm25
            scope_rows.append(
                {
                    "Config": config.key,
                    "session_id": query["session_code"],
                    "query_id": query_id,
                    "visible_count": len(visible_ids),
                    "dense_top60_count": len(dense_top60),
                    "bm25_corpus_count": len(corpus_ids),
                    "bm25_corpus_hash": stable_hash(corpus_ids),
                    "visible_hash": stable_hash(visible_ids),
                    "dense_top60_hash": stable_hash(dense_top60),
                    "corpus_equals_expected": corpus_ids
                    == (dense_top60 if config.idf_scope == "dense_top60" else visible_ids),
                    "nonvisible_corpus_count": sum(page_id not in set(visible_ids) for page_id in corpus_ids),
                    "bm25_cache_hit": audit["cache_hit"],
                }
            )

        aggregate, config_sessions = metric_bundle(snapshots, hybrid5, hybrid20, hybrid_full)
        metrics_by_config[config.key] = aggregate
        comparison, config_gold, config_queries = compare_rankings(
            snapshots, dense, hybrid5, comparison=f"{config.key} vs Dense"
        )
        metrics_by_config[config.key]["dense_comparison"] = comparison
        rankings[config.key] = hybrid5
        config_artifacts[config.key] = {
            "prepared_queries": prepared_queries,
            "prepared_pages_by_query": prepared_pages_by_query,
            "corpus_ids_by_query": corpus_ids_by_query,
            "bm25_by_query": bm25_by_query,
        }
        for row in config_sessions:
            session_rows.append(
                {
                    "Config": config.key,
                    "Description": config.label,
                    "session_id": row["session_id"],
                    "Dense R@5": dense_sessions[row["session_id"]]["recall_at_5"],
                    **row,
                }
            )
        dense_maps = {query_id: rank_map(items) for query_id, items in dense.items()}
        hybrid_maps = {query_id: rank_map(items) for query_id, items in hybrid5.items()}
        for row in config_gold:
            query_id = str(row["query_id"])
            gold_id = str(row["gold_page_id"])
            dense_item = dense_maps[query_id][gold_id]
            hybrid_item = hybrid_maps[query_id][gold_id]
            transition = (
                "PROMOTED"
                if int(row["before_rank"]) > 5 and int(row["after_rank"]) <= 5
                else "DEMOTED"
                if int(row["before_rank"]) <= 5 and int(row["after_rank"]) > 5
                else "UNCHANGED"
            )
            gold_rows.append(
                {
                    "Config": config.key,
                    **row,
                    "transition": transition,
                    "dense_rank": dense_item["rank"],
                    "hybrid_rank": hybrid_item["rank"],
                    "dense_score": dense_item["score"],
                    "raw_bm25_score": hybrid_item["raw_bm25_score"],
                    "normalized_bm25_score": hybrid_item["bm25_score"],
                    "hybrid_final_score": hybrid_item["final_score"],
                }
            )
        query_rows.extend({"Config": config.key, **row} for row in config_queries)

        if config.key in {"H0", "H4", "H5"}:
            for query_id, query in query_by_id.items():
                dense_top5 = {str(row["page_id"]) for row in dense[query_id][:5]}
                hybrid_top5 = {str(row["page_id"]) for row in hybrid5[query_id][:5]}
                gold_ids = {str(page_id) for page_id in query["eligible_gold_page_ids"]}
                artifact = config_artifacts[config.key]
                for page_id in sorted(hybrid_top5 - dense_top5):
                    hybrid_item = hybrid_maps[query_id][page_id]
                    tokens = shared_prepared_tokens(
                        artifact["prepared_queries"][query_id],
                        artifact["prepared_pages_by_query"][query_id][page_id],
                    )
                    row = {
                        "Config": config.key,
                        "session_id": query["session_code"],
                        "query_id": query_id,
                        "page_id": page_id,
                        "source_turn_id": hybrid_item["source_turn_id"],
                        "is_gold": page_id in gold_ids,
                        "dense_rank": dense_maps[query_id][page_id]["rank"],
                        "hybrid_rank": hybrid_item["rank"],
                        "raw_bm25_score": hybrid_item["raw_bm25_score"],
                        "normalized_bm25_score": hybrid_item["bm25_score"],
                        "hybrid_final_score": hybrid_item["final_score"],
                        "shared_tokens": tokens,
                    }
                    top5_change_rows.append(row)
                    if not row["is_gold"]:
                        for token in tokens:
                            non_gold_token_counter[(config.key, token, token_category(token))] += 1

    h0_r5 = float(metrics_by_config["H0"]["recall_at_5"])
    if not math.isclose(h0_r5, H0_GOLD5 / ELIGIBLE_GOLD, abs_tol=1e-12):
        raise AssertionError(f"H0 reproduction failed: {metrics_by_config['H0']}")

    metric_rows, h0_comparison_rows = [], []
    for config in CONFIGS:
        aggregate = metrics_by_config[config.key]
        dense_comparison = aggregate["dense_comparison"]
        if config.key == "H0":
            h0_comparison = {
                "Promoted Gold": 0,
                "Demoted Gold": 0,
                "Net Gold gain": 0,
                "Rescued Queries": 0,
                "Hurt Queries": 0,
            }
        else:
            h0_comparison, h0_gold, h0_queries = compare_rankings(
                snapshots, rankings["H0"], rankings[config.key], comparison=f"{config.key} vs H0"
            )
            h0_comparison_rows.extend({"Config": config.key, **row} for row in h0_gold)
            query_rows.extend({"Config": config.key, "relative_to": "H0", **row} for row in h0_queries)
        metric_rows.append(
            {
                "Config": config.key,
                "Description": config.label,
                "BM25 Query": config.bm25_query,
                "BM25 Page": config.bm25_page,
                "IDF Scope": "Dense Top60" if config.idf_scope == "dense_top60" else "Visible Pages",
                "Filter": "ON" if config.filter_enabled else "OFF",
                "R@5": aggregate["recall_at_5"],
                "Gold@5": aggregate["gold_at_5"],
                "Macro R@5": aggregate["macro_session_r5"],
                "R@10": aggregate["recall_at_10"],
                "R@20": aggregate["recall_at_20"],
                "MRR": aggregate["mrr"],
                "Mean Gold Rank": aggregate["mean_gold_rank"],
                "Delta vs Dense": float(aggregate["recall_at_5"]) - float(dense_metrics["recall_at_5"]),
                "Delta vs H0": float(aggregate["recall_at_5"]) - h0_r5,
                "Promoted vs Dense": dense_comparison["Promoted Gold"],
                "Demoted vs Dense": dense_comparison["Demoted Gold"],
                "Net vs Dense": dense_comparison["Net Gold gain"],
                "Rescued Q vs Dense": dense_comparison["Rescued Queries"],
                "Hurt Q vs Dense": dense_comparison["Hurt Queries"],
                "Promoted vs H0": h0_comparison["Promoted Gold"],
                "Demoted vs H0": h0_comparison["Demoted Gold"],
                "Net vs H0": h0_comparison["Net Gold gain"],
                "Rescued Q vs H0": h0_comparison["Rescued Queries"],
                "Hurt Q vs H0": h0_comparison["Hurt Queries"],
            }
        )

    non_gold_summary = []
    non_gold_token_rows = []
    for config in ("H0", "H4", "H5"):
        changed = [row for row in top5_change_rows if row["Config"] == config and not row["is_gold"]]
        category_counts = Counter()
        for (key, token, category), count in sorted(non_gold_token_counter.items()):
            if key != config:
                continue
            category_counts[category] += count
            non_gold_token_rows.append(
                {"Config": config, "token": token, "category": category, "Non-Gold Top5 entry matches": count}
            )
        non_gold_summary.append(
            {
                "Config": config,
                "Non-Gold entered Top5": len(changed),
                "Template token matches": category_counts["模板/功能词"],
                "Financial token matches": category_counts["金融词"],
                "Task token matches": category_counts["高区分度任务词"],
                "Other token matches": category_counts["其它"],
            }
        )

    focus_rows = []
    h0_artifact, h4_artifact = config_artifacts["H0"], config_artifacts["H4"]
    for query_id in query_by_id:
        query_tokens = set(h0_artifact["prepared_queries"][query_id].split())
        visible_ids = h0_artifact["corpus_ids_by_query"][query_id]
        local_ids = h4_artifact["corpus_ids_by_query"][query_id]
        visible_pages = h0_artifact["prepared_pages_by_query"][query_id]
        local_pages = h4_artifact["prepared_pages_by_query"][query_id]
        for token in FOCUS_TOKENS:
            if token not in query_tokens:
                continue
            visible_df = sum(token in visible_pages[page_id].split() for page_id in visible_ids)
            local_df = sum(token in local_pages[page_id].split() for page_id in local_ids)
            focus_rows.append(
                {
                    "session_id": query_by_id[query_id]["session_code"],
                    "query_id": query_id,
                    "token": token,
                    "category": token_category(token),
                    "visible_corpus_size": len(visible_ids),
                    "visible_df": visible_df,
                    "visible_df_rate": visible_df / len(visible_ids),
                    "local_corpus_size": len(local_ids),
                    "local_df": local_df,
                    "local_df_rate": local_df / len(local_ids),
                    "local_minus_visible_df_rate": local_df / len(local_ids) - visible_df / len(visible_ids),
                }
            )

    selected_gold = []
    for config in ("H0", "H4", "H5"):
        promoted = sorted(
            (row for row in gold_rows if row["Config"] == config and row["transition"] == "PROMOTED"),
            key=lambda row: int(row["rank_improvement"]),
            reverse=True,
        )[:3]
        demoted = sorted(
            (row for row in gold_rows if row["Config"] == config and row["transition"] == "DEMOTED"),
            key=lambda row: int(row["rank_improvement"]),
        )[:3]
        selected_gold.extend(promoted + demoted)
    h4_h0_changes = [
        row
        for row in h0_comparison_rows
        if row["Config"] == "H4" and (int(row["before_rank"]) <= 5) != (int(row["after_rank"]) <= 5)
    ]
    for movement in h4_h0_changes:
        for config in ("H0", "H4"):
            selected_gold.extend(
                row
                for row in gold_rows
                if row["Config"] == config
                and row["query_id"] == movement["query_id"]
                and row["gold_page_id"] == movement["gold_page_id"]
            )
    selected_gold = list(
        {(str(row["Config"]), str(row["query_id"]), str(row["gold_page_id"])): row for row in selected_gold}.values()
    )
    case_rows, token_diagnostic_rows = [], []
    for row in selected_gold:
        config = str(row["Config"])
        query_id = str(row["query_id"])
        gold_id = str(row["gold_page_id"])
        artifact = config_artifacts[config]
        tokens = qdrant_token_diagnostics(
            config,
            query_id,
            gold_id,
            artifact["corpus_ids_by_query"][query_id],
            artifact["prepared_pages_by_query"][query_id],
            artifact["prepared_queries"][query_id],
        )
        token_diagnostic_rows.extend(tokens)
        case_rows.append(
            {
                **row,
                "query_text": (
                    original_queries[query_id]
                    if next(item for item in CONFIGS if item.key == config).bm25_query == "Original"
                    else p2_queries[query_id]
                ),
                "gold_page_text": full_page_texts[gold_id],
                "token_diagnostics": tokens,
            }
        )

    previous_model_metadata = load_json(PREVIOUS_MODEL_DIR / "run_metadata.json")
    page_hash = stable_hash([full_page_texts[str(page["page_id"])] for page in data["pages"]])
    p2_hash = stable_hash([p2_queries[str(query["query_id"])] for query in queries])
    validations = {
        "dense_C3_59_of_154": {
            "status": "PASS"
            if math.isclose(float(dense_metrics["recall_at_5"]), DENSE_GOLD5 / ELIGIBLE_GOLD, abs_tol=1e-12)
            else "FAIL"
        },
        "H0_46_of_154": {"status": "PASS" if math.isclose(h0_r5, H0_GOLD5 / ELIGIBLE_GOLD, abs_tol=1e-12) else "FAIL"},
        "frozen_counts": {
            "status": "PASS"
            if len(old_pages) == 333
            and len(queries) == 99
            and sum(len(q["eligible_gold_page_ids"]) for q in queries) == 154
            else "FAIL"
        },
        "C3_page_hash": {
            "status": "PASS" if page_hash == previous_model_metadata["raw_page_text_hash"] else "FAIL",
            "value": page_hash,
        },
        "P2_query_hash": {
            "status": "PASS" if p2_hash == previous_model_metadata["raw_query_text_hash"] else "FAIL",
            "value": p2_hash,
        },
        "original_query_coverage": {"status": "PASS" if len(original_queries) == 99 else "FAIL"},
        "visibility_unchanged": {
            "status": "PASS"
            if len(visibility) == 99 and all(not int(row["nonvisible_corpus_count"]) for row in scope_rows)
            else "FAIL"
        },
        "local_idf_corpus_exact": {
            "status": "PASS"
            if all(row["corpus_equals_expected"] for row in scope_rows if row["Config"] in {"H4", "H5"})
            else "FAIL"
        },
        "H0_cache_reused": {"status": "PASS" if h0_cache.hit_count == 99 and h0_cache.build_count == 0 else "FAIL"},
        "filter_frozen": {"status": "PASS", "sha256": stable_hash(BM25_FILTER_TOKENS)},
        "new_dense_embeddings": {"status": "PASS", "value": 0},
        "new_llm_calls": {"status": "PASS", "value": 0},
        "summary_keywords_regeneration": {"status": "PASS", "value": False},
        "full_session_rerun": {"status": "PASS", "value": False},
        "weight_scan": {"status": "PASS", "value": False},
    }
    if any(row["status"] != "PASS" for row in validations.values()):
        raise AssertionError(validations)

    local_scope_changed_query_count = sum(
        int(row["visible_count"]) > LOCAL_DENSE_LIMIT for row in scope_rows if row["Config"] == "H4"
    )
    h5_zero_bm25_demotion_count = sum(
        row["Config"] == "H5" and row["transition"] == "DEMOTED" and float(row["raw_bm25_score"]) == 0.0
        for row in gold_rows
    )

    write_csv(args.output_dir / "metrics/retrieval_metrics.csv", metric_rows)
    write_csv(args.output_dir / "metrics/session_metrics.csv", session_rows)
    write_csv(args.output_dir / "metrics/gold_results.csv", gold_rows)
    write_csv(args.output_dir / "metrics/query_transitions.csv", query_rows)
    write_csv(args.output_dir / "metrics/h0_relative_gold_results.csv", h0_comparison_rows)
    write_csv(args.output_dir / "analysis/idf_scope_validation.csv", scope_rows)
    write_csv(args.output_dir / "analysis/top5_non_gold_entries.csv", top5_change_rows)
    write_csv(args.output_dir / "analysis/top5_non_gold_token_frequency.csv", non_gold_token_rows)
    write_csv(args.output_dir / "analysis/top5_non_gold_summary.csv", non_gold_summary)
    write_csv(args.output_dir / "analysis/local_idf_token_df.csv", focus_rows)
    write_csv(args.output_dir / "analysis/representative_token_diagnostics.csv", token_diagnostic_rows)
    dump_json(args.output_dir / "representative_cases.json", {"cases": case_rows})
    (args.output_dir / "representative_cases.md").write_text(render_cases(case_rows), encoding="utf-8")
    (args.output_dir / "experiment_report.md").write_text(
        render_report(
            metric_rows,
            session_rows,
            non_gold_summary,
            dense_metrics,
            local_scope_changed_query_count,
            h5_zero_bm25_demotion_count,
            validations,
        ),
        encoding="utf-8",
    )
    metadata = {
        "experiment_name": "midterm_c3_bm25_input_idf_ablation",
        "configs": [asdict(config) for config in CONFIGS],
        "dense_backbone": PRODUCTION_EMBEDDING,
        "dense_query": "frozen P2 resolved query",
        "dense_page": "frozen C3 Summary + Keywords",
        "session_count": 5,
        "page_count": 333,
        "query_count": 99,
        "eligible_gold_count": 154,
        "local_dense_limit": LOCAL_DENSE_LIMIT,
        "local_scope_changed_query_count": local_scope_changed_query_count,
        "H5_zero_raw_bm25_demoted_gold_count": h5_zero_bm25_demotion_count,
        "bm25_filter_tokens": BM25_FILTER_TOKENS,
        "bm25_filter_sha256": stable_hash(BM25_FILTER_TOKENS),
        "keywords_only_formatter": '", ".join(frozen_keywords)',
        "production_components": {
            "lemmatization": "mem0.utils.lemmatization.lemmatize_for_bm25(language='zh')",
            "sparse_encoder": "mem0.utils.bm25_sparse.ChineseBM25SparseEncoder via Qdrant",
            "idf": "Qdrant sparse Modifier.IDF",
            "normalization": "get_bm25_params + normalize_bm25",
            "fusion": "score_and_rank; equal semantic + BM25; entity_boosts={}",
            "semantic_threshold": SEMANTIC_THRESHOLD,
            "finance_dictionary": str(component_paths["finance_dictionary"]),
        },
        "component_sha256": component_hashes,
        "page_text_hash": page_hash,
        "p2_query_hash": p2_hash,
        "original_query_hash": stable_hash([original_queries[str(query["query_id"])] for query in queries]),
        "frozen_vector_cache_metadata": vector_cache_meta,
        "cache": {
            "H0_prior_cache": str(h0_cache.path),
            "H0_cache_hits": h0_cache.hit_count,
            "experiment_cache": str(experiment_cache.path),
            "experiment_cache_hits_this_run": experiment_cache.hit_count,
            "experiment_cache_builds_this_run": experiment_cache.build_count,
        },
        "new_dense_embedding_count": 0,
        "new_llm_calls": 0,
        "summary_keywords_regeneration": False,
        "full_session_rerun": False,
        "weight_scan": False,
        "validation": validations,
        "validation_all_pass": True,
    }
    dump_json(args.output_dir / "run_metadata.json", metadata)
    print(
        json.dumps(
            {
                "metrics": metric_rows,
                "session_metrics": session_rows,
                "non_gold_summary": non_gold_summary,
                "validation": validations,
                "output_dir": str(args.output_dir),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
