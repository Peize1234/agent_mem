#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
根据 experiment_paths_s001_s005.yaml 自动生成实验路径图。

特点：
1. YAML 内部 id/config 完全不改，只修改“图上显示文本”。
2. 自动隐藏 Q5 / P8 / E1 / C3 / RF1 / SF1 / PR1 等内部实验编号。
3. 尽量将 Query / Page / Rerank / Candidate / status 等显示为中文。
4. 保留专业缩写或指标：R@5、R@10、R@20、MRR、NDCG、BM25、RRF、DeepSeek、Embedding 模型名等。
5. 支持 full / collapsed / main 三种模式。
6. 支持 PNG / SVG / PDF。
"""

import argparse
import os
import re
from collections import defaultdict

import yaml
from graphviz import Digraph


# ============================================================
# 1. 显示文本
# ============================================================

STATUS_ZH = {
    "baseline": "基线",
    "best": "阶段最佳",
    "improved": "有提升",
    "completed": "已完成",
    "degraded": "效果下降",
    "tied": "基本持平",
    "blocked": "未完成",
    "diagnostic": "诊断",
    "oracle": "理想上界",
}

METRIC_ZH = {
    "r5": "R@5",
    "r10": "R@10",
    "r20": "R@20",
    "mrr": "MRR",
    "ndcg": "NDCG",
    "ndcg@5": "NDCG@5",
    "gold_at_5": "Top5 Gold 命中",
    "candidate_recall": "候选召回率",
    "heldout_micro_r5": "验证集 Micro R@5",
    "heldout_macro_session_r5": "验证集 Macro R@5",
    "delta_micro_pp": "Micro R@5 变化",
    "warm_e2e_p95_ms": "端到端 P95 延迟",
    "eligible_gold": "有效 Gold 数",
    "baseline_r5": "基线 R@5",
    "variant_r5": "实验 R@5",
    "promoted": "进入 Top5",
    "demoted": "移出 Top5",
    "net": "净增 Gold",
    "candidate_r20": "候选 R@20",
    "mean_gold_rank": "Gold 平均排名",
    "llm_calls_per_query": "每 Query LLM 调用",
    "dense_gold_at_5": "稠密检索 Top5 Gold",
    "gold5": "Top5 Gold",
}

GROUP_TITLE_ZH = {
    "G0": "0. 基线与评测设置",
    "G1": "1. S001 初始检索方案探索",
    "G2": "2. S001 重排序与融合调优",
    "G3": "3. 问题改写与引用解析（S001–S005）",
    "G4": "4. 记忆生成、上下文与 Prompt 实验（S001–S005）",
    "G5": "5. 记忆页表征实验（S001–S005）",
    "G6": "6. 向量模型实验（S001–S005）",
    "G7": "7. BM25 与混合检索实验（S001–S005）",
    "G8": "8. 多向量与重排序诊断（S001–S005）",
    "G9": "9. 冻结配置的多 Session 验证",
}


# 这些是“显示层”的替换，不影响 YAML 原始内容。
TEXT_REPLACEMENTS = [
    # 基本术语
    ("Initial Retrieval Exploration", "初始检索方案探索"),
    ("Query Ablation", "检索问题对比实验"),
    ("Query Rewrite", "问题改写"),
    ("Reference Resolution", "引用解析"),
    ("Page Representation", "记忆页表征"),
    ("Page representation", "记忆页表征"),
    ("Candidate Depth", "候选深度"),
    ("Candidate Source", "候选来源"),
    ("Candidate", "候选"),
    ("candidate", "候选"),
    ("Rerank Tuning", "重排序调优"),
    ("Reranker", "重排序器"),
    ("reranker", "重排序器"),
    ("Rerank", "重排序"),
    ("rerank", "重排序"),
    ("Rank Fusion", "排序融合"),
    ("Score Fusion", "分数融合"),
    ("Protection", "保护策略"),
    ("Baseline", "基线"),
    ("baseline", "基线"),
    ("Best Quality", "最佳质量方案"),
    ("Best Stable", "最佳稳定方案"),
    ("Best Practical", "最佳实用方案"),
    ("Best Low-Latency", "最佳低延迟方案"),
    ("Best Local", "最佳本地方案"),
    ("Best query only", "仅优化检索问题"),
    ("Best representation + embedding only", "仅优化表征与向量模型"),
    ("Candidate expansion + local rerank only", "仅扩展候选并进行本地重排序"),
    ("Best no-online-LLM", "最佳无在线 LLM 方案"),
    ("Best quality", "最佳质量方案"),
    ("Best practical", "最佳实用方案"),

    # Query
    ("Current user query", "原始问题"),
    ("Current Query", "当前问题"),
    ("Original Query", "原始问题"),
    ("Original query", "原始问题"),
    ("Original + standalone rewrite", "原始问题 + 独立改写"),
    ("DeepSeek no-thinking standalone rewrite", "DeepSeek 关闭思考的独立改写"),
    ("standalone rewrite", "独立改写"),
    ("Two-query RRF", "双路问题 RRF 融合"),
    ("required_context Oracle", "required_context 理想上界"),
    ("Current + previous 1 QA", "当前问题 + 前 1 轮问答"),
    ("Current + previous 2 QA", "当前问题 + 前 2 轮问答"),
    ("Current + previous 3 QA", "当前问题 + 前 3 轮问答"),
    ("previous 1 QA", "前 1 轮问答"),
    ("previous 2 QA", "前 2 轮问答"),
    ("previous 3 QA", "前 3 轮问答"),
    ("Clean Replacement Ablation", "纯替换消融实验"),
    ("Old rewrite only", "旧问题改写方案"),
    ("Conservative reference resolution only", "仅使用保守引用解析"),
    ("Explicit-reference prompt", "显式引用 Prompt"),
    ("Slot-bounded reference resolution", "槽位约束引用解析"),
    ("One-hop dependency prompt", "单跳依赖 Prompt"),
    ("Identity-only reference resolution", "仅补全对象身份的引用解析"),
    ("Current conservative reference-resolution", "当前保守引用解析"),

    # Page / Add
    ("summary + keywords + user_input", "摘要 + 关键词 + 用户问题"),
    ("summary + keywords + user", "摘要 + 关键词 + 用户问题"),
    ("summary + keywords", "摘要 + 关键词"),
    ("summary + user", "摘要 + 用户问题"),
    ("summary only", "仅摘要"),
    ("keywords + user", "关键词 + 用户问题"),
    ("user only", "仅用户问题"),
    ("user_input", "用户问题"),
    ("raw_dialogue", "原始对话"),
    ("user_input + assistant_response", "用户问题 + 助手回答"),
    ("user_input + summary", "用户问题 + 摘要"),
    ("summary + raw_dialogue", "摘要 + 原始对话"),
    ("user_input + keywords", "用户问题 + 关键词"),
    ("Production Add", "原始记忆生成"),
    ("Production Old", "原始记忆生成"),
    ("Add-Old", "原始记忆生成"),
    ("Add-New single-turn retrieval", "单轮检索导向记忆生成"),
    ("Add-New", "新记忆生成"),
    ("Add Prompt", "记忆生成 Prompt"),
    ("Context Add Prompt Candidates", "上下文记忆 Prompt 候选"),
    ("Eviction-time Local Context", "淘汰时局部上下文"),
    ("previous + following context", "前文 + 后文上下文"),
    ("previous context", "前文上下文"),
    ("following context", "后文上下文"),
    ("current User+Assistant", "当前用户问题 + 助手回答"),
    ("Production Add reference", "原始记忆生成参考方案"),
    ("Current previous+following Prompt", "当前前后文 Prompt"),
    ("Relation-resolution first; no context fact expansion", "关系解析优先，禁止上下文事实扩写"),
    ("Current-turn first + one relation sentence", "当前轮信息优先 + 一句关系补充"),
    ("Distinctive information first; suppress repetition", "区分性信息优先，抑制重复内容"),
    ("Task-first", "任务优先"),
    ("Task + dependency", "任务 + 依赖关系"),
    ("Production-old-lite", "原始方案精简版"),
    ("Minimal change", "最小改动"),
    ("Multi retrieval entry", "多检索入口保留"),
    ("Task + evidence chain", "任务 + 证据链保留"),
    ("Conservative Retrieval-tuned Add Prompt", "保守检索导向记忆 Prompt"),

    # Dense / BM25 / Hybrid
    ("Dense / BM25 / Hybrid", "稠密检索 / BM25 / 混合检索"),
    ("Dense+BM25 RRF", "稠密检索 + BM25 的 RRF 融合"),
    ("Dense + Chinese BM25", "稠密检索 + 中文 BM25"),
    ("Dense", "稠密检索"),
    ("dense", "稠密检索"),
    ("Hybrid", "混合检索"),
    ("hybrid", "混合检索"),
    ("Fixed-budget Dense/BM25 union", "固定预算的稠密检索 / BM25 候选并集"),
    ("Expanded union", "扩展候选并集"),
    ("union_fixed", "固定预算并集"),
    ("expanded_union", "扩展并集"),
    ("weighted", "加权"),
    ("normalized fusion", "归一化融合"),
    ("BM25 Query denoising", "BM25 查询去噪"),
    ("BM25 Page = Keywords Only", "BM25 记忆页仅使用关键词"),
    ("Stopword/high-frequency template filter", "停用词 / 高频模板词过滤"),
    ("Dense Top60 Local-IDF BM25", "稠密 Top60 局部 IDF BM25"),
    ("Combined", "组合方案"),
    ("Production Hybrid", "当前混合检索方案"),
    ("Input / IDF Scope", "输入与 IDF 范围"),
    ("Fusion Weight Sweep", "融合权重消融"),
    ("checkpoint", "阶段节点"),

    # Embedding
    ("Embedding Model Exploration", "向量模型探索"),
    ("Embedding Model", "向量模型"),
    ("Embedding", "向量模型"),
    ("Financial / Lightweight Embeddings", "金融领域 / 轻量向量模型"),
    ("current baseline", "当前基线"),
    ("completed", "已完成"),
    ("blocked by local resources", "受本地资源限制，未完成"),
    ("official artifacts/credentials unavailable", "缺少官方模型产物或凭据，未完成"),
    ("local resource/runtime impractical", "本地资源或耗时不可接受，未完成"),

    # Rerank
    ("First-stage Rerank", "第一阶段重排序"),
    ("No rerank", "不进行重排序"),
    ("Local rerank input", "本地重排序输入"),
    ("DeepSeek rerank input", "DeepSeek 重排序输入"),
    ("Local reranker", "本地重排序器"),
    ("DeepSeek reranker", "DeepSeek 重排序器"),
    ("reranker-only", "仅重排序器排序"),
    ("Candidate Top", "候选 Top"),
    ("Top2 protection", "Top2 保护"),
    ("Top3 protection", "Top3 保护"),
    ("equal RRF", "等权 RRF"),
    ("candidate weight", "候选排序权重"),
    ("reranker weight", "重排序器权重"),

    # Field-aware
    ("Field-aware Multi-vector", "字段感知多向量检索"),
    ("Task", "任务"),
    ("Fact", "事实"),
    ("Relation", "关系"),
    ("Multi-vector", "多向量检索"),

    # Validation
    ("Frozen S001 Chains → S002-S005 Held-out", "S001 冻结方案 → S002–S005 验证"),
    ("Held-out", "验证集"),
    ("Quality K20", "质量方案（候选数 20）"),
    ("Pareto K15 + Top2 protection", "Pareto 方案（候选数 15 + Top2 保护）"),
    ("Cross-session", "跨 Session"),
    ("Multi-session", "多 Session"),
    ("Validation", "验证"),
    ("validation", "验证"),

    # Misc
    ("Prompt Candidates", "Prompt 候选"),
    ("Prompt Multi-candidate", "多 Prompt 候选"),
    ("Prompt", "Prompt"),
    ("Cross Ablation", "交叉消融"),
    ("Ablation", "消融实验"),
    ("Decomposition", "拆解实验"),
    ("Diagnosis", "诊断"),
    ("Diagnostics", "诊断"),
    ("Current Local", "当前本地方案"),
    ("Current", "当前"),
    ("Original", "原始"),
    ("Old", "原始方案"),
    ("New", "新方案"),
    ("Quality", "质量方案"),
    ("Pareto", "Pareto 方案"),
]


# ============================================================
# 2. 样式
# ============================================================

STATUS_STYLE = {
    "baseline": {
        "fillcolor": "#EEF2F7",
        "color": "#6C7A89",
        "fontcolor": "#25313C",
        "style": "filled,rounded",
    },
    "best": {
        "fillcolor": "#FDEBEC",
        "color": "#B3262E",
        "fontcolor": "#5A1015",
        "style": "filled,rounded,bold",
    },
    "improved": {
        "fillcolor": "#F4E7E8",
        "color": "#A9444B",
        "fontcolor": "#552126",
        "style": "filled,rounded",
    },
    "completed": {
        "fillcolor": "#FAFAFA",
        "color": "#A7A7A7",
        "fontcolor": "#333333",
        "style": "filled,rounded",
    },
    "degraded": {
        "fillcolor": "#F3F3F3",
        "color": "#B5B5B5",
        "fontcolor": "#666666",
        "style": "filled,rounded",
    },
    "tied": {
        "fillcolor": "#F4F6F8",
        "color": "#81909E",
        "fontcolor": "#34424E",
        "style": "filled,rounded",
    },
    "blocked": {
        "fillcolor": "#FFFFFF",
        "color": "#A7A7A7",
        "fontcolor": "#777777",
        "style": "rounded,dashed",
    },
    "diagnostic": {
        "fillcolor": "#FFFFFF",
        "color": "#7D7D7D",
        "fontcolor": "#555555",
        "style": "rounded",
    },
    "oracle": {
        "fillcolor": "#FFF9E8",
        "color": "#B18A00",
        "fontcolor": "#6B5400",
        "style": "rounded,dotted",
    },
}

EDGE_STYLE = {
    "main_path": {"color": "#A6242F", "penwidth": "2.5"},
    "heldout_validation": {"color": "#6F4A8E", "penwidth": "2.0"},
    "branch": {"color": "#AFAFAF", "style": "dashed"},
    "ablation": {"color": "#BBBBBB"},
    "grid": {"color": "#CDCDCD"},
    "fusion": {"color": "#9670B6"},
    "candidate_source": {"color": "#6E8FA6"},
    "candidate_k": {"color": "#6E8FA6"},
    "representation": {"color": "#6C8D82"},
    "model_ablation": {"color": "#8D99AE"},
    "prompt_ablation": {"color": "#B58A62"},
    "weight_ablation": {"color": "#A66D5C"},
    "context_ablation": {"color": "#88758D"},
    "checkpoint": {"color": "#6D8798"},
    "diagnosis": {"color": "#888888", "style": "dotted"},
    "transfer": {"color": "#6C8D82", "penwidth": "1.6"},
    "combination": {"color": "#9C6671"},
    "multi_vector": {"color": "#6F7F9A"},
    "model_ablation": {"color": "#8D99AE"},
}
DEFAULT_EDGE_STYLE = {"color": "#BDBDBD", "penwidth": "1.0"}

GROUP_COLORS = [
    "#F8FAFC",
    "#F4F7FB",
    "#FAF7F7",
    "#F8F7FA",
    "#F7F9F8",
    "#FAF9F5",
    "#F7F8FA",
    "#F9F7F7",
    "#F7F8FA",
    "#FAF7F8",
]


# ============================================================
# 3. 文本处理
# ============================================================

def remove_internal_prefix(text: str) -> str:
    """
    去掉图上没有必要出现的内部实验编号。
    例如：
      Q5 — 原始问题 + 独立改写 -> 原始问题 + 独立改写
      P8 — summary + keywords -> 摘要 + 关键词
      RF2_reranker_weight_2 -> 重排序器权重 2
    """
    value = str(text).strip()

    # 行首常见内部编号
    patterns = [
        r"^(?:QO|Q\d+)\s*[—\-:]\s*",
        r"^P\d+\s*[—\-:]\s*",
        r"^E\d+(?:-\d+)?\s*[—\-:]\s*",
        r"^C\d+\s*[—\-:]\s*",
        r"^R\d+\s*[—\-:]\s*",
        r"^H\d+\s*[—\-:]\s*",
        r"^LQ\d+(?:_[A-Z_]+)?\s*[—\-:]\s*",
        r"^T\d+\s*[—\-:]\s*",
    ]
    for pattern in patterns:
        value = re.sub(pattern, "", value, flags=re.IGNORECASE)

    # 单独一整段就是内部策略 id 时，转成更可读形式
    value = re.sub(r"\bRF0_reranker_only\b", "仅重排序器排序", value, flags=re.IGNORECASE)
    value = re.sub(r"\bRF1_equal_rrf_c(\d+)\b", r"等权 RRF（常数 \1）", value, flags=re.IGNORECASE)
    value = re.sub(
        r"\bRF2_reranker_weight_([0-9.]+)\b",
        r"提高重排序器权重（\1）",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\bRF3_candidate_weight_([0-9.]+)\b",
        r"提高候选排序权重（\1）",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\bSF\d+_candidate_([0-9.]+)_reranker_([0-9.]+)\b",
        r"分数融合（候选 \1 / 重排序 \2）",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\bPR1_candidate_top(\d+)_protection\b",
        r"候选 Top\1 保护",
        value,
        flags=re.IGNORECASE,
    )

    # K20 这种内部写法改为候选数
    value = re.sub(r"\bK\s*=\s*(\d+)\b", r"候选数 = \1", value)
    value = re.sub(r"\bK(\d+)\b", r"候选数 \1", value)

    return value.strip()


def translate_text(text: str) -> str:
    if not text:
        return ""

    result = str(text)

    # 先去内部编号
    result = remove_internal_prefix(result)

    # 再做术语翻译
    for old, new in TEXT_REPLACEMENTS:
        result = result.replace(old, new)

    # 进一步清理常见机械英文
    result = re.sub(r"\bstatus\s*=\s*", "结果：", result, flags=re.IGNORECASE)
    result = re.sub(r"\bmax_length\s*=\s*", "最大长度 = ", result, flags=re.IGNORECASE)
    result = re.sub(r"\bcandidate_k\s*=\s*", "候选数 = ", result, flags=re.IGNORECASE)
    result = re.sub(r"\bTop[- ]?K\b", "TopK", result, flags=re.IGNORECASE)

    # 避免重复中文
    result = result.replace("结果：已完成已完成", "结果：已完成")

    return result.strip()


def format_metric_value(key, value):
    if isinstance(value, float):
        if key == "delta_micro_pp":
            return f"{value:+.2f} pp"
        if "ms" in key:
            return f"{value:.2f} ms"

        # Recall/MRR 等保留小数形式，与实验原始结果一致
        return f"{value:.4f}"

    return str(value)


def metric_text(metrics: dict, max_items: int = 4) -> str:
    if not metrics:
        return ""

    preferred_order = [
        "r5",
        "heldout_micro_r5",
        "heldout_macro_session_r5",
        "gold_at_5",
        "candidate_recall",
        "mrr",
        "r20",
        "r10",
        "delta_micro_pp",
        "warm_e2e_p95_ms",
        "promoted",
        "demoted",
        "net",
    ]

    lines = []
    used = set()

    for key in preferred_order:
        if key not in metrics:
            continue

        name = METRIC_ZH.get(key, translate_text(key))
        lines.append(f"{name} = {format_metric_value(key, metrics[key])}")
        used.add(key)

        if len(lines) >= max_items:
            return "\\n".join(lines)

    for key, value in metrics.items():
        if key in used:
            continue

        name = METRIC_ZH.get(key, translate_text(key))
        lines.append(f"{name} = {format_metric_value(key, value)}")

        if len(lines) >= max_items:
            break

    return "\\n".join(lines)


def build_node_label(node: dict, show_metrics: bool = True) -> str:
    title = translate_text(node.get("label", node["id"]))
    lines = [title]

    # hub 不再显示 [hub] / status=completed
    if node.get("kind") == "hub":
        return "\\n".join(lines)

    if node.get("kind") == "diagnostic":
        lines.append("类型：诊断")

    status = node.get("status")
    if status and status not in {"completed"}:
        lines.append(f"结果：{STATUS_ZH.get(status, status)}")

    if show_metrics and node.get("metrics"):
        metric_lines = metric_text(node["metrics"])
        if metric_lines:
            lines.append(metric_lines)

    return "\\n".join(line for line in lines if line)


def truncate_text(text: str, limit: int = 36) -> str:
    text = translate_text(text)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


# ============================================================
# 4. 图结构
# ============================================================

def sanitize_id(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", value)


def style_for_node(node: dict) -> dict:
    status = node.get("status", "completed")
    style = STATUS_STYLE.get(status, STATUS_STYLE["completed"]).copy()

    if node.get("kind") == "hub":
        style["shape"] = "box"
        style["style"] = "filled,rounded"
        style["fillcolor"] = "#FFFFFF"
        style["color"] = "#929292"
    elif node.get("kind") == "diagnostic":
        style["shape"] = "note"
    else:
        style["shape"] = "box"

    return style


def collect_main_path_nodes(data: dict) -> set:
    result = set()
    for edge in data.get("edges", []):
        if edge.get("type") == "main_path":
            result.add(edge["from"])
            result.add(edge["to"])
    return result


def resolve_nodes_for_main_mode(data: dict) -> set:
    """
    main 模式尽量只保留真正主路径。
    """
    keep = collect_main_path_nodes(data)

    # 保留最终验证节点
    for node in data.get("nodes", []):
        if node.get("group") == "G9":
            keep.add(node["id"])

    return keep


def should_include_node(node: dict, mode: str, collapsed_hubs: set) -> bool:
    if mode == "full":
        return True

    if mode == "collapsed":
        # 折叠模式下隐藏指定 hub 自身，但保留其重要子节点
        return node.get("id") not in collapsed_hubs

    return True


def make_graph(
    data: dict,
    mode: str,
    output_base: str,
    fmt: str,
    rankdir: str,
    dpi: int,
    show_metrics: bool,
    font_name: str,
):
    graph = Digraph("experiment_paths", format=fmt)

    graph.attr(
        rankdir=rankdir,
        splines="spline",
        overlap="false",
        concentrate="false",
        compound="true",
        newrank="true",
        bgcolor="white",
        pad="0.2",
        nodesep="0.35",
        ranksep="0.65",
        dpi=str(dpi),
        fontname=font_name,
    )

    graph.attr(
        "node",
        fontname=font_name,
        fontsize="10",
        margin="0.14,0.10",
    )

    graph.attr(
        "edge",
        fontname=font_name,
        fontsize="8.5",
        arrowsize="0.65",
    )

    nodes = {node["id"]: node for node in data["nodes"]}
    groups = {group["id"]: group for group in data["groups"]}

    collapsed_hubs = set(
        data.get("rendering", {}).get("collapse_hubs_by_default", [])
    )

    if mode == "main":
        visible_node_ids = resolve_nodes_for_main_mode(data)
    else:
        visible_node_ids = {
            node["id"]
            for node in data["nodes"]
            if should_include_node(node, mode, collapsed_hubs)
        }

    # --------------------------------------------------------
    # 分组
    # --------------------------------------------------------
    group_to_nodes = defaultdict(list)
    for node in data["nodes"]:
        if node["id"] in visible_node_ids:
            group_to_nodes[node["group"]].append(node)

    for index, group in enumerate(data["groups"]):
        gid = group["id"]
        group_nodes = group_to_nodes.get(gid, [])

        if not group_nodes:
            continue

        with graph.subgraph(name=f"cluster_{sanitize_id(gid)}") as cluster:
            cluster.attr(
                label=GROUP_TITLE_ZH.get(gid, translate_text(group.get("label", gid))),
                style="rounded,filled",
                color="#D6D6D6",
                fillcolor=GROUP_COLORS[index % len(GROUP_COLORS)],
                fontname=font_name,
                fontsize="13",
                margin="18",
            )

            for node in group_nodes:
                style = style_for_node(node)

                cluster.node(
                    node["id"],
                    label=build_node_label(node, show_metrics=show_metrics),
                    shape=style.get("shape", "box"),
                    style=style.get("style", "filled,rounded"),
                    fillcolor=style.get("fillcolor", "#FFFFFF"),
                    color=style.get("color", "#888888"),
                    fontcolor=style.get("fontcolor", "#222222"),
                    penwidth="1.2" if node.get("status") != "best" else "1.8",
                )

    # --------------------------------------------------------
    # collapsed 模式需要跨过被隐藏的 hub
    # --------------------------------------------------------
    child_to_parents = defaultdict(list)

    for edge in data["edges"]:
        child_to_parents[edge["to"]].append(edge["from"])

    def nearest_visible_ancestors(node_id, visited=None):
        if visited is None:
            visited = set()

        if node_id in visited:
            return set()

        visited.add(node_id)
        parents = child_to_parents.get(node_id, [])

        if not parents:
            return set()

        result = set()

        for parent in parents:
            if parent in visible_node_ids:
                result.add(parent)
            else:
                result |= nearest_visible_ancestors(parent, visited)

        return result

    rendered_edges = set()

    for edge in data["edges"]:
        src = edge["from"]
        dst = edge["to"]
        edge_type = edge.get("type", "default")

        if mode == "main" and edge_type != "main_path":
            # final validation 内部关系保留
            if not (nodes.get(src, {}).get("group") == "G9" and nodes.get(dst, {}).get("group") == "G9"):
                continue

        if src in visible_node_ids and dst in visible_node_ids:
            pairs = [(src, dst)]

        elif mode == "collapsed" and dst in visible_node_ids:
            pairs = [
                (ancestor, dst)
                for ancestor in nearest_visible_ancestors(src)
                if ancestor != dst
            ]
        else:
            continue

        edge_style = DEFAULT_EDGE_STYLE.copy()
        edge_style.update(EDGE_STYLE.get(edge_type, {}))

        for final_src, final_dst in pairs:
            key = (
                final_src,
                final_dst,
                edge_type,
                edge.get("label", ""),
            )

            if key in rendered_edges:
                continue

            rendered_edges.add(key)

            kwargs = {
                "color": edge_style.get("color", "#BDBDBD"),
                "penwidth": edge_style.get("penwidth", "1.0"),
            }

            if "style" in edge_style:
                kwargs["style"] = edge_style["style"]

            # 主路径才显示边文字，避免整张图太乱
            if edge.get("label") and edge_type == "main_path":
                kwargs["label"] = truncate_text(edge["label"])

            graph.edge(final_src, final_dst, **kwargs)

    graph.render(output_base, cleanup=True)

    return f"{output_base}.{fmt}"


# ============================================================
# 5. 命令行
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="从实验路径 YAML 自动生成中文实验路径图。"
    )

    parser.add_argument(
        "--yaml",
        default="experiment_paths_s001_s005.yaml",
        help="实验路径 YAML 文件",
    )

    parser.add_argument(
        "--mode",
        choices=["full", "collapsed", "main", "all"],
        default="collapsed",
        help=(
            "full=完整展开；"
            "collapsed=折叠大分支；"
            "main=只显示主路径；"
            "all=三种都生成"
        ),
    )

    parser.add_argument(
        "--format",
        choices=["png", "svg", "pdf"],
        default="png",
        help="输出格式",
    )

    parser.add_argument(
        "--output",
        default="experiment_paths_cn",
        help="输出文件名前缀，不需要扩展名",
    )

    parser.add_argument(
        "--rankdir",
        choices=["LR", "TB"],
        default="LR",
        help="LR=从左到右；TB=从上到下",
    )

    parser.add_argument(
        "--dpi",
        type=int,
        default=240,
        help="PNG 输出 DPI",
    )

    parser.add_argument(
        "--font",
        default="Microsoft YaHei",
        help=(
            "Graphviz 中文字体。"
            "Windows 推荐 Microsoft YaHei；"
            "Linux 可使用 Noto Sans CJK SC；"
            "macOS 可使用 PingFang SC。"
        ),
    )

    parser.add_argument(
        "--hide-metrics",
        action="store_true",
        help="不在节点中显示指标",
    )

    args = parser.parse_args()

    if not os.path.exists(args.yaml):
        raise FileNotFoundError(
            f"找不到 YAML 文件：{args.yaml}"
        )

    with open(args.yaml, "r", encoding="utf-8") as file:
        data = yaml.safe_load(file)

    modes = (
        ["full", "collapsed", "main"]
        if args.mode == "all"
        else [args.mode]
    )

    outputs = []

    for mode in modes:
        output_base = f"{args.output}_{mode}"

        path = make_graph(
            data=data,
            mode=mode,
            output_base=output_base,
            fmt=args.format,
            rankdir=args.rankdir,
            dpi=args.dpi,
            show_metrics=not args.hide_metrics,
            font_name=args.font,
        )

        outputs.append(path)

    print("\n生成完成：")
    for path in outputs:
        print(f"  {os.path.abspath(path)}")


if __name__ == "__main__":
    main()
