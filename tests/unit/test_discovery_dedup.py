"""
Unit tests for discovery deduplication and identifier normalization.
"""

from unittest.mock import MagicMock

from zocrates.discovery.dedup import Deduplicator, normalize_arxiv_id, normalize_doi, normalize_title
from zocrates.discovery.models import CandidatePaper


def test_normalize_doi_variants():
    assert normalize_doi("https://doi.org/10.1145/12345") == "10.1145/12345"
    assert normalize_doi("http://doi.org/10.1145/12345") == "10.1145/12345"
    assert normalize_doi("https://dx.doi.org/10.1145/12345/") == "10.1145/12345"
    assert normalize_doi("doi.org/10.1145/12345") == "10.1145/12345"
    assert normalize_doi("doi: 10.1145/ABCD") == "10.1145/abcd"
    assert normalize_doi("DOI:10.1038/s41586-020-2649-2") == "10.1038/s41586-020-2649-2"
    assert normalize_doi("") is None
    assert normalize_doi(None) is None


def test_normalize_arxiv_id_variants():
    assert normalize_arxiv_id("2305.12345") == "2305.12345"
    assert normalize_arxiv_id("2305.12345v2") == "2305.12345"
    assert normalize_arxiv_id("arXiv:2305.12345v1") == "2305.12345"
    assert normalize_arxiv_id("arxiv: 2305.12345") == "2305.12345"
    assert normalize_arxiv_id("https://arxiv.org/abs/2305.12345v3") == "2305.12345"
    assert normalize_arxiv_id("https://arxiv.org/pdf/2305.12345.pdf") == "2305.12345"
    assert normalize_arxiv_id("10.48550/arXiv.2305.12345") == "2305.12345"
    assert normalize_arxiv_id("math/0211159v1") == "math/0211159"
    assert normalize_arxiv_id("arXiv:math.PR/0211159") == "math.pr/0211159"
    assert normalize_arxiv_id("") is None
    assert normalize_arxiv_id(None) is None


def test_normalize_title():
    assert normalize_title("Attention Is All You Need!") == "attention is all you need"
    assert normalize_title("Deep Learning: A Review.") == "deep learning a review"
    assert normalize_title("  Spaces   and \t tabs \n ") == "spaces and tabs"
    assert normalize_title("") == ""
    assert normalize_title(None) == ""


def test_deduplicator_doi_match():
    dedup = Deduplicator(
        existing_dois={"10.1145/12345"},
        existing_arxiv_ids=set(),
        existing_records=[],
    )
    paper_in = CandidatePaper(
        candidate_id="c1",
        title="Some Novel Title",
        doi="https://doi.org/10.1145/12345",
    )
    paper_out = CandidatePaper(
        candidate_id="c2",
        title="Other Title",
        doi="10.1145/99999",
    )
    assert dedup.is_in_library(paper_in) is True
    assert dedup.is_in_library(paper_out) is False


def test_deduplicator_arxiv_match():
    dedup = Deduplicator(
        existing_dois=set(),
        existing_arxiv_ids={"2305.12345"},
        existing_records=[],
    )
    paper_in = CandidatePaper(
        candidate_id="c1",
        title="Some Novel Title",
        arxiv_id="arXiv:2305.12345v2",
    )
    paper_out = CandidatePaper(
        candidate_id="c2",
        title="Other Title",
        arxiv_id="2401.00001",
    )
    assert dedup.is_in_library(paper_in) is True
    assert dedup.is_in_library(paper_out) is False


def test_deduplicator_fuzzy_title_match():
    dedup = Deduplicator(
        existing_dois=set(),
        existing_arxiv_ids=set(),
        existing_records=[
            {
                "title": "Deep Residual Learning for Image Recognition",
                "year": 2016,
                "creators": ["He, Kaiming", "Zhang, Xiangyu"],
            }
        ],
    )
    # Exact title (different case/punctuation)
    paper_exact = CandidatePaper(
        candidate_id="c1",
        title="deep residual learning for image recognition!",
        year=2016,
    )
    assert dedup.is_in_library(paper_exact) is True

    # Very close fuzzy match with matching author
    paper_fuzzy = CandidatePaper(
        candidate_id="c2",
        title="Deep Residual Learning for Image Recognition: Extended",
        authors=["Kaiming He"],
        year=2016,
    )
    # Sequence similarity should match with author
    assert dedup.is_in_library(paper_fuzzy) is True

    # Distinct paper
    paper_diff = CandidatePaper(
        candidate_id="c3",
        title="Generative Adversarial Nets",
        authors=["Ian Goodfellow"],
        year=2014,
    )
    assert dedup.is_in_library(paper_diff) is False


def test_deduplicator_filter_novel_and_intra_batch():
    dedup = Deduplicator(
        existing_dois={"10.1000/182"},
        existing_arxiv_ids={"1706.03762"},
        existing_records=[{"title": "Existing Paper in Library", "year": 2020}],
    )

    candidates = [
        # In library by DOI
        CandidatePaper(candidate_id="1", title="Title 1", doi="10.1000/182"),
        # In library by arXiv ID
        CandidatePaper(candidate_id="2", title="Title 2", arxiv_id="1706.03762v5"),
        # In library by title
        CandidatePaper(candidate_id="3", title="Existing Paper in Library"),
        # Novel paper 1
        CandidatePaper(candidate_id="4", title="Novel Paper A", doi="10.2000/novel-a"),
        # Duplicate of Novel Paper 1 in batch (by DOI)
        CandidatePaper(candidate_id="5", title="Novel Paper A Duplicate", doi="10.2000/novel-a"),
        # Novel paper 2
        CandidatePaper(candidate_id="6", title="Novel Paper B", arxiv_id="2305.99999"),
        # Duplicate of Novel Paper 2 in batch (by arXiv)
        CandidatePaper(candidate_id="7", title="Novel Paper B2", arxiv_id="2305.99999v1"),
        # Novel paper 3
        CandidatePaper(candidate_id="8", title="Novel Paper C"),
        # Duplicate of Novel Paper 3 in batch (by title)
        CandidatePaper(candidate_id="9", title="novel paper c!"),
    ]

    novel = dedup.filter_novel(candidates)
    assert len(novel) == 3
    assert [p.candidate_id for p in novel] == ["4", "6", "8"]


def test_deduplicator_loads_from_zotero_db():
    mock_db = MagicMock()
    mock_db.get_all_dois.return_value = {"10.1145/test"}
    mock_db.get_all_arxiv_ids.return_value = {"2101.00001"}
    mock_db.get_all_dedup_records.return_value = [
        {"title": "Zotero DB Paper", "year": 2021, "creators": ["Smith, John"]}
    ]

    dedup = Deduplicator(zotero_db=mock_db)
    assert "10.1145/test" in dedup.existing_dois
    assert "2101.00001" in dedup.existing_arxiv_ids
    assert len(dedup.existing_records) == 1

    paper = CandidatePaper(candidate_id="p1", title="Zotero DB Paper")
    assert dedup.is_in_library(paper) is True
