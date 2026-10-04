"""DOGA Plugin — probabilistic, goal-aware reasoning for Hermes Agent.

Injects a thinking guidance prompt before each LLM call, registers a
``simulate`` tool for Monte Carlo analysis, and formats the output to
show a simulation summary alongside the final answer.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any, Dict, Optional

from . import de_bono_hats
from . import depth_selector
from . import simulation_engine
from . import thinking_prompt
from . import output_formatter
from . import response_contract

logger = logging.getLogger(__name__)

# Optional Mnemosyne memory backend
try:
    from mnemosyne import remember, recall
    MNEMOSYNE_AVAILABLE = True
except ImportError:
    MNEMOSYNE_AVAILABLE = False

# ---------------------------------------------------------------------------
# PHASE 3 — Recursive Reasoning via Hermes tool-calling loop
#
# Hermes' existing tool-calling loop (tool → execute → feed back → repeat)
# enables recursive reasoning without core changes. reason_deeper tool
# triggers the loop; handler tracks depth via _reasoning_stack.
# max_iterations in Hermes prevents infinite loops.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Plugin state
# ---------------------------------------------------------------------------

class _PluginState:
    """Mutable plugin settings, adjustable via /doga slash command.

    Global settings (enabled, depth, etc.) are shared across sessions.
    Per-turn state (_recursion_depth, etc.) is thread-local to prevent
    cross-session contamination in concurrent conversation handling.
    """

    def __init__(self):
        self.enabled: bool = True
        self.auto_depth: bool = True
        self.depth: int = 3
        self.show_simulation: bool = True
        self.max_scenarios: int = 5
        self.memory_enabled: bool = True
        self.jev_enabled: bool = True
        self._last_jev_status: str = "enabled"
        self.decision_provider: str = os.environ.get("DOGA_DECISION_PROVIDER", "jev").strip().lower()
        self.jev_fallback: bool = os.environ.get("DOGA_LAYA_JEV_FALLBACK", "0").strip().lower() in {"1", "true", "yes"}
        startup_mode = os.environ.get("DOGA_DECISION_MODE", "").strip().lower()
        if startup_mode:
            self.decision_provider, self.jev_fallback = {
                "jev_api": ("jev", False),
                "clef_api": ("clef", False),
                "laya_local": ("laya", False),
                "laya_with_jev_fallback": ("laya", True),
            }.get(startup_mode, (startup_mode, False))
        elif self.decision_provider != "laya":
            self.jev_fallback = False
        self.de_bono_enabled: bool = True
        self.max_recursion: int = 3
        self._local = threading.local()

    @property
    def _current_user_message(self) -> str:
        return getattr(self._local, 'current_user_message', "")

    @_current_user_message.setter
    def _current_user_message(self, value: str):
        self._local.current_user_message = value

    @property
    def _last_complexity(self) -> str:
        return getattr(self._local, 'last_complexity', "medium")

    @_last_complexity.setter
    def _last_complexity(self, value: str):
        self._local.last_complexity = value

    @property
    def _active_hats(self) -> list[str]:
        if not hasattr(self._local, 'active_hats'):
            self._local.active_hats = []
        return self._local.active_hats

    @_active_hats.setter
    def _active_hats(self, value: list[str]):
        self._local.active_hats = value

    @property
    def _recursion_depth(self) -> int:
        return getattr(self._local, 'recursion_depth', 0)

    @_recursion_depth.setter
    def _recursion_depth(self, value: int):
        self._local.recursion_depth = value

    @property
    def _reasoning_stack(self) -> list[dict]:
        if not hasattr(self._local, 'reasoning_stack'):
            self._local.reasoning_stack = []
        return self._local.reasoning_stack

    @_reasoning_stack.setter
    def _reasoning_stack(self, value: list[dict]):
        self._local.reasoning_stack = value

    @property
    def _stop_sent(self) -> bool:
        return getattr(self._local, 'stop_sent', False)

    @_stop_sent.setter
    def _stop_sent(self, value: bool):
        self._local.stop_sent = value

    @property
    def _stop_count(self) -> int:
        return getattr(self._local, 'stop_count', 0)

    @_stop_count.setter
    def _stop_count(self, value: int):
        self._local.stop_count = value

    @property
    def decision_mode(self) -> str:
        if self.decision_provider == "jev":
            return "jev_api"
        if self.decision_provider == "clef":
            return "clef_api"
        if self.decision_provider == "laya":
            return "laya_with_jev_fallback" if self.jev_fallback else "laya_local"
        return f"invalid ({self.decision_provider})"

    def to_dict(self) -> dict:
        mode = f"auto (complexity: {self._last_complexity})" if self.auto_depth else f"manual (depth: {self.depth})"
        return {
            "enabled": self.enabled,
            "mode": mode,
            "depth": self.depth,
            "show_simulation": self.show_simulation,
            "max_scenarios": self.max_scenarios,
            "de_bono_hats": "enabled" if self.de_bono_enabled else "disabled",
            "max_recursion": self.max_recursion,
            "memory": "enabled" if self.memory_enabled else "disabled",
            "jev": "enabled" if self.jev_enabled else "disabled",
            "decision_mode": self.decision_mode,
            "decision_provider": self.decision_provider,
            "jev_fallback": self.jev_fallback,
            "mnemosyne": "available" if MNEMOSYNE_AVAILABLE else "not installed",
        }


_state = _PluginState()

# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

def _on_pre_llm_call(
    user_message: str = "",
    is_first_turn: bool = False,
    **_: Any,
) -> Optional[Dict[str, str]]:
    """Inject thinking guidance before the LLM call.

    Returns a dict with a ``context`` key that gets appended to the
    current turn's user message.
    """
    if not _state.enabled:
        return None

    # Reset recursion state for this turn
    _state._recursion_depth = 0
    _state._reasoning_stack = []
    _state._stop_sent = False
    _state._stop_count = 0

    _state._current_user_message = user_message

    if _state.auto_depth and user_message:
        _state._last_complexity = depth_selector.assess_complexity(user_message)
        _state.depth = depth_selector.complexity_to_depth(_state._last_complexity)

    if _state.de_bono_enabled:
        _state._active_hats = de_bono_hats.hats_for_depth(_state.depth)
    else:
        _state._active_hats = []

    past_patterns = None
    if MNEMOSYNE_AVAILABLE and _state.memory_enabled and user_message:
        try:
            results = recall(user_message, top_k=3)
            if results:
                seen = {}
                for r in results:
                    meta = getattr(r, "metadata", {}) or {}
                    gt = meta.get("goal_type", "unknown")
                    seen[gt] = seen.get(gt, 0) + 1
                past_patterns = [
                    {"goal_type": k, "count": v}
                    for k, v in sorted(seen.items(), key=lambda x: -x[1])
                ]
        except Exception:
            logger.debug("doga memory recall failed", exc_info=True)

    guide = thinking_prompt.build_goal_prompt(
        user_message,
        depth=_state.depth,
        past_patterns=past_patterns,
        hats_enabled=_state.de_bono_enabled,
    )
    if _state.jev_enabled and user_message:
        try:
            judged = response_contract.evaluate_contract(user_message, provider=_state.decision_provider,
                                                        fallback_to_jev=_state.jev_fallback)
            contract = response_contract.build_contract(judged)
            guide += "\n\n" + response_contract.render_contract(contract)
            _state._last_jev_status = "ok (jev fallback)" if judged.get("_doga_provider") == "jev_fallback" else "ok"
        except Exception as exc:
            _state._last_jev_status = "error"
            logger.warning("DOGA %s contract failed (%s); using standard guidance", _state.decision_mode, type(exc).__name__)
    return {"context": guide}


def _on_transform_llm_output(
    response_text: str = "",
    **_: Any,
) -> Optional[str]:
    """Format the LLM output: simulation summary + final answer."""
    if not _state.enabled or not response_text:
        return None

    # Strip guide blocks first so goal type detection is accurate
    cleaned_for_goal = re.sub(
        r"\[world_model_guide\].*?\[/world_model_guide\]",
        "",
        response_text,
        flags=re.DOTALL,
    ).strip()

    # Save detected goal pattern to Mnemosyne if available
    if MNEMOSYNE_AVAILABLE and _state.memory_enabled and _state._current_user_message:
        try:
            m = re.search(
                r"<world_model>.*?(Information|Understanding|Action)",
                cleaned_for_goal,
                re.DOTALL | re.IGNORECASE,
            )
            goal_type = m.group(1).lower() if m else "unknown"
            remember(
                content=_state._current_user_message,
                importance=0.7,
                source="doga_goal",
                metadata={"goal_type": goal_type, "depth": _state.depth},
            )
        except Exception:
            logger.debug("doga memory save failed", exc_info=True)

    formatted = output_formatter.format_response(
        response_text,
        show_simulation=_state.show_simulation,
        active_hats=_state._active_hats,
    )
    return formatted if formatted != response_text else None


def _on_post_tool_call(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    result: Any = None,
    **_: Any,
) -> None:
    """Track tool usage for logging and recursion state."""
    if tool_name == "simulate" and isinstance(result, str):
        logger.debug(
            "doga simulate tool called with args=%s, result_len=%d",
            args,
            len(result),
        )
    elif tool_name == "reason_deeper" and isinstance(args, dict):
        _state._recursion_depth += 1
        _state._reasoning_stack.append({
            "level": _state._recursion_depth,
            "focus": args.get("focus", ""),
        })
        logger.debug(
            "doga reason_deeper called (level %d/%d, focus=%s)",
            _state._recursion_depth,
            _state.max_recursion,
            args.get("focus", ""),
        )

# ---------------------------------------------------------------------------
# Tool: simulate
# ---------------------------------------------------------------------------

_SIMULATE_SCHEMA = {
    "name": "simulate",
    "description": (
        "Run a Monte Carlo simulation over probabilistic scenarios. "
        "Provide scenarios with variable probabilities and optional logical conditions. "
        "Returns probability distribution and uncertainty metrics. "
        "Use this when you need to quantitatively weigh multiple uncertain factors."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "scenarios": {
                "type": "array",
                "description": "List of scenarios to simulate.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Short label for this scenario (e.g. 'contract_valid').",
                        },
                        "variables": {
                            "type": "object",
                            "description": (
                                "Variable name → probability (0.0–1.0). "
                                "Each variable represents an independent binary factor. "
                                "Example: {\"signature_ok\": 0.8, \"duress\": 0.1}"
                            ),
                            "additionalProperties": {"type": "number"},
                        },
                        "conditions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Optional Python boolean expressions that must be True "
                                "for this scenario to match. Variables use Python identifiers. "
                                'Example: ["signature_ok and not duress"]'
                            ),
                        },
                    },
                    "required": ["name", "variables"],
                },
            },
            "n_iterations": {
                "type": "integer",
                "description": "Number of Monte Carlo iterations (default 10000, max 50000).",
                "default": 10000,
                "minimum": 100,
                "maximum": 50000,
            },
        },
        "required": ["scenarios"],
    },
}


def _simulate_tool_handler(args: Any, **kwargs: Any) -> str:
    """Handle the simulate tool call."""
    if _state._stop_sent:
        return json.dumps({"stop": True, "message": "Recursion limit reached. Produce your final answer."})
    if not isinstance(args, dict):
        try:
            args = json.loads(args) if isinstance(args, str) else {}
        except (json.JSONDecodeError, TypeError):
            return json.dumps({"error": "Invalid tool arguments: expected JSON object."})
    scenarios = args.get("scenarios", [])
    n_iterations = min(args.get("n_iterations", 10000), 50000)

    # Cap scenarios
    if len(scenarios) > _state.max_scenarios:
        scenarios = scenarios[:_state.max_scenarios]

    if not scenarios:
        return json.dumps({"error": "No scenarios provided."})

    try:
        result = simulation_engine.run_scenarios(
            scenarios,
            n_iterations=n_iterations,
        )
        return json.dumps(result)
    except Exception as exc:
        logger.warning("simulate tool failed: %s", exc)
        return json.dumps({
            "error": f"Simulation failed: {exc}",
            "scenarios": [],
            "summary": {"total_iterations": 0},
        })


def _check_simulate_requirements() -> bool:
    """simulate tool has no special requirements — always available."""
    return True

# ---------------------------------------------------------------------------
# Tool: reason_deeper (Phase 3 — recursive reasoning)
# ---------------------------------------------------------------------------

_REASON_DEEPER_SCHEMA = {
    "name": "reason_deeper",
    "description": (
        "Recursively deepen your own reasoning. Call this after your initial "
        "<world_model> analysis to critique and refine your thinking. "
        "Each call asks you to examine what you missed, consider edge cases, "
        "and produce a deeper analysis. Stop calling when analysis is thorough."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "focus": {
                "type": "string",
                "description": (
                    "What specific aspect to dig deeper into. "
                    "e.g., 'black hat risk cascade', 'yellow hat assumption', "
                    "'green hat alternative we overlooked'"
                ),
            },
        },
        "required": ["focus"],
    },
}


def _reason_deeper_handler(args: Any, **kwargs: Any) -> str:
    """Handle reason_deeper tool call — triggers next recursion level."""
    if not isinstance(args, dict):
        try:
            args = json.loads(args) if isinstance(args, str) else {}
        except (json.JSONDecodeError, TypeError):
            return json.dumps({"error": "Invalid tool arguments."})

    focus = args.get("focus", "general")
    current_level = _state._recursion_depth

    if current_level >= _state.max_recursion or _state._stop_sent:
        _state._stop_sent = True
        _state._stop_count += 1
        _state._reasoning_stack.clear()
        if _state._stop_count >= 3:
            return json.dumps({
                "stop": True,
                "hard_break": True,
                "message": (
                    f"Maximum recursion depth ({_state.max_recursion}) reached. "
                    "Produce your final synthesized answer now."
                ),
            })
        return json.dumps({
            "stop": True,
            "message": (
                f"Maximum recursion depth ({_state.max_recursion}) reached. "
                "Produce your final synthesized answer now."
            ),
        })

    next_level = current_level + 1
    recursion_hats = de_bono_hats.hats_for_recursion_level(next_level)

    hat_lines = "\n".join(f"  [{h.upper()}] {de_bono_hats._HAT_DEFS[h]}"
                         for h in recursion_hats)

    instruction = (
        f"--- RECURSION LEVEL {next_level} ---\n"
        f"Focus: {focus}\n\n"
        "Critique your previous analysis. What did you miss?\n"
        "Consider edge cases, hidden assumptions, and alternative interpretations.\n"
        f"Use these parallel thinking lenses:\n"
        f"{hat_lines}\n\n"
        f"Produce a new <world_model> section for Level {next_level}.\n"
        "When done, call `reason_deeper` again to go deeper, "
        "or produce your final answer if the analysis is thorough."
    )

    return json.dumps({"continue": True, "instruction": instruction})


def _check_reason_deeper_requirements() -> bool:
    """reason_deeper has no special requirements."""
    return True

# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

_DOGA_HELP = """\
/doga — probabilistic DOGA thinking controller

Subcommands:
  on                   Enable DOGA thinking (default)
  off                  Disable DOGA thinking
  status               Show current settings
  auto                 Automatic depth (default — decides low/medium/high per query)
  manual low|medium|high  Force a specific thinking level
  depth <1-5>          Set depth manually (switches to manual mode)
  hats on              Enable De Bono Six Thinking Hats (default)
  hats off             Disable De Bono parallel thinking lenses
  max_recursion <1-5>  Max recursion depth for reason_deeper tool (default: 3)
  show                 Show simulation panel in responses
  hide                 Hide simulation panel (only final answer)
  memory on           Enable Mnemosyne goal memory (requires pip install mnemosyne-memory)
  memory off          Disable Mnemosyne goal memory
  jev on|off          Legacy alias: enable or disable response contracts
  mode jev_api|clef_api|laya_local|laya_with_jev_fallback  Select one classifier route
  provider jev|clef|laya   Legacy alias: select Jev, Cloudflare Clef, or local-only Laya
  fallback on|off     Legacy alias: change the Laya error fallback

Current state: {state}
"""


def _handle_doga(raw_args: str) -> Optional[str]:
    """Handle /doga slash command."""
    argv = raw_args.strip().split()
    if not argv or argv[0] in {"help", "-h", "--help"}:
        return _DOGA_HELP.format(state=_state.to_dict())

    sub = argv[0].lower()

    if sub == "on":
        _state.enabled = True
        return "DOGA thinking enabled."

    if sub == "off":
        _state.enabled = False
        return "DOGA thinking disabled."

    if sub == "status":
        mem_status = "available" if MNEMOSYNE_AVAILABLE else "not installed"
        if _state.auto_depth:
            mode = f"auto (complexity: {_state._last_complexity})"
        else:
            mode = f"manual (depth: {_state.depth}/5)"
        hat_status = "enabled" if _state.de_bono_enabled else "disabled"
        return (
            "DOGA status:\n"
            f"  Enabled: {_state.enabled}\n"
            f"  Mode: {mode}\n"
            f"  Response contracts: {'enabled' if _state.jev_enabled else 'disabled'} (mode: {_state.decision_mode}, last: {_state._last_jev_status})\n"
            f"  Show simulation: {_state.show_simulation}\n"
            f"  Max scenarios: {_state.max_scenarios}\n"
            f"  De Bono hats: {hat_status}\n"
            f"  Max recursion: {_state.max_recursion}\n"
            f"  Memory: {_state.memory_enabled} ({mem_status})"
        )

    if sub == "auto":
        _state.auto_depth = True
        return f"DOGA set to auto mode (complexity: {_state._last_complexity})."

    if sub == "manual":
        if len(argv) < 2:
            return "Usage: /doga manual low|medium|high"
        level = argv[1].lower()
        mapping = {"low": 1, "medium": 3, "high": 5}
        if level not in mapping:
            return "Level must be low, medium, or high."
        _state.auto_depth = False
        _state.depth = mapping[level]
        return f"DOGA set to manual {level} (depth: {_state.depth}/5)."

    if sub == "depth":
        if len(argv) < 2:
            return f"Current depth: {_state.depth}/5\nUsage: /doga depth <1-5>"
        try:
            d = int(argv[1])
            if d < 1 or d > 5:
                return "Depth must be between 1 and 5."
            _state.auto_depth = False
            _state.depth = d
            return f"DOGA depth set to {d}/5 (manual mode)."
        except ValueError:
            return "Invalid number. Use /doga depth <1-5>."

    if sub == "show":
        _state.show_simulation = True
        return "DOGA simulation panel will be shown in responses."

    if sub == "hide":
        _state.show_simulation = False
        return "DOGA simulation panel hidden. Only final answer will be shown."

    if sub == "hats":
        if len(argv) < 2:
            return f"De Bono hats: {'enabled' if _state.de_bono_enabled else 'disabled'}\nUsage: /doga hats on|off"
        h = argv[1].lower()
        if h == "on":
            _state.de_bono_enabled = True
            return "De Bono parallel thinking hats enabled."
        elif h == "off":
            _state.de_bono_enabled = False
            _state._active_hats = []
            return "De Bono parallel thinking hats disabled."
        return "Usage: /doga hats on|off"

    if sub == "max_recursion":
        if len(argv) < 2:
            return f"Current max recursion: {_state.max_recursion}\nUsage: /doga max_recursion <1-5>"
        try:
            r = int(argv[1])
            if r < 1 or r > 5:
                return "Max recursion must be between 1 and 5."
            _state.max_recursion = r
            return f"DOGA max recursion set to {r}."
        except ValueError:
            return "Invalid number. Use /doga max_recursion <1-5>."

    if sub == "mode":
        modes = {
            "jev_api": ("jev", False),
            "clef_api": ("clef", False),
            "laya_local": ("laya", False),
            "laya_with_jev_fallback": ("laya", True),
        }
        if len(argv) != 2 or argv[1].lower() not in modes:
            return "Usage: /doga mode jev_api|clef_api|laya_local|laya_with_jev_fallback"
        _state.decision_provider, _state.jev_fallback = modes[argv[1].lower()]
        _state._last_jev_status = "enabled"
        return f"DOGA response contract mode: {_state.decision_mode}."

    if sub == "provider":
        if len(argv) != 2 or argv[1].lower() not in {"jev", "clef", "laya"}:
            return "Usage: /doga provider jev|clef|laya"
        _state.decision_provider = argv[1].lower()
        _state.jev_fallback = False
        _state._last_jev_status = "enabled"
        return f"DOGA response contract mode: {_state.decision_mode}."

    if sub == "fallback":
        if len(argv) != 2 or argv[1].lower() not in {"on", "off"}:
            return "Usage: /doga fallback on|off"
        if _state.decision_provider != "laya":
            return "Jev fallback is only available with Laya. Select /doga mode laya_with_jev_fallback."
        _state.jev_fallback = argv[1].lower() == "on"
        return f"DOGA response contract mode: {_state.decision_mode}."

    if sub == "jev":
        if len(argv) < 2:
            return f"Jev: {'enabled' if _state.jev_enabled else 'disabled'}\nUsage: /doga jev on|off"
        setting = argv[1].lower()
        if setting == "on":
            _state.jev_enabled = True
            _state._last_jev_status = "enabled"
            return "DOGA response contracts enabled."
        if setting == "off":
            _state.jev_enabled = False
            _state._last_jev_status = "disabled"
            return "DOGA response contracts disabled."
        return "Usage: /doga jev on|off"

    if sub == "memory":
        if len(argv) < 2:
            return f"Memory: {'enabled' if _state.memory_enabled else 'disabled'} ({'available' if MNEMOSYNE_AVAILABLE else 'not installed'})\nUsage: /doga memory on|off"
        m = argv[1].lower()
        if m == "on":
            if not MNEMOSYNE_AVAILABLE:
                return "Mnemosyne is not installed. Run: pip install mnemosyne-memory"
            _state.memory_enabled = True
            return "DOGA goal memory enabled."
        elif m == "off":
            _state.memory_enabled = False
            return "DOGA goal memory disabled."
        return "Usage: /doga memory on|off"

    return f"Unknown subcommand: {sub}\n\n{_DOGA_HELP.format(state=_state.to_dict())}"

# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("transform_llm_output", _on_transform_llm_output)
    ctx.register_hook("post_tool_call", _on_post_tool_call)

    ctx.register_tool(
        name="simulate",
        toolset="doga",
        schema=_SIMULATE_SCHEMA,
        handler=_simulate_tool_handler,
        check_fn=_check_simulate_requirements,
        description="Run Monte Carlo probability simulations over scenarios.",
        emoji="🎲",
    )

    ctx.register_tool(
        name="reason_deeper",
        toolset="doga",
        schema=_REASON_DEEPER_SCHEMA,
        handler=_reason_deeper_handler,
        check_fn=_check_reason_deeper_requirements,
        description="Recursively deepen reasoning via self-critique.",
        emoji="🔄",
    )

    ctx.register_command(
        "doga",
        handler=_handle_doga,
        description="Control DOGA probabilistic thinking.",
        args_hint="on|off|status|mode jev_api|laya_local|laya_with_jev_fallback|auto|manual low|medium|high|depth <1-5>|hats on|off|max_recursion <1-5>|show|hide|memory on|off|jev on|off",
    )
