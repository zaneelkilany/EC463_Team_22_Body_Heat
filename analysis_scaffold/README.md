# Analysis-layer comparison harness

This package records one synthetic scenario and replays identical packets through
an independent deterministic baseline, the existing pod-alert relay comparator,
and this branch's model-involved alternative. The relay is not ground truth:
expectations come from scripted scenario labels.

Two branches share identical `record.py`, `analyzer.py`, `replay.py`,
`expectations.py`, `README.md`, and `__init__.py`:

- `analysis-det-flag-llm-explain`: deterministic rules produce flags; a model
  selects a staff-facing explanation for each flag. Deterministic flags remain
  available if explanation fails, alongside an `analysis unavailable` event.
- `analysis-rolling-window-llm`: the model scores each wearer's rolling window.
  Repeated active kinds are suppressed; recovery produces a resume event.

**All tests and verification use the deterministic `FakeLLMClient` offline. These
results establish software plumbing only (the harness runs, contracts validate,
scoring works); they say NOTHING about Qwen/model effectiveness, real token usage,
latency or cost. The real model comparison is untested and requires an API key
later.** Fake rules intentionally share the deterministic decision primitives;
agreement is expected and is not independent evidence of analysis quality.

## Recording prerequisite and running

Both alternatives are based on this fork's `demo-prototype` branch (`c15a72c`),
which supplies `body_heat_demo.engine.Engine`, the 200-second scripted scenario,
and the 29-test existing suite. Application code and existing tests are unchanged.

From the repository root:

```sh
python3 -m analysis_scaffold.record
python3 -m analysis_scaffold.replay --analyzer relay analysis_scaffold/recordings/session_default.jsonl
python3 -m analysis_scaffold.replay --analyzer deterministic analysis_scaffold/recordings/session_default.jsonl
python3 -m analysis_scaffold.replay --analyzer llm analysis_scaffold/recordings/session_default.jsonl
python3 -m analysis_scaffold.replay --analyzer all --call-log work/calls.jsonl analysis_scaffold/recordings/session_default.jsonl
python3 -m pytest tests/ -q
python3 -m pytest analysis_scaffold/tests/ -q
```

`all` runs the relay, deterministic baseline, and the branch's LLM variant on the
same loaded recording. It prints every flag, positive/negative scoring, first
rise flag time, delay from t=15, missed events, and model call counters. A failed
expectation or unavailable analysis causes a nonzero replay exit code. `null`
token counts mean unknown, never zero measured tokens. Fake latency is measured
local execution time only. `--call-log` writes per-call measurement time `t`,
simulated broadcast `session_t`, wall-clock `latency_s`, nullable token counts,
and validation status. Failures are printed separately and counted. Recordings
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
| `SCORE_EVERY_S` | 0 | Score every new broadcast per wearer, including rebroadcasts |
| `RISE_LATEST_S` | 100 | Allow 85 s from the labeled rise, including warmup and sampling |
| `ABSOLUTE_LATEST_S` | 150 | Expect absolute-limit detection before the plateau |
| `RESUME_LATEST_S` | 200 | Allow 30 s after the labeled plateau, including recovery |
| `MODEL_CANDIDATE` | `qwen/qwen3.5-9b` | Requested candidate; confirm actual provider route before use |
| `TIMEOUT_S` | 30 s | Bound blocking provider requests; no automatic retries |

The 200-second recording duration and fixed seed 0 preserve the supplied
recording protocol. Pass lines are provisional agent assumptions, not validated
requirements or proven effectiveness. They were exercised against the existing
demo engine, but still require human review. Negative windows remain pod-07 t=40..110, pod-18 t=0..200, and pod-24
t=10..200. Pod `absolute` is normalized to `above_limit` for scoring only.

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
wording. For direct scoring a validated `none` starts the recovery period;
a model-proposed `resume` cannot bypass that period. Any timeout, HTTP error,
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
