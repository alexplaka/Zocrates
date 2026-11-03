"""
Unit tests for online discovery endpoints, download staging, dedup re-check, and cleanup.
"""

import os
from pathlib import Path
import time
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
import httpx
import pytest

from zocrates.discovery.models import CandidatePaper
from zocrates.discovery.staging import (
    cleanup_stale_staging_directories,
    download_and_stage_pdf,
    get_staging_root,
)
from zocrates.config import get_auth_token
from zocrates.fast_api_app import app

client = TestClient(app, headers={"Authorization": f"Bearer {get_auth_token()}"})


# ---------------------------------------------------------------------------
# 1. Search Endpoint
# ---------------------------------------------------------------------------


def test_discover_search_endpoint():
    """Verify POST /discover/search queries the discovery engine and returns serialized candidates."""
    mock_candidates = [
        CandidatePaper(
            candidate_id="w12345",
            title="Attention Is All You Need",
            authors=["Vaswani et al."],
            year=2017,
            venue="NeurIPS",
            doi="10.5555/3295222.3295349",
            arxiv_id="1706.03762",
            is_oa=True,
            oa_pdf_url="https://arxiv.org/pdf/1706.03762.pdf",
            citation_count=100000,
        )
    ]

    mock_engine = MagicMock()
    mock_engine.search = AsyncMock(return_value=mock_candidates)

    with patch("zocrates.fast_api_app.get_default_discovery_engine", return_value=mock_engine):
        response = client.post(
            "/discover/search",
            json={
                "query": "transformer attention",
                "limit": 5,
                "year_from": 2017,
                "year_to": 2024,
                "is_oa_only": True,
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert "candidates" in data
    assert len(data["candidates"]) == 1
    cand = data["candidates"][0]
    assert cand["candidate_id"] == "w12345"
    assert cand["title"] == "Attention Is All You Need"
    assert cand["year"] == 2017
    assert cand["is_oa"] is True
    assert cand["oa_pdf_url"] == "https://arxiv.org/pdf/1706.03762.pdf"

    mock_engine.search.assert_awaited_once_with(
        query="transformer attention",
        limit=5,
        year_from=2017,
        year_to=2024,
        is_oa_only=True,
    )


# ---------------------------------------------------------------------------
# 2. Resolve Endpoint
# ---------------------------------------------------------------------------


def test_discover_resolve_endpoint():
    """Verify POST /discover/resolve correctly resolves novel, existing, and unknown papers."""
    mock_novel = CandidatePaper(
        candidate_id="w999",
        title="Novel Exploration of SLMs",
        doi="10.1000/182",
        in_library=False,
    )
    mock_existing = CandidatePaper(
        candidate_id="w888",
        title="Existing Classic Paper",
        doi="10.1000/existing",
        in_library=True,
    )

    mock_engine = MagicMock()

    # Case A: Novel paper
    mock_engine.resolve_identifier = AsyncMock(return_value=(mock_novel, False))
    with patch("zocrates.fast_api_app.get_default_discovery_engine", return_value=mock_engine):
        res_novel = client.post("/discover/resolve", json={"identifier": "10.1000/182"})

    assert res_novel.status_code == 200
    data_novel = res_novel.json()
    assert data_novel["in_library"] is False
    assert data_novel["candidate"]["candidate_id"] == "w999"
    assert data_novel["candidate"]["title"] == "Novel Exploration of SLMs"

    # Case B: Paper already in library
    mock_engine.resolve_identifier = AsyncMock(return_value=(mock_existing, True))
    with patch("zocrates.fast_api_app.get_default_discovery_engine", return_value=mock_engine):
        res_existing = client.post("/discover/resolve", json={"identifier": "10.1000/existing"})

    assert res_existing.status_code == 200
    data_existing = res_existing.json()
    assert data_existing["in_library"] is True
    assert data_existing["candidate"]["candidate_id"] == "w888"

    # Case C: Unresolvable identifier
    mock_engine.resolve_identifier = AsyncMock(return_value=(None, False))
    with patch("zocrates.fast_api_app.get_default_discovery_engine", return_value=mock_engine):
        res_none = client.post("/discover/resolve", json={"identifier": "invalid:identifier"})

    assert res_none.status_code == 200
    data_none = res_none.json()
    assert data_none["candidate"] is None
    assert data_none["in_library"] is False


# ---------------------------------------------------------------------------
# 3. Prepare Batch: Success (Downloads PDF with %PDF- magic bytes, stages file)
# ---------------------------------------------------------------------------


def test_discover_prepare_batch_success(tmp_path, monkeypatch):
    """Verify POST /discover/prepare_batch downloads a valid PDF, stages it on disk, and returns token."""
    monkeypatch.setattr("zocrates.discovery.staging.get_staging_root", lambda: tmp_path / "staging")
    monkeypatch.setattr("zocrates.fast_api_app.get_staging_root", lambda: tmp_path / "staging")

    fake_pdf_bytes = b"%PDF-1.5\n%Header\n4 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"

    def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/valid.pdf":
            return httpx.Response(200, content=fake_pdf_bytes)
        return httpx.Response(404)

    mock_transport = httpx.MockTransport(mock_handler)
    mock_client = httpx.AsyncClient(transport=mock_transport)

    candidate_payload = {
        "candidate_id": "cand_valid",
        "title": "A Valid Paper",
        "authors": ["Alice Smith"],
        "year": 2024,
        "oa_pdf_url": "https://example.com/valid.pdf",
        "is_oa": True,
    }

    # Patch deduplicator so paper is novel, and mock httpx.AsyncClient
    with patch("zocrates.fast_api_app._get_live_zotero_db", return_value=None), \
         patch("httpx.AsyncClient", return_value=mock_client):
        response = client.post(
            "/discover/prepare_batch",
            json={"candidates": [candidate_payload]},
        )

    assert response.status_code == 200
    data = response.json()
    assert "batch_token" in data
    assert data["batch_token"]
    assert data["skipped_duplicates"] == []
    assert len(data["staged_items"]) == 1

    item = data["staged_items"][0]
    assert item["candidate_id"] == "cand_valid"
    staged_path = Path(item["staged_path"])
    assert staged_path.exists()
    assert staged_path.is_file()
    assert staged_path.read_bytes() == fake_pdf_bytes
    assert staged_path.read_bytes().startswith(b"%PDF-")


# ---------------------------------------------------------------------------
# 4. Prepare Batch: SSRF Protection (Rejects non-https URLs)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_prepare_batch_ssrf_protection(tmp_path):
    """Verify download_and_stage_pdf rejects non-https URLs immediately without making network calls."""
    staging_dir = tmp_path / "ssrf_test"

    insecure_urls = [
        "http://example.com/paper.pdf",
        "file:///etc/passwd",
        "ftp://mirror.edu/paper.pdf",
        "gopher://ancient.net/doc",
        "javascript:alert(1)",
        "",
    ]

    for bad_url in insecure_urls:
        cand = CandidatePaper(
            candidate_id="cand_bad",
            title="Insecure Link",
            oa_pdf_url=bad_url,
        )
        res = await download_and_stage_pdf(cand, staging_dir)
        assert res is None, f"Expected rejection for URL: {bad_url}"

    # Also verify via POST /discover/prepare_batch
    with patch("zocrates.fast_api_app._get_live_zotero_db", return_value=None):
        res_endpoint = client.post(
            "/discover/prepare_batch",
            json={"candidates": [{"candidate_id": "cand_ssrf", "title": "SSRF", "oa_pdf_url": "http://evil.com/test.pdf"}]},
        )
    assert res_endpoint.status_code == 200
    data = res_endpoint.json()
    assert len(data["staged_items"]) == 1
    assert data["staged_items"][0]["staged_path"] == ""


@pytest.mark.asyncio
async def test_discover_prepare_batch_ssrf_redirect_to_http_rejected(tmp_path):
    """Verify download_and_stage_pdf rejects an https URL that attempts to redirect to http."""
    staging_dir = tmp_path / "ssrf_redirect_test"

    def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.scheme == "https" and request.url.path == "/redirect":
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data"})
        return httpx.Response(200, content=b"%PDF-1.4\nfake")

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    cand = CandidatePaper(
        candidate_id="cand_ssrf_redir",
        title="SSRF Redirect Attack",
        oa_pdf_url="https://legit-looking.com/redirect",
    )
    res = await download_and_stage_pdf(cand, staging_dir, client=mock_client)
    assert res is None, "Expected rejection when redirecting to http://"


def test_discover_prepare_batch_max_candidates_cap():
    """Verify POST /discover/prepare_batch rejects payloads with > 50 candidates."""
    candidates = [{"candidate_id": f"c_{i}", "title": f"Paper {i}"} for i in range(51)]
    with patch("zocrates.fast_api_app._get_live_zotero_db", return_value=None):
        res = client.post("/discover/prepare_batch", json={"candidates": candidates})
    assert res.status_code == 400
    assert "exceeds maximum of 50" in res.json()["detail"]


# ---------------------------------------------------------------------------
# 5. Prepare Batch: Magic Bytes Validation (Rejects HTML/fake PDFs)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_prepare_batch_magic_bytes_validation(tmp_path):
    """Verify download_and_stage_pdf rejects non-PDF responses (e.g. HTML 404/Cloudflare pages)."""
    staging_dir = tmp_path / "magic_test"
    html_content = b"<!DOCTYPE html><html><body><h1>403 Forbidden</h1></body></html>"

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=html_content)

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    cand = CandidatePaper(
        candidate_id="cand_html",
        title="Fake PDF HTML Page",
        oa_pdf_url="https://publisher.com/article.html",
    )

    res = await download_and_stage_pdf(cand, staging_dir, client=mock_client)
    assert res is None
    # Ensure no corrupt file was retained on disk
    assert not (staging_dir / "cand_html.pdf").exists()


# ---------------------------------------------------------------------------
# 6. Prepare Batch: Byte Cap (Aborts download exceeding byte limit)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_prepare_batch_byte_cap(tmp_path, monkeypatch):
    """Verify download_and_stage_pdf aborts and deletes destination when byte cap is exceeded."""
    staging_dir = tmp_path / "cap_test"
    # Temporarily set MAX_PDF_BYTES to 100 bytes for fast deterministic testing
    monkeypatch.setattr("zocrates.discovery.staging.MAX_PDF_BYTES", 100)

    # 200 bytes of data starting with valid PDF header
    oversized_data = b"%PDF-1.4\n" + b"X" * 200

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=oversized_data)

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    cand = CandidatePaper(
        candidate_id="cand_huge",
        title="Oversized Document",
        oa_pdf_url="https://example.com/huge.pdf",
    )

    res = await download_and_stage_pdf(cand, staging_dir, client=mock_client)
    assert res is None
    # Verify file was cleaned up and not left partially on disk
    assert not (staging_dir / "cand_huge.pdf").exists()


# ---------------------------------------------------------------------------
# 7. Prepare Batch: Dedup Re-check (Skips papers already added to Zotero)
# ---------------------------------------------------------------------------


def test_discover_prepare_batch_dedup_recheck(tmp_path, monkeypatch):
    """Verify POST /discover/prepare_batch re-checks live library and skips papers already present."""
    monkeypatch.setattr("zocrates.discovery.staging.get_staging_root", lambda: tmp_path / "staging")
    monkeypatch.setattr("zocrates.fast_api_app.get_staging_root", lambda: tmp_path / "staging")

    mock_db = MagicMock()
    mock_db.get_all_dois.return_value = ["10.1000/already_here"]
    mock_db.get_all_arxiv_ids.return_value = []
    mock_db.get_all_dedup_records.return_value = []

    candidates = [
        {
            "candidate_id": "cand_dup",
            "title": "Already in Library Paper",
            "doi": "10.1000/already_here",
            "oa_pdf_url": "https://example.com/dup.pdf",
        },
        {
            "candidate_id": "cand_fresh",
            "title": "Fresh New Paper",
            "doi": "10.1000/fresh",
            "oa_pdf_url": None,
        },
    ]

    with patch("zocrates.fast_api_app._get_live_zotero_db", return_value=mock_db):
        response = client.post(
            "/discover/prepare_batch",
            json={"candidates": candidates},
        )

    assert response.status_code == 200
    data = response.json()
    assert "cand_dup" in data["skipped_duplicates"]
    assert len(data["staged_items"]) == 1
    assert data["staged_items"][0]["candidate_id"] == "cand_fresh"
    assert data["staged_items"][0]["metadata"]["title"] == "Fresh New Paper"


# ---------------------------------------------------------------------------
# 8. Prepare Batch: Metadata-Only Fallback (Missing or failed PDF download)
# ---------------------------------------------------------------------------


def test_discover_prepare_batch_metadata_only_fallback(tmp_path, monkeypatch):
    """Verify candidate without OA PDF or with failed download stages gracefully with staged_path=''."""
    monkeypatch.setattr("zocrates.discovery.staging.get_staging_root", lambda: tmp_path / "staging")
    monkeypatch.setattr("zocrates.fast_api_app.get_staging_root", lambda: tmp_path / "staging")

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"Server Error")

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    candidates = [
        {
            "candidate_id": "cand_no_url",
            "title": "No URL Available",
            "oa_pdf_url": None,
            "is_oa": False,
        },
        {
            "candidate_id": "cand_failing_url",
            "title": "Failing 500 URL",
            "oa_pdf_url": "https://example.com/broken.pdf",
            "is_oa": True,
        },
    ]

    with patch("zocrates.fast_api_app._get_live_zotero_db", return_value=None), \
         patch("httpx.AsyncClient", return_value=mock_client):
        response = client.post(
            "/discover/prepare_batch",
            json={"candidates": candidates},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["skipped_duplicates"] == []
    assert len(data["staged_items"]) == 2

    # Both items should be staged with empty staged_path and complete metadata
    item1, item2 = data["staged_items"]
    assert item1["candidate_id"] == "cand_no_url"
    assert item1["staged_path"] == ""
    assert item1["metadata"]["title"] == "No URL Available"

    assert item2["candidate_id"] == "cand_failing_url"
    assert item2["staged_path"] == ""
    assert item2["metadata"]["title"] == "Failing 500 URL"


# ---------------------------------------------------------------------------
# 9. Cleanup Batch: Valid cleanup & Path Traversal Rejection
# ---------------------------------------------------------------------------


def test_discover_cleanup_batch(tmp_path, monkeypatch):
    """Verify POST /discover/cleanup_batch deletes batch dir and rejects path traversal."""
    staging_root = tmp_path / "staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("zocrates.discovery.staging.get_staging_root", lambda: staging_root)
    monkeypatch.setattr("zocrates.fast_api_app.get_staging_root", lambda: staging_root)

    # 1. Valid batch directory cleanup
    batch_dir = staging_root / "batch_token_xyz"
    batch_dir.mkdir()
    test_file = batch_dir / "paper.pdf"
    test_file.write_bytes(b"%PDF-test")

    assert batch_dir.exists()
    res = client.post("/discover/cleanup_batch", json={"batch_token": "batch_token_xyz"})
    assert res.status_code == 200
    assert res.json() == {"status": "cleaned"}
    assert not batch_dir.exists()

    # 2. Path traversal rejection
    bad_tokens = [
        "../../etc",
        "/etc/passwd",
        "../staging",
        "..",
        "dir/slash",
        "",
        "   ",
        "token with spaces",
    ]

    for bad_token in bad_tokens:
        res_bad = client.post("/discover/cleanup_batch", json={"batch_token": bad_token})
        assert res_bad.status_code == 400, f"Expected 400 for token: {bad_token}"


# ---------------------------------------------------------------------------
# 10. Stale Staging Directories Sweep
# ---------------------------------------------------------------------------


def test_cleanup_stale_staging_directories(tmp_path, monkeypatch):
    """Verify cleanup_stale_staging_directories sweeps dirs older than TTL while preserving fresh ones."""
    staging_root = tmp_path / "staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("zocrates.discovery.staging.get_staging_root", lambda: staging_root)

    # Dir 1: Old directory (mtime 8000s ago > 7200s)
    old_dir = staging_root / "batch_old"
    old_dir.mkdir()
    old_file = old_dir / "file.pdf"
    old_file.write_bytes(b"data")
    past_mtime = time.time() - 8000
    os.utime(old_dir, (past_mtime, past_mtime))

    # Dir 2: Recent directory (mtime 100s ago <= 7200s)
    recent_dir = staging_root / "batch_recent"
    recent_dir.mkdir()
    recent_file = recent_dir / "file.pdf"
    recent_file.write_bytes(b"data")
    recent_mtime = time.time() - 100
    os.utime(recent_dir, (recent_mtime, recent_mtime))

    # Regular file in root (should not be treated as a batch directory)
    root_file = staging_root / "keepme.txt"
    root_file.write_text("info")

    removed_count = cleanup_stale_staging_directories(max_age_seconds=7200)

    assert removed_count == 1
    assert not old_dir.exists()
    assert recent_dir.exists()
    assert root_file.exists()


# ---------------------------------------------------------------------------
# 11. Landing Page PDF Resolution
# ---------------------------------------------------------------------------


def test_extract_pdf_url_from_html():
    """Verify HTML landing page parser extracts PDF URLs from Google Scholar, link tags, and DSpace rewrites."""
    from zocrates.discovery.staging import extract_pdf_from_link_header, extract_pdf_url_from_html

    base = "https://repo.example.edu/handle/123/456"

    # 1. Google Scholar meta tag
    html_meta = '<html><head><meta name="citation_pdf_url" content="https://repo.example.edu/bitstreams/123/download"></head></html>'
    res1 = extract_pdf_url_from_html(html_meta, base)
    assert res1 == "https://repo.example.edu/bitstreams/123/download"

    # 2. DSpace 7 SPA rewrite
    html_dspace = '<html><head><meta name="citation_pdf_url" content="https://repo.example.edu/bitstreams/b5497b1e-816e-4445-8a4a-757f6c80102b/download"></head></html>'
    res2 = extract_pdf_url_from_html(html_dspace, base)
    assert res2 == "https://repo.example.edu/server/api/core/bitstreams/b5497b1e-816e-4445-8a4a-757f6c80102b/content"

    # 3. Link rel="item" type="application/pdf"
    html_link = '<html><head><link href="/files/paper.pdf" rel="item" type="application/pdf"></head></html>'
    res3 = extract_pdf_url_from_html(html_link, base)
    assert res3 == "https://repo.example.edu/files/paper.pdf"

    # 4. HTTP Link Signposting Header
    link_header = '<https://repo.example.edu/bitstreams/b5497b1e-816e-4445-8a4a-757f6c80102b/download>; rel="item"; type="application/pdf"'
    res4 = extract_pdf_from_link_header(link_header, base)
    assert res4 == "https://repo.example.edu/server/api/core/bitstreams/b5497b1e-816e-4445-8a4a-757f6c80102b/content"

