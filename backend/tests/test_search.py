# backend/tests/test_search.py
"""
Tests for app/services/company_index.py

The CompanyIndex is populated with a small fixture dataset (no HTTP calls).
All tests exercise the search algorithm directly.

Coverage:
  - Exact ticker match wins over prefix match
  - Name prefix match
  - Name substring match
  - Case-insensitivity (both ticker and name)
  - Results capped at MAX_RESULTS (5)
  - CIK deduplication (same CIK appears only once in results)
  - Empty query returns empty list
  - Whitespace-only query returns empty list
  - CIK_10 normalisation (integers -> zero-padded 10-digit strings)
"""

from __future__ import annotations

from app.services.company_index import CompanyIndex, _IndexEntry, MAX_RESULTS
from app.models.company import normalise_cik


# ---------------------------------------------------------------------------
# Fixture: small deterministic company dataset
# ---------------------------------------------------------------------------

_FIXTURE_ENTRIES: list[_IndexEntry] = [
    _IndexEntry(cik_10="0000320193", ticker="AAPL", name="Apple Inc.", name_lower="apple inc."),
    _IndexEntry(cik_10="0000789019", ticker="MSFT", name="Microsoft Corp", name_lower="microsoft corp"),
    _IndexEntry(cik_10="0001018724", ticker="AMZN", name="Amazon.com Inc.", name_lower="amazon.com inc."),
    _IndexEntry(cik_10="0001652044", ticker="GOOGL", name="Alphabet Inc.", name_lower="alphabet inc."),
    _IndexEntry(cik_10="0001326801", ticker="META", name="Meta Platforms Inc.", name_lower="meta platforms inc."),
    _IndexEntry(cik_10="0000051143", ticker="IBM", name="International Business Machines Corp", name_lower="international business machines corp"),
    _IndexEntry(cik_10="0000200406", ticker="JNJ", name="Johnson & Johnson", name_lower="johnson & johnson"),
    # Duplicate CIK (same company, different ticker) - dedup test
    _IndexEntry(cik_10="0000320193", ticker="AAPL.NQ", name="Apple Inc. (NQ)", name_lower="apple inc. (nq)"),
]


def _make_index() -> CompanyIndex:
    idx = CompanyIndex()
    idx._entries = list(_FIXTURE_ENTRIES)
    return idx


# ---------------------------------------------------------------------------
# Exact ticker match
# ---------------------------------------------------------------------------


def test_exact_ticker_match_aapl():
    idx = _make_index()
    results = idx.search("AAPL")
    assert len(results) >= 1
    assert results[0].cik_10 == "0000320193"
    assert results[0].ticker == "AAPL"


def test_exact_ticker_match_case_insensitive():
    idx = _make_index()
    results = idx.search("msft")
    assert len(results) >= 1
    assert results[0].ticker == "MSFT"


def test_exact_ticker_beats_prefix_match():
    """'IBM' exact ticker should rank above any name starting with 'ibm'."""
    idx = _make_index()
    results = idx.search("IBM")
    assert results[0].ticker == "IBM"


# ---------------------------------------------------------------------------
# Name prefix match
# ---------------------------------------------------------------------------


def test_name_prefix_match():
    idx = _make_index()
    results = idx.search("apple")
    cik_10s = [r.cik_10 for r in results]
    assert "0000320193" in cik_10s


def test_name_prefix_match_partial():
    idx = _make_index()
    results = idx.search("micro")
    cik_10s = [r.cik_10 for r in results]
    assert "0000789019" in cik_10s


# ---------------------------------------------------------------------------
# Name substring match
# ---------------------------------------------------------------------------


def test_name_substring_match():
    idx = _make_index()
    results = idx.search("johnson")
    cik_10s = [r.cik_10 for r in results]
    assert "0000200406" in cik_10s


def test_name_substring_in_middle():
    idx = _make_index()
    # "business" is a substring of "International Business Machines Corp"
    results = idx.search("business")
    cik_10s = [r.cik_10 for r in results]
    assert "0000051143" in cik_10s


# ---------------------------------------------------------------------------
# No match
# ---------------------------------------------------------------------------


def test_no_match_returns_empty():
    idx = _make_index()
    results = idx.search("ZZZNOTACOMPANY")
    assert results == []


# ---------------------------------------------------------------------------
# Edge cases: empty / whitespace
# ---------------------------------------------------------------------------


def test_empty_query_returns_empty():
    idx = _make_index()
    assert idx.search("") == []


def test_whitespace_query_returns_empty():
    idx = _make_index()
    assert idx.search("   ") == []


# ---------------------------------------------------------------------------
# Result cap at MAX_RESULTS
# ---------------------------------------------------------------------------


def test_results_capped_at_max():
    # Create an index with more than MAX_RESULTS entries that all match
    entries = [
        _IndexEntry(
            cik_10=normalise_cik(i),
            ticker=f"FOO{i}",
            name=f"FooBar Company {i}",
            name_lower=f"foobar company {i}",
        )
        for i in range(1, 12)  # 11 entries
    ]
    idx = CompanyIndex()
    idx._entries = entries

    results = idx.search("foobar")
    assert len(results) == MAX_RESULTS


# ---------------------------------------------------------------------------
# CIK deduplication
# ---------------------------------------------------------------------------


def test_cik_deduplication():
    """Same CIK should appear at most once in results."""
    idx = _make_index()
    # 'apple' matches both Apple Inc. (0000320193) and Apple Inc. (NQ) (same CIK)
    results = idx.search("apple")
    ciks = [r.cik_10 for r in results]
    assert len(ciks) == len(set(ciks)), f"Duplicate CIKs in results: {ciks}"


# ---------------------------------------------------------------------------
# CIK normalisation
# ---------------------------------------------------------------------------


def test_normalise_cik_from_int():
    assert normalise_cik(320193) == "0000320193"


def test_normalise_cik_from_string():
    assert normalise_cik("320193") == "0000320193"


def test_normalise_cik_already_padded():
    assert normalise_cik("0000320193") == "0000320193"


def test_normalise_cik_large_value():
    assert normalise_cik(1234567890) == "1234567890"


# ---------------------------------------------------------------------------
# __len__
# ---------------------------------------------------------------------------


def test_index_len():
    idx = _make_index()
    assert len(idx) == len(_FIXTURE_ENTRIES)
