"""Record a deterministic packet replay of the scripted demo scenario.

Runs the existing Engine offline (no server, no hardware) with a capture hub
that stores every broadcast packet exactly as the pods emit it. The recording
is the shared input for both analysis approaches, so their comparison is fair.

Determinism: pod noise comes from per-pod seeded RNGs inside Engine; the only
unseeded randomness is the air-temperature drift, seeded here.
"""
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from body_heat_demo.engine import Engine, demo_roster

SESSION_LENGTH_S = 200          # covers the full scripted timeline (last event t=170)
OUT_PATH = os.path.join(os.path.dirname(__file__), "recordings", "session_default.jsonl")


class CaptureHub:
    """Stands in for the hub during recording: keeps every packet and tick."""

    def __init__(self):
        self.packets = []       # (t, pkt) in broadcast order
        self.last_tick = None

    def accept(self, pkt, t):
        self.packets.append((t, dict(pkt)))

    def tick(self, t):
        self.last_tick = t


def main():
    random.seed(0)              # air-temperature drift only; pod RNGs are already seeded
    roster = demo_roster()
    hub = CaptureHub()
    eng = Engine(hub, store=None, roster=roster, autoplay=True)
    eng.t = 0
    for _ in range(SESSION_LENGTH_S):
        eng.step()

    header = {
        "type": "header",
        "session_id": "replay-default",
        "source": "body_heat_demo synthetic scripted scenario (no hardware)",
        "scripted_events": [
            {"t": t, "action": action, "jersey": jersey}
            for (t, action, jersey) in eng.scripted
        ],
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        f.write(json.dumps(header) + "\n")
        for t, pkt in hub.packets:
            f.write(json.dumps({"type": "packet", "t": t, "packet": pkt}) + "\n")
    print(f"wrote {len(hub.packets)} packets to {OUT_PATH}")


if __name__ == "__main__":
    main()
