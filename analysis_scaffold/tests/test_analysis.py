"""Offline contract/state tests. Fake results verify software plumbing only."""
import json
from unittest.mock import patch

import pytest

from analysis_scaffold import llm_analyzer as model
from analysis_scaffold.deterministic_analyzer import DeterministicAnalyzer, ReadingWindows
from analysis_scaffold.replay import load_recording, score
from analysis_scaffold.analyzer import Flag


@pytest.fixture(autouse=True)
def no_network():
    with patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden in tests")):
        yield


def packet(t, skin=36.0, contact=True, dev="pod-11"):
    return {"device_id": dev, "skin_c": skin, "skin_contact": contact,
            "battery_pct": 99.0, "measurement_id": str(t), "alert": "absolute",
            "name": "NEVER SEND THIS"}


@pytest.fixture(params=["explain", "rolling"])
def variant(request):
    return request.param


def analyzers(variant):
    # State-machine unit tests isolate logic from scheduling, which is tested below.
    return [DeterministicAnalyzer(), model.LLMAnalyzer(model.FakeLLMClient(),
            variant=variant, score_every_s=0, max_calls_session=1000,
            max_calls_per_wearer=1000, fail_cooldown_s=0)]


def test_off_skin_never_flags(variant):
    for analyzer in analyzers(variant):
        assert analyzer.ingest(packet(0, skin=40, contact=False), 0) == []
        assert analyzer.ingest(packet(10, skin=41, contact=False), 10) == []


def test_recovery_and_no_stale_events(variant):
    for analyzer in analyzers(variant):
        assert analyzer.ingest(packet(0, skin=39), 0)[0].kind == "above_limit"
        assert analyzer.ingest(packet(10), 10) == []
        flags = analyzer.ingest(packet(20), 20)
        assert [flag.kind for flag in flags] == ["resume"]
        assert analyzer.ingest(packet(20), 20) == []


def test_rules_ignore_alert_and_recompute_trend():
    analyzer = DeterministicAnalyzer()
    for t in range(0, 40, 10):
        assert not analyzer.ingest(packet(t, 36 + t * .01), t)
    assert analyzer.ingest(packet(40, 36.4), 40)[0].kind == "trend"
    quiet = DeterministicAnalyzer()
    assert quiet.ingest(packet(0), 0) == []  # pod alert claims absolute, ignored


def test_detach_and_gap_reset_history(variant):
    for analyzer in analyzers(variant):
        analyzer.ingest(packet(0, skin=39), 0)
        assert analyzer.ingest(packet(10, skin=39, contact=False), 10) == []
        assert analyzer.ingest(packet(20), 20) == []
        assert analyzer.ingest(packet(30), 30) == []  # no fabricated resume after detach
        assert analyzer.ingest(packet(100), 100) == []


def test_contract_and_privacy():
    windows = ReadingWindows()
    window = windows.add(packet(0), 0)
    assert set(window) == {"device_id", "window_s", "readings", "team_settings"}
    assert "NEVER SEND THIS" not in json.dumps(window)
    assert "alert" not in json.dumps(window)
    client = model.FakeLLMClient()
    result = client.complete(window)
    assert model.validate_response(result) == result
    assert model.validate_response(json.dumps(result)) == result
    assert client.calls == 1 and client.failures == 0
    assert client.prompt_tokens is None and client.completion_tokens is None
    assert client.logs[0]["latency_s"] >= 0
    assert client.logs[0]["t"] == 0


@pytest.mark.parametrize("response", ["not JSON", "{", "[]", {},
    {"flag": False, "kind": "none", "reason": "bad", "evidence": {}},
    {"flag": 0, "kind": "none", "reason": model.REASONS["none"],
     "evidence": {"latest_skin_c": 36, "slope_c_per_5min": 0}},
    {"flag": True, "kind": "none", "reason": model.REASONS["none"],
     "evidence": {"latest_skin_c": 36, "slope_c_per_5min": 0}},
    {"flag": False, "kind": "none", "reason": model.REASONS["none"],
     "evidence": {"latest_skin_c": float("nan"), "slope_c_per_5min": 0}},
])
def test_malformed_response_is_unavailable(response, variant):
    client = model.FakeLLMClient()
    client._request = lambda window: (response, None, None)
    analyzer = model.LLMAnalyzer(client, variant=variant)
    flags = analyzer.ingest(packet(0, skin=39), 0)
    assert analyzer.events[0]["kind"] == "analysis unavailable"
    assert client.failures == 1 and client.calls == 1
    assert client.logs[0]["status"] == "analysis unavailable"
    assert all(flag.kind != "resume" for flag in flags)
    if variant == "rolling":
        assert flags == []
    else:
        assert [f.kind for f in flags] == ["above_limit"]  # independent stage remains usable


@pytest.mark.parametrize("error", [TimeoutError, OSError, ValueError])
def test_call_errors_do_not_become_no_flag(error, variant):
    client = model.FakeLLMClient()
    def fail(window):
        raise error("provider text must not reach staff")
    client._request = fail
    analyzer = model.LLMAnalyzer(client, variant=variant)
    analyzer.ingest(packet(0, skin=39), 0)
    assert analyzer.events == [{"t": 0, "device_id": "pod-11", "kind": "analysis unavailable",
                                "message": "analysis unavailable", "error_type": error.__name__}]
    assert client.logs[0]["status"] == "analysis unavailable"


def test_real_client_missing_key_never_connects(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = model.LLMClient()
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        client.complete(ReadingWindows().add(packet(0), 0))
    assert client.calls == client.failures == 1


def test_real_transport_and_usage_are_mocked():
    window = ReadingWindows().add(packet(0), 0)
    result = model.FakeLLMClient().complete(window)
    from io import BytesIO
    response = {"choices": [{"message": {"content": json.dumps(result)}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 8}}
    with patch.dict("os.environ", {"OPENROUTER_API_KEY": "TEST_ONLY"}):
        client = model.LLMClient()
    with patch("urllib.request.urlopen", return_value=BytesIO(json.dumps(response).encode())) as transport:
        assert client.complete(window) == result
    request = transport.call_args.args[0]
    assert request.full_url == model.PROVIDER_BASE_URL
    payload = json.loads(request.data)
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert "NEVER SEND THIS" not in request.data.decode()
    assert client.prompt_tokens == 12 and client.completion_tokens == 8


def test_score_counts_events_and_negative_windows(capsys):
    flags = [Flag(60, "pod-11", "trend", ""), Flag(90, "pod-11", "absolute", ""),
             Flag(180, "pod-11", "resume", "")]
    summary = score(flags)
    assert summary == {"first_rise_t": 60, "time_to_first_flag_s": 45,
                       "missed_expected_events": 0, "flags_in_negative_windows": 0, "scoring_status": "complete"}
    assert "PASS" in capsys.readouterr().out
    assert score(flags + [Flag(50, "pod-07", "trend", "")])["flags_in_negative_windows"] == 1


def test_load_recording_rejects_unordered_packets(tmp_path):
    path = tmp_path / "recording.jsonl"
    path.write_text('\n'.join(json.dumps(row) for row in [
        {"type": "header"}, {"type": "packet", "t": 2, "packet": {}},
        {"type": "packet", "t": 1, "packet": {}}]))
    with pytest.raises(ValueError, match="time order"):
        load_recording(path)


def test_slope_recovers_without_waiting_full_input_window(variant):
    for analyzer in analyzers(variant):
        for t in range(0, 170, 10):
            analyzer.ingest(packet(t, min(39, 36 + t * .04)), t)
        assert analyzer.ingest(packet(170, 37), 170) == []
        assert analyzer.ingest(packet(180, 37), 180)[0].kind == "resume"


def test_failure_interrupts_recovery_period():
    variant = "rolling"
    client = model.FakeLLMClient()
    analyzer = model.LLMAnalyzer(client, variant=variant, score_every_s=0, fail_cooldown_s=0)
    analyzer.ingest(packet(0, 39), 0)
    analyzer.ingest(packet(10, 36), 10)
    good_request = client._request
    client._request = lambda window: ("invalid JSON", None, None)
    assert analyzer.ingest(packet(20, 36), 20) == []
    client._request = good_request
    assert analyzer.ingest(packet(30, 36), 30) == []
    assert analyzer.ingest(packet(40, 36), 40)[0].kind == "resume"


def test_cadence_is_per_device():
    client = model.FakeLLMClient()
    analyzer = model.LLMAnalyzer(client, score_every_s=10, variant="rolling")
    analyzer.ingest(packet(0), 0)
    analyzer.ingest(packet(1), 1)
    analyzer.ingest(packet(1, dev="pod-07"), 1)
    analyzer.ingest(packet(10), 10)
    assert client.calls == 3


def test_future_key_does_not_enable_network_for_all(monkeypatch):
    from analysis_scaffold.replay import build_analyzer
    monkeypatch.setenv("OPENROUTER_API_KEY", "TEST_ONLY")
    assert isinstance(build_analyzer("llm").client, model.FakeLLMClient)


def test_http_error_is_unavailable(variant):
    import urllib.error
    client = model.FakeLLMClient()
    def fail(window):
        raise urllib.error.HTTPError(model.PROVIDER_BASE_URL, 503, "unavailable", {}, None)
    client._request = fail
    analyzer = model.LLMAnalyzer(client, variant=variant, score_every_s=0, fail_cooldown_s=0)
    analyzer.ingest(packet(0, 39), 0)
    assert analyzer.events[0]["kind"] == "analysis unavailable"
    assert client.failures == 1


def test_replay_cli_uses_same_inputs_for_all(tmp_path, capsys):
    import sys
    from analysis_scaffold import replay
    # Small contract fixture, deliberately not represented as the demo recording.
    rows = [{"type": "header", "source": "unit contract fixture"}]
    for t, skin in [(0, 36), (10, 36), (20, 36.4), (30, 36.7), (40, 37),
                    (50, 37.2), (60, 37.4), (70, 37.8), (80, 38.2), (90, 39),
                    (100, 39), (110, 39), (120, 39), (130, 39), (140, 39),
                    (150, 39), (160, 39), (170, 37), (180, 37)]:
        pkt = packet(t, skin)
        pkt["alert"] = "absolute" if 90 <= t < 170 else "trend" if 40 <= t < 90 else None
        rows.append({"type": "packet", "t": t, "packet": pkt})
    path = tmp_path / "fixture.jsonl"
    path.write_text('\n'.join(json.dumps(row) for row in rows))
    with patch.object(sys, "argv", ["replay", "--analyzer", "all", "--score-every-s", "0", "--max-calls-session", "1000", "--max-calls-per-wearer", "1000", str(path)]):
        assert replay.main() == 0
    output = capsys.readouterr().out
    assert "software plumbing only" in output
    assert output.count('"flags_in_negative_windows": 0') == 4
    assert output.count('"missed_expected_events": 0') == 4


@pytest.mark.parametrize("variant", ["rolling", "explain"])
def test_per_wearer_budget_visible_and_flags_preserved(variant):
    client = model.FakeLLMClient()
    analyzer = model.LLMAnalyzer(client, variant=variant, score_every_s=0,
                                 max_calls_per_wearer=1, max_calls_session=10)
    analyzer.ingest(packet(0, 39), 0)
    analyzer.ingest(packet(10, 36), 10)
    flags = analyzer.ingest(packet(20, 36), 20)
    analyzer.ingest(packet(30, 36), 30)
    assert client.calls == 1
    assert sum(e["kind"] == "budget_exhausted" for e in analyzer.events) == 1
    assert analyzer.events[-1]["scope"] == "wearer"
    assert [f.kind for f in flags] == (["resume"] if variant == "explain" else [])


def test_session_budget_applies_across_wearers():
    client = model.FakeLLMClient()
    analyzer = model.LLMAnalyzer(client, variant="rolling", max_calls_session=1)
    analyzer.ingest(packet(0), 0)
    analyzer.ingest(packet(0, dev="pod-07"), 0)
    analyzer.ingest(packet(30, dev="pod-07"), 30)
    assert client.calls == 1
    assert analyzer.events == [{"t": 0, "device_id": "pod-07", "kind": "budget_exhausted",
                                "scope": "session", "message": "Model analysis unavailable: call budget exhausted."}]


def test_default_cadence_updates_windows_without_calls():
    client = model.FakeLLMClient()
    analyzer = model.LLMAnalyzer(client, variant="rolling")
    for t in range(61):
        analyzer.ingest(packet(t), t)
    assert [log["session_t"] for log in client.logs] == [0, 30, 60]
    assert analyzer.windows.samples["pod-11"][-1][0] == 60


@pytest.mark.parametrize("variant", ["rolling", "explain"])
def test_failed_call_has_cooldown_and_no_immediate_retry(variant):
    client = model.FakeLLMClient()
    good = client._request
    client._request = lambda window: ("not JSON", None, None)
    analyzer = model.LLMAnalyzer(client, variant=variant)
    analyzer.ingest(packet(0, 39), 0)
    client._request = good
    for t in range(1, 60):
        # Force deterministic kind changes to exercise explain's cooldown as well.
        analyzer.ingest(packet(t, 39 if t % 2 == 0 else 36, contact=t != 59), t)
    assert client.calls == 1
    analyzer.ingest(packet(60, 39), 60)
    assert client.calls == 2
    assert client.logs[1]["session_t"] == 60


def test_detach_does_not_reset_call_limits():
    client = model.FakeLLMClient()
    analyzer = model.LLMAnalyzer(client, variant="rolling")
    analyzer.ingest(packet(0), 0)
    analyzer.ingest(packet(1, contact=False), 1)
    analyzer.ingest(packet(2), 2)
    assert client.calls == 1


def test_runtime_variants_are_distinct_paths():
    explain = model.LLMAnalyzer(variant="explain")
    rolling = model.LLMAnalyzer(variant="rolling")
    for t in range(91):
        pkt = packet(t, 36 if t < 40 else 39)
        explain.ingest(pkt, t)
        rolling.ingest(pkt, t)
    assert [log["session_t"] for log in explain.client.logs] == [40]
    assert [log["session_t"] for log in rolling.client.logs] == [0, 30, 60, 90]


def test_zero_budget_never_calls_and_is_visible():
    analyzer = model.LLMAnalyzer(variant="rolling", max_calls_session=0)
    analyzer.ingest(packet(0), 0)
    assert analyzer.client.calls == 0
    assert analyzer.events[0]["kind"] == "budget_exhausted"


def test_cooldown_waits_for_next_cadence_slot():
    client = model.FakeLLMClient()
    good = client._request
    client._request = lambda window: ("not JSON", None, None)
    analyzer = model.LLMAnalyzer(client, variant="rolling", fail_cooldown_s=61)
    analyzer.ingest(packet(0), 0)
    client._request = good
    for t in range(1, 90):
        analyzer.ingest(packet(t), t)
    assert client.calls == 1
    analyzer.ingest(packet(90), 90)
    assert client.calls == 2


def test_rebroadcast_does_not_call_even_when_cadence_is_due():
    analyzer = model.LLMAnalyzer(variant="rolling")
    original = packet(0)
    analyzer.ingest(original, 0)
    for t in range(1, 61):
        analyzer.ingest(dict(original), t)
    assert analyzer.client.calls == 1
    assert analyzer.windows.last["pod-11"] == 60
    analyzer.ingest(packet(61), 61)
    assert analyzer.client.calls == 2


def test_rolling_recovery_uses_observed_span_not_an_extra_call():
    analyzer = model.LLMAnalyzer(variant="rolling")
    for t in range(181):
        flags = analyzer.ingest(packet(t, 39 if t < 170 else 37), t)
    assert [f.kind for f in flags] == ["resume"]
    assert analyzer.client.calls == 7


def test_default_budgets_cover_scenario_cadence_with_headroom():
    import math
    opportunities = math.ceil(200 / model.SCORE_EVERY_S)
    assert model.MAX_CALLS_PER_WEARER >= opportunities + 1
    assert model.MAX_CALLS_SESSION >= 30 * (opportunities + 1)


def test_demo_defaults_complete_all_paths_without_exhaustion(capsys):
    import random
    from body_heat_demo.engine import Engine, demo_roster
    from analysis_scaffold.record import CaptureHub
    from analysis_scaffold.analyzer import PodRelayAnalyzer
    state = random.getstate()
    try:
        random.seed(0)
        hub = CaptureHub()
        engine = Engine(hub, store=None, roster=demo_roster(), autoplay=True)
        engine.t = 0
        for _ in range(200):
            engine.step()
    finally:
        random.setstate(state)
    assert len(hub.packets) == 5813
    for analyzer in [PodRelayAnalyzer(), DeterministicAnalyzer(),
                     model.LLMAnalyzer(variant="explain"), model.LLMAnalyzer(variant="rolling")]:
        flags = []
        for t, pkt in hub.packets:
            flags.extend(analyzer.ingest(pkt, t))
        result = score(flags)
        assert result["missed_expected_events"] == 0
        assert result["flags_in_negative_windows"] == 0
        if isinstance(analyzer, model.LLMAnalyzer):
            assert not analyzer.events
            assert analyzer.client.calls <= model.MAX_CALLS_SESSION


def test_live_safety_ceiling_overrides_larger_budgets_without_network():
    client = model.LLMClient()
    client.calls = model.LIVE_SAFETY_CEILING
    analyzer = model.LLMAnalyzer(client, variant="rolling", max_calls_session=100000,
                                 max_calls_per_wearer=100000)
    assert analyzer.ingest(packet(0), 0) == []
    assert analyzer.events[0]["kind"] == "budget_exhausted"
    assert analyzer.events[0]["scope"] == "live_safety"
    with pytest.raises(RuntimeError, match="safety ceiling"):
        client.complete(ReadingWindows().add(packet(0), 0))
    assert client.calls == model.LIVE_SAFETY_CEILING


def test_session_allocation_is_first_come_and_late_wearers_are_visible():
    analyzer = model.LLMAnalyzer(variant="rolling", max_calls_session=2)
    for dev in ("pod-03", "pod-07", "pod-11"):
        analyzer.ingest(packet(0, dev=dev), 0)
    assert analyzer.calls_by_wearer == {"pod-03": 1, "pod-07": 1}
    assert analyzer.events[0]["device_id"] == "pod-11"
    assert analyzer.events[0]["scope"] == "session"


@pytest.mark.parametrize("kind", ["budget_exhausted", "analysis unavailable"])
def test_unavailable_outputs_are_not_scored_as_normal(kind, capsys):
    events = [{"t": 30, "device_id": "pod-07", "kind": kind, "scope": "session"}]
    result = score([], events)
    assert result["scoring_status"] == "incomplete"
    assert result["missed_expected_events"] == 3
    assert "INCOMPLETE" in capsys.readouterr().out
