# DOGA with Jev, Cloudflare Clef, or Laya for Hermes

[![MIT License](https://img.shields.io/github/license/bojansandhaus/doga-jev-hermes)](https://github.com/bojansandhaus/doga-jev-hermes/blob/main/LICENSE)
[![Python 3.10 | 3.11 | 3.12](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue)](https://github.com/bojansandhaus/doga-jev-hermes)
[![Last Commit](https://img.shields.io/github/last-commit/bojansandhaus/doga-jev-hermes)](https://github.com/bojansandhaus/doga-jev-hermes)

![DOGA](assets/DOGA.png)

**Probabilistic, goal-aware thinking layer for Hermes Agent.**

Built by [@0z1-ghb](https://github.com/0z1-ghb). This independent fork adds typed response contracts using a hosted API (Jev through OpenRouter with direct TypeSafe fallback, or Cloudflare Clef on Workers AI) or an interchangeable local decision model. Both sides are [System One decision models](https://systemonemodels.org/guides/what-is-a-system-one-model/), also written *typed decision model*: a model that returns typed values (Choice, Score, Noul) with a probability for each, rather than prose. TypeSafe coined the category alongside Jev on 15 September 2026. Jev is one vendor's member of it, not the category.

DOGA (Doğa, Turkish for “nature”) adds scenario simulation, Monte Carlo reasoning, and goal detection to Hermes responses. It remains a plugin and does not modify Hermes core.

---

## Features

- **Goal Detection**  Identifies whether the user needs Information, Understanding, or Action before responding
- **Four Response Contract Modes**  Choose the hosted API or the local model, each alone or with the other as its error fallback: `api_only`, `api_with_local_fallback`, `local_only`, or `local_with_api_fallback`. The hosted side is Jev (OpenRouter first, direct TypeSafe fallback) or Cloudflare Clef on Workers AI. The local side is a configurable slot whose engine is selected by `DOGA_LOCAL_MODEL` and defaults to Laya. Any of them judges the user's goal, response mode, stakes, need for clarification, and scenario analysis. DOGA turns that assessment into answer requirements for the main model. A healthy local engine makes no classifier provider API call.
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

## System One decision models

The category term is **System One decision model**, also written *typed decision model* ([reference](https://systemonemodels.org/guides/what-is-a-system-one-model/)). A member returns typed values (Choice, Score, Noul) with a probability for each instead of prose, which is why DOGA's five facets map onto any of them without a second contract. TypeSafe coined the term on 15 September 2026 alongside Jev; it is also written System 1. **Jev is one vendor's member of the category, not the category.** This documentation never calls the category "Jev-like".

Other members catalogued in the same index: **CLM** and **GLiNER2.5-Decide** (open weights), plus hosted **d1** (Liquid AI), **Mercury Decide** (Inception, free on OpenRouter), **Solar Decide** (Upstage), **pplx-decider** (Perplexity), **Span-01** (Respan), **Decider 1** (meraGPT), and the **OpenAI Decisions API**.

Members worth naming:

| Model | Host | Weights | Note |
| --- | --- | --- | --- |
| **Jev** | TypeSafe, or through OpenRouter | closed | DOGA's default hosted side, model `typesafe/jev-1.13` |
| **Clef**, **Clef Flash** | Cloudflare Workers AI | closed as served, Apache 2.0 weights published | `clef` 27B and `clef-flash` 9B; the second hosted side |
| **Laya** | local, runs in the Hermes process | open | Convai Innovations. The local slot's default |
| **Kev** | local or any host | open | 0.8B to 27B on Qwen3.5 and Qwen3.8 bases; serves the same `/v1/systemone` request shape as TypeSafe's API |
| **Tev1** | Together AI | open | Qwen3.5-based; `Tev1-4B` and `Tev1-0.8B` checkpoints |

Honest limit: membership of the category and the shared wire contract are documented claims from those projects, not measurements made here. Nothing below was confirmed by calling a provider, apart from the two live behaviours this repository has already recorded in its changelog: local Laya inference with a cached checkpoint, and one Jev request made under a key that has since been removed.

### Repository topic tags

This repository carries these GitHub topics, so the tags and this documentation name the same taxonomy: `cloudflare`, `clef`, `laya`, `kev`, `tev1`, `system-one`, `decision-model`, alongside its existing `doga`, `hermes-agent`, `jev`, `monte-carlo`, `openrouter`, `probabilistic-reasoning`, and `typesafe` topics.

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

The default is `api_only`, which is Jev through OpenRouter with direct TypeSafe failover. Select a mode with `/doga mode <mode>` for the current process, or set `DOGA_DECISION_MODE` to the same names in the Hermes process environment to select one at startup. This setting takes precedence over the legacy `DOGA_DECISION_PROVIDER` and `DOGA_LAYA_JEV_FALLBACK` variables. The running gateway needs a restart to pick up environment or plugin-file changes; the slash command only changes the current process.

There are four modes. Each says which side answers and whether the other side sits behind it as a fallback:

| Mode | Leads | Fallback | Classifier path | When the request leaves DOGA for a classifier |
| --- | --- | --- | --- | --- |
| `api_only` | Hosted API | none | Jev through OpenRouter, then direct TypeSafe on API failure; or Clef on Workers AI | Every classified request |
| `api_with_local_fallback` | Hosted API | Local model | The hosted route, then the local model on a hosted failure | Every classified request; a hosted failure also reaches the local model |
| `local_only` | Local model | none | The local model only | Never, once the engine is cached |
| `local_with_api_fallback` | Local model | Hosted API | The local model first; the hosted route only on a local exception | Only if local load, inference, or schema validation fails, up to three consecutive failures |

`api_only` and `local_only` are single-provider routes. A failure is reported and never rerouted, and neither keeps a cooldown list. `api_with_local_fallback` and `local_with_api_fallback` are two-provider chains and use the existing cooldown, trigger, and breaker machinery unchanged.

Both hosted providers are remote, so any mode that leads with one sends the request off the machine. Only `local_only` and a healthy `local_with_api_fallback` keep classification entirely local. A hosted failure has never been rerouted to a *different* hosted provider: `api_with_local_fallback` tries the hosted route and then the local slot, never Jev on top of Clef, because a failure of one classifier is not permission to silently ask a different one.

### Mode aliases

Every mode name this plugin has ever accepted keeps working. An alias selects the mode it names and is then reported canonically, so an old name never appears in status output, a log line, or a request.

| Accepted name | Resolves to | Notes |
| --- | --- | --- |
| `api_with_local_fallback` | itself | Canonical |
| `api_only` | itself | Canonical |
| `local_only` | itself | Canonical |
| `local_with_api_fallback` | itself | Canonical |
| `auto` | `api_with_local_fallback`, else `api_only` | Picks the local side only when a usable local model is present |
| `jev`, `jev_api`, `typesafe`, `openrouter` | `api_only` on Jev | `typesafe` and `openrouter` do not pin one over the other; Jev's own OpenRouter-then-TypeSafe order applies |
| `clef`, `clef_api` | `api_only` on Clef | |
| `clef_with_local_fallback` | `api_with_local_fallback` on Clef | |
| `laya`, `laya_local` | `local_only` | |
| `laya_then_hosted`, `laya_with_jev_fallback` | `local_with_api_fallback` | |

Names are case-insensitive. An unrecognised name is rejected and, in the case of `DOGA_DECISION_MODE`, reported as an invalid mode so a typo fails closed instead of quietly routing somewhere nobody asked for. The error names every accepted spelling.

The fallback is **error-only**, not a quality or low-confidence fallback. A successful but incorrect local judgment does not invoke the API. Following three consecutive local failures, DOGA suppresses further remote fallback and retains ordinary guidance until a local evaluation succeeds. The warning log records only the error type, not the request. The main Hermes model and other plugins have their own separate network behavior. A [matched 100-question evaluation](docs/benchmarks/2026-09-26-100-question.md) found that the local model underperformed Jev against authored labels, so keep `api_only` as the recommended default until local questions and the checkpoint are validated on new labels.

For live Jev assessments, make `OPENROUTER_API_KEY` available to the Hermes process. To enable TypeSafe failover, also provide `TYPESAFE_API_KEY`. DOGA reads keys from the process environment, not DOGA configuration or model prompts. Without either key, the Jev request cannot be evaluated and DOGA continues with its standard guidance.

DOGA sends the user's request to Jev through OpenRouter first, using model `typesafe/jev-1.13` at `https://openrouter.ai/api/alpha/decisions`. If that key is missing or the request fails, DOGA tries TypeSafe directly, using model `jev-latest` at `https://api.typesafe.ai/v1/systemone`. If only `TYPESAFE_API_KEY` is set, DOGA uses the direct TypeSafe route. If both calls fail, DOGA continues with its standard guidance. `JEV_PROVIDER_MODE` configures the separate `jev-decisions` Hermes plugin and does not control DOGA's provider route.

### Local decision models

The local side is a generic System One decision-model slot. Its configuration name stays `laya`, but that name no longer binds one engine: `DOGA_LOCAL_MODEL` selects which local System One model answers.

| Setting | Kind | Default | Purpose |
| --- | --- | --- | --- |
| `DOGA_LOCAL_MODEL` | Configuration | `convaiinnovations/laya` | The checkpoint or engine name sent to the local server |

Select the local side with `local_only` or `local_with_api_fallback`, then point it at an engine:

```bash
# Run from this fork's checkout:
uv pip install --python /path/to/hermes-python '.[laya]'
# Select /doga mode local_only for this process, or set
# DOGA_DECISION_MODE=local_only in Hermes' startup environment.
# Then choose the engine, for example:
# DOGA_LOCAL_MODEL=kev
```

If you copied the plugin directory instead of installing the Python package, install `laya>=0.3.20,<1` into Hermes' Python environment. The optional dependency brings PyTorch and Transformers; allow disk space for them and the model checkpoint. The default engine is `convaiinnovations/laya`, loaded once per process and reused; switching `DOGA_LOCAL_MODEL` loads the newly named engine rather than reusing the previous one. Its first load can download weights from Hugging Face and block the first classified request while doing so. Cache the checkpoint before using `HF_HUB_OFFLINE=1` for offline operation. A local smoke test emitted a Laya warning about invalid saved choice temperatures that it clamped; treat affected confidence values as uncalibrated. No Jev keys are needed for `local_only`.

`DOGA_LOCAL_MODEL` is **not** checked against a list of known engines. That is deliberate: the whole point of the slot is that a new local System One decision model works by configuration alone, with no code change. Only a value that could not be used safely is rejected, namely an empty or whitespace-only value, a value containing whitespace, quotes, a backslash, a control character, or a URL separator, and a `.` or `..` path segment. System One decision models known to speak the same `/v1/systemone` contract and therefore fit this slot:

| Engine | Note |
| --- | --- |
| `laya` | Convai Innovations. Also `laya-multilingual` and `laya-typed-decisions`. The default |
| `kev` | Open weights, 0.8B to 27B on Qwen3.5 and Qwen3.8 bases, also published as `kev-0.8b`. Serves the same `/v1/systemone` request shape as TypeSafe's API |
| `tev1` | Together AI, Qwen3.5-based, open weights. `Tev1-4B` and `Tev1-0.8B` checkpoints |
| `jeff-qwen3.5-0.8b`, `jeff-gemma4-e2b` | |

Listing an engine here is a documented claim from its own project that it speaks the shared contract; it is not a measurement made in this repository. For the interchangeable-engine claim, see [chaitin/Decis](https://github.com/chaitin/Decis), which is self-hosted, serves the shared `/v1/systemone` contract that Jev's TypeSafe route also uses, and ships one Docker image per engine, so swapping `base_url` is the whole migration. For Tev1, see [togethercomputer/tev1](https://github.com/togethercomputer/tev1). A local server URL remains its own setting; pointing DOGA at a different engine's server is a configuration change, not a code change. An unrecognised mode name fails closed to ordinary guidance rather than reaching for the API.

### Cloudflare Clef setup

[Clef](https://developers.cloudflare.com/workers-ai/models/clef/) is Cloudflare's System One decision model, a member of the same category as Jev rather than something derived from it. It reads a state plus a set of typed `noul`, `choice`, and `score` questions and returns a probability for every allowed answer, so the same five DOGA facets map onto it without a second contract. Cloudflare hosts two checkpoints, both Apache 2.0 licensed:

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

Select `/doga mode api_only` after selecting Clef, or `/doga mode clef_api` for the same result in one step, or set `DOGA_DECISION_MODE=clef_api` at startup. Both values are checked before any request, and a missing one fails with the variable name rather than a silent fallback. Every classified request is sent to Cloudflare, so treat `api_only` on Clef as a remote route like Jev's. DOGA validates Clef's typed answers against the same criteria it uses for Jev, rejects an unknown choice or an out-of-range probability, and reads both the bare model output and Cloudflare's `success`/`result` REST envelope, surfacing Cloudflare's own error codes on failure. On any failure DOGA keeps its ordinary guidance. No key is written to disk, and warning logs record the error type only, never the request or the token.

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
| `/doga mode api_only` | Use the hosted API only (recommended default). Aliases: `jev_api`, `clef_api`, `typesafe`, `openrouter`, `clef` |
| `/doga mode api_with_local_fallback` | Use the hosted API first, the local model on its failure. Alias: `clef_with_local_fallback`, `auto` |
| `/doga mode local_only` | Use only the local model. Aliases: `laya`, `laya_local` |
| `/doga mode local_with_api_fallback` | Use the local model first, the hosted API on its failure. Aliases: `laya_with_jev_fallback`, `laya_then_hosted` |
| `/doga jev off` | Legacy alias: disable response contracts for the selected classifier |
| `/doga jev on` | Legacy alias: re-enable response contracts |
| `/doga provider clef` | Legacy alias: select Cloudflare Clef and reset fallback |
| `/doga provider laya` | Legacy alias: select the local model only and reset fallback |
| `/doga provider jev` | Legacy alias: select Jev and reset fallback |
| `/doga fallback on` | Legacy alias: enable the API fallback when the local model leads |
| `/doga fallback off` | Legacy alias: keep local errors local |
| `/doga max_recursion 3` | Example: set maximum `reason_deeper` depth from 1 to 5 |

### Response Contract with the hosted API or the local model

The hosted API and the local model are System One decision models used here as the two sides of one classifier route. The hosted side is Jev by default or Cloudflare Clef; the local side is whichever System One engine `DOGA_LOCAL_MODEL` selects. For each user request, DOGA asks the selected model for one structured assessment of five facets:

1. **Goal:** information, understanding, or action.
2. **Response mode:** answer, explain, recommend, or clarify.
3. **Stakes:** low, medium, or high.
4. **Clarification:** whether a missing fact materially changes the useful answer.
5. **Scenario need:** none, compare options, or analyze explicit uncertainty.

DOGA maps those judgments into a compact response contract. An action request can require a recommendation and next step, and high stakes add material risks and uncertainty. When the selected model chooses clarify and its ambiguity score is at least 0.7, DOGA asks one focused question. When the ambiguity score is at least 0.7 but it selects another response mode, DOGA preserves that mode while requiring a conditional answer that states material assumptions and what missing information could change the answer. The contract is added to DOGA's pre-model guidance; the main Hermes model still reasons through the task and writes the answer. None of these models writes the final response. The local engine's scores have not been calibrated on DOGA's five questions, and Clef's have not been measured against them either, so do not assume their classifications or the shared 0.7 threshold perform like Jev's; evaluate against labeled examples before relying on either for consequential decisions.

With Jev selected, the classification request goes to OpenRouter. TypeSafe is tried only when OpenRouter is unavailable or its request fails, or when no OpenRouter key is configured. The same user request may therefore be sent to TypeSafe during failover. Use this feature only when sending that request to those providers is acceptable; provider usage may incur charges. If both routes fail, DOGA keeps its ordinary goal and scenario guidance without a typed contract and logs the failure category without the request.

With Clef selected, the request goes to Cloudflare Workers AI in one call, and only to Cloudflare. There is no second provider behind it, so the request is not also sent to Jev or Laya when Clef fails. Clef usage is billed by Cloudflare through Workers AI neurons, so the same cost consideration applies. If the request fails, DOGA keeps its ordinary guidance.

That routing applies in `api_only` or when the local engine fails in `local_with_api_fallback`. In `local_only`, classification stays local after the engine is cached; a missing dependency, failed engine load, or invalid result produces ordinary DOGA guidance. In `local_with_api_fallback` the first three consecutive local failures may each send a request remotely; subsequent failures stay local until a successful local evaluation resets the counter. This is a per-process limit, not a durable rate limit across restarts. The main Hermes model and other enabled tools or plugins may still make their own network requests. The old `/doga jev on|off` command remains for compatibility and toggles response contracts for any mode.

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

This repository is an independent fork and adjustment of [DOGA by @0z1-ghb](https://github.com/0z1-ghb/doga-hermes), released under the upstream MIT license. It retains DOGA's original probabilistic reasoning, simulation, and Hermes plugin behavior, and adds response contracts through a hosted API (Jev through OpenRouter with direct TypeSafe fallback, or Cloudflare Clef) or an interchangeable local decision model, all of them members of the System One decision model category. The selected model classifies the user's request; the main Hermes model remains responsible for reasoning through it and writing the answer.

Original DOGA was built by [@0z1-ghb](https://github.com/0z1-ghb). This community maintained fork adds Jev and Laya response contracts and is not an official Hermes, TypeSafe, OpenRouter, Cloudflare, Convai Innovations, or Laya project.

---

## License

MIT
