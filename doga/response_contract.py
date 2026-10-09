"""Jev, Cloudflare Clef, or a local System One decision model, and DOGA contracts.

Both sides are members of the System One decision model category, also written
typed decision model, coined by TypeSafe on 15 September 2026:
https://systemonemodels.org/guides/what-is-a-system-one-model/ Jev is one
vendor's member of that category, not the category itself. Membership and the
shared wire contract are claims from those projects, not measurements made here.

Two sides answer DOGA's five typed questions, and four canonical modes say
which side leads and whether the other one is behind it as a fallback:

=================  ==================  ==========
Mode               Leads               Fallback
=================  ==================  ==========
api_only           the hosted API      none
api_with_local_... the hosted API      the local model
local_only         the local model     none
local_with_...     the local model     the hosted API
=================  ==================  ==========

The hosted side is one of:

- ``jev``, hosted through OpenRouter first and direct TypeSafe second.
- ``clef``, hosted at Cloudflare Workers AI. It needs an account ID plus an API
  token with the Workers AI read permission. The account ID is configuration,
  the token is a credential; neither is read from DOGA config.

The local side is a generic decision-model slot whose configuration name stays
``laya``. ``DOGA_LOCAL_MODEL`` selects which local model answers, so swapping
engines is configuration rather than a code change, and the default path is
unchanged. The slot runs in the Hermes process with no credential and no network
egress of its own.

Both hosted providers are remote, so any mode that leads with one sends the
request off the machine. A mode that leads with the local model does not, and
sends the request off the machine only when the local attempt raises and the
fallback breaker still allows it.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
from typing import Any, Callable, NamedTuple

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
OPENROUTER_API_URL = "https://openrouter.ai/api/alpha/decisions"
OPENROUTER_MODEL = "typesafe/jev-1.13"
LAYA_MODEL = "convaiinnovations/laya"
LOCAL_MODEL_ENV = "DOGA_LOCAL_MODEL"
# A local engine name is either a bare engine id or a Hugging Face style
# ``namespace/name``. Both are accepted verbatim and never mapped, because the
# point of the slot is that an unlisted engine works without a code change.
# Anything outside this shape is rejected: a name carrying whitespace, quotes, a
# backslash, or a control character would not be a usable engine identifier, and
# the value is split on ``/`` before it is handed to the runtime, so a stray
# separator would turn one name into two. Dots are allowed, because real engine
# ids carry them, such as ``jeff-qwen3.5-0.8b``.
_LOCAL_MODEL_PATTERN = re.compile(r"\A[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*\Z")
# Clef is Cloudflare-hosted. The endpoint is per account, so the account ID is a
# required part of the URL rather than an optional setting.
CLEF_RUN_PATH = "/ai/run/@cf/cloudflare/{model}"
CLEF_API_BASE = "https://api.cloudflare.com/client/v4/accounts"
CLEF_DEFAULT_MODEL = "clef"
CLEF_MODELS = ("clef", "clef-flash")
CLEF_ACCOUNT_ENV = "CLOUDFLARE_ACCOUNT_ID"
CLEF_TOKEN_ENV = "CLOUDFLARE_API_TOKEN"
CLEF_MODEL_ENV = "DOGA_CLEF_MODEL"
CLEF_TIMEOUT = 15.0
PROVIDERS = ("jev", "clef", "laya")
logger = logging.getLogger(__name__)
_laya_lock = threading.RLock()
_laya_failure_lock = threading.Lock()
_laya_failure_count = 0
# The documented cooldown, in consecutive local failures. It guards both
# directions of the chain: a local-led chain stops calling the hosted side,
# and a hosted-led chain stops calling the local slot.
_LAYA_FALLBACK_FAILURE_LIMIT = 3
# The ambiguity signal at or above which a contract becomes conditional, or
# becomes a clarifying question. Named so the two comparisons in
# build_contract cannot drift apart, and so the value is changed in one place.
_CLARIFICATION_SIGNAL_THRESHOLD = 0.7
_laya_agent: Any = None
_laya_model: str | None = None
QUESTIONS = {
    "goal": {"type": "choice", "instructions": "What is the user's primary desired outcome?", "criteria": {"information": "Factual answer, analysis, or explanation.", "understanding": "Feel heard, validated, or understood.", "action": "A decision, recommendation, or next step."}},
    "mode": {"type": "choice", "instructions": "What response mode best serves the request?", "criteria": {"answer": "Give the requested direct answer or information.", "explain": "Explain concepts or implications without deciding for the user.", "recommend": "Make a recommendation or propose a concrete next action.", "clarify": "A missing fact materially changes the answer; ask one focused question."}},
    "stakes": {"type": "choice", "instructions": "How consequential is an erroneous answer or recommendation?", "criteria": {"low": "Minor, easily reversible consequence.", "medium": "Meaningful but bounded consequence.", "high": "Material legal, medical, financial, safety, or irreversible consequence."}},
    "clarification": {"type": "noul", "instructions": "Would an unresolved ambiguity materially change the useful answer?", "criteria": {"true": "A key unknown changes the answer or recommendation.", "false": "A useful answer is possible without clarification."}},
    "scenario_need": {"type": "choice", "instructions": "What level of scenario analysis is useful for this request?", "criteria": {"none": "A direct response is sufficient; scenario analysis adds noise.", "compare_options": "The request involves meaningfully different plausible options or outcomes.", "uncertainty_analysis": "Explicit uncertain factors and outcomes merit sensitivity analysis."}},
}

# ---------------------------------------------------------------------------
# Modes
#
# Four canonical names. Each resolves to a provider order expressed in concrete
# provider names, and every alias is rewritten to a canonical name before it
# reaches a chain, a log line, a diagnostic, or a URL, so no alias string is ever
# observable.
# ---------------------------------------------------------------------------

class Mode(NamedTuple):
    """One resolved mode: an ordered provider list plus what it names.

    ``providers`` uses the concrete ``PROVIDERS`` names, so the same value can
    drive both a chain and a dispatch lookup without a translation step.
    ``hosted`` and ``fallback`` say which side leads and what sits behind it.
    """

    name: str
    providers: tuple[str, ...]
    hosted: str
    fallback: str


API_ONLY = "api_only"
API_WITH_LOCAL_FALLBACK = "api_with_local_fallback"
LOCAL_ONLY = "local_only"
LOCAL_WITH_API_FALLBACK = "local_with_api_fallback"

MODES = (API_WITH_LOCAL_FALLBACK, API_ONLY, LOCAL_ONLY, LOCAL_WITH_API_FALLBACK)
MODE_DESCRIPTIONS = {
    API_WITH_LOCAL_FALLBACK: "hosted API first, local model as fallback",
    API_ONLY: "hosted API only, a failure is reported",
    LOCAL_ONLY: "local model only, a failure is reported",
    LOCAL_WITH_API_FALLBACK: "local model first, hosted API as fallback",
}
# The local slot's configuration name, and the concrete provider name for it.
LOCAL_PROVIDER = "laya"
HOSTED_PROVIDERS = ("jev", "clef")
# Which hosted provider the API side resolves to when no mode or legacy
# setting pins one. Jev is the shipped default and stays the default.
DEFAULT_HOSTED = "jev"

# The four canonical modes, parameterised by which hosted provider the API side
# resolves to. Both canonical API modes name the same chain here: this
# repository's API side is one hosted route, and a hosted failure has never been
# rerouted to another provider, so ``api_with_local_fallback`` tries the hosted
# side and then the local slot, and never a second hosted provider.
_MODE_TABLE: dict[str, Mode] = {
    API_ONLY: Mode(API_ONLY, ("hosted",), "hosted", "none"),
    API_WITH_LOCAL_FALLBACK: Mode(API_WITH_LOCAL_FALLBACK, ("hosted", "laya"), "hosted", "laya"),
    LOCAL_ONLY: Mode(LOCAL_ONLY, ("laya",), "laya", "none"),
    LOCAL_WITH_API_FALLBACK: Mode(LOCAL_WITH_API_FALLBACK, ("laya", "hosted"), "laya", "hosted"),
}

# Which hosted provider each ``api_only`` alias pins. An alias naming a hosted
# provider stays on that provider, exactly as it did before this change. The
# bare ``DOGA_DECISION_PROVIDER`` spellings are here too, so that setting keeps
# working unchanged on its own.
_HOSTED_BY_API_ONLY_ALIAS = {
    "jev": "jev",
    "jev_api": "jev",
    "typesafe": "jev",
    "openrouter": "jev",
    "clef": "clef",
    "clef_api": "clef",
}

# Every accepted spelling. ``auto`` is resolved at load time rather than being a
# fixed alias, because it means "API first when a local model is usable".
MODE_ALIASES: dict[str, str] = {
    **_HOSTED_BY_API_ONLY_ALIAS,
    "auto": API_WITH_LOCAL_FALLBACK,
    "clef_with_local_fallback": API_WITH_LOCAL_FALLBACK,
    "laya": LOCAL_ONLY,
    "laya_local": LOCAL_ONLY,
    "laya_then_hosted": LOCAL_WITH_API_FALLBACK,
    "laya_with_jev_fallback": LOCAL_WITH_API_FALLBACK,
}

# Shown wherever a caller needs the accepted names, for example the error for
# an unknown mode and the ``/doga mode`` usage line.
_ACCEPTED_MODES = (
    f"{API_WITH_LOCAL_FALLBACK}, {API_ONLY}, {LOCAL_ONLY}, {LOCAL_WITH_API_FALLBACK} "
    f"(aliases: {', '.join(sorted(MODE_ALIASES))})"
)


class ModeError(ValueError):
    """An unknown mode name. The message lists every accepted spelling."""


class AnswerValidationError(RuntimeError):
    """A route answered 200 OK and its typed answers cannot be trusted.

    Distinct from a transport failure, because the two demand opposite
    responses. A transport failure is what the fallback exists for: the
    request did not reach a working model, so trying the other route is the
    point. A 200 with invalid choices means the request was already paid for
    and the answer is unusable, so the fallback would spend a second time to
    get a second answer of equally unknown provenance. Callers must let this
    escalate rather than fall through.
    """


def _local_model_usable() -> bool:
    """Whether the local slot can actually load, judged without importing it.

    ``auto`` needs this: it must pick the local side only when a local model is
    present. A missing import or an unset engine name is answered without
    importing the engine, because ``auto`` is resolved once at plugin start.

    A malformed ``DOGA_LOCAL_MODEL`` answers False too. ``local_model()`` raises
    ``RuntimeError`` for an empty or ill-formed engine id, and this call used to
    be unguarded, so ``DOGA_DECISION_MODE=auto`` with an unusable value made
    ``import doga`` raise at module scope — ``_apply_mode`` catches only
    ``ModeError`` — and every slash command with it. A bad local model name is a
    reason to route hosted; it is not a reason the plugin cannot load.

    The trade-off is deliberate and worth stating: a misconfiguration degrades
    instead of erroring, which means it is quiet. It is logged rather than
    swallowed so it is not invisible.
    """
    try:
        if not local_model().strip():
            return False
    except RuntimeError as exc:
        logger.warning(
            "local decision route unavailable: %s; %s resolves to %s",
            exc,
            "DOGA_DECISION_MODE",
            API_ONLY,
        )
        return False
    try:
        import laya  # noqa: F401
    except ImportError:
        return False
    return True


def resolve_mode(mode: str, *, hosted: str = "jev") -> Mode:
    """Resolve any accepted mode spelling to a canonical :class:`Mode`.

    Case-insensitive, and total: an unknown name raises :class:`ModeError` whose
    message names every accepted spelling. ``hosted`` is the hosted provider the
    API side resolves to, and is what keeps ``clef_api`` on Clef while
    ``jev_api`` stays on Jev.
    """
    name = str(mode or "").strip().lower()
    if not name:
        raise ModeError(f"DOGA decision mode must be one of: {_ACCEPTED_MODES}")
    if name in _MODE_TABLE:
        # Already canonical. Returned through the table anyway, so the
        # canonical name and its alias cannot drift apart.
        canonical = name
    elif name == "auto":
        canonical = API_WITH_LOCAL_FALLBACK if _local_model_usable() else API_ONLY
    elif name in _HOSTED_BY_API_ONLY_ALIAS:
        hosted = _HOSTED_BY_API_ONLY_ALIAS[name]
        canonical = API_ONLY
    else:
        resolved_alias = MODE_ALIASES.get(name)
        if resolved_alias is None:
            # The legacy DOGA_DECISION_PROVIDER value reaches this too, so the
            # message names both settings rather than only the newer one.
            raise ModeError(f"DOGA decision mode, or decision provider, must be one of: {_ACCEPTED_MODES}")
        canonical = resolved_alias
    resolved = _MODE_TABLE[canonical]
    if "hosted" not in resolved.providers:
        return resolved
    if hosted not in HOSTED_PROVIDERS:
        raise ModeError(f"DOGA hosted provider must be one of: {', '.join(HOSTED_PROVIDERS)}")
    return resolved._replace(
        providers=tuple(hosted if p == "hosted" else p for p in resolved.providers),
        fallback=hosted if resolved.fallback == "hosted" else resolved.fallback,
    )


def accepted_mode_names() -> str:
    """The accepted spellings, for help text and error messages."""
    return _ACCEPTED_MODES


def _validate_typed_answers(
    data: Any,
    questions: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    """Check a response envelope and every typed answer it carries.

    One validator for all three routes, because the wire contract is one wire
    contract. It previously existed as three near-identical copies, and they had
    already drifted: the Laya copy iterated its ``questions`` argument while the
    Clef copy reached for the module-global ``QUESTIONS``. A provider therefore
    got validation only against the question set its own caller happened to
    pass, so the shipped default route validated nothing at all.

    ``questions`` is the question set the caller asked, so a route can only ever
    be checked against what it actually sent. ``label`` names the provider in the
    message, because a bare "invalid response" does not say which route drifted.

    Rejected, because each one means the provider's answer cannot be trusted to
    mean what DOGA says it means:

    - the payload is not a JSON object, or carries no ``answers`` object;
    - a question has no answer, or the answer is not an object;
    - a choice question whose choice is not one of its own criteria;
    - a noul question whose score is a bool, is not a number, or falls outside
      0 to 1 inclusive.
    """
    if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
        raise AnswerValidationError(f"invalid {label} response: no answers object")
    answers = data["answers"]
    for name, question in questions.items():
        criteria = question.get("criteria") if isinstance(question, dict) else None
        kind = question.get("type") if isinstance(question, dict) else None
        if kind not in ("choice", "noul") or not isinstance(criteria, dict):
            # A question this validator cannot check would otherwise be skipped
            # silently, so a malformed question spec is an error rather than a
            # hole in the validation.
            raise AnswerValidationError(f"invalid {label} question set: cannot validate {name!r}")
        answer = answers.get(name)
        if not isinstance(answer, dict):
            raise AnswerValidationError(f"invalid {label} response: missing typed answer for {name!r}")
        if kind == "choice" and answer.get("choice") not in criteria:
            raise AnswerValidationError(f"invalid {label} response: unknown choice for {name!r}")
        if kind == "noul":
            score = answer.get("noul")
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1:
                raise AnswerValidationError(f"invalid {label} response: invalid probability for {name!r}")
    return data


def _request_typesafe(state: dict[str, Any], questions: dict[str, Any], api_key: str | None = None) -> dict[str, Any]:
    key = api_key or os.environ.get("TYPESAFE_API_KEY")
    if not key:
        raise RuntimeError("Jev is enabled but TYPESAFE_API_KEY is not set")
    payload = json.dumps({"state": state, "model": MODEL, "questions": questions}).encode()
    request = urllib.request.Request(API_URL, data=payload, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            data = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        # The status only. The body is an upstream error page, and an upstream
        # error page is exactly where a DSN, a signed URL, or an account
        # identifier shows up; OpenRouter and Clef already log this way and the
        # repo's own documentation claims error-type-only logging.
        raise RuntimeError(f"TypeSafe API returned HTTP {exc.code}") from exc
    # Validated against the question set that was sent, not against the module
    # global, so a partial question set cannot pass unvalidated.
    return _validate_typed_answers(data, questions, "Jev")


def _request_openrouter(state: dict[str, Any], questions: dict[str, Any], api_key: str | None = None) -> dict[str, Any]:
    key = api_key or os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    payload = json.dumps({"state": state, "model": OPENROUTER_MODEL, "questions": questions}).encode()
    request = urllib.request.Request(
        OPENROUTER_API_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://hermes-agent.nousresearch.com",
            "X-Title": "DOGA Hermes Plugin",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            data = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"OpenRouter API returned HTTP {exc.code}") from exc
    return _validate_typed_answers(data, questions, "Jev")


def _request_jev(state: dict[str, Any], questions: dict[str, Any]) -> dict[str, Any]:
    """Use OpenRouter first, then direct TypeSafe if the primary route fails.

    The second call is paid for, so what counts as "failed" is the whole
    question. Only a transport failure qualifies: the request never reached a
    model that could answer it. A 200 response whose typed answers are unusable
    is not that — it is a completed, billed call that came back untrustworthy,
    and repeating the same payload to a second vendor buys another answer of
    equally unknown provenance. Such a rejection propagates instead.
    """
    openrouter_key = os.environ.get("OPENROUTER_API_KEY")
    typesafe_key = os.environ.get("TYPESAFE_API_KEY")
    errors: list[str] = []

    if openrouter_key:
        try:
            return _request_openrouter(state, questions, api_key=openrouter_key)
        except AnswerValidationError:
            raise
        except Exception as exc:
            errors.append(f"OpenRouter request failed: {exc}")

    if typesafe_key:
        try:
            return _request_typesafe(state, questions, api_key=typesafe_key)
        except AnswerValidationError:
            raise
        except Exception as exc:
            errors.append(f"TypeSafe fallback failed: {exc}")

    if errors:
        if not typesafe_key:
            errors.append("TYPESAFE_API_KEY is not set for fallback")
        raise RuntimeError("; ".join(errors))
    raise RuntimeError("Set OPENROUTER_API_KEY or TYPESAFE_API_KEY to enable Jev")


def local_model() -> str:
    """The local engine name handed to the in-process runtime, defaulting to Laya.

    ``DOGA_LOCAL_MODEL`` is the generic slot selector. The value is passed
    through verbatim: it is not checked against a list of known models, because
    a new local engine must work without a code change. What is rejected is a
    value that could not be used safely, namely an empty or whitespace-only
    name, or one carrying a character that would corrupt the identifier it is
    split on. That is a character check rather than a name check, so it rejects
    garbage without rejecting an engine nobody has heard of.

    The only consumer is :func:`_load_local_agent`, which calls ``laya.load(name)``
    inside the Hermes process. There is no local server and no request body, so
    nothing is interpolated into a URL or into JSON: the engine name is one
    Python argument. The ``.`` and ``..`` segments are rejected because the
    value is split on ``/`` and such a segment would climb out of that split,
    not because any URL is built from it.

    An unset variable takes the default. A variable that is set but empty is an
    error rather than a silent default, so a misconfigured value surfaces
    instead of quietly answering as the default engine.

    ``LAYA_MODEL`` is the default and keeps the shipped behaviour exactly: a
    Hugging Face style id for the in-process Laya runtime.
    """
    configured = os.environ.get(LOCAL_MODEL_ENV)
    if configured is None:
        return LAYA_MODEL
    name = configured.strip()
    if not name:
        raise RuntimeError(f"{LOCAL_MODEL_ENV} is set but empty; unset it to use the default")
    if not _LOCAL_MODEL_PATTERN.match(name):
        raise RuntimeError(
            f"{LOCAL_MODEL_ENV} must be a bare engine id or a namespace/name path "
            f"using letters, digits, dot, underscore, and hyphen"
        )
    if any(segment in {".", ".."} for segment in name.split("/")):
        # Dots are legal inside a segment, so a whole '.' or '..' segment is
        # what is rejected: the value would otherwise climb out of the split it
        # is handed to as one identifier.
        raise RuntimeError(f"{LOCAL_MODEL_ENV} must not contain a '.' or '..' path segment")
    return name


def _load_local_agent(name: str):
    """Load one local engine by name.

    One agent is cached by the caller, keyed by name, so switching engines loads
    the new one instead of reusing the previous engine's agent. There is no
    second cache here on purpose: two caches would keep an agent alive after the
    configured name changed, and would hold engine objects for engines no longer
    selected.
    """
    try:
        import laya
    except ImportError as exc:
        # Named after the slot, not the default engine, because a non-default
        # engine fails the same way: the runtime that loads one loads them all.
        raise RuntimeError("The local decision model is unavailable; install doga-hermes[laya] in Hermes' Python environment") from exc
    return laya.load(name)


def _request_laya(state: dict[str, Any], questions: dict[str, Any]) -> dict[str, Any]:
    """Use one cached local model; never fall back to a network provider.

    The engine name comes from :func:`local_model`, so the slot is generic. The
    cached agent is keyed by that name: switching engines loads the new one
    rather than reusing the previous engine's agent.
    """
    global _laya_agent, _laya_model
    model = local_model()
    with _laya_lock:
        if _laya_agent is None or _laya_model != model:
            _laya_agent = _load_local_agent(model)
            _laya_model = model
        result = _laya_agent.predict(state, questions)
    return _validate_typed_answers(result, questions, "local Laya")


def _clef_checkpoint() -> str:
    """The Clef checkpoint to call: the 27B ``clef`` by default, or ``clef-flash``.

    Clef Flash is the 9B variant Cloudflare documents for latency-bound paths.
    Both are System One decision models answering the same typed questions, so
    it is a setting rather than a separate provider. An unknown value fails
    instead of silently calling a checkpoint that does not exist.
    """
    model = (os.environ.get(CLEF_MODEL_ENV) or CLEF_DEFAULT_MODEL).strip().lower()
    if model not in CLEF_MODELS:
        raise RuntimeError(f"{CLEF_MODEL_ENV} must be one of: {', '.join(CLEF_MODELS)}")
    return model


def _request_clef(state: dict[str, Any], questions: dict[str, Any]) -> dict[str, Any]:
    """Call Cloudflare Workers AI once. Never falls back to another provider.

    Both credentials are checked before any socket work, and the error names the
    missing variable rather than the value, so a misconfigured route fails on the
    first request instead of degrading into another classifier.
    """
    account = (os.environ.get(CLEF_ACCOUNT_ENV) or "").strip()
    token = (os.environ.get(CLEF_TOKEN_ENV) or "").strip()
    if not account:
        raise RuntimeError(f"Clef is selected but {CLEF_ACCOUNT_ENV} is not set")
    if not token:
        raise RuntimeError(f"Clef is selected but {CLEF_TOKEN_ENV} is not set")
    model = _clef_checkpoint()
    url = f"{CLEF_API_BASE}/{account}{CLEF_RUN_PATH.format(model=model)}"
    payload = json.dumps({"model": model, "state": state, "questions": questions}).encode()
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=CLEF_TIMEOUT) as response:
            data = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Cloudflare Workers AI returned HTTP {exc.code}") from exc
    return _clef_result(data)


def _clef_result(data: Any) -> dict[str, Any]:
    """Unwrap a Clef response and validate its typed answers.

    Cloudflare serves Clef from a REST endpoint that answers with the model
    output directly, while its general API surface wraps results in a
    ``success``/``result`` envelope. Both are accepted, top level answers first.
    A ``success: false`` envelope carries Cloudflare's own error codes, which are
    more useful than a generic parse failure, so they are surfaced as they are.
    """
    if not isinstance(data, dict):
        raise RuntimeError("Cloudflare Workers AI returned an invalid response")
    if data.get("success") is False:
        codes = [str(error.get("code")) for error in (data.get("errors") or []) if isinstance(error, dict)]
        detail = ", ".join(codes) if codes else "unknown error"
        raise RuntimeError(f"Cloudflare Workers AI request failed (code {detail})")
    answers = data.get("answers")
    if not isinstance(answers, dict):
        inner = data.get("result")
        answers = inner.get("answers") if isinstance(inner, dict) else None
    if not isinstance(answers, dict):
        raise RuntimeError("Cloudflare Workers AI returned an invalid response")
    # Validated against the module-global question set, which is what Clef is
    # always asked, rather than a caller-supplied one that could be partial.
    _validate_typed_answers({"answers": answers}, QUESTIONS, "Clef")
    return {**data, "answers": answers, "_doga_provider": "clef"}


def _request_by_provider(provider: str) -> Callable[..., dict[str, Any]]:
    """Look the provider's request function up at call time.

    Read from the module namespace on every call rather than captured in a table
    built at import time, so replacing a route, whether in a test or at
    runtime, is honoured everywhere. A captured table would silently keep
    calling the original.
    """
    if provider == LOCAL_PROVIDER:
        return _request_laya
    return {"jev": _request_jev, "clef": _request_clef}[provider]


def evaluate_contract(
    user_message: str,
    evaluator: Callable[..., dict[str, Any]] | None = None,
    provider: str = "jev",
    fallback_to_jev: bool = False,
    mode: str | Mode | None = None,
    hosted: str | None = None,
) -> dict[str, Any]:
    """Ask independent typed judgments in one call over the request only.

    ``mode`` is the canonical mode or any accepted alias and is resolved here, so
    a caller holding only the old ``provider`` plus ``fallback_to_jev`` pair
    keeps working and produces the same routing decision as before. ``provider``
    selects the hosted side when the mode does not pin one, and ``hosted``
    overrides that selection explicitly, which is what makes an API-led mode
    reach a chosen hosted provider without naming a mode alias for it.
    """
    global _laya_failure_count
    if mode is None:
        # The legacy pair, so an existing configuration routes exactly as it did.
        mode = LOCAL_WITH_API_FALLBACK if (provider == LOCAL_PROVIDER and fallback_to_jev) else provider
    # A local-led mode resolves its own hosted side, so the seed only matters
    # for the API side. Passing ``provider='laya'`` must not be mistaken for a
    # request to pin the API side to the local slot.
    seed = hosted or (provider if provider in HOSTED_PROVIDERS else DEFAULT_HOSTED)
    if seed not in HOSTED_PROVIDERS:
        raise ModeError(f"DOGA hosted provider must be one of: {', '.join(HOSTED_PROVIDERS)}")
    resolved = mode if isinstance(mode, Mode) else resolve_mode(mode, hosted=seed)
    if evaluator is None:
        if len(resolved.providers) > 1:
            return _evaluate_chain(resolved, user_message)
        evaluator = _request_by_provider(resolved.providers[0])
    state = {"user_request": user_message}
    result = evaluator(state=state, questions=QUESTIONS)
    if resolved.providers[0] == LOCAL_PROVIDER:
        with _laya_failure_lock:
            _laya_failure_count = 0
    return result


def _evaluate_chain(resolved: Mode, user_message: str) -> dict[str, Any]:
    """Run a two-provider chain, leading side first, on a configured trigger.

    The only trigger is the leading call raising. A single-provider mode has no
    chain at all, so a failure in ``api_only`` or ``local_only`` is reported and
    never rerouted. Both two-provider directions share one cooldown: after
    ``_LAYA_FALLBACK_FAILURE_LIMIT`` consecutive local failures the other
    provider is no longer called in this process until a local evaluation
    succeeds. The counter counts local failures whichever side led, so a local
    engine that is failing is not re-paid its load cost on every request.
    """
    global _laya_failure_count
    lead, backup = resolved.providers
    state = {"user_request": user_message}
    if lead == "laya":
        try:
            local = _request_by_provider("laya")(state=state, questions=QUESTIONS)
        except Exception as exc:
            logger.warning("DOGA local model failed (%s); considering hosted fallback", type(exc).__name__)
            with _laya_failure_lock:
                _laya_failure_count += 1
                allow_fallback = _laya_failure_count <= _LAYA_FALLBACK_FAILURE_LIMIT
            if not allow_fallback:
                logger.warning("DOGA hosted fallback suppressed after repeated local failures")
                raise
            remote = _request_by_provider(backup)(state=state, questions=QUESTIONS)
            return {**remote, "_doga_provider": f"{backup}_fallback"}
        with _laya_failure_lock:
            _laya_failure_count = 0
        return local
    # The API leads. It has never been rerouted to another hosted provider, so
    # the only remaining provider is the local slot. The local slot carries the
    # same cooldown as the other direction, so a failing engine is not loaded and
    # retried on every subsequent request: once the documented number of
    # consecutive local failures is reached, the slot is not called again in
    # this process.
    with _laya_failure_lock:
        # Reached means the limit has already been spent, so the slot is skipped
        # rather than paying its load cost one more time.
        local_retry_exhausted = _laya_failure_count >= _LAYA_FALLBACK_FAILURE_LIMIT
    try:
        hosted = _request_by_provider(lead)(state=state, questions=QUESTIONS)
    except Exception as exc:
        logger.warning("DOGA hosted API failed (%s); trying the local model", type(exc).__name__)
        if local_retry_exhausted:
            # The hosted failure is still the error to report. Suppressing the
            # retry must not turn a reported failure into a silent one, so the
            # local failure that would have been re-paid is not attempted. The
            # counter still advances, so a suppressed run stays visible.
            with _laya_failure_lock:
                _laya_failure_count += 1
            logger.warning(
                "DOGA local fallback suppressed after repeated local failures (count %d)",
                _laya_failure_count,
            )
            raise
        try:
            local = _request_by_provider("laya")(state=state, questions=QUESTIONS)
        except Exception:
            with _laya_failure_lock:
                _laya_failure_count += 1
            raise
        with _laya_failure_lock:
            _laya_failure_count = 0
        return {**local, "_doga_provider": "laya_fallback"}
    with _laya_failure_lock:
        _laya_failure_count = 0
    return hosted


def build_contract(response: dict[str, Any]) -> dict[str, Any]:
    """Build a contract from typed answers, falling back on anything untrusted.

    Reachable with a hand-built or legacy Jev dict as well as with a validated
    provider response, so every value is checked here rather than assumed. A
    choice outside its own criteria falls back to the safe default. An
    out-of-range or non-numeric noul score is treated as no signal at all
    rather than as a high or a low one: a score of 99 is not strong evidence of
    ambiguity, and honouring it would make a broken provider look like a
    confident one. Both Laya and Clef reject such a score at the provider edge,
    and this closes the same hole on the path they do not cover.
    """
    answers = response.get("answers", {}) if isinstance(response, dict) else {}

    def choice(name: str, allowed: set[str], fallback: str) -> str:
        answer = answers.get(name, {})
        value = answer.get("choice") if isinstance(answer, dict) else None
        return value if value in allowed else fallback

    goal = choice("goal", {"information", "understanding", "action"}, "information")
    mode = choice("mode", {"answer", "explain", "recommend", "clarify"}, "answer")
    stakes = choice("stakes", {"low", "medium", "high"}, "medium")
    scenario = choice("scenario_need", {"none", "compare_options", "uncertainty_analysis"}, "none")
    clarification_answer = answers.get("clarification", {})
    raw_clarification = (
        clarification_answer.get("noul", 0)
        if isinstance(clarification_answer, dict)
        else 0
    )
    # A probability is only a signal inside 0 to 1 inclusive. Anything else,
    # including a bool, is a malformed answer and reads as no signal.
    clarification_score = (
        raw_clarification
        if isinstance(raw_clarification, (int, float))
        and not isinstance(raw_clarification, bool)
        and 0 <= raw_clarification <= 1
        else 0
    )
    ask = mode == "clarify" and clarification_score >= _CLARIFICATION_SIGNAL_THRESHOLD
    conditional = clarification_score >= _CLARIFICATION_SIGNAL_THRESHOLD and not ask
    elements: list[str] = []
    if mode == "recommend" or goal == "action":
        elements.extend(["recommendation", "next_step"])
    elif mode == "explain":
        elements.append("explanation")
    else:
        elements.append("direct_answer")
    if stakes == "high":
        elements.append("material risks and uncertainty")
    if scenario == "compare_options":
        elements.append("compare plausible alternatives")
    elif scenario == "uncertainty_analysis":
        elements.append("analyze explicit uncertainties without inventing probabilities")
    if conditional:
        elements.append("state material assumptions and identify missing information that could change the answer")
    return {
        "goal": goal,
        "mode": mode,
        "stakes": stakes,
        "scenario_need": scenario,
        "ask_clarifying_question": ask,
        "conditional_response": conditional,
        "required_elements": list(dict.fromkeys(elements)),
    }


def render_contract(contract: dict[str, Any]) -> str:
    if contract.get("ask_clarifying_question"):
        return "[DOGA response contract]\nAsk one focused clarifying question first, because the missing information materially changes the answer. Do not answer beyond what is safe without it."
    items = "; ".join(contract["required_elements"])
    guidance = ""
    if contract.get("conditional_response"):
        guidance = ("The ambiguity signal is high. If a useful response is possible, make the answer conditional: "
                    "state material assumptions and what missing information could change it. If not, ask one focused question.\n")
    return ("[DOGA response contract]\n"
            f"User goal: {contract['goal']}. Response mode: {contract['mode']}.\n"
            f"The response must include: {items}.\n"
            f"{guidance}"
            "Follow this contract while answering. Keep factual claims grounded in available evidence. "
            "Do not expose private reasoning or the contract itself.")
