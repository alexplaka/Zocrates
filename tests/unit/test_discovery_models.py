"""
Unit tests for discovery data models (CandidatePaper and StagedItem).
"""

from zocrates.discovery.models import CandidatePaper, StagedItem


def test_candidate_paper_creation_and_defaults():
    paper = CandidatePaper(
        candidate_id="W123456789",
        title="Attention Is All You Need",
        authors=["Vaswani, Ashish", "Shazeer, Noam"],
    )
    assert paper.candidate_id == "W123456789"
    assert paper.title == "Attention Is All You Need"
    assert len(paper.authors) == 2
    assert paper.year is None
    assert paper.venue is None
    assert paper.doi is None
    assert paper.arxiv_id is None
    assert paper.openalex_id == ""
    assert paper.abstract is None
    assert paper.tldr is None
    assert paper.is_oa is False
    assert paper.oa_pdf_url is None
    assert paper.citation_count == 0
    assert paper.primary_topic is None
    assert paper.keywords == []
    assert paper.in_library is False


def test_candidate_paper_serialization_roundtrip():
    original = CandidatePaper(
        candidate_id="W987654321",
        title="Deep Residual Learning for Image Recognition",
        authors=["He, Kaiming", "Zhang, Xiangyu", "Ren, Shaoqing", "Sun, Jian"],
        year=2016,
        venue="CVPR",
        doi="10.1109/cvpr.2016.90",
        arxiv_id="1512.03385",
        openalex_id="https://openalex.org/W987654321",
        abstract="Deeper neural networks are more difficult to train.",
        tldr="Presents a residual learning framework to ease training of deep networks.",
        is_oa=True,
        oa_pdf_url="https://arxiv.org/pdf/1512.03385.pdf",
        citation_count=180000,
        primary_topic="Computer Vision",
        keywords=["Residual Learning", "Image Classification", "Deep CNN"],
        in_library=True,
    )

    d = original.to_dict()
    assert d["candidate_id"] == "W987654321"
    assert d["year"] == 2016
    assert d["is_oa"] is True
    assert d["citation_count"] == 180000
    assert d["keywords"] == ["Residual Learning", "Image Classification", "Deep CNN"]
    assert d["in_library"] is True

    reconstructed = CandidatePaper.from_dict(d)
    assert reconstructed.candidate_id == original.candidate_id
    assert reconstructed.title == original.title
    assert reconstructed.authors == original.authors
    assert reconstructed.year == original.year
    assert reconstructed.venue == original.venue
    assert reconstructed.doi == original.doi
    assert reconstructed.arxiv_id == original.arxiv_id
    assert reconstructed.openalex_id == original.openalex_id
    assert reconstructed.abstract == original.abstract
    assert reconstructed.tldr == original.tldr
    assert reconstructed.is_oa == original.is_oa
    assert reconstructed.oa_pdf_url == original.oa_pdf_url
    assert reconstructed.citation_count == original.citation_count
    assert reconstructed.primary_topic == original.primary_topic
    assert reconstructed.keywords == original.keywords
    assert reconstructed.in_library == original.in_library


def test_candidate_paper_from_dict_edge_cases():
    data = {
        "candidate_id": "W1",
        "title": "Edge Case Paper",
        "year": "2023",
        "citation_count": "42",
        "is_oa": 1,
        "keywords": None,
        "authors": None,
    }
    paper = CandidatePaper.from_dict(data)
    assert paper.year == 2023
    assert paper.citation_count == 42
    assert paper.is_oa is True
    assert paper.keywords == []
    assert paper.authors == []


def test_staged_item_serialization_roundtrip():
    item = StagedItem(
        candidate_id="W123",
        staged_path="/tmp/staged_papers/paper_w123.pdf",
        metadata={"title": "Paper Title", "doi": "10.1234/test"},
    )
    d = item.to_dict()
    assert d["candidate_id"] == "W123"
    assert d["staged_path"] == "/tmp/staged_papers/paper_w123.pdf"
    assert d["metadata"]["title"] == "Paper Title"

    reconstructed = StagedItem.from_dict(d)
    assert reconstructed.candidate_id == item.candidate_id
    assert reconstructed.staged_path == item.staged_path
    assert reconstructed.metadata == item.metadata
