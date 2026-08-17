#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent Memory 金融问答数据集统计脚本

特点：
- 仅使用 Python 标准库，直接读取 .xlsx（无需 pandas/openpyxl）。
- 支持整个工作簿或指定 Session 范围，例如 S001-S010。
- 同时统计 Query / Answer 长度、Gold requirement、ShortTerm(window=N)、
  MidTerm eligible、回溯距离、依赖结构、数据质量、模板化/同质化等指标。
- 输出 JSON、Markdown 和多份 CSV，便于后续实验直接比较。

Gold 规则：
- A；B        => AND，算 2 个 Gold requirements。
- （A；B）    => OR，算 1 个 Gold requirement；A/B 任意一个在窗口内即可命中。

示例：
    python analyze_agent_memory_dataset.py dataset.xlsx
    python analyze_agent_memory_dataset.py dataset.xlsx --sessions S001-S010
    python analyze_agent_memory_dataset.py dataset.xlsx --sessions S001,S003,S010 --short-window 3

输出目录默认：<输入文件名>_metrics/
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import statistics
import sys
import unicodedata
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
import xml.etree.ElementTree as ET


NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL_DOC = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_REL_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"

EXPECTED_COLUMNS = [
    "编号",
    "当前问题",
    "最终回答",
    "是否需要前文",
    "关联前序对话",
    "实际需召回内容（原始回答）",
    "所需前文信息",
    "依赖类型",
    "最大回溯轮数",
    "当前角色",
    "会话主题",
    "问题形态",
    "问题长度档",
    "公司",
    "股票代码",
    "行业",
    "分析期间",
    "数据性质",
    "来源文件",
    "来源发布日期",
    "回答字符数",
    "问题归一化指纹",
    "问题结构指纹",
    "验证结果",
    "Session轮数",
]

REF_RE = re.compile(r"S\d{3}-Q\d{3}", re.I)
QID_RE = re.compile(r"^(S\d{3})-Q(\d{3,})$", re.I)
SESSION_RE = re.compile(r"^(S\d{3})", re.I)
CHINESE_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
SENTENCE_SPLIT_RE = re.compile(r"[。！？!?；;]+")

EXPLICIT_LOCATOR_PATTERNS = {
    "回到N轮前": re.compile(r"回到\s*[一二三四五六七八九十百两\d]+\s*轮前"),
    "回到第N轮": re.compile(r"回到\s*第\s*[一二三四五六七八九十百两\d]+\s*轮"),
    "N轮前关于": re.compile(r"[一二三四五六七八九十百两\d]+\s*轮前\s*关于"),
    "第N次对话": re.compile(r"第\s*[一二三四五六七八九十百两\d]+\s*次对话"),
    "往前数N轮": re.compile(r"往前数\s*[一二三四五六七八九十百两\d]+\s*轮"),
    "Sxxx-Qxxx引用": re.compile(r"\bS\d{3}-Q\d{3}\b", re.I),
    "Qxxx引用": re.compile(r"(?<![A-Za-z0-9-])Q\d{3}(?![A-Za-z0-9])", re.I),
    "关于【...】": re.compile(r"关于\s*【"),
}

IMPLICIT_HISTORY_PATTERNS = {
    "前面": re.compile(r"前面"),
    "之前": re.compile(r"之前"),
    "此前": re.compile(r"此前"),
    "刚才": re.compile(r"刚才"),
    "前述": re.compile(r"前述"),
    "早先": re.compile(r"早先"),
    "还是按": re.compile(r"还是按"),
    "接着": re.compile(r"接着"),
    "这个判断": re.compile(r"这个判断"),
    "这个口径": re.compile(r"这个口径"),
}


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = re.sub(r"\s+", "", text)
    return text


def normalize_for_duplicate(text: str) -> str:
    text = normalize_text(text)
    text = re.sub(r"[，。！？、；：,.!?;:'\"“”‘’（）()\[\]【】{}<>《》—…·`~_\-]+", "", text)
    return text


def text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def char_ngrams(text: str, n: int = 4) -> set:
    t = normalize_for_duplicate(text)
    if not t:
        return set()
    if len(t) <= n:
        return {t}
    return {t[i:i+n] for i in range(len(t) - n + 1)}


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def percentile(values: Sequence[float], p: float) -> Optional[float]:
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return float(xs[0])
    pos = (len(xs) - 1) * p
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(xs[lo])
    w = pos - lo
    return float(xs[lo] * (1 - w) + xs[hi] * w)


def describe(values: Sequence[float]) -> Dict[str, Optional[float]]:
    vals = [float(x) for x in values if x is not None]
    if not vals:
        return {
            "count": 0, "mean": None, "std_population": None, "std_sample": None,
            "min": None, "p05": None, "p10": None, "p25": None, "median": None,
            "p75": None, "p90": None, "p95": None, "p99": None, "max": None,
            "cv": None,
        }
    mean = statistics.fmean(vals)
    stdp = statistics.pstdev(vals) if len(vals) >= 1 else None
    stds = statistics.stdev(vals) if len(vals) >= 2 else None
    return {
        "count": len(vals),
        "mean": mean,
        "std_population": stdp,
        "std_sample": stds,
        "min": min(vals),
        "p05": percentile(vals, 0.05),
        "p10": percentile(vals, 0.10),
        "p25": percentile(vals, 0.25),
        "median": percentile(vals, 0.50),
        "p75": percentile(vals, 0.75),
        "p90": percentile(vals, 0.90),
        "p95": percentile(vals, 0.95),
        "p99": percentile(vals, 0.99),
        "max": max(vals),
        "cv": (stdp / mean) if mean else None,
    }


def fmt_num(x, digits=2) -> str:
    if x is None:
        return "-"
    if isinstance(x, int):
        return f"{x:,}"
    return f"{x:,.{digits}f}"


def fmt_pct(num: int, den: int, digits: int = 2) -> str:
    if not den:
        return "-"
    return f"{100.0 * num / den:.{digits}f}%"


def parse_int(value: str) -> Optional[int]:
    try:
        if value is None or str(value).strip() == "":
            return None
        return int(float(str(value).strip()))
    except Exception:
        return None


def col_letters(cell_ref: str) -> str:
    m = re.match(r"([A-Z]+)", cell_ref)
    return m.group(1) if m else ""


def load_shared_strings(zf: zipfile.ZipFile) -> List[str]:
    path = "xl/sharedStrings.xml"
    if path not in zf.namelist():
        return []
    out: List[str] = []
    with zf.open(path) as f:
        for event, elem in ET.iterparse(f, events=("end",)):
            if elem.tag == f"{{{NS_MAIN}}}si":
                parts = []
                for t in elem.iter(f"{{{NS_MAIN}}}t"):
                    parts.append(t.text or "")
                out.append("".join(parts))
                elem.clear()
    return out


def load_sheet_metadata(zf: zipfile.ZipFile) -> List[Tuple[str, str]]:
    wb_root = ET.fromstring(zf.read("xl/workbook.xml"))
    rel_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    relmap = {r.attrib["Id"]: r.attrib["Target"] for r in rel_root}
    result = []
    sheets = wb_root.find(f"{{{NS_MAIN}}}sheets")
    if sheets is None:
        return result
    for s in sheets:
        rid = s.attrib.get(f"{{{NS_REL_DOC}}}id")
        target = relmap.get(rid, "")
        if target.startswith("/"):
            target = target.lstrip("/")
        elif not target.startswith("xl/"):
            target = "xl/" + target
        result.append((s.attrib.get("name", ""), target))
    return result


def read_cell_value(cell: ET.Element, shared: Sequence[str]) -> str:
    ctype = cell.attrib.get("t")
    if ctype == "inlineStr":
        is_node = cell.find(f"{{{NS_MAIN}}}is")
        if is_node is None:
            return ""
        return "".join((t.text or "") for t in is_node.iter(f"{{{NS_MAIN}}}t"))
    v = cell.find(f"{{{NS_MAIN}}}v")
    if v is None:
        return ""
    raw = v.text or ""
    if ctype == "s":
        try:
            return shared[int(raw)]
        except Exception:
            return ""
    if ctype == "b":
        return "TRUE" if raw == "1" else "FALSE"
    return raw


def iter_sheet_rows(zf: zipfile.ZipFile, sheet_path: str, shared: Sequence[str]) -> Iterator[Dict[str, str]]:
    with zf.open(sheet_path) as f:
        for event, elem in ET.iterparse(f, events=("end",)):
            if elem.tag != f"{{{NS_MAIN}}}row":
                continue
            row = {}
            for cell in elem.findall(f"{{{NS_MAIN}}}c"):
                col = col_letters(cell.attrib.get("r", ""))
                row[col] = read_cell_value(cell, shared)
            yield row
            elem.clear()


def parse_session_selector(spec: str, available: Sequence[str]) -> List[str]:
    available_set = set(available)
    if not spec or spec.lower() == "all":
        return list(available)
    selected = []
    for token in re.split(r"[,，\s]+", spec.strip()):
        if not token:
            continue
        m = re.fullmatch(r"(S\d{3})-(S\d{3})", token, re.I)
        if m:
            a, b = int(m.group(1)[1:]), int(m.group(2)[1:])
            lo, hi = sorted((a, b))
            selected.extend(f"S{i:03d}" for i in range(lo, hi + 1))
        else:
            t = token.upper()
            if re.fullmatch(r"S\d{3}", t):
                selected.append(t)
            else:
                raise ValueError(f"无法解析 --sessions 项: {token}")
    dedup = []
    seen = set()
    for sid in selected:
        if sid in available_set and sid not in seen:
            dedup.append(sid)
            seen.add(sid)
    return dedup


def split_top_level(text: str, separators="；;") -> List[str]:
    text = (text or "").replace("（", "(").replace("）", ")")
    out, buf = [], []
    depth = 0
    for ch in text:
        if ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch in separators and depth == 0:
            item = "".join(buf).strip()
            if item:
                out.append(item)
            buf = []
        else:
            buf.append(ch)
    item = "".join(buf).strip()
    if item:
        out.append(item)
    return out


def parse_gold_requirements(text: str) -> List[Dict]:
    raw = (text or "").strip()
    if not raw or raw.lower() in {"无", "none", "n/a", "na", "否", "-"}:
        return []
    requirements = []
    for item in split_top_level(raw):
        stripped = item.strip()
        is_or = stripped.startswith("(") and stripped.endswith(")")
        inner = stripped[1:-1] if is_or else stripped
        refs = REF_RE.findall(inner.upper())
        # 非括号项中若意外包含多个 ref，按当前项的 refs 保留，并在质量检查中标记。
        requirements.append({
            "raw": item,
            "is_or": is_or,
            "refs": refs,
        })
    return requirements


def get_q_index(qid: str) -> Optional[int]:
    m = QID_RE.match((qid or "").strip())
    return int(m.group(2)) if m else None


def get_session_id(qid: str) -> Optional[str]:
    m = QID_RE.match((qid or "").strip())
    return m.group(1).upper() if m else None


def extract_headings(answer: str) -> List[str]:
    headings = []
    for line in (answer or "").splitlines():
        s = line.strip()
        if not s:
            continue
        s2 = re.sub(r"^#{1,6}\s*", "", s).strip()
        # 明确 Markdown heading，或较短且不像完整句子的独立行。
        is_md = s.startswith("#")
        looks_heading = (
            2 <= len(s2) <= 36
            and not re.search(r"[。！？!?；;]$", s2)
            and not re.match(r"^[-*+>]\s+", s2)
            and not re.match(r"^\d+[.)、]\s*", s2)
        )
        if is_md or looks_heading:
            headings.append(s2)
    return headings


def split_paragraphs(answer: str) -> List[str]:
    return [re.sub(r"\s+", " ", x).strip() for x in re.split(r"\n\s*\n+", answer or "") if x.strip()]


def text_features(text: str) -> Dict[str, int]:
    text = text or ""
    nonspace = re.sub(r"\s", "", text)
    paragraphs = split_paragraphs(text)
    sentence_count = len([x for x in SENTENCE_SPLIT_RE.split(text) if x.strip()])
    return {
        "chars": len(text),
        "nonspace_chars": len(nonspace),
        "chinese_chars": len(CHINESE_RE.findall(text)),
        "lines": len(text.splitlines()) if text else 0,
        "paragraphs": len(paragraphs),
        "sentences": sentence_count,
    }


def distance_bucket(d: int) -> str:
    if d <= 0:
        return "invalid<=0"
    if d <= 3:
        return "1-3"
    if d <= 6:
        return "4-6"
    if d <= 10:
        return "7-10"
    if d <= 20:
        return "11-20"
    return "21+"


def length_bucket_question(n: int) -> str:
    if n <= 10: return "<=10"
    if n <= 20: return "11-20"
    if n <= 40: return "21-40"
    if n <= 80: return "41-80"
    if n <= 120: return "81-120"
    return "121+"


def length_bucket_answer(n: int) -> str:
    if n < 5000: return "<5000"
    if n < 8000: return "5000-7999"
    if n < 9500: return "8000-9499"
    if n <= 10500: return "9500-10500"
    if n <= 12000: return "10501-12000"
    return ">12000"


@dataclass
class RowRecord:
    sheet_name: str
    session_id: str
    qid: str
    question: str
    answer: str
    needs_context: str
    gold_raw: str
    recalled_content: str
    dependency_reason: str
    dependency_type: str
    stored_max_back: Optional[int]
    role: str
    topic: str
    question_form: str
    question_length_class: str
    company: str
    stock_code: str
    industry: str
    period: str
    data_nature: str
    source_file: str
    source_date: str
    stored_answer_chars: Optional[int]
    normalized_fingerprint: str
    structure_fingerprint: str
    validation_result: str
    stored_session_turns: Optional[int]


@dataclass
class Analyzer:
    short_window: int = 3
    similarity_threshold: float = 0.80
    max_similarity_pairs: int = 2000
    min_repeated_paragraph_chars: int = 80

    rows: List[RowRecord] = field(default_factory=list)
    qids: set = field(default_factory=set)
    session_rows: Dict[str, List[RowRecord]] = field(default_factory=lambda: defaultdict(list))
    column_missing: Counter = field(default_factory=Counter)
    column_total: Counter = field(default_factory=Counter)

    question_lengths: List[int] = field(default_factory=list)
    answer_lengths: List[int] = field(default_factory=list)
    question_nonspace_lengths: List[int] = field(default_factory=list)
    answer_nonspace_lengths: List[int] = field(default_factory=list)
    question_chinese_lengths: List[int] = field(default_factory=list)
    answer_chinese_lengths: List[int] = field(default_factory=list)
    question_sentence_counts: List[int] = field(default_factory=list)
    answer_paragraph_counts: List[int] = field(default_factory=list)
    answer_heading_counts: List[int] = field(default_factory=list)

    question_length_buckets: Counter = field(default_factory=Counter)
    answer_length_buckets: Counter = field(default_factory=Counter)
    dependency_types: Counter = field(default_factory=Counter)
    topics: Counter = field(default_factory=Counter)
    question_forms: Counter = field(default_factory=Counter)
    question_length_classes: Counter = field(default_factory=Counter)
    roles: Counter = field(default_factory=Counter)
    companies: Counter = field(default_factory=Counter)
    industries: Counter = field(default_factory=Counter)
    validation_results: Counter = field(default_factory=Counter)
    needs_context_values: Counter = field(default_factory=Counter)

    normalized_fingerprints: Counter = field(default_factory=Counter)
    structure_fingerprints: Counter = field(default_factory=Counter)
    recomputed_question_hashes: Counter = field(default_factory=Counter)
    exact_answer_hashes: Counter = field(default_factory=Counter)
    normalized_question_examples: Dict[str, List[str]] = field(default_factory=lambda: defaultdict(list))
    structure_fp_examples: Dict[str, List[str]] = field(default_factory=lambda: defaultdict(list))

    question_prefixes: Dict[int, Counter] = field(default_factory=lambda: {4: Counter(), 6: Counter(), 8: Counter(), 12: Counter()})
    answer_openings: Dict[int, Counter] = field(default_factory=lambda: {20: Counter(), 50: Counter(), 100: Counter()})
    heading_doc_freq: Counter = field(default_factory=Counter)
    heading_total_freq: Counter = field(default_factory=Counter)
    answer_structure_signatures: Counter = field(default_factory=Counter)
    repeated_paragraph_doc_freq: Counter = field(default_factory=Counter)
    paragraph_examples: Dict[str, str] = field(default_factory=dict)

    explicit_locator_occurrences: Counter = field(default_factory=Counter)
    explicit_locator_queries: Counter = field(default_factory=Counter)
    implicit_history_queries: Counter = field(default_factory=Counter)

    stored_answer_length_mismatches: List[Tuple[str, int, int]] = field(default_factory=list)
    quality_issues: List[Dict[str, str]] = field(default_factory=list)

    # Gold-derived
    total_gold_requirements: int = 0
    valid_gold_requirements: int = 0
    invalid_gold_requirements: int = 0
    or_requirements: int = 0
    total_gold_alternatives: int = 0
    valid_reference_count: int = 0
    invalid_reference_count: int = 0
    short_covered_requirements: int = 0
    outside_short_requirements: int = 0
    reference_distances: List[int] = field(default_factory=list)
    effective_requirement_distances: List[int] = field(default_factory=list)
    query_max_reference_distances: List[int] = field(default_factory=list)
    reference_distance_counter: Counter = field(default_factory=Counter)
    requirement_distance_counter: Counter = field(default_factory=Counter)
    requirement_distance_bucket_counter: Counter = field(default_factory=Counter)
    gold_requirements_per_query: Counter = field(default_factory=Counter)
    gold_alternatives_per_query: Counter = field(default_factory=Counter)
    query_dependency_class: Counter = field(default_factory=Counter)
    short_query_completion_values: List[float] = field(default_factory=list)
    short_fully_complete_queries: int = 0
    short_partially_complete_queries: int = 0
    short_zero_complete_gold_queries: int = 0
    midterm_eligible_queries: int = 0
    multi_gold_queries: int = 0
    max_back_mismatches: List[Tuple[str, Optional[int], int]] = field(default_factory=list)
    gold_rows_summary: Dict[str, Dict] = field(default_factory=dict)

    high_similarity_question_pairs: List[Tuple[float, str, str, str, str, str]] = field(default_factory=list)

    def add_row(self, rec: RowRecord, raw_row: Dict[str, str]):
        self.rows.append(rec)
        self.qids.add(rec.qid)
        self.session_rows[rec.session_id].append(rec)

        for col in EXPECTED_COLUMNS:
            self.column_total[col] += 1
            if not (raw_row.get(col) or "").strip():
                self.column_missing[col] += 1

        qf = text_features(rec.question)
        af = text_features(rec.answer)
        self.question_lengths.append(qf["chars"])
        self.answer_lengths.append(af["chars"])
        self.question_nonspace_lengths.append(qf["nonspace_chars"])
        self.answer_nonspace_lengths.append(af["nonspace_chars"])
        self.question_chinese_lengths.append(qf["chinese_chars"])
        self.answer_chinese_lengths.append(af["chinese_chars"])
        self.question_sentence_counts.append(qf["sentences"])
        self.answer_paragraph_counts.append(af["paragraphs"])

        self.question_length_buckets[length_bucket_question(qf["chars"])] += 1
        self.answer_length_buckets[length_bucket_answer(af["chars"])] += 1

        self.dependency_types[rec.dependency_type or "<空>"] += 1
        self.topics[rec.topic or "<空>"] += 1
        self.question_forms[rec.question_form or "<空>"] += 1
        self.question_length_classes[rec.question_length_class or "<空>"] += 1
        self.roles[rec.role or "<空>"] += 1
        self.companies[rec.company or "<空>"] += 1
        self.industries[rec.industry or "<空>"] += 1
        self.validation_results[rec.validation_result or "<空>"] += 1
        self.needs_context_values[rec.needs_context or "<空>"] += 1

        if rec.normalized_fingerprint:
            self.normalized_fingerprints[rec.normalized_fingerprint] += 1
            if len(self.normalized_question_examples[rec.normalized_fingerprint]) < 3:
                self.normalized_question_examples[rec.normalized_fingerprint].append(rec.qid)
        if rec.structure_fingerprint:
            self.structure_fingerprints[rec.structure_fingerprint] += 1
            if len(self.structure_fp_examples[rec.structure_fingerprint]) < 3:
                self.structure_fp_examples[rec.structure_fingerprint].append(rec.qid)

        nq = normalize_for_duplicate(rec.question)
        self.recomputed_question_hashes[text_hash(nq)] += 1
        self.exact_answer_hashes[text_hash(rec.answer)] += 1

        q_no_ws = re.sub(r"\s+", "", rec.question or "")
        for n, counter in self.question_prefixes.items():
            if q_no_ws:
                counter[q_no_ws[:n]] += 1

        ans_norm = re.sub(r"\s+", "", rec.answer or "")
        for n, counter in self.answer_openings.items():
            if ans_norm:
                counter[ans_norm[:n]] += 1

        headings = extract_headings(rec.answer)
        self.answer_heading_counts.append(len(headings))
        for h in headings:
            self.heading_total_freq[h] += 1
        for h in set(headings):
            self.heading_doc_freq[h] += 1
        signature = " | ".join(headings[:24])
        if signature:
            self.answer_structure_signatures[text_hash(signature)] += 1

        seen_para = set()
        for para in split_paragraphs(rec.answer):
            pnorm = normalize_text(para)
            if len(pnorm) < self.min_repeated_paragraph_chars:
                continue
            ph = text_hash(pnorm)
            if ph in seen_para:
                continue
            seen_para.add(ph)
            self.repeated_paragraph_doc_freq[ph] += 1
            if ph not in self.paragraph_examples:
                self.paragraph_examples[ph] = para[:180].replace("\n", " ")

        for name, pattern in EXPLICIT_LOCATOR_PATTERNS.items():
            hits = pattern.findall(rec.question or "")
            if hits:
                self.explicit_locator_occurrences[name] += len(hits)
                self.explicit_locator_queries[name] += 1
        for name, pattern in IMPLICIT_HISTORY_PATTERNS.items():
            if pattern.search(rec.question or ""):
                self.implicit_history_queries[name] += 1

        if rec.stored_answer_chars is not None and rec.stored_answer_chars != af["chars"]:
            self.stored_answer_length_mismatches.append((rec.qid, rec.stored_answer_chars, af["chars"]))

        needs = (rec.needs_context or "").strip()
        gold = (rec.gold_raw or "").strip()
        if needs in {"是", "yes", "Yes", "YES", "1", "true", "TRUE"} and (not gold or gold in {"无", "-"}):
            self.quality_issues.append({"qid": rec.qid, "type": "needs_context_but_no_gold", "detail": gold})
        if needs in {"否", "no", "No", "NO", "0", "false", "FALSE"} and gold and gold not in {"无", "-"}:
            self.quality_issues.append({"qid": rec.qid, "type": "no_context_but_has_gold", "detail": gold})
        if needs in {"是", "yes", "Yes", "YES", "1", "true", "TRUE"} and not rec.dependency_reason.strip():
            self.quality_issues.append({"qid": rec.qid, "type": "missing_dependency_reason", "detail": ""})
        if needs in {"是", "yes", "Yes", "YES", "1", "true", "TRUE"} and not rec.recalled_content.strip():
            self.quality_issues.append({"qid": rec.qid, "type": "missing_recalled_content", "detail": ""})

    def analyze_gold(self):
        for rec in self.rows:
            reqs = parse_gold_requirements(rec.gold_raw)
            self.gold_requirements_per_query[len(reqs)] += 1
            if len(reqs) > 1:
                self.multi_gold_queries += 1

            qidx = get_q_index(rec.qid)
            qsid = get_session_id(rec.qid)
            query_short_hits = 0
            query_valid_reqs = 0
            query_alt_count = 0
            query_all_ref_distances = []
            query_invalid = False
            query_has_outside = False
            query_has_short = False
            parsed_detail = []

            for req in reqs:
                self.total_gold_requirements += 1
                if req["is_or"]:
                    self.or_requirements += 1
                refs = req["refs"]
                self.total_gold_alternatives += len(refs)
                query_alt_count += len(refs)
                valid_distances = []
                invalid_refs = []

                if not refs:
                    self.invalid_gold_requirements += 1
                    query_invalid = True
                    parsed_detail.append({**req, "valid_distances": [], "invalid_refs": ["NO_REF"]})
                    self.quality_issues.append({"qid": rec.qid, "type": "gold_requirement_no_ref", "detail": req["raw"]})
                    continue

                if not req["is_or"] and len(refs) > 1:
                    self.quality_issues.append({"qid": rec.qid, "type": "multiple_refs_in_non_or_requirement", "detail": req["raw"]})

                for ref in refs:
                    rsid = get_session_id(ref)
                    ridx = get_q_index(ref)
                    valid = True
                    reason = ""
                    if qidx is None or qsid is None or ridx is None or rsid is None:
                        valid = False; reason = "malformed_id"
                    elif rsid != qsid:
                        valid = False; reason = "cross_session_ref"
                    elif ref not in self.qids:
                        valid = False; reason = "target_not_found"
                    elif ridx >= qidx:
                        valid = False; reason = "not_previous_turn"
                    if not valid:
                        invalid_refs.append(f"{ref}:{reason}")
                        self.invalid_reference_count += 1
                        query_invalid = True
                        self.quality_issues.append({"qid": rec.qid, "type": reason, "detail": ref})
                        continue
                    d = qidx - ridx
                    valid_distances.append(d)
                    query_all_ref_distances.append(d)
                    self.reference_distances.append(d)
                    self.reference_distance_counter[d] += 1
                    self.valid_reference_count += 1

                if valid_distances:
                    self.valid_gold_requirements += 1
                    query_valid_reqs += 1
                    # OR requirement：任一 alternative 即可，因此有效距离取最近可满足 alternative。
                    # 非 OR 正常只有一个 ref；若有多个，保守取最小并另行质量告警。
                    effective_d = min(valid_distances)
                    self.effective_requirement_distances.append(effective_d)
                    self.requirement_distance_counter[effective_d] += 1
                    self.requirement_distance_bucket_counter[distance_bucket(effective_d)] += 1
                    if effective_d <= self.short_window:
                        self.short_covered_requirements += 1
                        query_short_hits += 1
                        query_has_short = True
                    else:
                        self.outside_short_requirements += 1
                        query_has_outside = True
                else:
                    self.invalid_gold_requirements += 1

                parsed_detail.append({**req, "valid_distances": valid_distances, "invalid_refs": invalid_refs})

            self.gold_alternatives_per_query[query_alt_count] += 1
            if query_all_ref_distances:
                computed_max = max(query_all_ref_distances)
                self.query_max_reference_distances.append(computed_max)
                if rec.stored_max_back is not None and rec.stored_max_back != computed_max:
                    self.max_back_mismatches.append((rec.qid, rec.stored_max_back, computed_max))
                    self.quality_issues.append({
                        "qid": rec.qid,
                        "type": "max_back_mismatch",
                        "detail": f"stored={rec.stored_max_back}, computed={computed_max}",
                    })

            if not reqs:
                dep_class = "independent"
            elif query_invalid and query_valid_reqs == 0:
                dep_class = "invalid"
            elif query_has_short and query_has_outside:
                dep_class = "mixed_short_and_outside"
            elif query_has_outside:
                dep_class = "outside_only"
            elif query_has_short:
                dep_class = "short_only"
            else:
                dep_class = "invalid_or_empty"
            self.query_dependency_class[dep_class] += 1

            if query_valid_reqs > 0:
                completion = query_short_hits / query_valid_reqs
                self.short_query_completion_values.append(completion)
                if completion >= 1.0:
                    self.short_fully_complete_queries += 1
                elif completion <= 0:
                    self.short_zero_complete_gold_queries += 1
                else:
                    self.short_partially_complete_queries += 1
                if query_short_hits < query_valid_reqs:
                    self.midterm_eligible_queries += 1

            self.gold_rows_summary[rec.qid] = {
                "requirements": len(reqs),
                "valid_requirements": query_valid_reqs,
                "short_hits": query_short_hits,
                "completion": (query_short_hits / query_valid_reqs) if query_valid_reqs else None,
                "class": dep_class,
                "parsed": parsed_detail,
            }

    def analyze_question_similarity(self):
        pairs = []
        for sid, recs in self.session_rows.items():
            feats = [(r.qid, r.question, char_ngrams(r.question, 4)) for r in recs]
            for i in range(len(feats)):
                qid1, q1, g1 = feats[i]
                for j in range(i + 1, len(feats)):
                    qid2, q2, g2 = feats[j]
                    score = jaccard(g1, g2)
                    if score >= self.similarity_threshold:
                        pairs.append((score, sid, qid1, qid2, q1[:160], q2[:160]))
        pairs.sort(reverse=True, key=lambda x: x[0])
        self.high_similarity_question_pairs = pairs[: self.max_similarity_pairs]

    def session_metrics(self) -> List[Dict]:
        rows = []
        for sid in sorted(self.session_rows):
            recs = self.session_rows[sid]
            qlens = [len(r.question or "") for r in recs]
            alens = [len(r.answer or "") for r in recs]
            valid_req = short_req = outside_req = multi = or_groups = eligible_q = explicit_q = 0
            ref_dists = []
            classes = Counter()
            for r in recs:
                gs = self.gold_rows_summary.get(r.qid, {})
                valid_req += gs.get("valid_requirements", 0)
                short_req += gs.get("short_hits", 0)
                outside_req += max(0, gs.get("valid_requirements", 0) - gs.get("short_hits", 0))
                if gs.get("requirements", 0) > 1:
                    multi += 1
                if gs.get("valid_requirements", 0) > gs.get("short_hits", 0):
                    eligible_q += 1
                classes[gs.get("class", "unknown")] += 1
                for req in gs.get("parsed", []):
                    if req.get("is_or"):
                        or_groups += 1
                    ref_dists.extend(req.get("valid_distances", []))
                if any(p.search(r.question or "") for p in EXPLICIT_LOCATOR_PATTERNS.values()):
                    explicit_q += 1
            qd = describe(qlens); ad = describe(alens)
            first = recs[0]
            stored_turns = [r.stored_session_turns for r in recs if r.stored_session_turns is not None]
            rows.append({
                "session_id": sid,
                "sheet_name": first.sheet_name,
                "company": first.company,
                "role": first.role,
                "turns": len(recs),
                "stored_session_turns_mode": Counter(stored_turns).most_common(1)[0][0] if stored_turns else "",
                "dependent_queries": len(recs) - classes.get("independent", 0),
                "independent_queries": classes.get("independent", 0),
                "short_only_queries": classes.get("short_only", 0),
                "mixed_queries": classes.get("mixed_short_and_outside", 0),
                "outside_only_queries": classes.get("outside_only", 0),
                "midterm_eligible_queries": eligible_q,
                "valid_gold_requirements": valid_req,
                "short_covered_requirements": short_req,
                "outside_short_requirements": outside_req,
                "short_requirement_pct": (short_req / valid_req) if valid_req else None,
                "outside_requirement_pct": (outside_req / valid_req) if valid_req else None,
                "multi_gold_queries": multi,
                "or_groups": or_groups,
                "max_reference_distance": max(ref_dists) if ref_dists else 0,
                "question_len_mean": qd["mean"],
                "question_len_std_population": qd["std_population"],
                "question_len_median": qd["median"],
                "answer_len_mean": ad["mean"],
                "answer_len_std_population": ad["std_population"],
                "answer_len_median": ad["median"],
                "explicit_locator_queries": explicit_q,
            })
        return rows

    def company_metrics(self, session_metrics: List[Dict]) -> List[Dict]:
        groups = defaultdict(list)
        for row in session_metrics:
            groups[row["company"]].append(row)
        out = []
        for company, rs in sorted(groups.items()):
            turns = sum(r["turns"] for r in rs)
            valid = sum(r["valid_gold_requirements"] for r in rs)
            short = sum(r["short_covered_requirements"] for r in rs)
            outside = sum(r["outside_short_requirements"] for r in rs)
            out.append({
                "company": company,
                "sessions": len(rs),
                "turns": turns,
                "valid_gold_requirements": valid,
                "short_covered_requirements": short,
                "outside_short_requirements": outside,
                "short_requirement_pct": short / valid if valid else None,
                "outside_requirement_pct": outside / valid if valid else None,
                "midterm_eligible_queries": sum(r["midterm_eligible_queries"] for r in rs),
                "multi_gold_queries": sum(r["multi_gold_queries"] for r in rs),
                "explicit_locator_queries": sum(r["explicit_locator_queries"] for r in rs),
            })
        return out

    def overall(self) -> Dict:
        total_queries = len(self.rows)
        valid_req = self.valid_gold_requirements
        short_req = self.short_covered_requirements
        outside_req = self.outside_short_requirements
        dependent_queries = total_queries - self.query_dependency_class.get("independent", 0)
        duplicate_norm_fp_rows = sum(c for c in self.normalized_fingerprints.values() if c > 1)
        duplicate_struct_fp_rows = sum(c for c in self.structure_fingerprints.values() if c > 1)
        duplicate_question_hash_rows = sum(c for c in self.recomputed_question_hashes.values() if c > 1)
        duplicate_answer_hash_rows = sum(c for c in self.exact_answer_hashes.values() if c > 1)
        repeated_para = sum(1 for c in self.repeated_paragraph_doc_freq.values() if c >= 2)
        return {
            "short_window": self.short_window,
            "total_sessions": len(self.session_rows),
            "total_queries": total_queries,
            "dependent_queries": dependent_queries,
            "independent_queries": self.query_dependency_class.get("independent", 0),
            "dependent_query_pct": dependent_queries / total_queries if total_queries else None,
            "valid_gold_requirements": valid_req,
            "raw_gold_requirements": self.total_gold_requirements,
            "invalid_gold_requirements": self.invalid_gold_requirements,
            "or_requirements": self.or_requirements,
            "gold_alternatives": self.total_gold_alternatives,
            "valid_reference_count": self.valid_reference_count,
            "invalid_reference_count": self.invalid_reference_count,
            "short_covered_requirements": short_req,
            "outside_short_requirements": outside_req,
            "short_requirement_pct": short_req / valid_req if valid_req else None,
            "outside_short_requirement_pct": outside_req / valid_req if valid_req else None,
            "midterm_eligible_gold": outside_req,
            "midterm_eligible_queries": self.midterm_eligible_queries,
            "multi_gold_queries": self.multi_gold_queries,
            "multi_gold_query_pct_of_dependent": self.multi_gold_queries / dependent_queries if dependent_queries else None,
            "short_fully_complete_gold_queries": self.short_fully_complete_queries,
            "short_partially_complete_gold_queries": self.short_partially_complete_queries,
            "short_zero_complete_gold_queries": self.short_zero_complete_gold_queries,
            "short_query_completion_mean": statistics.fmean(self.short_query_completion_values) if self.short_query_completion_values else None,
            "question_length": describe(self.question_lengths),
            "answer_length": describe(self.answer_lengths),
            "question_nonspace_length": describe(self.question_nonspace_lengths),
            "answer_nonspace_length": describe(self.answer_nonspace_lengths),
            "question_chinese_chars": describe(self.question_chinese_lengths),
            "answer_chinese_chars": describe(self.answer_chinese_lengths),
            "question_sentence_count": describe(self.question_sentence_counts),
            "answer_paragraph_count": describe(self.answer_paragraph_counts),
            "answer_heading_count": describe(self.answer_heading_counts),
            "reference_distance": describe(self.reference_distances),
            "effective_requirement_distance": describe(self.effective_requirement_distances),
            "query_max_reference_distance": describe(self.query_max_reference_distances),
            "query_dependency_class": dict(self.query_dependency_class),
            "gold_requirements_per_query": dict(sorted(self.gold_requirements_per_query.items())),
            "gold_alternatives_per_query": dict(sorted(self.gold_alternatives_per_query.items())),
            "reference_distance_buckets": dict(self.requirement_distance_bucket_counter),
            "question_length_buckets": dict(self.question_length_buckets),
            "answer_length_buckets": dict(self.answer_length_buckets),
            "needs_context_values": dict(self.needs_context_values),
            "duplicate_normalized_fingerprint_rows": duplicate_norm_fp_rows,
            "duplicate_structure_fingerprint_rows": duplicate_struct_fp_rows,
            "duplicate_recomputed_question_rows": duplicate_question_hash_rows,
            "duplicate_exact_answer_rows": duplicate_answer_hash_rows,
            "repeated_answer_paragraph_patterns_df_ge_2": repeated_para,
            "high_similarity_question_pairs": len(self.high_similarity_question_pairs),
            "explicit_locator_query_union": sum(1 for r in self.rows if any(p.search(r.question or "") for p in EXPLICIT_LOCATOR_PATTERNS.values())),
            "stored_answer_length_mismatches": len(self.stored_answer_length_mismatches),
            "max_back_mismatches": len(self.max_back_mismatches),
            "quality_issue_count": len(self.quality_issues),
        }


def sheet_id_from_name(name: str) -> Optional[str]:
    m = SESSION_RE.match(name or "")
    return m.group(1).upper() if m else None


def map_row_by_headers(row_by_col: Dict[str, str], header_by_col: Dict[str, str]) -> Dict[str, str]:
    return {header_by_col[col]: value for col, value in row_by_col.items() if col in header_by_col}


def write_csv(path: Path, rows: Sequence[Dict], fieldnames: Optional[Sequence[str]] = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    if fieldnames is None:
        names = []
        seen = set()
        for row in rows:
            for k in row.keys():
                if k not in seen:
                    seen.add(k); names.append(k)
        fieldnames = names
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)


def counter_rows(counter: Counter, key_name="value", total: Optional[int] = None, min_count: int = 1, limit: Optional[int] = None):
    items = [(k, v) for k, v in counter.most_common() if v >= min_count]
    if limit:
        items = items[:limit]
    return [
        {key_name: k, "count": v, "pct": (v / total if total else None)}
        for k, v in items
    ]


def flatten_overall(overall: Dict) -> List[Dict[str, object]]:
    rows = []
    def walk(prefix, obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(f"{prefix}.{k}" if prefix else str(k), v)
        else:
            rows.append({"metric": prefix, "value": obj})
    walk("", overall)
    return rows


def build_report(an: Analyzer, overall: Dict, session_rows: List[Dict]) -> str:
    q = overall["question_length"]
    a = overall["answer_length"]
    rd = overall["effective_requirement_distance"]
    lines = []
    lines.append("# Agent Memory 数据集统计报告")
    lines.append("")
    lines.append(f"- ShortTerm window：最近 **{an.short_window} 个 QA turn**")
    lines.append(f"- Session 数：**{overall['total_sessions']}**")
    lines.append(f"- Query 数：**{overall['total_queries']}**")
    lines.append("")

    lines.append("## 1. 核心规模与依赖")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("|---|---:|")
    lines.append(f"| 总 Query | {overall['total_queries']:,} |")
    lines.append(f"| 依赖前文 Query | {overall['dependent_queries']:,} ({fmt_pct(overall['dependent_queries'], overall['total_queries'])}) |")
    lines.append(f"| 独立 Query | {overall['independent_queries']:,} ({fmt_pct(overall['independent_queries'], overall['total_queries'])}) |")
    lines.append(f"| 有效 Gold requirements | {overall['valid_gold_requirements']:,} |")
    lines.append(f"| OR requirements | {overall['or_requirements']:,} |")
    lines.append(f"| Multi-Gold Query | {overall['multi_gold_queries']:,} |")
    lines.append(f"| ShortTerm 可覆盖 Gold | {overall['short_covered_requirements']:,} ({fmt_pct(overall['short_covered_requirements'], overall['valid_gold_requirements'])}) |")
    lines.append(f"| ShortTerm 外 / MidTerm eligible Gold | {overall['outside_short_requirements']:,} ({fmt_pct(overall['outside_short_requirements'], overall['valid_gold_requirements'])}) |")
    lines.append(f"| 至少含 1 个 ShortTerm 外 requirement 的 Query | {overall['midterm_eligible_queries']:,} |")
    lines.append(f"| ShortTerm Query Completion 均值 | {fmt_num((overall['short_query_completion_mean'] or 0) * 100)}% |")
    lines.append("")

    lines.append("## 2. Question / Answer 长度")
    lines.append("")
    lines.append("长度单位为 Python Unicode 字符数，包含空格和换行；另有 non-space 口径写入 JSON。")
    lines.append("")
    lines.append("| 指标 | Question | Answer |")
    lines.append("|---|---:|---:|")
    for label, key in [("均值", "mean"), ("总体标准差", "std_population"), ("样本标准差", "std_sample"), ("最小", "min"), ("P25", "p25"), ("中位数", "median"), ("P75", "p75"), ("P90", "p90"), ("P95", "p95"), ("P99", "p99"), ("最大", "max")]:
        lines.append(f"| {label} | {fmt_num(q.get(key))} | {fmt_num(a.get(key))} |")
    lines.append("")

    lines.append("## 3. ShortTerm=window 的 Query 分类")
    lines.append("")
    lines.append("| 分类 | Query 数 |")
    lines.append("|---|---:|")
    for k, v in sorted(an.query_dependency_class.items()):
        lines.append(f"| {k} | {v:,} |")
    lines.append("")
    lines.append("定义：`short_only` 表示所有有效 Gold requirements 均能被最近 window 轮覆盖；`outside_only` 表示一个都不能；`mixed` 表示同时存在窗口内和窗口外 requirement。")
    lines.append("")

    lines.append("## 4. Gold 回溯距离")
    lines.append("")
    lines.append("OR group 的 requirement effective distance 取其有效 alternatives 中的最小距离，因为任意一个召回即可满足该 requirement。")
    lines.append("")
    lines.append("| 统计 | Effective Gold Distance |")
    lines.append("|---|---:|")
    for label, key in [("均值", "mean"), ("总体标准差", "std_population"), ("中位数", "median"), ("P90", "p90"), ("P95", "p95"), ("最大", "max")]:
        lines.append(f"| {label} | {fmt_num(rd.get(key))} |")
    lines.append("")
    lines.append("### 距离分桶")
    lines.append("")
    lines.append("| 距离 | Requirement 数 | 占有效 Gold |")
    lines.append("|---|---:|---:|")
    for bucket in ["1-3", "4-6", "7-10", "11-20", "21+", "invalid<=0"]:
        n = an.requirement_distance_bucket_counter.get(bucket, 0)
        lines.append(f"| {bucket} | {n:,} | {fmt_pct(n, an.valid_gold_requirements)} |")
    lines.append("")

    lines.append("## 5. 数据质量")
    lines.append("")
    lines.append("| 检查项 | 数量 |")
    lines.append("|---|---:|")
    lines.append(f"| 无效 Gold requirement | {an.invalid_gold_requirements:,} |")
    lines.append(f"| 无效历史引用 | {an.invalid_reference_count:,} |")
    lines.append(f"| `最大回溯轮数` 与计算结果不一致 | {len(an.max_back_mismatches):,} |")
    lines.append(f"| `回答字符数` 与实际 len(answer) 不一致 | {len(an.stored_answer_length_mismatches):,} |")
    lines.append(f"| 所有质量问题记录 | {len(an.quality_issues):,} |")
    lines.append("")

    lines.append("## 6. 显式轮次定位 / 历史承接措辞")
    lines.append("")
    lines.append(f"出现任意显式轮次定位的 Query：**{overall['explicit_locator_query_union']:,}**")
    lines.append("")
    lines.append("| 模式 | Query 数 | 出现次数 |")
    lines.append("|---|---:|---:|")
    for name in EXPLICIT_LOCATOR_PATTERNS:
        lines.append(f"| {name} | {an.explicit_locator_queries.get(name,0):,} | {an.explicit_locator_occurrences.get(name,0):,} |")
    lines.append("")

    lines.append("## 7. 同质化 / 重复度")
    lines.append("")
    lines.append("| 指标 | 数量 |")
    lines.append("|---|---:|")
    lines.append(f"| 高相似 Question pair（同 Session，4-gram Jaccard ≥ {an.similarity_threshold:.2f}） | {len(an.high_similarity_question_pairs):,} |")
    lines.append(f"| 重复问题归一化指纹覆盖行数 | {overall['duplicate_normalized_fingerprint_rows']:,} |")
    lines.append(f"| 重复问题结构指纹覆盖行数 | {overall['duplicate_structure_fingerprint_rows']:,} |")
    lines.append(f"| 重算归一化问题重复覆盖行数 | {overall['duplicate_recomputed_question_rows']:,} |")
    lines.append(f"| 完全相同 Answer 覆盖行数 | {overall['duplicate_exact_answer_rows']:,} |")
    lines.append(f"| 被至少 2 个 Answer 复用的长段落模式 | {overall['repeated_answer_paragraph_patterns_df_ge_2']:,} |")
    lines.append("")

    lines.append("## 8. Session 级摘要")
    lines.append("")
    lines.append("| Session | Turn | Gold | Short | Outside | Outside% | Mid Eligible Q | Q均长 | A均长 | A标准差 | MaxDist | Locator Q |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in session_rows:
        lines.append(
            f"| {r['session_id']} | {r['turns']} | {r['valid_gold_requirements']} | "
            f"{r['short_covered_requirements']} | {r['outside_short_requirements']} | "
            f"{fmt_pct(r['outside_short_requirements'], r['valid_gold_requirements'])} | "
            f"{r['midterm_eligible_queries']} | {fmt_num(r['question_len_mean'])} | "
            f"{fmt_num(r['answer_len_mean'])} | {fmt_num(r['answer_len_std_population'])} | "
            f"{r['max_reference_distance']} | {r['explicit_locator_queries']} |"
        )
    lines.append("")

    lines.append("## 9. 输出文件说明")
    lines.append("")
    lines.append("完整分布、Top 高频项、重复段落、高相似问题 pair 和逐条质量问题均在同目录 CSV/JSON 中。")
    lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="统计 Agent Memory 金融问答 XLSX 数据集")
    ap.add_argument("input", type=Path, help="输入 .xlsx 文件")
    ap.add_argument("--sessions", default="all", help="Session 范围，如 all / S001-S010 / S001,S003,S010")
    ap.add_argument("--short-window", type=int, default=3, help="ShortTerm 最近 QA turn 数，默认 3")
    ap.add_argument("--output-dir", type=Path, default=None, help="输出目录")
    ap.add_argument("--similarity-threshold", type=float, default=0.80, help="同 Session 问题 4-gram Jaccard 高相似阈值")
    ap.add_argument("--skip-similarity", action="store_true", help="跳过 Question pairwise similarity，可进一步加速")
    ap.add_argument("--top-n", type=int, default=100, help="Top 高频/异常项输出数量")
    args = ap.parse_args()

    if args.short_window < 1:
        raise SystemExit("--short-window 必须 >= 1")
    if not args.input.exists():
        raise SystemExit(f"输入文件不存在: {args.input}")
    if args.input.suffix.lower() != ".xlsx":
        raise SystemExit("当前脚本只支持 .xlsx")

    outdir = args.output_dir or args.input.with_name(args.input.stem + "_metrics")
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"[1/6] 读取工作簿结构: {args.input}")
    with zipfile.ZipFile(args.input) as zf:
        sheets = load_sheet_metadata(zf)
        session_to_sheet = {}
        for name, path in sheets:
            sid = sheet_id_from_name(name)
            if sid:
                session_to_sheet[sid] = (name, path)
        available = sorted(session_to_sheet)
        selected = parse_session_selector(args.sessions, available)
        if not selected:
            raise SystemExit("没有选中任何 Session")

        print(f"[2/6] 载入 shared strings（{len(selected)} 个 Session 将被统计）")
        shared = load_shared_strings(zf)

        analyzer = Analyzer(
            short_window=args.short_window,
            similarity_threshold=args.similarity_threshold,
        )

        print("[3/6] 扫描各 Session")
        for idx, sid in enumerate(selected, 1):
            sheet_name, sheet_path = session_to_sheet[sid]
            row_iter = iter_sheet_rows(zf, sheet_path, shared)
            try:
                header_row = next(row_iter)
            except StopIteration:
                print(f"  - {sid}: 空 Sheet，跳过")
                continue
            header_by_col = {col: val.strip() for col, val in header_row.items() if val.strip()}
            missing_headers = [c for c in EXPECTED_COLUMNS if c not in header_by_col.values()]
            if missing_headers:
                print(f"  ! {sid} 缺少字段: {missing_headers}")

            row_count = 0
            for raw_by_col in row_iter:
                raw = map_row_by_headers(raw_by_col, header_by_col)
                qid = (raw.get("编号") or "").strip()
                if not qid:
                    continue
                row_count += 1
                rec = RowRecord(
                    sheet_name=sheet_name,
                    session_id=sid,
                    qid=qid,
                    question=raw.get("当前问题", ""),
                    answer=raw.get("最终回答", ""),
                    needs_context=raw.get("是否需要前文", ""),
                    gold_raw=raw.get("关联前序对话", ""),
                    recalled_content=raw.get("实际需召回内容（原始回答）", ""),
                    dependency_reason=raw.get("所需前文信息", ""),
                    dependency_type=raw.get("依赖类型", ""),
                    stored_max_back=parse_int(raw.get("最大回溯轮数", "")),
                    role=raw.get("当前角色", ""),
                    topic=raw.get("会话主题", ""),
                    question_form=raw.get("问题形态", ""),
                    question_length_class=raw.get("问题长度档", ""),
                    company=raw.get("公司", ""),
                    stock_code=raw.get("股票代码", ""),
                    industry=raw.get("行业", ""),
                    period=raw.get("分析期间", ""),
                    data_nature=raw.get("数据性质", ""),
                    source_file=raw.get("来源文件", ""),
                    source_date=raw.get("来源发布日期", ""),
                    stored_answer_chars=parse_int(raw.get("回答字符数", "")),
                    normalized_fingerprint=raw.get("问题归一化指纹", ""),
                    structure_fingerprint=raw.get("问题结构指纹", ""),
                    validation_result=raw.get("验证结果", ""),
                    stored_session_turns=parse_int(raw.get("Session轮数", "")),
                )
                analyzer.add_row(rec, raw)
            print(f"  - {sid}: {row_count} turns")

    print("[4/6] 解析 Gold / ShortTerm / distance / consistency")
    analyzer.analyze_gold()
    if not args.skip_similarity:
        print("[5/6] 计算同 Session Question 高相似 pair")
        analyzer.analyze_question_similarity()
    else:
        print("[5/6] 已跳过 Question pairwise similarity")

    print("[6/6] 写出报告")
    overall = analyzer.overall()
    session_metrics = analyzer.session_metrics()
    company_metrics = analyzer.company_metrics(session_metrics)

    # JSON
    payload = {
        "input": str(args.input),
        "sessions": sorted(analyzer.session_rows),
        "overall": overall,
        "distributions": {
            "dependency_type": dict(analyzer.dependency_types),
            "topic": dict(analyzer.topics),
            "question_form": dict(analyzer.question_forms),
            "question_length_class": dict(analyzer.question_length_classes),
            "role": dict(analyzer.roles),
            "company": dict(analyzer.companies),
            "industry": dict(analyzer.industries),
            "validation_result": dict(analyzer.validation_results),
            "reference_distance_exact": dict(sorted(analyzer.reference_distance_counter.items())),
            "requirement_effective_distance_exact": dict(sorted(analyzer.requirement_distance_counter.items())),
            "requirement_distance_bucket": dict(analyzer.requirement_distance_bucket_counter),
            "question_length_bucket": dict(analyzer.question_length_buckets),
            "answer_length_bucket": dict(analyzer.answer_length_buckets),
            "implicit_history_query_patterns": dict(analyzer.implicit_history_queries),
            "explicit_locator_query_patterns": dict(analyzer.explicit_locator_queries),
        },
        "session_metrics": session_metrics,
        "company_metrics": company_metrics,
        "column_missing": {
            col: {"missing": analyzer.column_missing[col], "total": analyzer.column_total[col], "missing_pct": analyzer.column_missing[col] / analyzer.column_total[col] if analyzer.column_total[col] else None}
            for col in EXPECTED_COLUMNS
        },
    }
    (outdir / "metrics.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # 核心 CSV
    write_csv(outdir / "overall_metrics.csv", flatten_overall(overall))
    write_csv(outdir / "session_metrics.csv", session_metrics)
    write_csv(outdir / "company_metrics.csv", company_metrics)

    write_csv(outdir / "gold_effective_distance.csv", [
        {"distance": d, "count": c, "pct_of_valid_gold": c / analyzer.valid_gold_requirements if analyzer.valid_gold_requirements else None}
        for d, c in sorted(analyzer.requirement_distance_counter.items())
    ])
    write_csv(outdir / "gold_distance_buckets.csv", [
        {"bucket": b, "count": analyzer.requirement_distance_bucket_counter.get(b, 0), "pct_of_valid_gold": analyzer.requirement_distance_bucket_counter.get(b, 0) / analyzer.valid_gold_requirements if analyzer.valid_gold_requirements else None}
        for b in ["1-3", "4-6", "7-10", "11-20", "21+", "invalid<=0"]
    ])
    write_csv(outdir / "dependency_type_distribution.csv", counter_rows(analyzer.dependency_types, "dependency_type", len(analyzer.rows)))
    write_csv(outdir / "topic_distribution.csv", counter_rows(analyzer.topics, "topic", len(analyzer.rows)))
    write_csv(outdir / "question_form_distribution.csv", counter_rows(analyzer.question_forms, "question_form", len(analyzer.rows)))
    write_csv(outdir / "question_length_class_distribution.csv", counter_rows(analyzer.question_length_classes, "question_length_class", len(analyzer.rows)))
    write_csv(outdir / "validation_result_distribution.csv", counter_rows(analyzer.validation_results, "validation_result", len(analyzer.rows)))
    write_csv(outdir / "question_length_buckets.csv", counter_rows(analyzer.question_length_buckets, "bucket", len(analyzer.rows)))
    write_csv(outdir / "answer_length_buckets.csv", counter_rows(analyzer.answer_length_buckets, "bucket", len(analyzer.rows)))

    missing_rows = []
    for col in EXPECTED_COLUMNS:
        total = analyzer.column_total[col]
        missing = analyzer.column_missing[col]
        missing_rows.append({"column": col, "missing": missing, "total": total, "missing_pct": missing / total if total else None})
    write_csv(outdir / "missingness.csv", missing_rows)

    # 高频 / 同质化
    for n, counter in analyzer.question_prefixes.items():
        write_csv(outdir / f"question_prefix_{n}.csv", counter_rows(counter, "prefix", len(analyzer.rows), min_count=2, limit=args.top_n))
    for n, counter in analyzer.answer_openings.items():
        write_csv(outdir / f"answer_opening_{n}.csv", counter_rows(counter, "opening", len(analyzer.rows), min_count=2, limit=args.top_n))
    write_csv(outdir / "answer_heading_doc_frequency.csv", counter_rows(analyzer.heading_doc_freq, "heading", len(analyzer.rows), min_count=2, limit=args.top_n))

    repeated_paras = []
    for ph, count in analyzer.repeated_paragraph_doc_freq.most_common(args.top_n):
        if count < 2:
            break
        repeated_paras.append({
            "paragraph_hash": ph,
            "answer_doc_count": count,
            "answer_doc_pct": count / len(analyzer.rows) if analyzer.rows else None,
            "example": analyzer.paragraph_examples.get(ph, ""),
        })
    write_csv(outdir / "repeated_answer_paragraphs.csv", repeated_paras)

    dup_norm = []
    for fp, count in analyzer.normalized_fingerprints.most_common():
        if count < 2: break
        dup_norm.append({"fingerprint": fp, "count": count, "example_qids": "；".join(analyzer.normalized_question_examples.get(fp, []))})
    write_csv(outdir / "duplicate_normalized_fingerprints.csv", dup_norm[:args.top_n])

    dup_struct = []
    for fp, count in analyzer.structure_fingerprints.most_common():
        if count < 2: break
        dup_struct.append({"fingerprint": fp, "count": count, "example_qids": "；".join(analyzer.structure_fp_examples.get(fp, []))})
    write_csv(outdir / "duplicate_structure_fingerprints.csv", dup_struct[:args.top_n])

    sim_rows = [
        {"similarity": s, "session_id": sid, "qid_1": q1, "qid_2": q2, "question_1": t1, "question_2": t2}
        for s, sid, q1, q2, t1, t2 in analyzer.high_similarity_question_pairs
    ]
    write_csv(outdir / "high_similarity_questions.csv", sim_rows)

    explicit_rows = []
    for name in EXPLICIT_LOCATOR_PATTERNS:
        explicit_rows.append({
            "pattern": name,
            "query_count": analyzer.explicit_locator_queries.get(name, 0),
            "occurrence_count": analyzer.explicit_locator_occurrences.get(name, 0),
            "query_pct": analyzer.explicit_locator_queries.get(name, 0) / len(analyzer.rows) if analyzer.rows else None,
        })
    write_csv(outdir / "explicit_locator_patterns.csv", explicit_rows)
    write_csv(outdir / "implicit_history_patterns.csv", counter_rows(analyzer.implicit_history_queries, "pattern", len(analyzer.rows)))

    # 质量问题
    write_csv(outdir / "quality_issues.csv", analyzer.quality_issues, fieldnames=["qid", "type", "detail"])
    write_csv(outdir / "answer_length_mismatches.csv", [
        {"qid": qid, "stored": stored, "computed": computed, "delta": computed - stored}
        for qid, stored, computed in analyzer.stored_answer_length_mismatches
    ])
    write_csv(outdir / "max_back_mismatches.csv", [
        {"qid": qid, "stored": stored, "computed": computed}
        for qid, stored, computed in analyzer.max_back_mismatches
    ])

    # Markdown 报告
    report = build_report(analyzer, overall, session_metrics)
    (outdir / "report.md").write_text(report, encoding="utf-8")

    print("\n完成。核心结果：")
    print(f"  Query: {overall['total_queries']}")
    print(f"  Valid Gold requirements: {overall['valid_gold_requirements']}")
    print(f"  ShortTerm(window={args.short_window}): {overall['short_covered_requirements']} / {overall['valid_gold_requirements']} = {fmt_pct(overall['short_covered_requirements'], overall['valid_gold_requirements'])}")
    print(f"  Outside ShortTerm / MidTerm eligible: {overall['outside_short_requirements']} / {overall['valid_gold_requirements']} = {fmt_pct(overall['outside_short_requirements'], overall['valid_gold_requirements'])}")
    print(f"  Question len mean/std(pop): {fmt_num(overall['question_length']['mean'])} / {fmt_num(overall['question_length']['std_population'])}")
    print(f"  Answer len mean/std(pop): {fmt_num(overall['answer_length']['mean'])} / {fmt_num(overall['answer_length']['std_population'])}")
    print(f"  Report: {outdir / 'report.md'}")
    print(f"  JSON:   {outdir / 'metrics.json'}")


if __name__ == "__main__":
    main()
