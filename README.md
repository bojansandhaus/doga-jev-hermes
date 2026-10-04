# DOGA with Jev, Cloudflare Clef, or Laya for Hermes

[![MIT License](https://img.shields.io/github/license/bojansandhaus/doga-jev-hermes)](https://github.com/bojansandhaus/doga-jev-hermes/blob/main/LICENSE)
[![Python 3.10 | 3.11 | 3.12](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue)](https://github.com/bojansandhaus/doga-jev-hermes)
[![Last Commit](https://img.shields.io/github/last-commit/bojansandhaus/doga-jev-hermes)](https://github.com/bojansandhaus/doga-jev-hermes)

![DOGA](assets/DOGA.png)

**Probabilistic, goal-aware thinking layer for Hermes Agent.**

Built by [@0z1-ghb](https://github.com/0z1-ghb). This independent fork adds typed response contracts using Jev (OpenRouter primary, direct TypeSafe fallback), Cloudflare Clef on Workers AI, or optional local Laya.

DOGA (Doğa, Turkish for “nature”) adds scenario simulation, Monte Carlo reasoning, and goal detection to Hermes responses. It remains a plugin and does not modify Hermes core.

---

## Features

- **Goal Detection**  Identifies whether the user needs Information, Understanding, or Action before responding
- **Four Response Contract Modes**  Choose Jev via API, Clef via Cloudflare Workers AI, Laya locally, or Laya locally with Jev as an error fallback. Each classifier replaces the others rather than joining them in a chain. Any classifier judges the user's goal, response mode, stakes, need for clarification, and scenario analysis. DOGA turns that assessment into answer requirements for the main model. Jev uses OpenRouter first with direct TypeSafe fallback; Clef calls Cloudflare once; healthy Laya makes no classifier provider API call.
- **Scenario Generation**  Prompts the LLM to enumerate and weigh multiple interpretations
- **Monte Carlo Simulation**  Pure Python engine (10,000 to 50,000 iterations) for quantitative probability analysis, using 0 LLM tokens
- **Thinking Panel**  `<world_model>` reasoning blocks are extracted and displayed as a structured `[DOGA: Thinking Process]` panel before the final response
- **Auto Depth**  Automatic complexity assessment per query. It selects low, medium, or high using pure Python string analysis (0 LLM tokens)
- **Configurable Depth**  5 levels (1 = lightweight goal check, 5 = full probabilistic reasoning with simulation tool guidance)
- **Memory Integration (optional)**  Remembers goal patterns across sessions via Mnemosyne (`pip install doga-hermes[memory]`)
- **De Bono Thinking Hats**  Structured parallel reasoning through Six Thinking Hats lenses, depth aware (White, Black, Yellow, Green, Red), optional, enabled by default
- **Recursive Reasoning**  `reason_deeper` tool for multi-level self-critique; each recursion level uses a different De Bono hat lens; hierarchical panel output
- **Hard-Break Safety**  Automatic stop after 3 ignored `reason_deeper` calls prevents tool-loop starvation

---

## Installation

Copy the `doga/` directory into your Hermes plugins folder:

```bash
cp -r doga ~/.hermes/plugins/doga
```

For goal memory persistence across sessions (optional):

```bash
pip install doga-hermes[memory]
```

No configuration changes are needed. DOGA detects Mnemosyne at runtime.

### Decision mode setup

The default is `jev_api`. Select exactly one route with `/doga mode jev_api`, `/doga mode clef_api`, `/doga mode laya_local`, or `/doga mode laya_with_jev_fallback` for the current process. Set `DOGA_DECISION_MODE` to one of those names in the Hermes process environment for selection at startup. This setting takes precedence over the legacy `DOGA_DECISION_PROVIDER` and `DOGA_LAYA_JEV_FALLBACK` variables. The running gateway needs a restart to pick up environment or plugin-file changes; the slash command only changes i

| Mode | Classifier path | When the request leaves DOGA for a classifier |
| --- | --- | --- |
| `jev_api` | Jev through OpenRouter, then direct TypeSafe on API failure | Every classified request |
| `clef_api` | Clef on Cloudflare Workers AI, one call | Every classified request |
| `laya_local` | Local Laya only | Never |
| `laya_with_jev_fallback` | Local Laya first; Jev only on a local exception | Only if Laya load, inference, or schema validation fails, up to three consecutive failures |

Jev and Clef are both hosted, so both send the request off the machine. Only `laya_local` and a healthy `laya_with_jev_fallback` keep classification entirely local. Clef has **no** fallback route: a Clef failure leaves DOGA on its ordinary guidance rather than reaching for Jev or Laya, because a failure of one classifier is not permission to silently ask a different one. If you want a remote fallback for a local classifier, that is `laya_with_jev_fallback`, and it targets Jev only.

The fallback is **error-only**, not a quality or low-confidence fallback. A successful but incorrect Laya judgment does not invoke Jev. Following three consecutive local failures, DOGA suppresses further remote fallback and retains ordinary guidance until a local evaluation succeeds. The warning log records only the error type, not the request. The main Hermes model and other plugins have their own separate network behavior. A [matched 100-question evaluation](docs/benchmarks/2026-09-26-100-question.md) found that local Laya underperformed Jev against authored labels, so keep `jev_api` as the recommended default until Laya questions and checkpoint are validated on new labels.

For live Jev assessments, make `OPENROUTER_API_KEY` available to the Hermes process. To enable TypeSafe failover, also provide `TYPESAFE_API_KEY`. DOGA reads keys from the process environment, not DOGA configuration or model prompts. Without either key, the Jev request cannot be evaluated and DOGA continues with its standard guidance.

DOGA sends the user's request to Jev through OpenRouter first, using model `typesafe/jev-1.13` at `https://openrouter.ai/api/alpha/decisions`. If that key is missing or the request fails, DOGA tries TypeSafe directly, using model `jev-latest` at `https://api.typesafe.ai/v1/systemone`. If only `TYPESAFE_API_KEY` is set, DOGA uses the direct TypeSafe route. If both calls fail, DOGA continues with its standard guidance. `JEV_PROVIDER_MODE` configures the separate `jev-decisions` Hermes plugin and does not control DOGA's provider route.

For local Laya, install the optional extra **in the Python environment running Hermes**, then select it:

```bash
# Run from this fork's checkout:
uv pip install --python /path/to/hermes-python '.[laya]'
# Select /doga mode laya_local for this process, or set
# DOGA_DECISION_MODE=laya_local in Hermes' startup environment.
```

If you copied the plugin directory instead of installing the Python package, install `laya>=0.3.20,<1` into Hermes' Python environment. The optional dependency brings PyTorch and Transformers; allow disk space for them and the model checkpoint. The default model is `convaiinnovations/laya`, loaded once and reused. Its first load can download weights from Hugging Face and block the first classified request while doing so. Cache the checkpoint before using `HF_HUB_OFFLINE=1` for offline operation. A local smoke test emitted a Laya warning about invalid saved choice temperatures that it clamped; treat affected confidence values as uncalibrated. No Jev keys are needed for `laya_local`. Invalid mode names fail closed to ordinary guidance rather than remote Jev.

For an explicit remote error fallback select `/doga mode laya_with_jev_fallback` or set `DOGA_DECISION_MODE=laya_with_jev_fallback` at startup. Set `OPENROUTER_API_KEY` and optionally `TYPESAFE_API_KEY` in Hermes' secret environment. OpenRouter is tried first; direct TypeSafe is tried if the first route fails. The user request is sent to those providers on a local error, so use `laya_local` if local-only classification is required. Legacy `/doga provider jev|clef|laya` and `/doga fallback on|off` still work; selecting a legacy provider resets fallback to off, and `fallback on` is rejected in Jev and Clef modes.

### Cloudflare Clef setup

[Clef](https://developers.cloudflare.com/workers-ai/models/clef/) is Cloudflare's decision model, in the same typed-answer family as Jev. It reads a state plus a set of typed `noul`, `choice`, and `score` questions and returns a probability for every allowed answer, so the same five DOGA facets map onto it without a second contract. Cloudflare hosts two checkpoints, both Apache 2.0 licensed:

| Checkpoint | Size | Use |
| --- | --- | --- |
| `clef` | 27B | Highest-precision decisions. DOGA's default |
| `clef-flash` | 9B | Latency-bound or high-volume paths |

Clef is served by Cloudflare, not by a service you run. DOGA calls Cloudflare's account endpoint directly over HTTPS, so **you do not need a Cloudflare Worker, a GPU, or a local deployment to use it**. Serving it yourself is possible, since Cloudflare publishes the weights on Hugging Face, but that is a separate deployment with its own resource and upgrade burden, and this release does not include it.

Two values are required in the Hermes process environment:

| Variable | Kind | Purpose |
| --- | --- | --- |
| `CLOUDFLARE_ACCOUNT_ID` | Configuration | Selects the account whose endpoint is called |
| `CLOUDFLARE_API_TOKEN` | Credential | Bearer token with the **Account > Workers AI > Read** permission |

```bash
# In Hermes' secret environment, not in a prompt or a DOGA config file:
CLOUDFLARE_ACCOUNT_ID=<your account id>
CLOUDFLARE_API_TOKEN=<token with Account > Workers AI > Read>
# Optional: DOGA_CLEF_MODEL=clef-flash  (defaults to clef)
```

Select `/doga mode clef_api` for the current process, or set `DOGA_DECISION_MODE=clef_api` at startup. Both values are checked before any request, and a missing one fails with the variable name rather than a silent fallback. Every classified request is sent to Cloudflare, so treat `clef_api` as a remote route like `jev_api`. DOGA validates Clef's typed answers against the same criteria it uses for Jev, rejects an unknown choice or an out-of-range probability, and reads both the bare model output and Cloudflare's `success`/`result` REST envelope, surfacing Cloudflare's own error codes on failure. On any failure DOGA keeps its ordinary guidance. No key is written to disk, and warning logs record the error type only, never the request or the token.

Enable the DOGA plugin in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled: [doga]

toolsets: [hermes-cli, doga]

doga:
  depth: 3
  show_simulation: true
  max_scenarios: 5
```

---

## Usage

### Slash Commands

| Command | Description |
|---------|-------------|
| `/doga on` | Enable DOGA |
| `/doga off` | Disable DOGA |
| `/doga status` | Show current settings |
| `/doga auto` | Automatic depth, selects low, medium, or high per query (default) |
| `/doga manual high` | Example: force high thinking depth. Use `low`, `medium`, or `high` |
| `/doga depth 4` | Example: set thinking depth to 4, from 1 to 5 |
| `/doga hats on` | Enable De Bono parallel thinking hats (default) |
| `/doga hats off` | Disable De Bono hats (reverts to standard goal/scenario prompts) |
| `/doga show` | Show simulation panel |
| `/doga hide` | Hide simulation panel |
| `/doga memory on` | Enable goal memory (requires Mnemosyne) |
| `/doga memory off` | Disable goal memory |
| `/doga mode jev_api` | Use remote Jev classifier (recommended default) |
| `/doga mode clef_api` | Use Cloudflare Clef on Workers AI as the classifier |
| `/doga mode laya_local` | Use only local Laya for classification |
| `/doga mode laya_with_jev_fallback` | Use Laya, with Jev only on local errors |
| `/doga jev off` | Legacy alias: disable response contracts for the selected classifier |
| `/doga jev on` | Legacy alias: re-enable response contracts |
| `/doga provider clef` | Legacy alias: select Cloudflare Clef and reset fallback |
| `/doga provider laya` | Legacy alias: select local-only Laya and reset fallback |
| `/doga provider jev` | Legacy alias: select Jev and reset fallback |
| `/doga fallback on` | Legacy alias: enable error fallback when Laya is selected |
| `/doga fallback off` | Legacy alias: keep Laya errors local |
| `/doga max_recursion 3` | Example: set maximum `reason_deeper` depth from 1 to 5 |

### Response Contract with Jev, Clef, or Laya

Jev, Cloudflare Clef, and Laya are typed decision models used here as alternative request classifiers. Jev is the default; select Clef for Cloudflare-hosted inference, or Laya for local inference. For each user request, DOGA asks the selected model for one structured assessment of five facets:

1. **Goal:** information, understanding, or action.
2. **Response mode:** answer, explain, recommend, or clarify.
3. **Stakes:** low, medium, or high.
4. **Clarification:** whether a missing fact materially changes the useful answer.
5. **Scenario need:** none, compare options, or analyze explicit uncertainty.

DOGA maps those judgments into a compact response contract. An action request can require a recommendation and next step, and high stakes add material risks and uncertainty. When the selected model chooses clarify and its ambiguity score is at least 0.7, DOGA asks one focused question. When the ambiguity score is at least 0.7 but it selects another response mode, DOGA preserves that mode while requiring a conditional answer that states material assumptions and what missing information could change the answer. The contract is added to DOGA's pre-model guidance; the main Hermes model still reasons through the task and writes the answer. None of Jev, Clef, or Laya writes the final response. Laya's scores have not been calibrated on DOGA's five questions, and Clef's have not been measured against them either, so do not assume their classifications or the shared 0.7 threshold perform like Jev's; evaluate against labeled examples before relying on either for consequential decisions.

With Jev selected, the classification request goes to OpenRouter. TypeSafe is tried only when OpenRouter is unavailable or its request fails, or when no OpenRouter key is configured. The same user request may therefore be sent to TypeSafe during failover. Use this feature only when sending that request to those providers is acceptable; provider usage may incur charges. If both routes fail, DOGA keeps its ordinary goal and scenario guidance without a typed contract and logs the failure category without the request.

With Clef selected, the request goes to Cloudflare Workers AI in one call, and only to Cloudflare. There is no second provider behind it, so the request is not also sent to Jev or Laya when Clef fails. Clef usage is billed by Cloudflare through Workers AI neurons, so the same cost consideration applies. If the request fails, DOGA keeps its ordinary guidance.

That routing applies in `jev_api` or when Laya fails in `laya_with_jev_fallback`. In `laya_local`, classification stays local after the checkpoint is cached; a missing dependency, failed model load, or invalid result produces ordinary DOGA guidance. In fallback mode the first three consecutive local failures may each send a request remotely; subsequent failures stay local until a successful Laya evaluation resets the counter. This is a per-process limit, not a durable rate limit across restarts. The main Hermes model and other enabled tools or plugins may still make their own network requests. The old `/doga jev on|off` command remains for compatibility and toggles response contracts for any mode.

### Simulate Tool

A `simulate` tool is registered in the `doga` toolset for Monte Carlo analysis. The LLM can call it when quantitative probability weighing is needed:

```json
{
  "scenarios": [
    {
      "name": "contract_valid",
      "variables": {"signature_authorized": 0.8, "no_duress": 0.95},
      "conditions": ["signature_authorized and no_duress"]
    }
  ],
  "n_iterations": 10000
}
```

Returns probability distribution, entropy, and uncertainty level. Scenarios can include nested `children` for hierarchical sub-simulations.

### Reason Deeper Tool

A `reason_deeper` tool is registered for recursive self-critique. The LLM calls it after the initial `<world_model>` analysis to identify missed aspects:

```json
{
  "focus": "black hat risk cascade"
}
```

Each recursion level applies a different De Bono thinking lens. The tool returns a structured instruction for deeper analysis. After `max_recursion` depth is reached, DOGA returns a stop signal; if the LLM ignores it 3 times, a hard-break terminates the loop.

---

## Architecture

DOGA uses three Hermes plugin hooks:

| Hook | Purpose |
|------|---------|
| `pre_llm_call` | Inject goal detection, scenario guidance, and the optional Jev or Laya response contract |
| `transform_llm_output` | Extract `<world_model>` blocks, format as thinking panel |
| `post_tool_call` | Log tool usage; track `reason_deeper` recursion depth and stack |

No Hermes core files are modified. DOGA is a pure plugin.

---

## About

This repository is an independent fork and adjustment of [DOGA by @0z1-ghb](https://github.com/0z1-ghb/doga-hermes), released under the upstream MIT license. It retains DOGA's original probabilistic reasoning, simulation, and Hermes plugin behavior, and adds response contracts through Jev (OpenRouter primary, direct TypeSafe fallback) or optional local Laya. The selected model classifies the user's request; the main Hermes model remains responsible for reasoning through it and writing the answer.

Original DOGA was built by [@0z1-ghb](https://github.com/0z1-ghb). This community maintained fork adds Jev and Laya response contracts and is not an official Hermes, TypeSafe, OpenRouter, or Laya project.

---

## License

MIT
