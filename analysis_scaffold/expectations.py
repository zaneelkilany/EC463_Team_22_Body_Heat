"""Scoring expectations come from scenario labels, not pod alerts.

These windows target the requested body_heat_demo 200-second scenario. They
are configurable agent assumptions; review them before interpreting scores.
"""
# implementation assumption by the coding agent, review and adjust before defending in the logbook
RISE_LATEST_S = 100  # <=85 seconds after scripted rise; tolerates 40s warmup and sampling
# implementation assumption by the coding agent, review and adjust before defending in the logbook
ABSOLUTE_LATEST_S = 150  # <=135 seconds after scripted rise, before plateau
# implementation assumption by the coding agent, review and adjust before defending in the logbook
RESUME_LATEST_S = 200  # <=30 seconds after plateau, including recovery and sampling
EXPECTED_FLAGS = [
    ("rise #11", "pod-11", "trend", 15, RISE_LATEST_S),
    ("rise #11 absolute", "pod-11", "above_limit", 15, ABSOLUTE_LATEST_S),
    ("rise #11 clears", "pod-11", "resume", 170, RESUME_LATEST_S),
]
NEGATIVE_WINDOWS = [
    ("pod-07", 40, 110),
    ("pod-18", 0, 200),
    ("pod-24", 10, 200),
]
