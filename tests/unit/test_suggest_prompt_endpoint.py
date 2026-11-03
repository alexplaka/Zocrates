"""Unit tests for POST /sessions/{session_id}/suggest_next_prompt."""

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from zocrates.agents.common.state_keys import RESEARCH_TURN_HISTORY
from zocrates.config import get_auth_token
from zocrates.fast_api_app import app

client = TestClient(app, headers={"Authorization": f"Bearer {get_auth_token()}"})

HISTORY = [
    {"invocation_id": "inv-1", "pipeline": "library_search", "user_query": "q1", "search_goal": "g1", "conclude_output": {}},
    {"invocation_id": "inv-2", "pipeline": "online_search", "user_query": "q2", "search_goal": "g2", "conclude_output": {}},
]


def _session_service(state):
    session = MagicMock()
    session.state = state
    service = MagicMock()
    service.get_session = AsyncMock(return_value=session)
    return service


TELEMETRY = {
    "model": "openrouter/custom/model",
    "latency_ms": 123.45,
    "usage": {"prompt_tokens": 100, "completion_tokens": 12, "total_tokens": 112},
    "cost_usd": 0.000321,
    "status": "success",
    "suggested_prompt": "Compare Medusa heads against our draft models",
}


def _call(
    state,
    cfg=None,
    invocation_id="inv-2",
    prediction="Compare Medusa heads against our draft models",
    telemetry=None,
):
    config = {"enable_predictive_ghost_input": True}
    config.update(cfg or {})

    async def _predict(**kwargs):
        if telemetry is not None:
            kwargs["telemetry_out"].update(telemetry)
        return prediction

    predictor = AsyncMock(side_effect=_predict)
    with (
        patch("zocrates.fast_api_app.get_syncer", return_value=MagicMock(_zotero_data_dir=None)),
        patch("zocrates.config.load_zocrates_config", return_value=config),
        patch("zocrates.config.get_secure_api_key", return_value="sk-test"),
        patch("zocrates.utils.services.get_session_service", return_value=_session_service(state)),
        patch("zocrates.agents.coordinator.predictor.predict_next_user_prompt", new=predictor),
        patch.object(app.state, "runner", None, create=True),
    ):
        response = client.post("/sessions/s1/suggest_next_prompt", json={"invocation_id": invocation_id})
    return response, predictor


def test_suggest_success_uses_last_two_turns_and_configured_model():
    response, predictor = _call(
        {RESEARCH_TURN_HISTORY: list(HISTORY)}, cfg={"predictive_prompt_model": "openrouter/custom/model"}
    )
    assert response.status_code == 200
    assert response.json() == {
        "status": "success",
        "suggested_prompt": "Compare Medusa heads against our draft models",
        "telemetry": None,
    }
    kwargs = predictor.call_args.kwargs
    assert kwargs["research_history"] == HISTORY
    assert kwargs["model"] == "openrouter/custom/model"
    assert kwargs["api_key"] == "sk-test"


def test_suggest_success_returns_telemetry_payload():
    response, predictor = _call({RESEARCH_TURN_HISTORY: list(HISTORY)}, telemetry=TELEMETRY)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["telemetry"] == TELEMETRY
    assert isinstance(predictor.call_args.kwargs["telemetry_out"], dict)


def test_suggest_no_suggestion_returns_telemetry_payload():
    suppressed = dict(TELEMETRY, status="suppressed", suggested_prompt=None)
    response, _ = _call({RESEARCH_TURN_HISTORY: list(HISTORY)}, prediction=None, telemetry=suppressed)
    assert response.json() == {"status": "no_suggestion", "suggested_prompt": None, "telemetry": suppressed}


def test_suggest_logs_telemetry(caplog):
    with caplog.at_level("INFO", logger="zocrates.fast_api_app"):
        _call({RESEARCH_TURN_HISTORY: list(HISTORY)}, telemetry=TELEMETRY)
    assert any(
        "Predictive next-prompt: Compare Medusa heads against our draft models in 123.5ms (tokens=112, cost=$0.000321)"
        in rec.getMessage()
        for rec in caplog.records
    )


def test_suggest_disabled_returns_disabled_without_llm():
    response, predictor = _call({RESEARCH_TURN_HISTORY: list(HISTORY)}, cfg={"enable_predictive_ghost_input": False})
    assert response.json() == {"status": "disabled", "suggested_prompt": None}
    predictor.assert_not_called()


def test_suggest_disabled_when_no_api_key_and_not_local():
    """Verify suggest_next_prompt returns disabled immediately when no cloud key exists and model is not local."""
    config = {"enable_predictive_ghost_input": True, "predictive_prompt_model": "openrouter/nvidia/nemotron-3.5-lightning:free"}
    predictor = AsyncMock()
    with (
        patch("zocrates.fast_api_app.get_syncer", return_value=MagicMock(_zotero_data_dir=None)),
        patch("zocrates.config.load_zocrates_config", return_value=config),
        patch("zocrates.config.get_secure_api_key", return_value=None),
        patch("zocrates.utils.services.get_session_service", return_value=_session_service({RESEARCH_TURN_HISTORY: list(HISTORY)})),
        patch("zocrates.agents.coordinator.predictor.predict_next_user_prompt", new=predictor),
        patch.object(app.state, "runner", None, create=True),
    ):
        response = client.post("/sessions/s1/suggest_next_prompt", json={"invocation_id": "inv-2"})
    assert response.json() == {"status": "disabled", "suggested_prompt": None}
    predictor.assert_not_called()



def test_suggest_invocation_gate_blocks_conversational_turns():
    response, predictor = _call({RESEARCH_TURN_HISTORY: list(HISTORY)}, invocation_id="inv-greeting")
    assert response.json() == {"status": "no_suggestion", "suggested_prompt": None}
    predictor.assert_not_called()


def test_suggest_empty_history_returns_no_suggestion():
    response, predictor = _call({"other": 1})
    assert response.json() == {"status": "no_suggestion", "suggested_prompt": None}
    predictor.assert_not_called()


def test_suggest_predictor_returns_none():
    response, _ = _call({RESEARCH_TURN_HISTORY: list(HISTORY)}, prediction=None)
    assert response.json() == {"status": "no_suggestion", "suggested_prompt": None, "telemetry": None}


def test_suggest_syncer_error_raises_400():
    with patch("zocrates.fast_api_app.get_syncer", side_effect=RuntimeError("Syncer not initialized")):
        response = client.post("/sessions/s1/suggest_next_prompt", json={"invocation_id": "inv-2"})
    assert response.status_code == 400
    assert "Syncer not initialized" in response.json()["detail"]


def test_suggest_no_session_service_returns_error():
    with (
        patch("zocrates.fast_api_app.get_syncer", return_value=MagicMock(_zotero_data_dir=None)),
        patch("zocrates.config.load_zocrates_config", return_value={"enable_predictive_ghost_input": True}),
        patch("zocrates.utils.services.get_session_service", return_value=None),
        patch.object(app.state, "runner", None, create=True),
    ):
        response = client.post("/sessions/s1/suggest_next_prompt", json={"invocation_id": "inv-2"})
    assert response.json() == {"status": "error", "suggested_prompt": None}


def test_suggest_session_fetch_exception_returns_error():
    service = MagicMock()
    service.get_session = AsyncMock(side_effect=RuntimeError("DB disconnected"))
    with (
        patch("zocrates.fast_api_app.get_syncer", return_value=MagicMock(_zotero_data_dir=None)),
        patch("zocrates.config.load_zocrates_config", return_value={"enable_predictive_ghost_input": True}),
        patch("zocrates.utils.services.get_session_service", return_value=service),
        patch.object(app.state, "runner", None, create=True),
    ):
        response = client.post("/sessions/s1/suggest_next_prompt", json={"invocation_id": "inv-2"})
    assert response.json() == {"status": "error", "suggested_prompt": None}


def test_suggest_stateless_session_returns_no_suggestion():
    session = MagicMock()
    session.state = None
    service = MagicMock()
    service.get_session = AsyncMock(return_value=session)
    with (
        patch("zocrates.fast_api_app.get_syncer", return_value=MagicMock(_zotero_data_dir=None)),
        patch("zocrates.config.load_zocrates_config", return_value={"enable_predictive_ghost_input": True}),
        patch("zocrates.utils.services.get_session_service", return_value=service),
        patch.object(app.state, "runner", None, create=True),
    ):
        response = client.post("/sessions/s1/suggest_next_prompt", json={"invocation_id": "inv-2"})
    assert response.json() == {"status": "no_suggestion", "suggested_prompt": None}

