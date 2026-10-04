import json
from unittest.mock import patch
import doga.__init__ as plugin


def test_jev_contract_is_used_in_pre_llm_prompt_when_enabled():
    plugin._state.enabled = True
    plugin._state.jev_enabled = True
    fake = {"answers": {
        "goal": {"choice": "action"}, "mode": {"choice": "recommend"},
        "stakes": {"choice": "low"}, "scenario_need": {"choice": "none"},
        "clarification": {"noul": 0.1},
    }}
    with patch.object(plugin.response_contract, "evaluate_contract", return_value=fake) as call:
        result = plugin._on_pre_llm_call(user_message="What should I do?")
    # The canonical mode travels alongside the legacy pair and cannot be
    # derived from it, since both API-led modes share one provider.
    call.assert_called_once_with("What should I do?", provider="jev", fallback_to_jev=False,
                                 mode="api_only", hosted="jev")
    assert "User goal: action" in result["context"]
    assert "recommendation; next_step" in result["context"]
    plugin._state.jev_enabled = False


def test_jev_api_failure_falls_back_to_normal_doga_prompt():
    plugin._state.enabled = True
    plugin._state.jev_enabled = True
    with patch.object(plugin.response_contract, "evaluate_contract", side_effect=RuntimeError("offline")):
        result = plugin._on_pre_llm_call(user_message="What is 2 + 2?")
    assert "[DOGA response contract]" not in result["context"]
    plugin._state.jev_enabled = False
