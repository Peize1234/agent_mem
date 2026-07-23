import logging
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from mem0.utils import lemmatization
from mem0.utils.entity_extraction import (
    _financial_entity_term_set,
    _normalize_chinese_financial_name,
    extract_entities,
    extract_entities_batch,
    is_valid_entity,
)
from mem0.utils.language import contains_chinese


class _LemmaToken:
    def __init__(self, text, lemma, *, is_stop=False, is_punct=False):
        self.text = text
        self.lemma_ = lemma
        self.is_stop = is_stop
        self.is_punct = is_punct


def _chinese_doc(source_text, entity_text, label="ORG"):
    start = source_text.index(entity_text)
    entity = SimpleNamespace(
        text=entity_text,
        label_=label,
        start_char=start,
        end_char=start + len(entity_text),
    )
    return SimpleNamespace(ents=[entity])


def test_contains_chinese_covers_extension_a_and_unified_ideographs():
    assert contains_chinese("\u3400")
    assert contains_chinese("\u9fff")
    assert not contains_chinese("plain English")


def test_chinese_bm25_uses_jieba_and_loads_finance_dictionary_once(monkeypatch):
    fake_jieba = SimpleNamespace(
        load_userdict=MagicMock(),
        lcut=MagicMock(return_value=["华夏", "沪深300", "ETF", "适合", "长期", "持有", "吗", "。"]),
    )
    monkeypatch.setitem(sys.modules, "jieba", fake_jieba)
    monkeypatch.setattr(lemmatization, "_jieba_initialized", False)

    first = lemmatization.lemmatize_for_bm25("华夏沪深300ETF适合长期持有吗")
    second = lemmatization.lemmatize_for_bm25("华夏沪深300ETF适合长期持有吗")

    assert first == second == "华夏 沪深300 ETF 适合 长期 持有 吗"
    fake_jieba.load_userdict.assert_called_once_with(str(lemmatization._FINANCE_DICT_PATH))
    assert fake_jieba.lcut.call_count == 2


def test_chinese_bm25_failure_falls_back_to_normalized_source(monkeypatch):
    fake_jieba = SimpleNamespace(load_userdict=MagicMock(), lcut=MagicMock(side_effect=RuntimeError("boom")))
    monkeypatch.setitem(sys.modules, "jieba", fake_jieba)
    monkeypatch.setattr(lemmatization, "_jieba_initialized", False)

    assert lemmatization.lemmatize_for_bm25("中文，ETF") == "中文,etf"


def test_english_bm25_keeps_spacy_lemma_path():
    nlp = MagicMock(
        return_value=[
            _LemmaToken("The", "the", is_stop=True),
            _LemmaToken("cats", "cat"),
            _LemmaToken("running", "run"),
        ]
    )
    with patch("mem0.utils.spacy_models.get_nlp_lemma", return_value=nlp):
        assert lemmatization.lemmatize_for_bm25("The cats running") == "cat run running"
    nlp.assert_called_once_with("the cats running")


def test_chinese_document_and_query_use_identical_bm25_tokens(monkeypatch):
    fake_jieba = SimpleNamespace(
        load_userdict=MagicMock(),
        lcut=MagicMock(return_value=["华夏", "沪深300", "ETF"]),
    )
    monkeypatch.setitem(sys.modules, "jieba", fake_jieba)
    monkeypatch.setattr(lemmatization, "_jieba_initialized", False)
    text = "华夏沪深300ETF"

    document_text = lemmatization.lemmatize_for_bm25(text)
    query_text = lemmatization.lemmatize_for_bm25(text)

    assert document_text == query_text == "华夏 沪深300 ETF"


def test_chinese_never_calls_english_spacy_and_missing_model_is_safe():
    text = "用户计划在2027年4月前后买房，并开始准备首付，这可能会影响其长期投资账户的安排。"
    with (
        patch("mem0.utils.spacy_models.get_nlp_chinese", return_value=None),
        patch("mem0.utils.spacy_models.get_nlp_full", side_effect=AssertionError("English model used")) as english,
    ):
        assert extract_entities(text) == []
    english.assert_not_called()


def test_installed_chinese_ner_model_is_used():
    text = "用户曾在招商银行办理业务。"
    chinese_nlp = MagicMock(return_value=_chinese_doc(text, "招商银行"))
    with patch("mem0.utils.spacy_models.get_nlp_chinese", return_value=chinese_nlp):
        assert ("PROPER", "招商银行") in extract_entities(text)
    chinese_nlp.assert_called_once_with(text)


@pytest.mark.parametrize("entity_text", ["账单日用于", "账单日", "还款日"])
def test_chinese_ner_rejects_predicate_tailed_and_generic_financial_terms(entity_text):
    text = f"助手解释{entity_text}确定当期应还金额。"
    chinese_nlp = MagicMock(return_value=_chinese_doc(text, entity_text))

    with patch("mem0.utils.spacy_models.get_nlp_chinese", return_value=chinese_nlp):
        assert extract_entities(text) == []


def test_chinese_batch_ner_rejects_predicate_tailed_false_positive():
    text = "助手解释账单日用于确定当期应还金额。"
    chinese_nlp = MagicMock()
    chinese_nlp.pipe.return_value = [_chinese_doc(text, "账单日用于")]

    with patch("mem0.utils.spacy_models.get_nlp_chinese", return_value=chinese_nlp):
        assert extract_entities_batch([text]) == [[]]


def test_whole_chinese_sentence_from_ner_is_filtered():
    text = "用户计划在2027年4月前后买房并开始准备首付。"
    chinese_nlp = MagicMock(return_value=_chinese_doc(text, text))
    with patch("mem0.utils.spacy_models.get_nlp_chinese", return_value=chinese_nlp):
        assert extract_entities(text) == []


def test_missing_chinese_model_logs_warning(caplog, monkeypatch):
    from mem0.utils import spacy_models

    fake_spacy = SimpleNamespace(util=SimpleNamespace(is_package=MagicMock(return_value=False)))
    monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
    monkeypatch.setattr(spacy_models, "_nlp_chinese", None)
    monkeypatch.setattr(spacy_models, "_load_failed_chinese", False)
    with caplog.at_level(logging.WARNING):
        assert spacy_models.get_nlp_chinese() is None
    assert "Chinese spaCy NER unavailable" in caplog.text


@pytest.mark.parametrize(
    ("entity_type", "entity_text", "source_text"),
    [
        ("PROPER", "", "some source"),
        ("PROPER", "完整原文", "完整原文"),
        ("PROPER", "准备在年底调整自己的长期投资安排", "用户准备在年底调整自己的长期投资安排，并已开始规划后续事项"),
        ("PROPER", "这是完整陈述。", "用户曾说这是完整陈述。随后还有其他内容。"),
        ("PROPER", "第一句. 第二句!", "用户复制了第一句和第二句作为完整陈述"),
        ("PROPER", "a" * 65, "source " * 30),
        ("PROPER", "---", "source"),
    ],
)
def test_entity_quality_filter_rejects_invalid_candidates(entity_type, entity_text, source_text):
    assert not is_valid_entity(entity_type, entity_text, source_text)


def test_financial_names_indexes_stocks_and_codes_are_preserved():
    text = "用户持有易方达蓝筹精选混合基金，关注华夏沪深300ETF、沪深300指数和贵州茅台（600519）。"
    with patch("mem0.utils.spacy_models.get_nlp_chinese", return_value=None):
        entities = extract_entities(text)

    assert ("FINANCIAL_PRODUCT", "易方达蓝筹精选混合基金") in entities
    assert ("FINANCIAL_PRODUCT", "华夏沪深300ETF") in entities
    assert ("INDEX", "沪深300指数") in entities
    assert ("STOCK", "贵州茅台") in entities
    assert ("SECURITY_CODE", "600519") in entities
    assert all(entity_text != text for _, entity_text in entities)


@pytest.mark.parametrize(
    ("raw_name", "expected"),
    [
        ("如货币基金", "货币基金"),
        ("例如货币基金", "货币基金"),
        ("比如沪深300指数", "沪深300指数"),
        ("诸如华夏沪深300ETF", "华夏沪深300ETF"),
        ("如意宝货币基金", "如意宝货币基金"),
        ("并推荐货币基金", "货币基金"),
        ("助手推荐货币基金", "货币基金"),
        ("中欧投资基金", "中欧投资基金"),
        ("华夏回报二号证券投资基金", "华夏回报二号证券投资基金"),
    ],
)
def test_chinese_financial_name_normalization(raw_name, expected):
    assert _normalize_chinese_financial_name(raw_name) == expected


def test_chinese_financial_example_prefix_is_not_persisted():
    text = "可以配置如货币基金等低风险产品。"
    with patch("mem0.utils.spacy_models.get_nlp_chinese", return_value=None):
        entities = extract_entities(text)

    assert ("FINANCIAL_PRODUCT", "货币基金") in entities
    assert ("FINANCIAL_PRODUCT", "如货币基金") not in entities


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("用户喜欢如货币基金。", ("FINANCIAL_PRODUCT", "货币基金")),
        ("用户喜欢如意宝货币基金。", ("FINANCIAL_PRODUCT", "如意宝货币基金")),
        ("如货币基金。", ("FINANCIAL_PRODUCT", "货币基金")),
        ("例如货币基金。", ("FINANCIAL_PRODUCT", "货币基金")),
        ("并推荐货币基金。", ("FINANCIAL_PRODUCT", "货币基金")),
        ("用户持有华夏回报二号证券投资基金。", ("FINANCIAL_PRODUCT", "华夏回报二号证券投资基金")),
    ],
)
def test_chinese_financial_entity_boundaries(text, expected):
    with patch("mem0.utils.spacy_models.get_nlp_chinese", return_value=None):
        entities = extract_entities(text)

    assert expected in entities
    assert ("FINANCIAL_PRODUCT", "喜欢如货币基金") not in entities
    assert ("FINANCIAL_PRODUCT", "如货币基金") not in entities
    assert ("FINANCIAL_PRODUCT", "推荐货币基金") not in entities


def test_chinese_batch_extracts_recommended_product_without_action_prefix():
    text = (
        "用户询问首付10万元是否应与长期定投放在一起，助手建议单独建立账户管理，"
        "理由包括资金用途与时间匹配、风险承受能力差异，并推荐货币基金、短期理财等低风险工具"
    )
    with patch("mem0.utils.spacy_models.get_nlp_chinese", return_value=None):
        assert extract_entities_batch([text]) == [[("FINANCIAL_PRODUCT", "货币基金")]]


def test_financial_entity_term_set_is_cached():
    _financial_entity_term_set.cache_clear()
    try:
        with patch("mem0.utils.entity_extraction._load_financial_entity_terms", return_value=("货币基金",)) as loader:
            first = _financial_entity_term_set()
            second = _financial_entity_term_set()

        assert first is second
        assert first == frozenset({"货币基金"})
        loader.assert_called_once_with()
    finally:
        _financial_entity_term_set.cache_clear()


def test_security_code_pattern_rejects_dates_quantities_and_wrong_exchange():
    text = "计划日期202704，预算123456，错误代码SH000001，正确代码SH.600519。"
    with patch("mem0.utils.spacy_models.get_nlp_chinese", return_value=None):
        entities = extract_entities(text)

    codes = [entity_text for entity_type, entity_text in entities if entity_type == "SECURITY_CODE"]
    assert codes == ["SH.600519"]


def test_chinese_batch_matches_individual_extraction():
    texts = [
        "用户长期关注华夏沪深300ETF。",
        "用户计划在2027年4月前后买房，并开始准备首付。",
        "用户关注贵州茅台股票和600519。",
    ]
    with (
        patch("mem0.utils.spacy_models.get_nlp_chinese", return_value=None),
        patch("mem0.utils.spacy_models.get_nlp_full", side_effect=AssertionError("English model used")),
    ):
        individual = [extract_entities(text) for text in texts]
        batched = extract_entities_batch(texts)

    assert batched == individual
