import json
from unittest.mock import patch
import doga.__init__ as plugin


def test_state_initial():
    s = plugin._PluginState()
    assert s.enabled is True
    assert s.auto_depth is True
    assert s.depth == 3
    assert s.max_recursion == 3
    assert s.show_simulation is True


def test_state_thread_local():
    s = plugin._PluginState()
    s._recursion_depth = 5
    assert s._recursion_depth == 5


def test_state_reset():
    s = plugin._PluginState()
    s._recursion_depth = 3
    s._stop_sent = True
    s._stop_count = 2
    s._recursion_depth = 0
    s._reasoning_stack = []
    s._stop_sent = False
    s._stop_count = 0
    assert s._recursion_depth == 0
    assert s._reasoning_stack == []
    assert s._stop_sent is False
    assert s._stop_count == 0


def test_on_pre_llm_call_disabled():
    plugin._state.enabled = False
    result = plugin._on_pre_llm_call(user_message="test")
    assert result is None


def test_on_pre_llm_call_resets_state():
    plugin._state.enabled = True
    plugin._state._recursion_depth = 99
    plugin._state._stop_sent = True
    plugin._state._stop_count = 5
    plugin._on_pre_llm_call(user_message="test")
    assert plugin._state._recursion_depth == 0
    assert plugin._state._stop_sent is False
    assert plugin._state._stop_count == 0


def test_on_post_tool_call_reason_deeper():
    plugin._state._recursion_depth = 0
    plugin._state._reasoning_stack = []
    plugin._on_post_tool_call(
        tool_name="reason_deeper",
        args={"focus": "test"},
    )
    assert plugin._state._recursion_depth == 1
    assert len(plugin._state._reasoning_stack) == 1
    assert plugin._state._reasoning_stack[0]["focus"] == "test"


def test_simulate_tool_handler_empty():
    result = plugin._simulate_tool_handler({})
    data = json.loads(result)
    assert "error" in data


def test_simulate_tool_handler_stop_sent():
    plugin._state._stop_sent = True
    result = json.loads(plugin._simulate_tool_handler({"scenarios": [{"name": "t", "variables": {"x": 0.5}}]}))
    assert result.get("stop") is True
    plugin._state._stop_sent = False


def test_simulate_tool_handler_success():
    result = plugin._simulate_tool_handler({
        "scenarios": [{"name": "t", "variables": {"x": 0.5}}],
        "n_iterations": 100,
    })
    data = json.loads(result)
    assert "scenarios" in data
    assert "summary" in data


def test_reason_deeper_handler_stop():
    plugin._state._recursion_depth = 3
    plugin._state.max_recursion = 3
    plugin._state._stop_sent = False
    plugin._state._stop_count = 0
    result = json.loads(plugin._reason_deeper_handler({"focus": "x"}))
    assert result["stop"] is True
    assert plugin._state._stop_sent is True
    assert plugin._state._stop_count == 1


def test_reason_deeper_handler_hard_break():
    plugin._state._recursion_depth = 3
    plugin._state.max_recursion = 3
    plugin._state._stop_sent = True
    plugin._state._stop_count = 2
    result = json.loads(plugin._reason_deeper_handler({"focus": "x"}))
    assert result["stop"] is True
    assert result.get("hard_break") is True
    assert plugin._state._stop_count == 3


def test_reason_deeper_handler_continue():
    plugin._state._recursion_depth = 1
    plugin._state.max_recursion = 3
    plugin._state._stop_sent = False
    result = json.loads(plugin._reason_deeper_handler({"focus": "x"}))
    assert result.get("continue") is True
    assert "instruction" in result


def test_on_transform_llm_output_disabled():
    plugin._state.enabled = False
    assert plugin._on_transform_llm_output(response_text="test") is None


def test_on_transform_llm_output_memory_save():
    plugin._state.enabled = True
    plugin._state.memory_enabled = True
    plugin._state._current_user_message = "what is the risk"
    with patch.object(plugin, "MNEMOSYNE_AVAILABLE", True), \
         patch.object(plugin, "remember", create=True) as mock_remember:
        # The formatted output is asserted, not discarded: the transform must
        # still return the formatted response.
        result = plugin._on_transform_llm_output(
            "<world_model>Information</world_model> answer"
        )
        mock_remember.assert_called_once()
    assert result is not None
    assert "Information" in result
    plugin._state.memory_enabled = False
