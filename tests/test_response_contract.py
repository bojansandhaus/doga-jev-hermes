from unittest.mock import patch

from doga.response_contract import build_contract, evaluate_contract, render_contract


def test_build_contract_uses_typed_answers_to_set_action_response_requirements():
    answers = {
        "goal": {"type": "choice", "choice": "action"},
        "mode": {"type": "choice", "choice": "recommend"},
        "stakes": {"type": "choice", "choice": "high"},
        "clarification": {"type": "noul", "noul": 0.1},
        "scenario_need": {"type": "choice", "choice": "compare_options"},
    }
    contract = build_contract({"answers": answers})
    assert contract["goal"] == "action"
    assert contract["mode"] == "recommend"
    assert contract["required_elements"] == ["recommendation", "next_step", "material risks and uncertainty", "compare plausible alternatives"]
    assert contract["ask_clarifying_question"] is False
    assert "recommendation" in render_contract(contract)


def test_ambiguous_contract_uses_safe_fallback():
    contract = build_contract({"answers": {"goal": {"choice": "surprise"}}})
    assert contract["goal"] == "information"
    assert contract["mode"] == "answer"
    assert contract["required_elements"] == ["direct_answer"]


def test_evaluate_contract_sends_bounded_structured_state_and_typed_questions():
    seen = {}

    def evaluator(**kwargs):
        seen.update(kwargs)
        return {"model": "jev-latest", "answers": {}}

    result = evaluate_contract("Should I switch jobs?", evaluator=evaluator)
    assert result["answers"] == {}
    assert seen["state"] == {"user_request": "Should I switch jobs?"}
    assert set(seen["questions"]) == {"goal", "mode", "stakes", "clarification", "scenario_need"}
    assert all("type" in question and "instructions" in question for question in seen["questions"].values())


def test_contract_requires_clarification_when_mode_and_signal_agree():
    contract = build_contract({"answers": {
        "goal": {"choice": "action"}, "mode": {"choice": "clarify"},
        "clarification": {"noul": 0.8},
    }})
    assert contract["ask_clarifying_question"] is True
    assert contract["conditional_response"] is False
    assert "clarifying question" in render_contract(contract)


def test_recommendation_with_high_ambiguity_requires_explicit_conditions():
    contract = build_contract({"answers": {
        "goal": {"choice": "action"}, "mode": {"choice": "recommend"},
        "clarification": {"noul": 0.85},
        "scenario_need": {"choice": "compare_options"},
    }})
    assert contract["ask_clarifying_question"] is False
    assert contract["conditional_response"] is True
    assert "recommendation" in contract["required_elements"]
    assert "next_step" in contract["required_elements"]
    assert "state material assumptions and identify missing information that could change the answer" in contract["required_elements"]
    rendered = render_contract(contract)
    assert "make the answer conditional" in rendered
    assert "what missing information could change it" in rendered


def test_low_ambiguity_recommendation_remains_unqualified():
    contract = build_contract({"answers": {
        "goal": {"choice": "action"}, "mode": {"choice": "recommend"},
        "clarification": {"noul": 0.2},
    }})
    assert contract["ask_clarifying_question"] is False
    assert contract["conditional_response"] is False
    assert "state material assumptions and identify missing information that could change the answer" not in contract["required_elements"]


def test_typesafe_http_request_uses_current_endpoint_and_secret_header():
    from doga import response_contract

    class FakeResponse:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return b'{"answers": {"goal": {"choice": "information"}}}'

    # A real question set is sent, because the route validates every typed
    # answer against the questions it actually asked.
    questions = {
        "goal": {"type": "choice", "criteria": {"information": "", "understanding": "", "action": ""}},
    }
    with patch.object(response_contract.urllib.request, "urlopen", return_value=FakeResponse()) as urlopen:
        response_contract._request_typesafe({"user_request": "hello"}, questions, api_key="test-secret")
    request = urlopen.call_args.args[0]
    assert request.full_url == "https://api.typesafe.ai/v1/systemone"
    assert request.get_header("Authorization") == "Bearer test-secret"


def test_jev_request_prefers_openrouter_when_both_keys_are_available(monkeypatch):
    from doga import response_contract

    monkeypatch.setenv("OPENROUTER_API_KEY", "router-key")
    monkeypatch.setenv("TYPESAFE_API_KEY", "typesafe-key")

    class FakeResponse:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return b'{"answers": {"goal": {"choice": "information"}}}'

    questions = {
        "goal": {"type": "choice", "criteria": {"information": "", "understanding": "", "action": ""}},
    }
    with patch.object(response_contract.urllib.request, "urlopen", return_value=FakeResponse()) as urlopen:
        result = response_contract._request_jev({"user_request": "hello"}, questions)

    request = urlopen.call_args.args[0]
    payload = __import__("json").loads(request.data)
    assert result["answers"]["goal"]["choice"] == "information"
    assert request.full_url == "https://openrouter.ai/api/alpha/decisions"
    assert request.get_header("Authorization") == "Bearer router-key"
    assert payload["model"] == "typesafe/jev-1.13"


def test_jev_request_falls_back_to_typesafe_after_openrouter_failure(monkeypatch):
    from doga import response_contract

    monkeypatch.setenv("OPENROUTER_API_KEY", "router-key")
    monkeypatch.setenv("TYPESAFE_API_KEY", "typesafe-key")
    fallback_result = {"model": "jev-latest", "answers": {"goal": {"choice": "action"}}}

    with patch.object(response_contract, "_request_openrouter", side_effect=RuntimeError("unavailable")) as primary, \
         patch.object(response_contract, "_request_typesafe", return_value=fallback_result) as fallback:
        result = response_contract._request_jev({"user_request": "hello"}, {"goal": {"type": "choice"}})

    primary.assert_called_once_with({"user_request": "hello"}, {"goal": {"type": "choice"}}, api_key="router-key")
    fallback.assert_called_once_with({"user_request": "hello"}, {"goal": {"type": "choice"}}, api_key="typesafe-key")
    assert result == fallback_result


def test_jev_request_uses_typesafe_when_openrouter_key_is_absent(monkeypatch):
    from doga import response_contract

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("TYPESAFE_API_KEY", "typesafe-key")
    expected = {"answers": {"goal": {"choice": "information"}}}

    with patch.object(response_contract, "_request_typesafe", return_value=expected) as fallback:
        result = response_contract._request_jev({}, {})

    fallback.assert_called_once_with({}, {}, api_key="typesafe-key")
    assert result == expected


def test_jev_request_requires_at_least_one_provider_key(monkeypatch):
    from doga import response_contract

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

    try:
        response_contract._request_jev({}, {})
    except RuntimeError as exc:
        assert "OPENROUTER_API_KEY" in str(exc)
        assert "TYPESAFE_API_KEY" in str(exc)
    else:
        raise AssertionError("expected an actionable missing-key error")
