import pytest

from mem0.utils.bm25_sparse import ChineseBM25SparseEncoder


def _as_mapping(sparse):
    return dict(zip(sparse.indices, sparse.values))


def test_document_and_query_share_token_indices():
    encoder = ChineseBM25SparseEncoder()

    document = next(encoder.embed("沪深300 ETF ETF"))
    query = next(encoder.query_embed("ETF 沪深300 ETF"))

    assert set(document.indices) == set(query.indices)
    assert query.values == [1.0, 1.0]


def test_document_uses_bm25_term_frequency_weights():
    encoder = ChineseBM25SparseEncoder(k=1.2, b=0.75, avg_len=256.0)

    sparse = _as_mapping(next(encoder.embed("基金 ETF ETF")))
    fund_index = encoder.compute_token_id("基金")
    etf_index = encoder.compute_token_id("ETF")
    length_factor = 1 - 0.75 + 0.75 * 3 / 256.0

    assert sparse[fund_index] == pytest.approx(1 * 2.2 / (1 + 1.2 * length_factor))
    assert sparse[etf_index] == pytest.approx(2 * 2.2 / (2 + 1.2 * length_factor))
    assert sparse[etf_index] > sparse[fund_index]


def test_query_uses_one_weight_per_unique_token():
    encoder = ChineseBM25SparseEncoder()

    sparse = next(encoder.query_embed("货币基金 货币基金 沪深300"))

    assert len(sparse.indices) == 2
    assert sparse.values == [1.0, 1.0]
