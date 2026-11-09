"""Model-involved alternatives with an offline fake and opt-in urllib client.

INPUT: device_id only (never wearer names), window_s, readings newest last with
{t, skin_c, slope_c_per_5min, battery_pct, skin_contact}, and team_settings
{trend_c_per_5min, absolute_c}. OUTPUT: flag (bool), kind (none|trend|
above_limit|resume), reason (<=240 characters, C-05), evidence
{latest_skin_c, slope_c_per_5min}. Every response is validated.
Timeout, HTTP, parsing, or schema failures become analysis unavailable events,
never normal readings. Wall-clock call latency and simulated t stay separate.

Fake results establish software plumbing only: nothing about Qwen/model
effectiveness, real token usage, latency, or cost. Real comparison is untested.
"""
import json
import os
import time
import urllib.request

from . import DEFAULT_MODE
from .analyzer import Analyzer, Flag
from .deterministic_analyzer import (
    DeterministicAnalyzer, ReadingWindows, Transitions, WINDOW_S, RECOVERY_S,
    MAX_GAP_S, MESSAGES, finite_number, reading_kind,
)

PROVIDER_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
# implementation assumption by the coding agent, review and adjust before defending in the logbook
MODEL_CANDIDATE = "qwen/qwen3.5-9b"  # candidate route must be confirmed before real use
# implementation assumption by the coding agent, review and adjust before defending in the logbook
SCORE_EVERY_S = 30  # minimum interval per wearer, in session seconds
# implementation assumption by the coding agent, review and adjust before defending in the logbook
TIMEOUT_S = 30
# implementation assumption by the coding agent, review and adjust before defending in the logbook
BUDGET_HEADROOM_TICKS = 1
# Scenario facts from demo-prototype, not fitted to expected alert times.
SCENARIO_WEARER_COUNT = 30
SCENARIO_DURATION_S = 200
CADENCE_TICKS = (SCENARIO_DURATION_S + SCORE_EVERY_S - 1) // SCORE_EVERY_S
# implementation assumption by the coding agent, review and adjust before defending in the logbook
MAX_CALLS_PER_WEARER = CADENCE_TICKS + BUDGET_HEADROOM_TICKS
# implementation assumption by the coding agent, review and adjust before defending in the logbook
MAX_CALLS_SESSION = SCENARIO_WEARER_COUNT * MAX_CALLS_PER_WEARER
# implementation assumption by the coding agent, review and adjust before defending in the logbook
LIVE_SAFETY_CEILING = 300  # independent hard ceiling; not exposed as a CLI override
# implementation assumption by the coding agent, review and adjust before defending in the logbook
FAIL_COOLDOWN_S = 60

# Constrain explanations to reviewed C-05 templates rather than exposing arbitrary
# provider prose to staff. The model selects an explanation from window evidence.
REASONS = dict(MESSAGES, none="Readings are below the team-set limits.")
OUTPUT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["flag", "kind", "reason", "evidence"],
    "properties": {
        "flag": {"type": "boolean"},
        "kind": {"type": "string", "enum": list(REASONS)},
        "reason": {"type": "string", "maxLength": 240, "enum": list(REASONS.values())},
        "evidence": {"type": "object", "additionalProperties": False,
                     "required": ["latest_skin_c", "slope_c_per_5min"],
                     "properties": {"latest_skin_c": {"type": "number"},
                                    "slope_c_per_5min": {"type": "number"}}},
    },
}


def validate_response(response):
    if isinstance(response, str):
        response = json.loads(response)
    if type(response) is not dict or set(response) != {"flag", "kind", "reason", "evidence"}:
        raise ValueError("response keys")
    kind = response["kind"]
    if type(kind) is not str or kind not in REASONS:
        raise ValueError("response kind")
    if type(response["flag"]) is not bool or response["flag"] != (kind != "none"):
        raise ValueError("response flag/kind mismatch")
    if type(response["reason"]) is not str or response["reason"] != REASONS[kind]:
        raise ValueError("response reason outside reviewed C-05 vocabulary")
    evidence = response["evidence"]
    if type(evidence) is not dict or set(evidence) != {"latest_skin_c", "slope_c_per_5min"}:
        raise ValueError("response evidence")
    if not all(finite_number(v) for v in evidence.values()):
        raise ValueError("response evidence numbers")
    return response


class LLMClient:
    """Explicit opt-in real client. API key comes only from environment.

    Usage fields are provider-reported counts; absent usage stays unknown.
    No automatic retries or fallback to a fake response.
    """
    fake = False

    def __init__(self, model=MODEL_CANDIDATE, timeout_s=TIMEOUT_S):
        self.api_key = os.environ.get("OPENROUTER_API_KEY")
        self.model = model
        self.timeout_s = timeout_s
        self.calls = self.failures = 0
        self.prompt_tokens = self.completion_tokens = None
        self.logs = []

    def _request(self, window):
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY is required for explicit real mode")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": (
                    "Score the supplied readings against team_settings. Use only device IDs. "
                    "Select the matching reviewed reason template in the schema. "
                    "Off-skin readings must not produce temperature flags. "
                    "Never name conditions, call a value abnormal, or direct clinical action.")},
                {"role": "user", "content": json.dumps(window, allow_nan=False)},
            ],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "analysis_result", "strict": True, "schema": OUTPUT_SCHEMA}},
        }
        request = urllib.request.Request(
            PROVIDER_BASE_URL, data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            envelope = json.load(response)
        usage = envelope.get("usage") or {}
        counts = []
        for key in ("prompt_tokens", "completion_tokens"):
            value = usage.get(key)
            counts.append(value if type(value) is int and value >= 0 else None)
        return envelope["choices"][0]["message"]["content"], *counts

    def complete(self, window):
        if not self.fake and self.calls >= LIVE_SAFETY_CEILING:
            raise RuntimeError("Live provider call safety ceiling exhausted")
        self.calls += 1
        started = time.perf_counter()
        log = {"t": window["readings"][-1]["t"], "device_id": window["device_id"],
               "prompt_tokens": None, "completion_tokens": None, "status": "analysis unavailable"}
        try:
            raw, prompt, completion = self._request(window)
            log.update(prompt_tokens=prompt, completion_tokens=completion)
            result = validate_response(raw)
            log["status"] = "validated"
            return result
        except Exception:
            self.failures += 1
            raise
        finally:
            log["latency_s"] = time.perf_counter() - started
            self.logs.append(log)
            for field in ("prompt_tokens", "completion_tokens"):
                value = log[field]
                total = getattr(self, field)
                if self.calls == 1:
                    setattr(self, field, value)
                else:
                    setattr(self, field, total + value if total is not None and value is not None else None)


class FakeLLMClient(LLMClient):
    """Rule-derived contract fixture, NOT a model or model-performance estimate.

    Tokens are unavailable (None); latency measures local fake plumbing only.
    """
    fake = True

    def __init__(self):
        super().__init__()
        self.api_key = None

    def _request(self, window):
        latest = window["readings"][-1]
        kind = reading_kind(latest, window["team_settings"])
        return {"flag": kind != "none", "kind": kind, "reason": REASONS[kind],
                "evidence": {"latest_skin_c": latest["skin_c"],
                             "slope_c_per_5min": latest["slope_c_per_5min"]}}, None, None


class LLMAnalyzer(Analyzer):
    """This branch's model variant; default client is always offline."""
    def __init__(self, client=None, window_s=WINDOW_S, recovery_s=RECOVERY_S,
                 score_every_s=SCORE_EVERY_S, variant=None,
                 max_calls_per_wearer=MAX_CALLS_PER_WEARER,
                 max_calls_session=MAX_CALLS_SESSION, fail_cooldown_s=FAIL_COOLDOWN_S):
        self.variant = DEFAULT_MODE if variant is None else variant
        if self.variant not in ("explain", "rolling"):
            raise ValueError("variant must be explain or rolling")
        for name, value in (("score_every_s", score_every_s),
                            ("max_calls_per_wearer", max_calls_per_wearer),
                            ("max_calls_session", max_calls_session),
                            ("fail_cooldown_s", fail_cooldown_s)):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        self.client = client if client is not None else FakeLLMClient()
        self.window_s = window_s
        self.score_every_s = score_every_s
        self.windows = ReadingWindows(window_s)
        self.stage = DeterministicAnalyzer(window_s, recovery_s)
        self.transitions = Transitions(recovery_s)
        self.last_score = {}
        self.calls_by_wearer = {}
        self.calls_session = 0
        self.max_calls_per_wearer = max_calls_per_wearer
        self.max_calls_session = max_calls_session
        self.fail_cooldown_s = fail_cooldown_s
        self.cooldown_until = {}
        self.recovery_floor = {}
        self.budget_notices = set()
        self.events = []
        self.name = f"llm-{self.variant} ({'FAKE offline plumbing only' if self.client.fake else 'real provider'})"

    def ingest(self, pkt, t):
        dev = pkt["device_id"]
        previous = self.windows.last.get(dev)
        mid = pkt.get("measurement_id")
        rebroadcast = mid is not None and mid == self.windows.ids.get(dev)
        if pkt.get("skin_contact") is not True or (previous is not None and t - previous > MAX_GAP_S):
            self.transitions.reset(dev)
            self.recovery_floor[dev] = t
            # Contact changes and gaps never reset cadence, cooldown, or budgets.
        window = self.windows.add(pkt, t)
        stage_flags = self.stage.ingest(pkt, t) if self.variant == "explain" else []
        if not window or not window["readings"]:
            return []
        if self.variant == "explain" and not stage_flags:
            return []
        if rebroadcast:
            if stage_flags:
                self.events.append({"t": t, "device_id": dev, "kind": "model_deferred",
                                    "message": "Model explanation unavailable without a new measurement."})
            return stage_flags
        # No calls between cadence slots; windows still update.
        last = self.last_score.get(dev)
        due = -float("inf") if last is None else last + self.score_every_s
        cooldown = self.cooldown_until.get(dev, -float("inf"))
        if last is not None and self.score_every_s and cooldown > due:
            # First cadence slot at/after cooldown, not the next broadcast.
            slots = (cooldown - last + self.score_every_s - 1) // self.score_every_s
            due = last + slots * self.score_every_s
        due = max(due, cooldown)
        if t < due:
            if stage_flags:
                self.events.append({"t": t, "device_id": dev, "kind": "model_deferred",
                                    "message": "Model explanation unavailable until the next eligible cadence slot."})
            return stage_flags
        scope = ("live_safety" if not self.client.fake and self.client.calls >= LIVE_SAFETY_CEILING else
                 "session" if self.calls_session >= self.max_calls_session else
                 "wearer" if self.calls_by_wearer.get(dev, 0) >= self.max_calls_per_wearer else None)
        if scope:
            notice = (scope, dev if scope == "wearer" else None)
            if notice not in self.budget_notices:
                self.budget_notices.add(notice)
                self.events.append({"t": t, "device_id": dev, "kind": "budget_exhausted",
                                    "scope": scope, "message": "Model analysis unavailable: call budget exhausted."})
            self.transitions.below_since.pop(dev, None)
            return stage_flags
        self.last_score[dev] = t
        self.calls_session += 1
        self.calls_by_wearer[dev] = self.calls_by_wearer.get(dev, 0) + 1
        log_count = len(self.client.logs)
        try:
            result = validate_response(self.client.complete(window))
            latest = window["readings"][-1]
            evidence = result["evidence"]
            if (evidence["latest_skin_c"] != latest["skin_c"] or
                    evidence["slope_c_per_5min"] != latest["slope_c_per_5min"]):
                raise ValueError("response evidence does not match supplied readings")
            if self.variant == "explain":
                # Deterministic flags remain authoritative even if explanation fails.
                event = stage_flags[0].kind
                if result["kind"] not in (("none", "resume") if event == "resume" else (event,)):
                    raise ValueError("explanation contradicts deterministic event")
                reason = MESSAGES["resume"] if event == "resume" else result["reason"]
                return [Flag(t, dev, event, reason)]
            kind = "none" if result["kind"] == "resume" else result["kind"]
            if kind == "none":
                # implementation assumption by the coding agent, review and adjust before defending in the logbook
                # A validated model clearance needs consecutive raw window evidence
                # below both settings. Use that observed span, not an extra paid call.
                # Never carry recovery evidence across a failed call or new flag.
                since = None
                for reading in reversed(window["readings"]):
                    if (reading["t"] <= self.recovery_floor.get(dev, -float("inf")) or
                            reading_kind(reading, window["team_settings"]) != "none"):
                        break
                    since = reading["t"]
                if since is None:
                    self.transitions.below_since.pop(dev, None)
                    return []
                self.transitions.below_since[dev] = since
            else:
                self.recovery_floor[dev] = t
            event = self.transitions.update(dev, kind, t)
            return [Flag(t, dev, event, REASONS[event])] if event else []
        except Exception as exc:
            # Keep exception type only: provider text may contain sensitive material.
            self.events.append({"t": t, "device_id": dev, "kind": "analysis unavailable",
                                "message": "analysis unavailable", "error_type": type(exc).__name__})
            self.transitions.below_since.pop(dev, None)
            self.cooldown_until[dev] = t + self.fail_cooldown_s
            self.recovery_floor[dev] = t
            return stage_flags  # direct scoring has no stage flags; never means a normal reading
        finally:
            if len(self.client.logs) > log_count:
                self.client.logs[-1]["session_t"] = t
