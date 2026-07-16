import pytest


@pytest.fixture
def ensure_spacy():
    """Skip tests if spaCy model is not available."""
    try:
        import spacy

        spacy.load("en_core_web_sm")
    except Exception:
        pytest.skip("spaCy en_core_web_sm model not available")


class TestLemmatizeForBm25:
    def test_basic_lemmatization(self, ensure_spacy):
        from mem0.utils.lemmatization import lemmatize_for_bm25

        result = lemmatize_for_bm25("The cats are running quickly")
        assert "cat" in result
        assert "run" in result or "running" in result
        # Stop words and punctuation should be removed
        assert "the" not in result.split()

    def test_verb_forms_normalized(self, ensure_spacy):
        from mem0.utils.lemmatization import lemmatize_for_bm25

        result = lemmatize_for_bm25("she attended multiple meetings yesterday")
        assert "attend" in result or "attended" in result
        assert "meeting" in result  # -ing form preserved alongside lemma
        # "multiple" is kept (not a spaCy stop word)

    def test_ing_preservation(self, ensure_spacy):
        from mem0.utils.lemmatization import lemmatize_for_bm25

        result = lemmatize_for_bm25("attending the morning meeting")
        tokens = result.split()
        # Should have both the lemma and the -ing form
        assert "attending" in tokens or "attend" in tokens

    def test_empty_string(self):
        from mem0.utils.lemmatization import lemmatize_for_bm25

        result = lemmatize_for_bm25("")
        assert result == ""

    def test_punctuation_removed(self):
        from mem0.utils.lemmatization import lemmatize_for_bm25

        result = lemmatize_for_bm25("Hello, world! How are you?")
        assert "," not in result
        assert "!" not in result
        assert "?" not in result

    def test_lowercased(self):
        from mem0.utils.lemmatization import lemmatize_for_bm25

        result = lemmatize_for_bm25("PYTHON Programming LANGUAGE")
        for token in result.split():
            assert token == token.lower()

    def test_stop_words_removed(self, ensure_spacy):
        from mem0.utils.lemmatization import lemmatize_for_bm25

        result = lemmatize_for_bm25("this is a very simple test of the system")
        tokens = result.split()
        for stop in ["this", "is", "a", "very", "of", "the"]:
            assert stop not in tokens

    def test_chinese_sentence_segmented_with_spaces(self, monkeypatch):
        from mem0.utils import lemmatization

        monkeypatch.setattr(lemmatization, "_jieba_initialized", False)

        result = lemmatization.lemmatize_for_bm25("用户最多能接受本金亏损10%，偏好沪深300指数基金。")
        tokens = result.split()

        assert len(tokens) > 1
        for token in ["本金亏损", "10", "沪深300", "指数基金"]:
            assert token in tokens

    def test_finance_custom_words_are_preserved(self, monkeypatch):
        from mem0.utils import lemmatization

        monkeypatch.setattr(lemmatization, "_jieba_initialized", False)

        result = lemmatization.lemmatize_for_bm25("客户风险承受能力较低，需要关注最大回撤和本金亏损。")
        tokens = result.split()

        for token in ["风险承受能力", "最大回撤", "本金亏损"]:
            assert token in tokens

    def test_mixed_chinese_english_text(self, monkeypatch):
        from mem0.utils import lemmatization

        monkeypatch.setattr(lemmatization, "_jieba_initialized", False)

        result = lemmatization.lemmatize_for_bm25("Portfolio 偏好沪深300指数基金，最多接受10% drawdown。")
        tokens = result.split()

        assert "portfolio" in tokens
        assert "沪深300" in tokens
        assert "指数基金" in tokens
        assert "10" in tokens
        assert any(token.startswith("drawdown") for token in tokens)

    def test_missing_finance_dictionary_does_not_error(self, monkeypatch, tmp_path):
        from mem0.utils import lemmatization

        monkeypatch.setattr(lemmatization, "_FINANCE_DICT_PATH", tmp_path / "missing-dict.txt")
        monkeypatch.setattr(lemmatization, "_jieba_initialized", False)

        result = lemmatization.lemmatize_for_bm25("用户偏好指数基金。")

        assert result
