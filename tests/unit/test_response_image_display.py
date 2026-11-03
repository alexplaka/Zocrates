"""Unit tests for Response Image Display & Mermaid Diagram Rendering (P1/P2/P3b).

Covers:
  - ``format_retrieved_evidence_str`` emitting ``zocrates-img://`` figure URIs
    with URL-encoded path components and raw-path fallback (P1).
  - ``GET /assets/{item_key}/{asset_path:path}`` byte serving with traversal
    guards, extension allowlist, key validation, and cache headers (P2).
  - Citation resolver image-Markdown masking so bare-number alt tokens are
    never rewritten as citations, including coexistence with code-block
    masking (P3b).
"""

import re
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from zocrates.agents.common.prompts import format_retrieved_evidence_str
from zocrates.agents.library_search.citation_resolver import resolve_citations_and_references
from zocrates.config import get_auth_token
from zocrates.fast_api_app import app


@pytest.fixture
def client():
    return TestClient(app, headers={"Authorization": f"Bearer {get_auth_token()}"})


class TestFormatRetrievedEvidenceFigureTags:
    """Verify <figure_image path="..."> display URIs in the evidence block."""

    def test_figure_chunk_emits_zocrates_img_uri(self) -> None:
        chunks = [
            {
                "title": "Attention Is All You Need",
                "item_key": "ABCD1234",
                "chunk_type": "figure",
                "heading": "Figure 3: Scaled Dot-Product Attention",
                "image_path": "images/figure_3.png",
                "text": "The attention function has three inputs: queries, keys, and values.",
            }
        ]
        rendered = format_retrieved_evidence_str(chunks)
        assert (
            '<figure_image path="zocrates-img://ABCD1234/images/figure_3.png" />'
            in rendered
        )

    def test_figure_chunk_url_encodes_path_component(self) -> None:
        """Spaces (PDF-extracted filenames) must be percent-encoded in the URI."""
        chunks = [
            {
                "title": "Doc",
                "item_key": "ABCD1234",
                "chunk_type": "figure",
                "image_path": "images/my figure.png",
                "text": "A figure.",
            }
        ]
        rendered = format_retrieved_evidence_str(chunks)
        assert (
            '<figure_image path="zocrates-img://ABCD1234/images/my%20figure.png" />'
            in rendered
        )
        # Raw relative path must NOT appear (would break Markdown link syntax)
        assert "images/my figure.png\"" not in rendered

    def test_missing_item_key_falls_back_to_raw_path(self) -> None:
        """Without item_key the formatter cannot build a URI: keep raw path."""
        chunks = [
            {
                "title": "Doc",
                "chunk_type": "figure",
                "image_path": "images/figure_3.png",
                "text": "A figure without document identity.",
            }
        ]
        rendered = format_retrieved_evidence_str(chunks)
        assert (
            '<figure_image path="images/figure_3.png" />' in rendered
        )
        assert "zocrates-img://" not in rendered

    def test_no_image_path_emits_no_figure_tag(self) -> None:
        chunks = [
            {
                "title": "Doc",
                "item_key": "ABCD1234",
                "chunk_type": "text",
                "text": "Plain narrative chunk without an image asset.",
            }
        ]
        rendered = format_retrieved_evidence_str(chunks)
        assert "figure_image" not in rendered

    def test_empty_evidence_block_preserved(self) -> None:
        assert format_retrieved_evidence_str(None) == (
            "<retrieved_evidence>\nNo evidence retrieved.\n</retrieved_evidence>"
        )


@pytest.fixture
def cache_fixture(tmp_path):
    """Build a minimal bundle tree under a fake syncer cache dir."""
    cache_dir = tmp_path / "cache"
    bundle_dir = cache_dir / "ABCD1234"
    images_dir = bundle_dir / "images"
    images_dir.mkdir(parents=True)

    png_bytes = b"\x89PNG\r\n\x1a\nfakepngdata"
    (images_dir / "figure_3.png").write_bytes(png_bytes)
    (images_dir / "my figure.png").write_bytes(png_bytes)

    mock_syncer = MagicMock()
    mock_syncer.processor._cache_dir = cache_dir
    return mock_syncer, png_bytes


class TestAssetsEndpoint:
    """Verify GET /assets/{item_key}/{asset_path} security and byte serving."""

    def test_valid_asset_served_with_media_type_and_cache(self, client, cache_fixture) -> None:
        mock_syncer, png_bytes = cache_fixture
        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.get("/assets/ABCD1234/images/figure_3.png")
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("image/png")
        assert res.headers["cache-control"] == "max-age=3600"
        assert res.content == png_bytes

    def test_url_encoded_space_in_segment_resolves(self, client, cache_fixture) -> None:
        mock_syncer, png_bytes = cache_fixture
        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.get("/assets/ABCD1234/images/my%20figure.png")
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("image/png")
        assert res.content == png_bytes

    def test_traversal_attempt_returns_404(self, client, cache_fixture) -> None:
        mock_syncer, _ = cache_fixture
        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            # %2E%2E%2F decodes to ../ after route matching, so the guard inside
            # the endpoint must reject it rather than the router 404ing.
            res = client.get("/assets/ABCD1234/%2E%2E%2F%2E%2E%2Fsecret.png")
        assert res.status_code == 404

    def test_disallowed_extension_returns_404(self, client, cache_fixture) -> None:
        mock_syncer, _ = cache_fixture
        (mock_syncer.processor._cache_dir / "ABCD1234" / "images" / "note.txt").write_text("nope")
        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.get("/assets/ABCD1234/images/note.txt")
        assert res.status_code == 404

    def test_invalid_item_key_returns_404(self, client, cache_fixture) -> None:
        mock_syncer, _ = cache_fixture
        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.get("/assets/BAD%20KEY/images/figure_3.png")
        assert res.status_code == 404

    def test_unknown_item_key_returns_404(self, client, cache_fixture) -> None:
        mock_syncer, _ = cache_fixture
        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.get("/assets/ZZZZZZZZ/images/figure_3.png")
        assert res.status_code == 404

    def test_unregistered_backend_returns_404(self, client) -> None:
        with patch("zocrates.fast_api_app.get_syncer", side_effect=RuntimeError("Not registered")):
            res = client.get("/assets/ABCD1234/images/figure_3.png")
        assert res.status_code == 404

    def test_missing_asset_file_returns_404(self, client, cache_fixture) -> None:
        mock_syncer, _ = cache_fixture
        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            res = client.get("/assets/ABCD1234/images/absent.png")
        assert res.status_code == 404


@pytest.fixture
def sample_ranked_chunks():
    return [
        {
            "chunk_id": "JONSSON::1",
            "item_key": "99R6SJ7T",
            "attachment_key": "ATT99R6",
            "title": "Somatic mutation rates in monozygotic twins",
            "authors": ["Jonsson, Hakon", "Stefansson, Kari"],
            "year": "2021",
            "heading": "Methods",
            "text": "The somatic mutation rate was estimated using whole-genome sequencing of 387 pairs of monozygotic twins.",
            "chunk_type": "text",
        },
        {
            "chunk_id": "VASWANI::1",
            "item_key": "2RE8APTV",
            "attachment_key": "ATT2RE8",
            "title": "Attention Is All You Need",
            "authors": ["Vaswani", "Shazeer", "Parmar"],
            "year": "2017",
            "heading": "3.2 Attention",
            "text": "An attention function can be described as mapping a query and a set of key-value pairs to an output.",
            "chunk_type": "text",
        },
    ]


class TestCitationResolverImageMasking:
    """Bare-number image alt tokens must never be rewritten as citations."""

    def test_image_markdown_with_bare_number_alt_passes_through(self, sample_ranked_chunks) -> None:
        text = (
            "The architecture diagram ![3](zocrates-img://ABCD1234/images/f3.png) "
            "shows the pipeline described in [1]."
        )
        res = resolve_citations_and_references(text, sample_ranked_chunks)

        # Image Markdown is byte-for-byte preserved (alt token not cited)
        assert "![3](zocrates-img://ABCD1234/images/f3.png)" in res["enriched_text"]
        # Surrounding prose citation still resolves normally
        citation_indices = [c["index"] for c in res["citations"]]
        assert citation_indices == [1]
        assert "[1]" in res["enriched_text"]

    def test_image_markdown_coexists_with_code_block_masking(self, sample_ranked_chunks) -> None:
        text = (
            "Claim [1] has details.\n"
            "```mermaid\n"
            "graph TD\n"
            "    A[3] --> B\n"
            "```\n"
            "See ![4](zocrates-img://ABCD1234/images/f4.png) for the plot."
        )
        res = resolve_citations_and_references(text, sample_ranked_chunks)

        # Code fence contents stay masked (no phantom citations)
        assert "A[3]" in res["enriched_text"]
        assert "```mermaid" in res["enriched_text"]
        # Image Markdown outside the fence is preserved verbatim
        assert "![4](zocrates-img://ABCD1234/images/f4.png)" in res["enriched_text"]
        # In-prose [1] is still resolved
        assert "[1]" in res["enriched_text"]
        citation_indices = [c["index"] for c in res["citations"]]
        assert citation_indices == [1]

    def test_out_of_bounds_alt_number_not_collapsed(self, sample_ranked_chunks) -> None:
        """[99] alt token must not be stripped as an out-of-bounds citation."""
        text = "![99](zocrates-img://ABCD1234/images/f99.png) is the outlier plot."
        res = resolve_citations_and_references(text, sample_ranked_chunks)

        assert "![99](zocrates-img://ABCD1234/images/f99.png)" in res["enriched_text"]
        assert len(res["citations"]) == 0

    def test_image_regex_guard_defined(self) -> None:
        """The masking regex is a module-level constant, not recompiled per call."""
        from zocrates.agents.library_search import citation_resolver

        assert citation_resolver._MD_IMAGE_RE is not None
        assert re.search(citation_resolver._MD_IMAGE_RE, "![x](zocrates-img://K/img.png)")

class TestReadDocumentEvidenceResolvedKey:
    """Regression: read_document evidence chunks must carry the RESOLVED bundle key.

    When the search subagent addresses a paper by its parent/chip key but the
    bundle lives under an attachment key, the synthetic evidence chunk must
    record the resolved key — the /assets endpoint serves from
    ``cache_dir/<item_key>``, so a requested-but-unresolved key 404s the image.
    """

    def _make_ctx(self):
        from zocrates.agents.common.state_keys import (
            TEMP_RETRIEVED_EVIDENCE,
            TEMP_SEEN_ASSETS,
            TEMP_SEEN_CHUNK_IDS,
        )

        ctx = MagicMock()
        ctx.state = {
            TEMP_SEEN_ASSETS: [],
            TEMP_SEEN_CHUNK_IDS: [],
            TEMP_RETRIEVED_EVIDENCE: [],
        }
        return ctx

    def test_evidence_chunk_uses_resolved_bundle_key(self) -> None:
        from zocrates.agents.common.state_keys import TEMP_RETRIEVED_EVIDENCE
        from zocrates.agents.common.tools import read_document

        ctx = self._make_ctx()
        fake_read_result = {
            "status": "success",
            "item_key": "ATTACH1",  # resolved attachment bundle key
            "title": "Some Paper",
            "assets": [
                {
                    "asset_id": "f2",
                    "asset_type": "figure",
                    "heading": "Figure 2: Model",
                    "content": "Caption text.",
                    "word_count": 2,
                    "path": "images/2fa4a1bf_img.jpg",
                    "structured_content": None,
                    "page": 1,
                    "total_pages": 1,
                    "has_more": False,
                }
            ],
            "intersecting_chunk_ids": [],
        }

        with patch(
            "zocrates.agents.common.tools.read_document_core", return_value=fake_read_result
        ) as mock_core:
            res = read_document("PARENT9", asset_id="f2", tool_context=ctx)

        # The core was called with the requested (parent) key...
        assert mock_core.call_args.kwargs["item_key"] == "PARENT9"
        assert res["status"] == "success"
        # ...but the synthetic evidence chunk records the RESOLVED bundle key.
        chunks = ctx.state[TEMP_RETRIEVED_EVIDENCE]
        assert len(chunks) == 1
        assert chunks[0]["item_key"] == "ATTACH1"
        assert chunks[0]["chunk_id"].startswith("ATTACH1:")
        assert chunks[0]["image_path"] == "images/2fa4a1bf_img.jpg"
