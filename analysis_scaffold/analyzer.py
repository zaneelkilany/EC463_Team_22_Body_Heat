"""Shared analyzer contract; pod alerts are a comparator, never ground truth.

C-05: staff messages describe readings versus team-set limits for staff review;
no disease labels, abnormality judgments, or clinical directions.
"""
from dataclasses import dataclass


@dataclass
class Flag:
    t: int
    device_id: str
    kind: str
    message: str


class Analyzer:
    name = "abstract"

    def ingest(self, pkt: dict, t: int) -> list:
        raise NotImplementedError


class PodRelayAnalyzer(Analyzer):
    """Existing on-pod behavior only; not independent scoring ground truth."""
    name = "pod-relay comparator"

    def __init__(self):
        self._active = {}

    def ingest(self, pkt, t):
        dev = pkt.get("device_id")
        alert = pkt.get("alert")
        if alert not in ("trend", "absolute", "above_limit", None):
            return []
        flags = []
        prev = self._active.get(dev)
        if alert and alert != prev:
            flags.append(Flag(t, dev, alert, f"pod-reported {alert} alert for {dev}"))
        if prev and not alert:
            flags.append(Flag(t, dev, "resume", f"pod-reported alert cleared for {dev}"))
        self._active[dev] = alert
        return flags
