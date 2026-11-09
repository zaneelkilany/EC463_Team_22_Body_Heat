# Body Heat demo prototype (hub + views, synthetic data)

Team 22 Body Heat, EC463 Fall 2026. A hard-coded, presentation-ready
prototype of the software platform: Subsystem E (hub receiver, validation,
live state, store, consent, export) and Subsystem F (supervisor roster,
alert strip, athlete detail, session/consent/export, athlete view), served as
a local web app with live updates over Server-Sent Events.

**All wearer data is synthetic.** Thirty fake wearers (six "pods" plus 24
emulated sources, per SAC-03) broadcast once per second from a scripted
scenario; nothing here talks to real hardware. It builds on the F1 sideline
demo (`f1_sideline_demo/`, issue #81) and reuses its hub and view rules.

Stakeholder: the athletic trainer monitoring a team from one sideline
screen (PRD G-02). Use case for the demo: outdoor team practice in late
summer heat. Stakeholder input came from the team's athlete interview.

## Run

    python3 -m body_heat_demo.server            # http://localhost:8463
    python3 -m body_heat_demo.server --no-autoplay   # presenter triggers everything

Standard library only, Python 3.9+. No internet, no CDN, no cloud.

Sign in (password `demo` for every account):

| account | role | sees |
|---|---|---|
| `k.smith` | staff (athletic trainer) | roster, session, demo controls |
| `z.elkilany` | staff | same |
| `athlete11`, `athlete07`, ... `athlete33` | athlete | own session only |

## Test

    python3 -m pytest tests/ -v       # F1 tests + demo prototype tests

## Guided tour

After signing in, a welcome card offers a two-minute guided tour (also the
**▶ Tour** button in the header, any time). It spotlights each part of the
screen and makes real things happen as it goes: it raises an alert on #12
(the pod reads above the limit and decides for itself), records "Got it",
opens the chart, visits Setup, sends a tampered packet and shows the hub
reject and count it, then puts #12 back and lets the pod clear its own
alert. Keyboard: → or Enter next, ← back, Esc leave. Wearer accounts get a
shorter tour of their own view. Code: `static/tour.js`.

## What the presentation shows

Scripted timeline (also triggerable from **Demo controls**):

| t (s) | event | what the screen shows |
|---|---|---|
| 5 | #18 battery low | "Replace batteries" tile |
| 10 | #24 pod stops broadcasting | "No signal for 60 s" at t=70, last values greyed |
| 15 | #11 skin temperature climbs | pod raises **trend** alert (~t=60), then **absolute** (~t=90); alert strip, acknowledge button |
| 40–110 | #7 pod off skin | "Pod not on skin"; pod suspends its own temperature decisions |
| 150–154 | replayed, altered and unknown-device packets | rejected and counted on the Hub counters panel; nothing on the roster changes |
| 170 | #11 eases back | pod clears the alert; "Back below team setting" resume notice |
| always | #33 never opted in | "Not collecting: no consent"; excluded from export |
| always | #3 alerts-only | temperature hidden, alert state still shown |

Also: click any tile for the session chart (skin, air, humidity, motion, alert
and acknowledgement markers); **Session** for consent, alerts-only, team
settings, CSV export, session log and denied-request log; sign in as an
athlete to see the own-data-only view and try the "request a teammate's
data" button (denied by the backend and logged, AC-F5). Pull the laptop's
network or stop the server to see the "Connection lost" banner (AC-F9).

## Rules this prototype keeps (and where)

| rule | where |
|---|---|
| Alert decisions on the pod only; F displays | `engine.SyntheticPod._decide_alert`, `views.py` has no thresholds |
| Ingest order: format → enrollment → auth → consent → counter; live state before storage | `hub.DemoHub.accept` |
| Rejected packets do not refresh readings or comm status; counted | `hub.rejections` |
| Repeated measurement IDs never add rows (AC-E5) | `store.measurements` primary key |
| No-signal 60 s after last accepted packet (AC-E3) | `f1_sideline_demo.hub.NO_SIGNAL_AFTER_S` |
| Acknowledgement recorded with time + user, never clears (AC-F7) | `hub.acknowledge`, `views.build_alert_strip` |
| Consent withdrawal stops ingest, prompts staff to stop the wearable (AC-F6) | `hub.set_consent`, `views.build_notices` |
| Table F2 priority with verbatim labels, glyph + stripe pattern per state (AC-F3) | `views.STATES`, `static/app.css` |
| Wording audit: every user-facing string vs banned list (AC-F4) | `views.all_user_facing_strings`, `tests/test_demo_prototype.py` |
| RBAC in the backend on every request, denials logged (AC-F5) | `auth.Accounts.require_*`, `server.py` |
| Fixed 6×5 grid in jersey order, 30 tiles at 1366×768 (AC-F1) | `static/app.css .grid` |
| SSE pushes only changed tiles | `server.Handler._sse`, `views.diff_tiles` |
| Storage failure is a visible fault; reception continues | `hub._drain`, banner in `app.js` |
| Export excludes non-consenting wearers, includes consent records | `store.export_csv` |

## Demo shortcuts (not the December design)

- Trend window is 60 s so an alert appears on stage; the pod design uses a
  5-min / 31-reading least-squares window.
- "Authentication" checks a synthetic `tag` field; the real AES-128 AEAD
  construction, byte format and key provisioning are **[OPEN]** (S1 sheet).
- Store is plain in-memory `sqlite3`; the hub uses SQLCipher (TM-23).
- Passwords are salted scrypt hashes; the hub uses Argon2id (6.4).
- No BLE: the engine calls `hub.accept()` directly. The real receiver
  (Bleak, or a XIAO forwarder — **[OPEN]**) plugs in at that call.
- A non-consenting wearer shows "Not collecting" instead of "No signal",
  even though Table F2 ranks no-signal higher. Nothing is collected from
  them, so no-signal would be misleading. Confirm with Krista (F owner).

## Integrated live hub analysis

Run from the repository root:

```sh
python3 -m body_heat_demo.server --analyzer deterministic --port 8463
# Also available: --analyzer relay | llm-explain | llm-rolling
# Optional presenter-only start: --no-autoplay
```

Every hub-accepted packet is passed to the selected `analysis_scaffold` analyzer
with the same session second, including rebroadcasts. Rejected packets never
reach analysis; the scaffold handles its own measurement deduplication. Seeded
chart history is not live input. The default `deterministic` choice is local,
zero-cost and requires no key. `relay` runs the pod comparator and preserves the
prior view payload/display. `llm-explain` uses deterministic flags with model
explanations; `llm-rolling` scores rolling windows. Both use `FakeLLMClient`
offline by default, even if a key is present. Real calls require the explicit
`--real-provider` flag with an LLM choice and `OPENROUTER_API_KEY` in the environment;
this reuses the scaffold's existing bounded provider path.

Staff roster cards and staff detail cards show a separate dashed **Hub analysis**
section beside the existing pod status. It shows the latest flag kind and the
analyzer's exact reviewed C-05 wording, including `resume`. This is a latest-event
display, not a new OK decision or a replacement for pod alerts. Before a flag it
says “Awaiting analysis output”. Analysis notices include their event time and
remain visible as the latest coverage notice: `analysis unavailable`,
`budget_exhausted`, or `model_deferred`. Session-wide budget exhaustion is shown
for every consenting wearer. A notice never becomes an OK result. No analysis
fields are sent in athlete detail JSON or athlete SSE. Non-consenting wearers
have no analysis section. Consent withdrawal stops future analyzer ingestion.

Standing labels: **implementation assumptions by the coding agent**;
**bounded-call offline-verified**; **real model untested**. Thresholds are
**team demo settings, not validated health thresholds**. Fake outputs verify
software wiring only, not model effectiveness, real token usage, latency or cost.
The live app retains the scaffold's cadence, budgets, recovery windows and
reviewed wording unchanged. Real calls are synchronous in the engine ingest
path and can delay demo ticks until the existing provider timeout; offline mode
is the verified presentation path.

Offline checks:

```sh
python3 -m pytest tests/ analysis_scaffold/tests/ -q
python3 -m analysis_scaffold.record
python3 -m analysis_scaffold.replay analysis_scaffold/recordings/session_default.jsonl --analyzer all
```
