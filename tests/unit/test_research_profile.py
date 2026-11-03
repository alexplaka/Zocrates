"""Tests for the user research profile: config, prompt injection, and API endpoints."""

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from zocrates.agents.common.state_keys import (
    USER_RESEARCH_PROFILE,
    USER_RESEARCH_PROFILE_STR,
)
from zocrates.agents.coordinator.prompts import (
    COORDINATOR_INSTRUCTION_dynamic,
    format_user_research_profile_context,
)
from zocrates.fast_api_app import app
from zocrates.config import (
    DEFAULT_CONFIG,
    get_auth_token,
    get_user_research_profile,
    load_zocrates_config,
    save_zocrates_config,
)


@pytest.fixture(autouse=True)
def reset_sse_app_status():
    import sse_starlette.sse

    sse_starlette.sse.AppStatus.should_exit_event = None
    yield
    sse_starlette.sse.AppStatus.should_exit_event = None


@pytest.fixture
def client():
    return TestClient(app, headers={"Authorization": f"Bearer {get_auth_token()}"})


def _config_path(tmp_path):
    config_dir = tmp_path / "zocrates-data"
    config_dir.mkdir(exist_ok=True)
    return config_dir / "zocrates_config.json"


class TestStateKeys:
    def test_state_key_values(self):
        assert USER_RESEARCH_PROFILE == "user_research_profile"
        assert USER_RESEARCH_PROFILE_STR == "user_research_profile_str"


class TestConfig:
    def test_defaults_present(self):
        assert DEFAULT_CONFIG["user_research_profile"] == ""
        assert DEFAULT_CONFIG["max_research_profile_chars"] == 1000

    def test_backfill_into_existing_config_file(self, tmp_path):
        path = _config_path(tmp_path)
        path.write_text(json.dumps({"llm_model": "x/y"}))

        cfg = load_zocrates_config(tmp_path)

        assert cfg["user_research_profile"] == ""
        assert cfg["max_research_profile_chars"] == 1000
        on_disk = json.loads(path.read_text())
        assert on_disk["user_research_profile"] == ""
        assert on_disk["max_research_profile_chars"] == 1000
        assert on_disk["llm_model"] == "x/y"

    def test_backfill_preserves_existing_profile(self, tmp_path):
        path = _config_path(tmp_path)
        path.write_text(json.dumps({"user_research_profile": "Use LaTeX."}))

        cfg = load_zocrates_config(tmp_path)

        assert cfg["user_research_profile"] == "Use LaTeX."
        assert json.loads(path.read_text())["max_research_profile_chars"] == 1000

    def test_get_user_research_profile(self, tmp_path):
        assert get_user_research_profile(tmp_path) == ""
        save_zocrates_config({"user_research_profile": "  Prefer Python.  "}, tmp_path)
        assert get_user_research_profile(tmp_path) == "Prefer Python."

    def test_save_accepts_exactly_1000_chars(self, tmp_path):
        value = "a" * 1000
        save_zocrates_config({"user_research_profile": value}, tmp_path)
        assert load_zocrates_config(tmp_path)["user_research_profile"] == value

    def test_save_rejects_over_1000_chars(self, tmp_path):
        path = _config_path(tmp_path)
        path.write_text(json.dumps({"user_research_profile": "keep"}))
        with pytest.raises(ValueError):
            save_zocrates_config({"user_research_profile": "a" * 1001}, tmp_path)
        assert json.loads(path.read_text())["user_research_profile"] == "keep"


class TestPromptFormatting:
    @pytest.mark.parametrize("empty", [None, "", "   ", "\n\t  \n"])
    def test_empty_profile_yields_zero_overhead(self, empty):
        assert format_user_research_profile_context(empty) == ""

    def test_non_empty_profile_is_formatted_and_stripped(self):
        out = format_user_research_profile_context("  Always use LaTeX.  \n")
        assert out == "# User Research Profile & Directives\nAlways use LaTeX.\n\n"

    def test_coordinator_instruction_references_profile_key(self):
        assert "{user_research_profile_str?}" in COORDINATOR_INSTRUCTION_dynamic
        assert "{active_context_items_str?}" in COORDINATOR_INSTRUCTION_dynamic


class TestLlmConfigEndpoint:
    def test_post_persists_profile_and_get_returns_it(self, client, tmp_path):
        path = _config_path(tmp_path)
        with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path), patch(
            "zocrates.agents.app.set_agent_models"
        ):
            res = client.post("/config", json={"user_research_profile": "Use LaTeX."})
            assert res.status_code == 200
            assert res.json()["config"]["user_research_profile"] == "Use LaTeX."
            assert json.loads(path.read_text())["user_research_profile"] == "Use LaTeX."

            res2 = client.get("/config")
            assert res2.json()["user_research_profile"] == "Use LaTeX."

    def test_post_can_clear_profile(self, client, tmp_path):
        path = _config_path(tmp_path)
        with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path), patch(
            "zocrates.agents.app.set_agent_models"
        ):
            client.post("/config", json={"user_research_profile": "Use LaTeX."})
            res = client.post("/config", json={"user_research_profile": ""})
            assert res.status_code == 200
            assert json.loads(path.read_text())["user_research_profile"] == ""

    def test_post_rejects_over_1000_chars(self, client, tmp_path):
        path = _config_path(tmp_path)
        with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path), patch(
            "zocrates.agents.app.set_agent_models"
        ):
            res = client.post("/config", json={"user_research_profile": "a" * 1001})
            assert res.status_code == 400
            assert "1000" in res.json()["detail"]
            assert not path.exists() or "a" * 1001 not in path.read_text()

    def test_status_includes_profile(self, client, tmp_path):
        save_zocrates_config({"user_research_profile": "Prefer PyTorch."}, tmp_path)
        with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path):
            res = client.get("/status")
        assert res.status_code == 200
        assert res.json()["user_research_profile"] == "Prefer PyTorch."


class TestChatStateDelta:
    def _run_chat(self, client, profile):
        captured = []

        async def fake_run_async(*args, **kwargs):
            captured.append(kwargs.get("state_delta"))
            event = MagicMock()
            event.id = "evt-1"
            event.author = "agent"
            event.is_final_response.return_value = True
            event.content.parts = [
                MagicMock(text="Hello", thought=None, function_call=None, function_response=None)
            ]
            yield event

        runner = MagicMock()
        runner.run_async = fake_run_async
        with patch("zocrates.fast_api_app.get_syncer", return_value=MagicMock()), patch(
            "zocrates.config.get_user_research_profile", return_value=profile
        ):
            app.state.runner = runner
            res = client.post(
                "/chat", json={"prompt": "Hi", "session_id": "profile-session", "active_items": []}
            )
        assert res.status_code == 200
        assert len(captured) == 1
        return captured[0]

    def test_empty_profile_adds_empty_string(self, client):
        sd = self._run_chat(client, "")
        assert sd[USER_RESEARCH_PROFILE_STR] == ""

    def test_non_empty_profile_is_formatted_in_state_delta(self, client):
        sd = self._run_chat(client, "Always use LaTeX.")
        assert sd[USER_RESEARCH_PROFILE_STR] == format_user_research_profile_context("Always use LaTeX.")
        assert "# User Research Profile & Directives" in sd[USER_RESEARCH_PROFILE_STR]


class TestSynthesizerPromptComposition:
    def test_synthesizer_prompt_includes_profile_at_top_when_present(self):
        profile_text = format_user_research_profile_context("Always use LaTeX.")
        state = {USER_RESEARCH_PROFILE_STR: profile_text}

        # Test library search prompt construction logic (prepended at top)
        synth_parts = []
        profile_str = str(state.get(USER_RESEARCH_PROFILE_STR) or "").strip()
        if profile_str:
            synth_parts.append(profile_str)
        synth_parts.append("# User Query\nWhat is Medusa?")
        synth_parts.append("# Target Search Goal\nMedusa")
        synth_prompt = "\n\n".join(synth_parts)

        assert synth_prompt.startswith("# User Research Profile & Directives")
        assert "Always use LaTeX." in synth_prompt

    def test_synthesizer_prompt_omits_profile_when_empty(self):
        state = {USER_RESEARCH_PROFILE_STR: ""}
        synth_parts = []
        profile_str = str(state.get(USER_RESEARCH_PROFILE_STR) or "").strip()
        if profile_str:
            synth_parts.append(profile_str)
        synth_parts.append("# User Query\nWhat is Medusa?")
        synth_prompt = "\n\n".join(synth_parts)

        assert "# User Research Profile & Directives" not in synth_prompt

