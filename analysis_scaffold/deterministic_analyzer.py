"""Hub rules ignore pod alert and recompute slope from received readings.

Rebroadcast measurements are not additional samples. Off-skin and long gaps
reset history; silence cannot generate events because ingest drives all work.
"""
from collections import deque
import math

from .analyzer import Analyzer, Flag

TREND_LIMIT_C_PER_5MIN = 1.0  # team setting FR-001
ABSOLUTE_LIMIT_C = 38.5  # team setting FR-002
# implementation assumption by the coding agent, review and adjust before defending in the logbook
WINDOW_S = 300
# implementation assumption by the coding agent, review and adjust before defending in the logbook
MIN_SPAN_S = 40
# implementation assumption by the coding agent, review and adjust before defending in the logbook
SLOPE_WINDOW_S = 60
# implementation assumption by the coding agent, review and adjust before defending in the logbook
RECOVERY_S = 10
# implementation assumption by the coding agent, review and adjust before defending in the logbook
MAX_GAP_S = 20

MESSAGES = {
    "trend": "Readings show a trend above the team-set limit; staff review requested.",
    "above_limit": "Readings are above the team-set absolute limit; staff review requested.",
    "resume": "Readings have returned below the team-set limits for the recovery period.",
}


def finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


class ReadingWindows:
    """Whitelist contract fields only; names and pod alerts never enter windows."""
    def __init__(self, window_s=WINDOW_S):
        if window_s < MIN_SPAN_S:
            raise ValueError("window_s must cover MIN_SPAN_S")
        self.window_s = window_s
        self.samples = {}
        self.last = {}
        self.ids = {}

    def add(self, pkt, t):
        dev = pkt["device_id"]
        if not isinstance(dev, str) or not dev.startswith("pod-") or not dev[4:].isdigit():
            raise ValueError("device_id must be a pod ID")
        previous = self.last.get(dev)
        if previous is not None and t <= previous:
            return None
        if pkt.get("skin_contact") is not True:
            self.samples.pop(dev, None)
            self.ids.pop(dev, None)
            self.last[dev] = t
            return None
        if not finite_number(pkt.get("skin_c")) or not finite_number(pkt.get("battery_pct")):
            raise ValueError("non-finite reading")
        if previous is not None and t - previous > MAX_GAP_S:
            self.samples.pop(dev, None)
            self.ids.pop(dev, None)
        self.last[dev] = t
        samples = self.samples.setdefault(dev, deque())
        mid = pkt.get("measurement_id")
        if not samples or mid is None or mid != self.ids.get(dev):
            samples.append((t, pkt["skin_c"], pkt["battery_pct"]))
            self.ids[dev] = mid
        while samples and samples[0][0] < t - self.window_s:
            samples.popleft()
        readings = []
        for sample_t, skin, battery in samples:
            start = next(s for s in samples if s[0] >= sample_t - SLOPE_WINDOW_S)
            span = sample_t - start[0]
            slope = (skin - start[1]) * 300 / span if span >= MIN_SPAN_S else 0.0
            readings.append({"t": sample_t, "skin_c": skin,
                             "slope_c_per_5min": slope,
                             "battery_pct": battery, "skin_contact": True})
        return {"device_id": dev, "window_s": self.window_s, "readings": readings,
                "team_settings": {"trend_c_per_5min": TREND_LIMIT_C_PER_5MIN,
                                  "absolute_c": ABSOLUTE_LIMIT_C}}


def reading_kind(reading, settings):
    if reading["skin_contact"] is not True:
        return "none"
    if reading["skin_c"] >= settings["absolute_c"]:
        return "above_limit"
    if reading["slope_c_per_5min"] >= settings["trend_c_per_5min"]:
        return "trend"
    return "none"


class Transitions:
    def __init__(self, recovery_s=RECOVERY_S):
        if recovery_s < 0:
            raise ValueError("recovery_s must be nonnegative")
        self.recovery_s = recovery_s
        self.active = {}
        self.below_since = {}

    def reset(self, dev):
        self.active.pop(dev, None)
        self.below_since.pop(dev, None)

    def update(self, dev, kind, t):
        if kind != "none":
            self.below_since.pop(dev, None)
            if self.active.get(dev) != kind:
                self.active[dev] = kind
                return kind
        elif self.active.get(dev):
            since = self.below_since.setdefault(dev, t)
            if t - since >= self.recovery_s:
                self.reset(dev)
                return "resume"
        return None


class DeterministicAnalyzer(Analyzer):
    name = "deterministic hub-side baseline"

    def __init__(self, window_s=WINDOW_S, recovery_s=RECOVERY_S):
        self.windows = ReadingWindows(window_s)
        self.transitions = Transitions(recovery_s)

    def ingest(self, pkt, t):
        dev = pkt["device_id"]
        previous = self.windows.last.get(dev)
        if pkt.get("skin_contact") is not True or (previous is not None and t - previous > MAX_GAP_S):
            self.transitions.reset(dev)
        window = self.windows.add(pkt, t)
        if not window or not window["readings"]:
            return []
        kind = reading_kind(window["readings"][-1], window["team_settings"])
        event = self.transitions.update(dev, kind, t)
        return [Flag(t, dev, event, MESSAGES[event])] if event else []
