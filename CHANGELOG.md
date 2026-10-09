# Changelog
## v1.5.0 (2026-10-09)

Two ways the plugin failed hard on ordinary bad input. Full notes: [RELEASE_NOTES_v1.5.0.md](RELEASE_NOTES_v1.5.0.md).

### Fixed

- **The world-model regex took 18.7 seconds on one malformed reply.** `<world_model>\s*(.*?)\s*</world_model>` backtracked on an unclosed tag: 400 sp = 39 ms, 800 = 310 ms, 1600 = 2.42 s, 3200 = 18.7 s, eight for one doubling. This ran on every assistant response, so a truncated reply was an 18-second hang. Both `\s*` are dropped; the captured text is unchanged because the caller already strips. `_FALLBACK_WM_RE` was measured and left alone — it costs 0.01 ms on the same inputs.
- **`DOGA_DECISION_MODE=auto` with an unusable local model broke `import doga`.** `_local_model_usable()` called `local_model()` unguarded, and `_apply_mode` catches only `ModeError`, so an empty or ill-formed `DOGA_LOCAL_MODEL` raised `RuntimeError` at module scope and took every slash command with it. A malformed model name now answers `False`, so `auto` resolves to the hosted route and the plugin loads. The degradation is logged rather than swallowed, which is the stated trade-off: quiet beats unloadable.

### Added

- `tests/test_hard_input_regressions.py`, 15 tests pinning both fixes: the quadratic shape rather than only a ceiling, captured-text equivalence on three shapes including interior newlines, extraction on zero/one/two blocks, the unclosed-tag fallback keeping its content, the import surviving four malformed model values, the warning naming the variable, and two guards against over-correction.


## v1.4.1 (2026-10-04)

### Added
- The README records this repository's GitHub topic tags so the tags and the documentation agree: `clef`, `cloudflare`, `jev`, `laya`, `system-one`, `decision-model`.
- `DOGA_LOCAL_MODEL` selects which local System One decision model answers, replacing a hard-coded binding to one engine. It defaults to `convaiinnovations/laya`, so the default path is byte-for-byte unchanged, and it is the checkpoint name sent to the local server. Switching engines is a configuration change: there is no new provider name and no code change. The value is **not** checked against an allowlist, because a new local model must work by configuration alone; only a value that could not be used safely is rejected, namely an empty or whitespace-only value, a value containing whitespace, quotes, a backslash, a control character, or a URL separator, and a `.` or `..` path segment. An unset variable takes the default, while a variable that is set but empty is an error rather than a silent default.
- `tests/test_modes.py` covers the mode contract and the local slot in 134 tests: the four canonical modes and their provider orders, every alias in the table below including its routing decision, that no alias reaches observable output, per-mode call and non-reroute behaviour, `local_model` selecting the engine with the default unchanged, rejected values, engine reload on a name change, single load per name, and log hygiene on a chain failure.

### Changed
- The mode vocabulary is now four canonical names that say which side leads and whether the other is behind it: `api_with_local_fallback`, `api_only`, `local_only`, and `local_with_api_fallback`. `api_only` and `local_only` are single-provider routes whose failures are reported and never rerouted; the two fallback modes are two-provider chains that use the existing cooldown, trigger, and breaker machinery unchanged.
- Mode resolution moved to a single table in `doga.response_contract`, exposed as `resolve_mode`. An alias resolves to its canonical name before it reaches a chain, a log line, a status line, or a URL, so no alias string is observable. Names stay case-insensitive, and an unknown name raises an error naming every accepted spelling.
- The plugin's mode state is derived from the resolved mode rather than stored as a separate provider plus flag, so the two can no longer disagree. `decision_provider` and `jev_fallback` are kept as properties and still mean what they meant, including for direct assignment.
- Help text, the `/doga mode` usage error, the registered command's `args_hint`, `/doga status`, and the failure log line all name the four canonical modes and the full alias list. `/doga status` also reports the configured local engine.
- The local error messages name the slot rather than one engine, and the chain log line says "hosted fallback" rather than "Jev fallback", because the hosted side is configurable.

### Kept working
- Every mode name this plugin has accepted before still selects the same routing decision: `auto` resolves to `api_with_local_fallback` when a usable local model is present and `api_only` otherwise; `jev_api`, `typesafe`, and `openrouter` resolve to `api_only` on Jev; `clef_api` and `clef` resolve to `api_only` on Clef; `laya` and `laya_local` resolve to `local_only`; `laya_then_hosted` and `laya_with_jev_fallback` resolve to `local_with_api_fallback`. `clef_with_local_fallback` was added as an alias for `api_with_local_fallback` on Clef. The bare `DOGA_DECISION_PROVIDER` values `jev`, `clef`, and `laya` keep working on their own.
- `DOGA_DECISION_MODE` still takes precedence over `DOGA_DECISION_PROVIDER` and `DOGA_LAYA_JEV_FALLBACK`, and the default configuration is still Jev with no fallback, which is now named `api_only`. The legacy `/doga provider jev|clef|laya`, `/doga fallback on|off`, and `/doga jev on|off` commands all still work, and selecting a legacy provider still resets the fallback.
- A hosted failure is still never rerouted to a *different* hosted provider: `api_with_local_fallback` tries the hosted route and then the local slot, never Jev on top of Clef. Clef is still not wired into the local error fallback.
- The per-model Clef checkpoint setting `DOGA_CLEF_MODEL`, its two allowed values, the Cloudflare endpoint, and both Clef credential checks are untouched.

### Tests updated, and why
- Existing assertions that named a mode string in status, help, or a command reply now assert the canonical name the alias denotes, because an alias must not reach observable output. Each of those tests still asserts the alias was accepted and that the correct provider leads, so the compatibility claim is still covered rather than dropped.
- Three call-shape assertions on `evaluate_contract` gained the new `mode` and `hosted` arguments. The mode is passed rather than inferred because the legacy provider plus fallback pair cannot express `api_with_local_fallback`: both API-led modes lead with the same provider and differ only in what sits behind them.
- The Laya test fixture now also clears `DOGA_LOCAL_MODEL`, since the engine name is read from the environment on every call. Without that, an ambient value changed which engine those tests loaded. This is the same class of trap fixed for `DOGA_DECISION_MODE` in v1.4.0.

### Deliberate limits
- `local_model` is a new setting and no existing setting was removed or had its meaning changed. It never appears in a fallback order and is never a mode alias.
- Naming the category was a documentation-only change. No mode, setting, provider route, help string, or resolution rule changed with it, so the test count is unchanged at 307.
- Membership of the System One decision model category and the shared `/v1/systemone` wire contract are documented claims from those projects, not measurements made here. No live call was made to any provider, and no local model other than the default has been called live.
- No release candidate was cut; this is the next minor version, as agreed for these repositories.

### Verification
- `python -m pytest tests -q` reports **307 passed**, up from 172. The suite was also run with `DOGA_DECISION_MODE` set to `local_with_api_fallback`, `clef_api`, and `api_only`, and reported 307 passed in every case. Each test file also passes in isolation.
- **No live call was made to any provider as part of this work.** Every test uses a fake module object, a monkeypatched function, or a socket fixture that fails loudly. No local model other than the default has been called live, and no credential available on this machine is authorized for Cloudflare Workers AI, so the standing limitation from v1.4.0 is unchanged: all Clef behaviour is mocked and the first real call should be treated as unverified. The only verified live behaviour remains what the earlier changelog entries record.

---

## v1.4.0 (2026-10-04)

### Added
- Cloudflare Clef as a fourth response-contract route, `clef_api`, alongside `jev_api`, `laya_local`, and `laya_with_jev_fallback`. Select `/doga mode clef_api` in the current process or set `DOGA_DECISION_MODE=clef_api` at startup; the legacy `/doga provider clef` alias also works.
- Clef is called on Cloudflare Workers AI's account endpoint, `POST https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/run/@cf/cloudflare/clef`, in one call per request. No Cloudflare Worker, GPU, or self-hosted deployment is needed, because Cloudflare serves the model. Serving it yourself from Cloudflare's published Apache 2.0 weights is possible and deliberately out of scope for this release.
- Two required environment values: `CLOUDFLARE_ACCOUNT_ID`, which selects the account and is configuration rather than a secret, and `CLOUDFLARE_API_TOKEN`, a bearer token with the Account > Workers AI > Read permission. Both are checked before any request, and the error names the missing variable.
- `DOGA_CLEF_MODEL` selects the checkpoint, `clef` (27B, the default) or `clef-flash` (9B). An unknown value fails rather than silently calling a checkpoint that does not exist.
- `tests/test_clef_integration.py` covers the route in 19 tests: Cloudflare as the only destination, both response envelopes, Cloudflare error codes, missing-credential failures before any socket work, the flash checkpoint, typed answer validation, contract injection through the real hook, and log hygiene.

### Changed
- Both `clef` and `clef-flash` response shapes are accepted, the bare model output and Cloudflare's `success`/`result` REST envelope, preferring top-level answers. A `success: false` envelope raises with Cloudflare's own numeric error codes instead of a generic parse failure.
- Clef answers are validated against the same criteria Jev's are: every question must be answered, a `choice` must be one of that question's allowed options, and a `noul` probability must be within 0 to 1.
- Status and help list the fourth mode, and the README documents the checkpoint choice, both required environment values, the privacy boundary, the billing model, and the failure behaviour.
- Two Laya tests that read the legacy provider environment variable now unset `DOGA_DECISION_MODE` first. They were relying on the caller's environment not already setting the newer, higher-precedence variable, which made them fail on any machine where DOGA is configured. One Laya test's help-text assertion was widened to the new provider list.

### Deliberate limits
- Clef has no fallback route. A Clef failure leaves DOGA on its ordinary guidance and never reaches Jev or Laya, and Clef is not wired into the Laya error fallback, which stays Jev-only. A classifier failure is not treated as permission to silently ask a different classifier.
- The 0.7 ambiguity threshold is shared across all three classifiers and has not been calibrated between them. Clef's agreement with authored labels has not been measured at all.
- Live Clef verification was not performed. No Cloudflare credential available on the release machine is authorized for Workers AI, so every candidate token returned HTTP 401 from `api.cloudflare.com`. All Clef tests use mocked responses. The wire format follows Cloudflare's published model documentation; treat the first real call as unverified until one succeeds.
- Selecting `clef_api` sends every classified request to Cloudflare, which bills Workers AI usage. Jev and Clef are both remote; only `laya_local` and a healthy `laya_with_jev_fallback` keep classification local.

### Verification
- `python -m pytest tests -q` reports 172 passed, run both with and without `DOGA_DECISION_MODE` set in the environment. Wheel and source distribution build. The installed plugin was not reloaded and the running gateway was not restarted, so the gateway still uses the v1.3.0 configuration.

---

## v1.3.0 (2026-09-26)

### Added
- One explicit response-contract mode selector: `jev_api`, `laya_local`, or `laya_with_jev_fallback`. Set `DOGA_DECISION_MODE` at startup or select `/doga mode` in the current process. Existing provider and fallback environment variables and commands remain as compatibility aliases; the new environment setting takes precedence.
- Local failure categories are logged without the request text. After three consecutive Laya failures, further Jev fallback is suppressed in that process until Laya succeeds; ordinary DOGA guidance remains available.
- Reproducible 100-question, three-mode benchmark report and sanitized per-case results under `docs/benchmarks/`.

### Changed
- Status and help report the effective mode instead of implying that Laya is an additional provider in Jev's route. The legacy `/doga jev on|off` still toggles response contracts regardless of mode. Selecting a legacy provider resets fallback, and enabling fallback while Jev is selected is rejected.
- GitHub metadata and README badges now point to this fork while the README continues to attribute upstream DOGA.

### Evaluation and limitations
- On 100 authored, subjective labels, Jev agreed on goal 88, mode 68, stakes 67, scenario need 70, and high/low ambiguity 87. Laya agreed on 56, 41, 37, 59, and 67 respectively. Both injected contracts in all 100 matched cases; healthy Laya fallback mode was identical to local-only and made no Jev provider request. Laya detected none of the 30 high-ambiguity authored labels at the existing 0.7 threshold. Keep Jev as the recommended default, and do not treat this as a final-answer quality study.
- The fallback circuit breaker bounds repeated remote egress after local errors, but it cannot detect a valid yet incorrect Laya judgment. Laya's checkpoint confidence remains uncalibrated. No threshold was tuned on the benchmark set.

### Verification
- Local test suite, package build, fresh Hermes plugin doctor, and representative hook probes: see the published release verification record. The running gateway is not activated by installing plugin files; a later restart is required.

---

## v1.2.0 (2026-09-26)

### Added
- Optional local Laya classifier for the existing five-facet DOGA response contract. Select it for the current process with `/doga provider laya`, or start Hermes with `DOGA_DECISION_PROVIDER=laya` for a persistent choice. Jev remains the default.
- Explicit opt-in Jev fallback when local Laya errors. Use `/doga fallback on` for this process or `DOGA_LAYA_JEV_FALLBACK=1` at process startup. Jev then tries OpenRouter first and direct TypeSafe if necessary. With fallback off, a local error never sends the request remotely.
- `laya` optional Python dependency group. The model loads once per process and its predictions are serialized to prevent concurrent model calls.

### Changed
- The selected classifier now supplies typed judgments to the same contract builder and ambiguity handling. `/doga jev on|off` continues to toggle response contracts for backward compatibility, including when Laya is selected.
- Healthy local Laya never calls Jev. If Laya errors, DOGA either tries Jev when explicitly opted in or retains ordinary guidance without a typed contract. A failed Jev fallback also retains ordinary guidance.

### Limitations
- The first local load can download model weights from Hugging Face. Cache them before requiring offline operation. The main Hermes model and other plugins have separate network behavior.
- Laya's classification accuracy and its ambiguity probability on DOGA's questions are not calibrated against Jev; the shared 0.7 threshold is a starting behavior, not a validated decision threshold.
- A local checkpoint smoke test emitted a Laya runtime warning about invalid saved choice temperatures; Laya clamped them. Treat affected confidence values as uncalibrated until the checkpoint is recalibrated.

### Verification
- Full local suite: 144 passed. Wheel and source distribution built.
- With the Laya checkpoint cached and `HF_HUB_OFFLINE=1`, a local five-facet prediction and a Hermes `pre_llm_call` hook both returned a contract without invoking Jev's provider path.

---

## v1.1.1 (2026-09-25)

### Fixed
- Preserve Jev's high ambiguity signal when its selected response mode is not `clarify`. DOGA now requires a conditional answer that states material assumptions and identifies missing information that could change the answer.
- Keep the focused clarification path when Jev selects `clarify` and the ambiguity score is at least 0.7.
- Correct setuptools' build backend and package discovery so the DOGA Python distribution builds without treating the `assets` directory as a package.

### Tests
- Add regression coverage for the conflicting `recommend` plus high ambiguity result, explicit clarification, and low ambiguity recommendation.

### Verification
- Full test suite: 133 passed.
- Python compilation, version consistency, and `git diff --check` passed.
- Wheel build passed.

---

## v1.1.0 (2026-05-24)

### Features
- De Bono Six Thinking Hats — 5 structured thinking lenses, depth-aware
- Recursive reasoning via `reason_deeper` tool with per-level hat rotation
- Hard-break safety mechanism — 3 ignored stop signals terminate the tool loop
- Auto depth — complexity-based depth selection (pure Python, 0 LLM tokens)
- Mnemosyne memory integration (optional, `pip install doga-hermes[memory]`)
- Content swallowing prevention — unclosed `<world_model>` tags no longer hide content

### Fixes
- `ast.Load` missing from AST whitelist — ALL condition expressions silently returned False
- `_default_engine` thread safety — fresh `MonteCarloEngine` per call
- Double-checked locking removed — always-lock pattern for `_ConditionCache`
- `RecursionError` catch in `_compile` — deep nested parentheses no longer crash
- `_simulate_tool_handler` now respects `_stop_sent` — simulate bypass vector closed
- `assess_complexity` type guard — non-string input safely returns `"low"`
- Test state contamination — `setup_method` → `@pytest.fixture(autouse=True)`

### Chores
- Repo restructured: source files moved into `doga/` subdirectory
- `plugin.yaml` added (Hermes best practice)
- 116 tests across 7 modules, 0 failures
- CI workflow added (GitHub Actions, Python 3.10–3.12)

---

## v1.0.0 (2026-05-22)

Initial release.
- Plugin registration with 3 Hermes hooks
- Monte Carlo simulation engine with AST whitelist safety
- Goal detection (Information / Understanding / Action)
- Thinking panel formatting
- `/doga` slash commands
