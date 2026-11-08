"""Unit tests for sentence-level attribution (Option B: Pre-Segmented Numbered Spans)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from zocrates.agents.common.state_keys import RETRIEVED_EVIDENCE
from zocrates.agents.library_search.sentence_attribution import (
    ATTRIBUTION_SYSTEM_PROMPT,
    SENTENCE_ATTRIBUTION_MODEL,
    compute_sentence_attribution,
    format_spans_for_prompt,
    parse_attribution_response,
    persist_sentence_attribution_to_session,
    segment_source_text,
)
from zocrates.fast_api_app import app


class TestSpanSplitter:
    """Tests for segment_source_text."""

    def test_empty_or_whitespace(self):
        assert segment_source_text("") == []
        assert segment_source_text("   \n\t  ") == []

    def test_prose_sentence_splitting_with_abbreviations(self):
        text = (
            "According to Smith et al. the model performs well. "
            "Furthermore, e.g. when N=5, it converges rapidly. "
            "Dr. Watson tested it against baseline vs. control. "
            "J. K. Rowling did not write this paper! "
            "Does it achieve 95% accuracy? Yes, it does."
        )
        spans = segment_source_text(text)
        assert len(spans) == 6
        assert spans[0] == "According to Smith et al. the model performs well."
        assert spans[1] == "Furthermore, e.g. when N=5, it converges rapidly."
        assert spans[2] == "Dr. Watson tested it against baseline vs. control."
        assert spans[3] == "J. K. Rowling did not write this paper!"
        assert spans[4] == "Does it achieve 95% accuracy?"
        assert spans[5] == "Yes, it does."

    def test_headings_and_tables(self):
        text = (
            "### 2. Experimental Results\n\n"
            "Here is the evaluation table:\n\n"
            "| Model | Score | Latency |\n"
            "| --- | --- | --- |\n"
            "| Baseline | 75.2 | 40ms |\n"
            "| Proposed | 94.8 | 12ms |\n\n"
            "The proposed model outperforms all others."
        )
        spans = segment_source_text(text)
        assert len(spans) == 6
        assert spans[0] == "### 2. Experimental Results"
        assert spans[1] == "Here is the evaluation table:"
        assert spans[2] == "| Model | Score | Latency |"
        # Table divider | --- | --- | --- | must NOT be present as a span
        assert spans[3] == "| Baseline | 75.2 | 40ms |"
        assert spans[4] == "| Proposed | 94.8 | 12ms |"
        assert spans[5] == "The proposed model outperforms all others."

    def test_display_math_and_code_blocks(self):
        text = (
            "Consider the objective function:\n\n"
            "$$\n"
            "\\min_{\\theta} \\mathcal{L}(\\theta) + \\lambda ||\\theta||^2\n"
            "$$\n\n"
            "Optimization is performed via SGD."
        )
        spans = segment_source_text(text)
        assert len(spans) == 3
        assert spans[0] == "Consider the objective function:"
        assert "$$" in spans[1]
        assert "\\min" in spans[1]
        assert spans[2] == "Optimization is performed via SGD."

    def test_format_spans_for_prompt(self):
        spans = ["First sentence.", "Second sentence."]
        formatted = format_spans_for_prompt(spans)
        assert formatted == "[1] First sentence.\n[2] Second sentence."


class TestAttributionPrompt:
    """Tests for ATTRIBUTION_SYSTEM_PROMPT structure and rules."""

    def test_prompt_excludes_support_level(self):
        assert "support_level" not in ATTRIBUTION_SYSTEM_PROMPT

    def test_prompt_includes_streamlined_schema_and_rules(self):
        assert "supporting_indices" in ATTRIBUTION_SYSTEM_PROMPT
        assert "unsupported_aspects" in ATTRIBUTION_SYSTEM_PROMPT
        assert "Direct Factual Support Only" in ATTRIBUTION_SYSTEM_PROMPT
        assert "Strictly Minimal Attribution" in ATTRIBUTION_SYSTEM_PROMPT
        assert "Quantitative & Parameter Rigor" in ATTRIBUTION_SYSTEM_PROMPT
        assert "Natural Scholarly Explanation" in ATTRIBUTION_SYSTEM_PROMPT
        assert "When No Sentences Support" in ATTRIBUTION_SYSTEM_PROMPT


class TestAttributionParser:
    """Tests for parse_attribution_response."""

    def test_clean_json(self):
        raw = '{"supporting_indices": [1, 2], "unsupported_aspects": ""}'
        res = parse_attribution_response(raw, total_spans=3)
        assert "support_level" not in res
        assert res["supporting_indices"] == [1, 2]
        assert res["unsupported_aspects"] == ""

    def test_code_fence_stripping(self):
        raw = (
            "```json\n"
            '{\n  "supporting_indices": [1],\n  "unsupported_aspects": "Latency was not reported"\n}\n'
            "```"
        )
        res = parse_attribution_response(raw, total_spans=2)
        assert "support_level" not in res
        assert res["supporting_indices"] == [1]
        assert res["unsupported_aspects"] == "Latency was not reported"

    def test_out_of_bounds_indices_filtered(self):
        raw = '{"supporting_indices": [0, 1, 4, 99], "unsupported_aspects": ""}'
        res = parse_attribution_response(raw, total_spans=3)
        assert "support_level" not in res
        assert res["supporting_indices"] == [1]

    def test_malformed_json_fallback(self):
        raw = "Sorry, I cannot evaluate this."
        res = parse_attribution_response(raw, total_spans=2)
        assert "support_level" not in res
        assert res["supporting_indices"] == []


class TestComputeSentenceAttribution:
    """Tests for compute_sentence_attribution."""

    @pytest.mark.asyncio
    async def test_empty_source_text(self):
        res = await compute_sentence_attribution(claim="Test claim", source_text="")
        assert res["status"] == "ok"
        assert "support_level" not in res
        assert res["supporting_indices"] == []
        assert res["spans"] == []
        assert res["latency_ms"] == 0.0

    @pytest.mark.asyncio
    async def test_successful_evaluation(self):
        mock_choice = MagicMock()
        mock_choice.message.content = '{"supporting_indices": [1], "unsupported_aspects": ""}'
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = mock_resp
            res = await compute_sentence_attribution(
                claim="Model achieves 95% accuracy.",
                source_text="The proposed architecture achieves 95% accuracy on ImageNet.",
                api_key="test-key",
            )
            assert res["status"] == "ok"
            assert "support_level" not in res
            assert res["supporting_indices"] == [1]
            assert res["unsupported_aspects"] == ""
            assert len(res["spans"]) == 1
            assert res["model"] == SENTENCE_ATTRIBUTION_MODEL

    @pytest.mark.asyncio
    async def test_llm_exception_handled(self):
        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = RuntimeError("OpenRouter API error")
            res = await compute_sentence_attribution(
                claim="Claim",
                source_text="Some valid source text.",
                api_key="test-key",
            )
            assert res["status"] == "error"
            assert "support_level" not in res
            assert res["supporting_indices"] == []
            assert "error" in res["unsupported_aspects"].lower()


class TestSessionPersistence:
    """Tests for persist_sentence_attribution_to_session."""

    @pytest.mark.asyncio
    async def test_persists_to_database_session_service(self, tmp_path):
        from google.adk.sessions.database_session_service import DatabaseSessionService

        db_path = tmp_path / "sessions.db"
        service = DatabaseSessionService(db_url=f"sqlite+aiosqlite:///{db_path}")
        await service.prepare_tables()

        initial_evidence = [
            {
                "invocation_id": "inv_1",
                "citation_verification_telemetry": {
                    "assessments": [
                        {
                            "chunk_index": 1,
                            "occurrence": 1,
                            "claim": "Claim to attribute",
                            "source_text": "Passage text",
                        }
                    ]
                },
            }
        ]

        sess = await service.create_session(
            app_name="zocrates",
            user_id="default_user",
            session_id="sess_attr_test",
            state={RETRIEVED_EVIDENCE: initial_evidence},
        )

        attr_data = {
            "status": "ok",
            "supporting_indices": [1],
            "spans": ["Passage text"],
            "unsupported_aspects": "",
            "model": SENTENCE_ATTRIBUTION_MODEL,
            "latency_ms": 115.0,
        }

        ok = await persist_sentence_attribution_to_session(
            session_id="sess_attr_test",
            invocation_id="inv_1",
            chunk_index=1,
            occurrence=1,
            claim="Claim to attribute",
            attribution_data=attr_data,
            session_service=service,
            app_name="zocrates",
        )
        assert ok is True

        # Re-fetch from clean session service instance to verify SQLite disk persistence
        service2 = DatabaseSessionService(db_url=f"sqlite+aiosqlite:///{db_path}")
        reloaded = await service2.get_session(
            app_name="zocrates", user_id="default_user", session_id="sess_attr_test"
        )
        assert reloaded is not None
        persisted_audit = reloaded.state[RETRIEVED_EVIDENCE]
        persisted_attr = persisted_audit[0]["citation_verification_telemetry"]["assessments"][0].get(
            "sentence_attribution"
        )
        assert persisted_attr is not None
        assert "support_level" not in persisted_attr
        assert persisted_attr["supporting_indices"] == [1]


class TestSentenceAttributionEndpoint:
    """Tests for POST /citations/sentence_attribution."""

    def test_unauthenticated_request_rejected(self):
        client = TestClient(app)
        res = client.post(
            "/citations/sentence_attribution",
            json={"claim": "Test", "source_text": "Passage"},
        )
        assert res.status_code == 401

    def test_missing_openrouter_api_key(self, monkeypatch):
        from zocrates.config import get_auth_token

        token = get_auth_token()
        monkeypatch.setattr("zocrates.config.get_secure_api_key", lambda *args, **kwargs: None)

        client = TestClient(app)
        res = client.post(
            "/citations/sentence_attribution",
            json={"claim": "Test", "source_text": "Passage"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res.status_code == 400
        assert "OpenRouter API key is not configured" in res.json()["detail"]

    def test_successful_endpoint_call(self, monkeypatch):
        from zocrates.config import get_auth_token

        token = get_auth_token()
        monkeypatch.setattr("zocrates.config.get_secure_api_key", lambda *args, **kwargs: "mock-or-key")

        mock_choice = MagicMock()
        mock_choice.message.content = (
            '{"supporting_indices": [1], "unsupported_aspects": ""}'
        )
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = mock_resp
            client = TestClient(app)
            res = client.post(
                "/citations/sentence_attribution",
                json={
                    "claim": "Proposed method achieves 94% accuracy.",
                    "source_text": "The proposed method achieves 94% accuracy.",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "ok"
            assert "support_level" not in data
            assert data["supporting_indices"] == [1]
            assert data["model"] == SENTENCE_ATTRIBUTION_MODEL
            assert "spans" in data
            assert len(data["spans"]) == 1
