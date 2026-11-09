# Analysis-layer comparison harness

This package records one synthetic scenario and replays identical packets through
an independent deterministic baseline, the existing pod-alert relay comparator,
and both runtime-selectable model alternatives. The relay is not ground truth:
expectations come from scripted scenario labels.

All three branches have the same code capability:

- `analysis-det-flag-llm-explain`: convenience default `explain`; deterministic
  flags remain available even when model explanation is unavailable.
- `analysis-rolling-window-llm`: convenience default `rolling`; the model scores
  windows on bounded cadence, subject to explicit call budgets.
- `analysis-compare`: cohesive deliverable, default `rolling`, containing both
  alternatives. No `VARIANT` module constant exists on any branch.

`LLMAnalyzer(variant="explain"|"rolling")` and replay's `--variant` select the
alternative at runtime. `DEFAULT_MODE` in `__init__.py` is only a documented
convenience default for `--analyzer llm` and constructors without a variant;
it does not remove either code path. The experiment branches differ only in
this per-alternative default, not code capability. All other package files,
including this README, are identical across branches.

**All tests and verification use the deterministic `FakeLLMClient` offline. These
results establish software plumbing only (the harness runs, contracts validate,
scoring works); they say NOTHING about Qwen/model effectiveness, real token usage,
latency or cost. The real model comparison is untested and requires an API key
later.** Fake rules intentionally share the deterministic decision primitives;
agreement is expected and is not independent evidence of analysis quality.

## Recording prerequisite and running

All three branches are based on this fork's `demo-prototype` branch (`c15a72c`),
which supplies `body_heat_demo.engine.Engine`, the 200-second scripted scenario,
and the 29-test existing suite. Application code and existing tests are unchanged.

From the repository root:

```sh
python3 -m analysis_scaffold.record
python3 -m analysis_scaffold.replay --analyzer relay analysis_scaffold/recordings/session_default.jsonl
python3 -m analysis_scaffold.replay --analyzer deterministic analysis_scaffold/recordings/session_default.jsonl
python3 -m analysis_scaffold.replay --analyzer llm --variant explain analysis_scaffold/recordings/session_default.jsonl
python3 -m analysis_scaffold.replay --analyzer llm --variant rolling analysis_scaffold/recordings/session_default.jsonl
python3 -m analysis_scaffold.replay --analyzer all --call-log work/calls.jsonl analysis_scaffold/recordings/session_default.jsonl
python3 -m pytest tests/ -q
python3 -m pytest analysis_scaffold/tests/ -q
```

`all` always runs four distinct paths: relay, deterministic, llm-explain, and
llm-rolling on the same loaded recording, regardless of the default or --variant. It prints every flag, positive/negative scoring, first
rise flag time, delay from t=15, missed events, and model call counters. A failed
expectation, unavailable analysis, or exhausted budget causes a nonzero replay
exit code. This deliberately exposes degraded coverage instead of hiding it. `null`
token counts mean unknown, never zero measured tokens. Fake latency is measured
local execution time only. `--call-log` writes per-call measurement time `t`,
simulated broadcast `session_t`, wall-clock `latency_s`, nullable token counts,
and validation status. Logs also identify the runtime variant. Failure, budget,
and deferred-explanation events are printed separately and counted. Recordings
are regenerable and must not be committed. Tests require the existing pytest
test runner; package runtime uses only stdlib, with no new project dependencies.

## Rules and implementation assumptions

The hub ignores the pod's `alert` field. It recomputes an endpoint slope from
received `skin_c` samples over a recent 60-second interval within the rolling window, rather than treating on-pod
choices as independent hub analysis. FR-001/FR-002 settings remain 1.0 C per
5 min and 38.5 C. Above-limit takes priority over trend. New events are emitted
on kind changes. Repeated measurement IDs do not add slope samples, but new
broadcast packets can advance recovery time. Silence performs no work. Off-skin
packets clear history and pending recovery without a temperature/resume flag;
reattachment starts fresh. Gaps reset history to avoid stale comparisons.

Each default below is an **implementation assumption by the coding agent, review
and adjust before defending in the logbook**:

| Configurable default | Value | Reason for initial implementation |
| --- | --- | --- |
| `WINDOW_S` / `window_s` | 300 s | Express slope on the team's five-minute scale |
| `SLOPE_WINDOW_S` | 60 s | Recent slope responds to changes rather than retaining the full rise for 300 s |
| `MIN_SPAN_S` | 40 s | Avoid amplifying single short noise differences |
| `RECOVERY_S` | 10 s | Require a period below both settings before resume |
| `MAX_GAP_S` | 20 s | Reset sample/recovery history after a stream gap |
| `SCORE_EVERY_S` | 30 s | Minimum interval between attempts per wearer |
| `BUDGET_HEADROOM_TICKS` | 1 | One extra cadence opportunity per wearer |
| `MAX_CALLS_PER_WEARER` | 8 | `ceil(200 / 30) + BUDGET_HEADROOM_TICKS` |
| `MAX_CALLS_SESSION` | 240 | `SCENARIO_WEARER_COUNT * MAX_CALLS_PER_WEARER` |
| `LIVE_SAFETY_CEILING` | 300 | Separate hard ceiling for real-provider attempts; no CLI override |
| `FAIL_COOLDOWN_S` | 60 s | After failure, wait for the first cadence slot at/after cooldown |
| `DEFAULT_MODE` | explain or rolling | Branch convenience preset only; runtime override is supported |
| `RISE_LATEST_S` | 100 | Allow 85 s from the labeled rise, including warmup and sampling |
| `ABSOLUTE_LATEST_S` | 150 | Expect absolute-limit detection before the plateau |
| `RESUME_LATEST_S` | 200 | Allow 30 s after the labeled plateau, including recovery |
| `MODEL_CANDIDATE` | `qwen/qwen3.5-9b` | Candidate route is unverified; confirm actual provider route before use |
| `TIMEOUT_S` | 30 s | Bound blocking provider requests; no automatic retries |

The 200-second recording duration and fixed seed 0 preserve the supplied
recording protocol. Pass lines are provisional agent assumptions, not validated
requirements or proven effectiveness. They were exercised against the existing
demo engine, but still require human review. Negative windows remain pod-07 t=40..110, pod-18 t=0..200, and pod-24
t=10..200. Pod `absolute` is normalized to `above_limit` for scoring only.

## Bounded compute and measured coverage tradeoff

Every packet still updates the window. Off-cadence packets and rebroadcasts do
not trigger model calls, even if a repeated measurement ID arrives after the
cadence interval expires. Attempts, including failures, consume both budgets.
Cadence, budgets, and cooldown are maintained per analyzer instance and never
reset on detach, reattach, or a stream gap. Reuse one instance for one session;
construct a new instance to start a new session. `all` gives each variant its own
independent budget, so one alternative cannot consume the other's allocation.

Explain attempts occur only when the deterministic stage emits a flag. A flag
blocked by cadence or cooldown is retained with reviewed deterministic wording
and a visible `model_deferred` event; there is no queued explanation or automatic
retry. Rolling attempts occur at the first eligible new packet, at least 30
session seconds after the preceding attempt. On failure, eligibility moves to
the first cadence slot at/after the cooldown expires (e.g. failure at t=0 with
61s cooldown and 30s cadence permits the next attempt at t=90). No retries occur
within a call. Calls are synchronous, so there are no concurrent requests or
unbounded pending queues.

The first attempted call beyond a cap emits `budget_exhausted`, with `scope`
`wearer` or `session`. The notice is latched once per exhausted scope, not spammed
on every packet; its restriction persists. Absence of model output is thus
visible, never a fabricated normal result. Explain retains deterministic flags
when capped. Rolling cannot produce fresh model flags after its cap. Allocation
is first eligible packet order: when the session cap binds, later wearers can
lose analysis opportunities. This is **not fair scheduling or clinical
prioritization**; cross-wearer exhaustion is explicitly tested and measured below. Cooldown/budget guards apply equally to both variants.

CLI overrides are `--score-every-s`, `--max-calls-per-wearer`,
`--max-calls-session`, and `--fail-cooldown-s`; constructor parameters have the
corresponding underscore names. All accept nonnegative integers; zero cadence
is an explicit opt-out for isolated tests, zero budget disables calls visibly.
A call budget bounds attempts, not token count or financial cost per call.
The separate `LIVE_SAFETY_CEILING=300` limits actual provider attempts even if
cadence or ordinary budgets are overridden or edited upward. Both the analyzer
and real client enforce it; exhaustion is visible with `scope=live_safety`, and
direct client use raises rather than making another request. The ceiling is an
**implementation assumption by the coding agent, review and adjust before
defending in the logbook**, independent of the scenario-derived defaults.
This is **bounded-call, offline-verified** software only. Real dollar cost is
unresolved until the model route and token usage are tested with a real key;
the rolling variant is not proven cheap.

Budget sizing is an **implementation assumption by the coding agent, review
and adjust before defending in the logbook**: the scenario has 30 wearers and
200 session seconds. At 30s cadence there are `ceil(200 / 30) = 7` opportunities
per wearer. One extra slot gives a per-wearer cap of 8; `30 * 8 = 240` gives the
session cap. These defaults are derived in code from scenario facts and cadence,
not fitted to expected detection times. This intentionally prioritizes full demo coverage over the earlier
sub-50-call exploration target. Smaller limits can still be selected through the
CLI and are covered by exhaustion tests. Budgets are finite guardrails, not a
claim that real token/compute cost is acceptable.

Final offline verification on each branch recorded 5,813 packets and passed 29
existing plus 60 package tests (89 total), with all package files parsing.
With defaults, a full 200s rolling replay makes **at most 240 calls; measured
202**. Explain measured 3 calls; four-path replay measured 205 fake calls total.
All positive expectations and all three negative windows PASS for all four
analyzers, with zero budget exhaustion, zero unavailable-analysis events, and
replay exit 0. Relay and deterministic flag timings remain unchanged. Rolling
flags at t=60 (trend), t=90 (above limit), and t=180 (resume); explain retains
t=40/90/180. Small-cap tests explicitly verify visible exhaustion and retained
deterministic flags. A full scenario regression test verifies all four paths
and zero exhausted budgets with the defaults. Exact per-branch output, including
measured local fake execution time, is pasted in the three PR descriptions.
These numbers establish software plumbing only; real Qwen effectiveness, token
usage, latency, and cost remain untested until an API key is supplied and real
calls are explicitly enabled.

### Measured aggressive-budget tradeoff

A separate final run of `--analyzer all --max-calls-session 40` keeps the 30s
cadence but limits each alternative to 40 attempts. Explain still makes 3 calls
and passes its expectations. Rolling makes 40, exhausts at t=30 (first denied
wearer: pod-13), and misses all three later positive events. First-come allocation
therefore denies later wearers further scoring; even pod-11 only receives its
t=0 and t=30 calls before exhaustion, before the positive detection window is
reached. Unit tests explicitly show early wearers consuming a session cap and a
later wearer receiving the visible exhaustion event. The strict run exits 1.
This is the detection-versus-call tradeoff, not a normal/no-flag outcome.

`budget_exhausted` and `analysis unavailable` remain separate event counters.
Scoring reports `scoring_status=incomplete` if either occurs and marks affected
negative windows INCOMPLETE instead of interpreting missing output as a normal
reading. Missed expected flags remain counted, including the strict run's three
misses. Only actual temperature flags are scored as detections; no unavailable
event becomes a synthetic model `none`. The default run has complete coverage;
the strict run has incomplete rolling coverage and no emitted negative-window
temperature flags. Both runs' full output is pasted in each PR.

## Model contracts, privacy, and failure semantics

The JSON input contains exactly `device_id`, `window_s`, `readings` (newest last:
`t`, `skin_c`, `slope_c_per_5min`, `battery_pct`, `skin_contact`), and
`team_settings` (`trend_c_per_5min`, `absolute_c`). Names, roster data, and pod
alerts never enter a model window (NFR-007). Only numeric pod IDs are accepted.
The analyzer expects upstream hub-validated packet streams; it does not replace
hub authentication, replay protection, or enrollment checks.

The output contains exactly `flag` (bool), `kind` (`none`, `trend`, `above_limit`,
`resume`), `reason` (<=240 characters), and `evidence` (`latest_skin_c`,
`slope_c_per_5min`). Every response is validated, including fake responses.
Finite numeric evidence must match the input. Flag/kind consistency is enforced.
C-05 is enforced by constraining the model's explanation to reviewed templates
in `REASONS`, through both the structured-output schema and local validation.
This is deliberately template selection, not unrestricted generated prose.
Staff strings describe readings versus team-set limits, with no disease names,
claims of abnormality, or clinical directions.

For deterministic-first explanations the deterministic event is authoritative;
a contradictory explanation is a failure. Recovery uses the reviewed resume
wording. For direct scoring, a validated `none` or `resume` must be backed by consecutive
raw readings below both team settings in the supplied window. That observed span
may satisfy the recovery period without requiring an extra paid call at a later
cadence tick. A new flag, failure, or contact/gap reset invalidates earlier
recovery evidence; a model-proposed `resume` cannot bypass the recovery period.
This recovery evidence policy is an **implementation assumption by the coding
agent, review and adjust before defending in the logbook**. Any timeout, HTTP error,
malformed/non-JSON/schema failure, contradictory explanation, or mismatched
evidence records an `analysis unavailable` event in `analyzer.events`. It never
becomes a normal reading or a fabricated `flag: false`. Failure resets pending
recovery. Deterministic-first flags are preserved with their safe fallback
wording when explanation is unavailable. Exception text and API secrets are
never shown to staff.

## Later real-provider use (untested)

Set `OPENROUTER_API_KEY` in your shell environment through your secret manager;
never put it in source, recordings, or commits. Confirm the candidate route and
review the assumptions before intentionally choosing real calls:

```sh
python3 -m analysis_scaffold.replay --analyzer llm --real-provider --call-log work/real-calls.jsonl analysis_scaffold/recordings/session_default.jsonl
```

Only explicit `--real-provider` with the LLM analyzer constructs the real client.
Default replay and `all` always use the fake even if a key exists. The real
stdlib urllib client POSTs to `https://openrouter.ai/api/v1/chat/completions`,
uses a strict JSON schema, and logs provider-reported token counts when present.
No provider calls or paid usage have been made for verification. Local unit tests
mock transport for the real request shape; this does not verify provider support.
No pricing or cost estimates are invented.

## Live web integration

The local `body_heat_demo` app now uses this package directly on every accepted
packet. Run `python3 -m body_heat_demo.server --analyzer deterministic` from the
repo root; see [integrated demo instructions](../body_heat_demo/README.md#integrated-live-hub-analysis)
for all four choices, explicit provider opt-in, staff visibility and standing labels.
