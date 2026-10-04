"""Local Laya option must not silently send a request to remote providers."""
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from doga import response_contract
import doga.__init__ as plugin


@pytest.fixture(autouse=True)
def reset_laya_model(monkeypatch):
    monkeypatch.setattr(response_contract, "_laya_agent", None)


def _answers():
    return {"answers": {
        "goal": {"type": "choice", "choice": "action"},
        "mode": {"type": "choice", "choice": "recommend"},
        "stakes": {"type": "choice", "choice": "medium"},
        "clarification": {"type": "noul", "noul": 0.85},
        "scenario_need": {"type": "choice", "choice": "compare_options"},
    }, "model": "convaiinnovations/laya"}


def test_laya_provider_reuses_local_model_and_injects_contract(monkeypatch):
    predictions = []
    loads = []
    class Agent:
        def predict(self, state, questions):
            predictions.append((state, questions))
            return _answers()
    def load(model):
        loads.append(model)
        return Agent()
    monkeypatch.setitem(sys.modules, "laya", SimpleNamespace(load=load))
    monkeypatch.setenv("OPENROUTER_API_KEY", "remote-key")
    monkeypatch.setenv("TYPESAFE_API_KEY", "remote-key")
    with patch.object(response_contract, "_request_jev", side_effect=AssertionError("remote call")):
        first = response_contract.evaluate_contract("Recommend one path", provider="laya")
        second = response_contract.evaluate_contract("Recommend another path", provider="laya")
    assert first["answers"]["clarification"]["noul"] == 0.85
    assert second["model"] == "convaiinnovations/laya"
    assert loads == ["convaiinnovations/laya"]
    assert predictions[0][0] == {"user_request": "Recommend one path"}
    assert set(predictions[0][1]) == set(response_contract.QUESTIONS)
    contract = response_contract.build_contract(first)
    assert contract["conditional_response"] is True
    assert "make the answer conditional" in response_contract.render_contract(contract)


def test_local_mode_failure_does_not_send_prompt_to_jev(monkeypatch):
    monkeypatch.setitem(sys.modules, "laya", SimpleNamespace(load=lambda model: (_ for _ in ()).throw(RuntimeError("offline"))))
    with patch.object(response_contract, "_request_jev", side_effect=AssertionError("remote call")):
        with pytest.raises(RuntimeError, match="offline"):
            response_contract.evaluate_contract("private message", provider="laya")


def test_laya_requires_full_typed_response(monkeypatch):
    monkeypatch.setitem(sys.modules, "laya", SimpleNamespace(load=lambda model: SimpleNamespace(predict=lambda state, questions: {"answers": {}})))
    with pytest.raises(RuntimeError, match="invalid.*Laya"), patch.object(response_contract, "_request_jev", side_effect=AssertionError("remote call")):
        response_contract.evaluate_contract("request", provider="laya")


def test_doga_can_select_local_provider_without_disabling_contract(monkeypatch):
    monkeypatch.setattr(plugin._state, "decision_provider", "jev")
    assert "laya" in plugin._handle_doga("provider laya").lower()
    assert plugin._state.decision_provider == "laya"
    fake = _answers()
    monkeypatch.setattr(plugin._state, "enabled", True)
    monkeypatch.setattr(plugin._state, "jev_enabled", True)
    monkeypatch.setattr(plugin._state, "memory_enabled", False)
    with patch.object(plugin.response_contract, "evaluate_contract", return_value=fake) as evaluator:
        result = plugin._on_pre_llm_call(user_message="Recommend one path")
    evaluator.assert_called_once_with("Recommend one path", provider="laya", fallback_to_jev=False)
    assert "[DOGA response contract]" in result["context"]
    assert "make the answer conditional" in result["context"]
    assert "laya" in plugin._handle_doga("status").lower()
    assert "jev" in plugin._handle_doga("provider jev").lower()
    assert plugin._state.decision_provider == "jev"


def test_invalid_provider_is_rejected_without_mutating_setting(monkeypatch):
    monkeypatch.setattr(plugin._state, "decision_provider", "jev")
    message = plugin._handle_doga("provider something-else")
    assert "jev|clef|laya" in message
    assert plugin._state.decision_provider == "jev"


def test_process_startup_reads_local_provider_selection(monkeypatch):
    # DOGA_DECISION_MODE takes precedence over the legacy provider variable, so
    # an ambient value in the caller's environment would silently win here.
    monkeypatch.delenv("DOGA_DECISION_MODE", raising=False)
    monkeypatch.setenv("DOGA_DECISION_PROVIDER", "laya")
    assert plugin._PluginState().decision_provider == "laya"


def test_invalid_startup_provider_does_not_fall_through_to_remote(monkeypatch):
    monkeypatch.delenv("DOGA_DECISION_MODE", raising=False)
    monkeypatch.setenv("DOGA_DECISION_PROVIDER", "unknown")
    provider = plugin._PluginState().decision_provider
    with patch.object(response_contract, "_request_jev", side_effect=AssertionError("remote call")):
        with pytest.raises(ValueError, match="provider"):
            response_contract.evaluate_contract("private message", provider=provider)


def test_missing_local_model_keeps_ordinary_hook_guidance(monkeypatch):
    monkeypatch.setattr(plugin._state, "decision_provider", "laya")
    monkeypatch.setattr(plugin._state, "enabled", True)
    monkeypatch.setattr(plugin._state, "jev_enabled", True)
    monkeypatch.setattr(plugin._state, "memory_enabled", False)
    with patch.object(response_contract, "_request_laya", side_effect=RuntimeError("not installed")), \
         patch.object(response_contract, "_request_jev", side_effect=AssertionError("remote call")):
        result = plugin._on_pre_llm_call(user_message="private request")
    assert "[DOGA response contract]" not in result["context"]
    assert result["context"]
    assert plugin._state._last_jev_status == "error"


def test_explicit_local_failure_uses_jev_fallback(monkeypatch):
    monkeypatch.setattr(plugin._state, "decision_provider", "laya")
    monkeypatch.setattr(plugin._state, "jev_fallback", True)
    monkeypatch.setattr(plugin._state, "enabled", True)
    monkeypatch.setattr(plugin._state, "jev_enabled", True)
    monkeypatch.setattr(plugin._state, "memory_enabled", False)
    with patch.object(response_contract, "_request_laya", side_effect=RuntimeError("local failure")), \
         patch.object(response_contract, "_request_jev", return_value=_answers()) as remote:
        result = plugin._on_pre_llm_call(user_message="public test request")
    assert remote.call_count == 1
    assert "[DOGA response contract]" in result["context"]
    assert plugin._state._last_jev_status == "ok (jev fallback)"


def test_healthy_laya_does_not_use_remote_even_with_fallback_enabled():
    with patch.object(response_contract, "_request_laya", return_value=_answers()), \
         patch.object(response_contract, "_request_jev", side_effect=AssertionError("remote call")):
        result = response_contract.evaluate_contract("private message", provider="laya", fallback_to_jev=True)
    assert result["answers"] == _answers()["answers"]


def test_fallback_logs_local_error_category_without_request(caplog):
    with patch.object(response_contract, "_request_laya", side_effect=RuntimeError("private request text")), \
         patch.object(response_contract, "_request_jev", return_value=_answers()):
        response_contract.evaluate_contract("private request text", provider="laya", fallback_to_jev=True)
    assert "RuntimeError" in caplog.text
    assert "private request text" not in caplog.text


def test_repeated_local_failures_stop_remote_fallback(monkeypatch):
    monkeypatch.setattr(response_contract, "_laya_failure_count", 0, raising=False)
    with patch.object(response_contract, "_request_laya", side_effect=RuntimeError("offline")), \
         patch.object(response_contract, "_request_jev", return_value=_answers()) as remote:
        for _ in range(3):
            assert response_contract.evaluate_contract("sample", provider="laya", fallback_to_jev=True)["_doga_provider"] == "jev_fallback"
        with pytest.raises(RuntimeError, match="offline"):
            response_contract.evaluate_contract("sample", provider="laya", fallback_to_jev=True)
    assert remote.call_count == 3


def test_healthy_local_call_resets_failure_breaker(monkeypatch):
    monkeypatch.setattr(response_contract, "_laya_failure_count", 3, raising=False)
    with patch.object(response_contract, "_request_laya", return_value=_answers()), \
         patch.object(response_contract, "_request_jev", side_effect=AssertionError("remote call")):
        response_contract.evaluate_contract("sample", provider="laya", fallback_to_jev=True)
    assert response_contract._laya_failure_count == 0


def test_local_only_success_resets_failure_breaker(monkeypatch):
    monkeypatch.setattr(response_contract, "_laya_failure_count", 3, raising=False)
    with patch.object(response_contract, "_request_laya", return_value=_answers()):
        response_contract.evaluate_contract("sample", provider="laya")
    assert response_contract._laya_failure_count == 0


def test_remote_failure_after_local_failure_leaves_standard_guidance(monkeypatch):
    monkeypatch.setattr(plugin._state, "decision_provider", "laya")
    monkeypatch.setattr(plugin._state, "jev_fallback", True)
    monkeypatch.setattr(plugin._state, "enabled", True)
    monkeypatch.setattr(plugin._state, "jev_enabled", True)
    monkeypatch.setattr(plugin._state, "memory_enabled", False)
    with patch.object(response_contract, "_request_laya", side_effect=RuntimeError("local failure")), \
         patch.object(response_contract, "_request_jev", side_effect=RuntimeError("remote failure")):
        result = plugin._on_pre_llm_call(user_message="test request")
    assert "[DOGA response contract]" not in result["context"]
    assert plugin._state._last_jev_status == "error"
