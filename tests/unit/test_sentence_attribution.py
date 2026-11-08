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

    def test_think_tag_stripping(self):
        raw = (
            "<think>Analyzing sentences against claim assertions...</think>\n"
            "```json\n"
            '{\n  "supporting_indices": [1],\n  "unsupported_aspects": ""\n}\n'
            "```"
        )
        res = parse_attribution_response(raw, total_spans=2)
        assert res["supporting_indices"] == [1]
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
        assert res["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        assert res["cost_usd"] == 0.0

    @pytest.mark.asyncio
    async def test_successful_evaluation(self):
        from zocrates.config import get_sentence_attribution_model

        mock_choice = MagicMock()
        mock_choice.message.content = '{"supporting_indices": [1], "unsupported_aspects": ""}'
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_resp.usage = MagicMock(prompt_tokens=120, completion_tokens=30, total_tokens=150)

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
            assert res["model"] == get_sentence_attribution_model()
            assert res["usage"]["prompt_tokens"] == 120
            assert res["usage"]["completion_tokens"] == 30
            assert res["usage"]["total_tokens"] == 150
            assert isinstance(res["cost_usd"], float)

    @pytest.mark.asyncio
    async def test_returns_usage_and_calculated_cost(self):
        mock_choice = MagicMock()
        mock_choice.message.content = '{"supporting_indices": [1], "unsupported_aspects": ""}'
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_resp.usage = MagicMock(prompt_tokens=250, completion_tokens=50, total_tokens=300)

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm, \
             patch("zocrates.utils.cost_calculator.calculate_event_cost", return_value=0.000123) as mock_cost:
            mock_llm.return_value = mock_resp
            res = await compute_sentence_attribution(
                claim="Model achieves 95% accuracy.",
                source_text="The proposed architecture achieves 95% accuracy on ImageNet.",
                api_key="test-key",
                model="test/custom-model",
            )
            assert res["status"] == "ok"
            assert res["usage"] == {"prompt_tokens": 250, "completion_tokens": 50, "total_tokens": 300}
            assert res["cost_usd"] == 0.000123
            mock_cost.assert_called_once_with(
                "test/custom-model",
                prompt_tokens=250,
                candidates_tokens=50,
                zotero_data_dir=None,
            )

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
            assert res["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            assert res["cost_usd"] == 0.0


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
            assert "spans" in data
            assert len(data["spans"]) == 1

    def test_local_model_does_not_require_api_key(self, monkeypatch):
        from zocrates.config import get_auth_token

        token = get_auth_token()
        monkeypatch.setattr("zocrates.config.get_secure_api_key", lambda *args, **kwargs: None)

        mock_choice = MagicMock()
        mock_choice.message.content = '{"supporting_indices": [1], "unsupported_aspects": ""}'
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]

        with patch("zocrates.config.load_zocrates_config", return_value={"sentence_attribution_model": "ollama/llama3", "llm_provider": "ollama"}), \
             patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = mock_resp
            client = TestClient(app)
            res = client.post(
                "/citations/sentence_attribution",
                json={"claim": "Test", "source_text": "Passage text"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "ok"
            assert data["model"] == "ollama_chat/llama3"

    @pytest.mark.parametrize(
        ("provider", "model_conf", "expected_model"),
        [
            ("vllm", "meta-llama/Llama-3-8B", "openai/meta-llama/Llama-3-8B"),
            ("openai_compatible", "local-model", "openai/local-model"),
        ],
    )
    def test_local_provider_or_prefix_does_not_require_api_key(
        self, monkeypatch, provider, model_conf, expected_model
    ):
        from zocrates.config import get_auth_token

        token = get_auth_token()
        monkeypatch.setattr("zocrates.config.get_secure_api_key", lambda *args, **kwargs: None)

        mock_choice = MagicMock()
        mock_choice.message.content = '{"supporting_indices": [1], "unsupported_aspects": ""}'
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]

        with patch(
            "zocrates.config.load_zocrates_config",
            return_value={"sentence_attribution_model": model_conf, "llm_provider": provider},
        ), patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = mock_resp
            client = TestClient(app)
            res = client.post(
                "/citations/sentence_attribution",
                json={"claim": "Test", "source_text": "Passage text"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "ok"
            assert data["model"] == expected_model


class TestSentenceAttributionModelConfig:
    """Tests for get_sentence_attribution_model and get_coordinator_model."""

    def test_inherits_coordinator_model_when_empty_or_none(self):
        from zocrates.config import get_sentence_attribution_model

        # 1. When coordinator_agent.model is active
        mock_coord = MagicMock()
        mock_coord.model = "openrouter/anthropic/claude-3.5-sonnet"
        with patch("zocrates.agents.app.coordinator_agent", mock_coord):
            cfg = {"sentence_attribution_model": "", "llm_provider": "openrouter_llm"}
            assert get_sentence_attribution_model(cfg) == "openrouter/anthropic/claude-3.5-sonnet"

            cfg_none = {"sentence_attribution_model": None, "llm_provider": "openrouter_llm"}
            assert get_sentence_attribution_model(cfg_none) == "openrouter/anthropic/claude-3.5-sonnet"

        # 2. When coordinator_agent.model is not set, falls back to override_agent_models
        with patch("zocrates.agents.app.coordinator_agent", None):
            cfg_override = {
                "sentence_attribution_model": "",
                "override_agent_models": {"coordinator": {"model": "meta-llama/llama-3-70b"}},
                "llm_provider": "openrouter_llm",
            }
            assert get_sentence_attribution_model(cfg_override) == "openrouter/meta-llama/llama-3-70b"

            # 3. Falls back to llm_model
            cfg_llm = {
                "sentence_attribution_model": "",
                "llm_model": "deepseek/deepseek-chat",
                "llm_provider": "openrouter_llm",
            }
            assert get_sentence_attribution_model(cfg_llm) == "openrouter/deepseek/deepseek-chat"

    def test_uses_explicit_override_when_configured(self):
        from zocrates.config import get_sentence_attribution_model

        cfg = {
            "sentence_attribution_model": "openrouter/nvidia/nemotron-3.5-lightning:free",
            "llm_provider": "openrouter_llm",
        }
        assert get_sentence_attribution_model(cfg) == "openrouter/nvidia/nemotron-3.5-lightning:free"

        # Provider slug resolution
        cfg_slug = {
            "sentence_attribution_model": "nvidia/nemotron-3.5-lightning:free",
            "llm_provider": "openrouter_llm",
        }
        assert get_sentence_attribution_model(cfg_slug) == "openrouter/nvidia/nemotron-3.5-lightning:free"

        cfg_ollama = {
            "sentence_attribution_model": "llama3.2:3b",
            "llm_provider": "ollama",
        }
        assert get_sentence_attribution_model(cfg_ollama) == "ollama_chat/llama3.2:3b"

    @pytest.mark.asyncio
    async def test_compute_sentence_attribution_passes_model_and_options(self):
        mock_choice = MagicMock()
        mock_choice.message.content = '{"supporting_indices": [1], "unsupported_aspects": ""}'
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = mock_resp
            res = await compute_sentence_attribution(
                claim="Model achieves 95% accuracy.",
                source_text="The model achieves 95% accuracy.",
                api_key="sk-test",
                model="openrouter/custom/model",
                base_url="http://localhost:11434",
            )
            assert res["status"] == "ok"
            assert res["model"] == "openrouter/custom/model"
            mock_llm.assert_called_once()
            call_kwargs = mock_llm.call_args.kwargs
            assert call_kwargs["model"] == "openrouter/custom/model"
            assert call_kwargs["max_tokens"] == 600
            assert call_kwargs["api_key"] == "sk-test"
            assert call_kwargs["api_base"] == "http://localhost:11434"
            assert call_kwargs["extra_body"] == {"reasoning": {"effort": "none"}}
            assert call_kwargs["extra_headers"] == {"X-OpenRouter-Title": "Zocrates"}

        # Non-openrouter model should not include extra_body/extra_headers
        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = mock_resp
            res = await compute_sentence_attribution(
                claim="Model achieves 95% accuracy.",
                source_text="The model achieves 95% accuracy.",
                model="ollama_chat/llama3.2:3b",
                base_url="http://localhost:11434",
            )
            assert res["model"] == "ollama_chat/llama3.2:3b"
            call_kwargs = mock_llm.call_args.kwargs
            assert call_kwargs["model"] == "ollama_chat/llama3.2:3b"
            assert "extra_body" not in call_kwargs
            assert "extra_headers" not in call_kwargs


class TestSessionFormattingSentenceAttribution:
    """Tests for session_formatting.py sentence attribution cost and token aggregation."""

    def test_aggregates_sentence_attribution_cost_and_tokens(self):
        from zocrates.utils.session_formatting import format_session_for_ui

        user_part = MagicMock(thought=False, text="Explain finding", function_call=None, function_response=None)
        evt_user = MagicMock(
            author="user",
            timestamp=100.0,
            content=MagicMock(parts=[user_part]),
            actions=None,
            usage_metadata=None,
            invocation_id="inv_1",
        )

        asst_part = MagicMock(thought=False, text="Here is the explanation.", function_call=None, function_response=None)
        evt_asst = MagicMock(
            author="library_synthesizer_agent",
            model_version="google/gemini-2.5-flash",
            timestamp=105.0,
            content=MagicMock(parts=[asst_part]),
            actions=None,
            usage_metadata={
                "prompt_token_count": 1000,
                "candidates_token_count": 200,
                "total_token_count": 1200,
            },
            invocation_id="inv_1",
            custom_metadata={
                "citation_verification": {
                    "assessments": [
                        {
                            "claim": "Claim 1",
                            "sentence_attribution": {
                                "status": "ok",
                                "cost_usd": 0.000150,
                                "usage": {
                                    "prompt_tokens": 120,
                                    "completion_tokens": 30,
                                    "total_tokens": 150,
                                },
                            },
                        },
                        {
                            "claim": "Claim 2",
                            "sentence_attribution": {
                                "status": "ok",
                                "cost_usd": 0.000080,
                                "usage": {
                                    "prompt_tokens": 90,
                                    "completion_tokens": 20,
                                    "total_tokens": 110,
                                },
                            },
                        },
                    ]
                }
            },
        )

        session = MagicMock()
        session.id = "sess_attr_test"
        session.state = {"session_title": "Attribution Test"}
        session.events = [evt_user, evt_asst]

        with patch("zocrates.utils.session_formatting.aggregate_turn_cost", return_value=(0.005000, {"library_synthesizer": "google/gemini-2.5-flash"})):
            ui_data = format_session_for_ui(session)

        # Base turn cost = 0.005, Attribution cost = 0.00015 + 0.00008 = 0.00023 -> Turn cost = 0.005230
        asst_msg = ui_data["messages"][-1]
        assert asst_msg["turn_cost_usd"] == 0.005230
        assert ui_data["total_cost_usd"] == 0.005230

        # Base tokens = 1200, Attribution tokens = 150 + 110 = 260 -> Total tokens = 1460
        assert asst_msg["usage"]["total_tokens"] == 1460
        assert asst_msg["usage"]["prompt_tokens"] == 1000 + (120 + 90)
        assert asst_msg["usage"]["candidates_tokens"] == 200 + (30 + 20)
        assert ui_data["total_tokens"] == 1460

    def test_ignores_non_ok_or_missing_attribution(self):
        from zocrates.utils.session_formatting import format_session_for_ui

        user_part = MagicMock(thought=False, text="Query", function_call=None, function_response=None)
        evt_user = MagicMock(
            author="user",
            timestamp=100.0,
            content=MagicMock(parts=[user_part]),
            actions=None,
            usage_metadata=None,
            invocation_id="inv_1",
        )

        asst_part = MagicMock(thought=False, text="Response", function_call=None, function_response=None)
        evt_asst = MagicMock(
            author="synthesizer",
            model_version="google/gemini-2.5-flash",
            timestamp=105.0,
            content=MagicMock(parts=[asst_part]),
            actions=None,
            usage_metadata={
                "prompt_token_count": 500,
                "candidates_token_count": 100,
                "total_token_count": 600,
            },
            invocation_id="inv_1",
            custom_metadata={
                "citation_verification": {
                    "assessments": [
                        {
                            "claim": "Claim error",
                            "sentence_attribution": {
                                "status": "error",
                                "cost_usd": 0.0005,
                                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
                            },
                        },
                        {
                            "claim": "Claim none",
                            "sentence_attribution": None,
                        },
                    ]
                }
            },
        )

        session = MagicMock(id="sess_err", state={}, events=[evt_user, evt_asst])
        with patch("zocrates.utils.session_formatting.aggregate_turn_cost", return_value=(0.001000, {})):
            ui_data = format_session_for_ui(session)

        asst_msg = ui_data["messages"][-1]
        assert asst_msg["turn_cost_usd"] == 0.001000
        assert ui_data["total_cost_usd"] == 0.001000
        assert asst_msg["usage"]["total_tokens"] == 600
        assert ui_data["total_tokens"] == 600

    def test_initializes_usage_when_originally_none(self):
        from zocrates.utils.session_formatting import format_session_for_ui

        user_part = MagicMock(thought=False, text="Query", function_call=None, function_response=None)
        evt_user = MagicMock(
            author="user",
            timestamp=100.0,
            content=MagicMock(parts=[user_part]),
            actions=None,
            usage_metadata=None,
            invocation_id="inv_1",
        )

        asst_part = MagicMock(thought=False, text="Response", function_call=None, function_response=None)
        evt_asst = MagicMock(
            author="synthesizer",
            model_version="test-model",
            timestamp=105.0,
            content=MagicMock(parts=[asst_part]),
            actions=None,
            usage_metadata=None,  # No event usage
            invocation_id="inv_1",
            custom_metadata={
                "citation_verification": {
                    "assessments": [
                        {
                            "claim": "Claim 1",
                            "sentence_attribution": {
                                "status": "ok",
                                "cost_usd": 0.000045,
                                "usage": {
                                    "prompt_tokens": 80,
                                    "completion_tokens": 15,
                                    "total_tokens": 95,
                                },
                            },
                        }
                    ]
                }
            },
        )

        session = MagicMock(id="sess_none_usage", state={}, events=[evt_user, evt_asst])
        with patch("zocrates.utils.session_formatting.aggregate_turn_cost", return_value=(0.0, {})):
            ui_data = format_session_for_ui(session)

        asst_msg = ui_data["messages"][-1]
        assert asst_msg["turn_cost_usd"] == 0.000045
        assert ui_data["total_cost_usd"] == 0.000045
        assert asst_msg["usage"] is not None
        assert asst_msg["usage"]["prompt_tokens"] == 80
        assert asst_msg["usage"]["candidates_tokens"] == 15
        assert asst_msg["usage"]["total_tokens"] == 95
        assert ui_data["total_tokens"] == 95

    def test_malformed_cost_or_tokens_fails_open(self):
        from zocrates.utils.session_formatting import format_session_for_ui

        user_part = MagicMock(thought=False, text="Query", function_call=None, function_response=None)
        evt_user = MagicMock(
            author="user",
            timestamp=100.0,
            content=MagicMock(parts=[user_part]),
            actions=None,
            usage_metadata=None,
            invocation_id="inv_1",
        )

        asst_part = MagicMock(thought=False, text="Response", function_call=None, function_response=None)
        evt_asst = MagicMock(
            author="synthesizer",
            model_version="test-model",
            timestamp=105.0,
            content=MagicMock(parts=[asst_part]),
            actions=None,
            usage_metadata={"total_token_count": 500},
            invocation_id="inv_1",
            custom_metadata={
                "citation_verification": {
                    "assessments": [
                        {
                            "claim": "Claim malformed",
                            "sentence_attribution": {
                                "status": "ok",
                                "cost_usd": "not-a-float",
                                "usage": {
                                    "prompt_tokens": "not-an-int",
                                    "completion_tokens": {},
                                    "total_tokens": "bad",
                                },
                            },
                        }
                    ]
                }
            },
        )

        session = MagicMock(id="sess_malformed", state={}, events=[evt_user, evt_asst])
        with patch("zocrates.utils.session_formatting.aggregate_turn_cost", return_value=(0.001, {})):
            ui_data = format_session_for_ui(session)

        asst_msg = ui_data["messages"][-1]
        assert asst_msg["turn_cost_usd"] == 0.001
        assert asst_msg["usage"]["total_tokens"] == 500


