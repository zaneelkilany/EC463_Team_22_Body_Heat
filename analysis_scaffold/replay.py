"""Replay identical packets through baselines and this branch's model variant.

Fake results establish software plumbing only; they say nothing about Qwen/model
effectiveness, real token usage, latency, or cost. No provider calls by default.
"""
import argparse
import json

from .analyzer import PodRelayAnalyzer
from .deterministic_analyzer import DeterministicAnalyzer
from . import DEFAULT_MODE
from .llm_analyzer import (LLMAnalyzer, FakeLLMClient, LLMClient, SCORE_EVERY_S,
                           MAX_CALLS_PER_WEARER, MAX_CALLS_SESSION, FAIL_COOLDOWN_S, LIVE_SAFETY_CEILING)
from . import expectations


def load_recording(path):
    header, packets = None, []
    with open(path) as source:
        for line in source:
            rec = json.loads(line)
            if rec["type"] == "header":
                header = rec
            elif rec["type"] == "packet":
                packets.append((rec["t"], rec["packet"]))
            else:
                raise ValueError("unknown recording record type")
    if header is None:
        raise ValueError("recording has no header")
    if any(a[0] > b[0] for a, b in zip(packets, packets[1:])):
        raise ValueError("recording packets must be in time order")
    return header, packets


def build_analyzer(name, real=False, variant=None, **limits):
    if name == "relay":
        return PodRelayAnalyzer()
    if name == "deterministic":
        return DeterministicAnalyzer()
    if name in ("llm", "llm-explain", "llm-rolling"):
        mode = name.removeprefix("llm-") if name != "llm" else variant
        return LLMAnalyzer(LLMClient() if real else FakeLLMClient(), variant=mode, **limits)
    raise ValueError(f"unknown analyzer {name!r}")


def score(flags, events=()):
    unavailable = [e for e in events if e["kind"] in ("analysis unavailable", "budget_exhausted")]
    incomplete = bool(unavailable)
    if incomplete:
        print("  scoring coverage: INCOMPLETE (unavailable model output is not a normal/no-flag result)")
    missed = negative = 0
    for label, dev, kind, t0, t1 in expectations.EXPECTED_FLAGS:
        # Normalize the pod comparator's absolute spelling only at scoring.
        hits = [f for f in flags if f.device_id == dev and
                ("above_limit" if f.kind == "absolute" else f.kind) == kind and t0 <= f.t <= t1]
        missed += not hits
        print(f"  {label}: {'PASS' if hits else 'MISS'} (window t={t0}..{t1})")
    for dev, t0, t1 in expectations.NEGATIVE_WINDOWS:
        bad = [f for f in flags if f.device_id == dev and t0 <= f.t <= t1 and
               f.kind in ("trend", "above_limit", "absolute")]
        negative += len(bad)
        unknown = any(e["t"] <= t1 and (e.get("scope") in ("session", "live_safety") or
                      e["device_id"] == dev) for e in unavailable)
        status = str(len(bad)) + " unexpected" if bad else "INCOMPLETE" if unknown else "PASS"
        print(f"  no temperature flags {dev} t={t0}..{t1}: {status}")
    rise = [f.t for f in flags if f.device_id == "pod-11" and f.t >= 15 and
            f.kind in ("trend", "above_limit", "absolute")]
    return {"first_rise_t": min(rise) if rise else None,
            "time_to_first_flag_s": min(rise) - 15 if rise else None,
            "missed_expected_events": missed, "flags_in_negative_windows": negative,
            "scoring_status": "incomplete" if incomplete else "complete"}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--analyzer", choices=["all", "relay", "deterministic", "llm", "llm-explain", "llm-rolling"], default="relay")
    ap.add_argument("--variant", choices=["explain", "rolling"], default=DEFAULT_MODE,
                    help="Runtime variant for --analyzer llm; all always runs both")
    ap.add_argument("--score-every-s", type=int, default=SCORE_EVERY_S)
    ap.add_argument("--max-calls-per-wearer", type=int, default=MAX_CALLS_PER_WEARER)
    ap.add_argument("--max-calls-session", type=int, default=MAX_CALLS_SESSION)
    ap.add_argument("--fail-cooldown-s", type=int, default=FAIL_COOLDOWN_S)
    ap.add_argument("--real-provider", action="store_true", help="Explicit paid-network opt-in; only with --analyzer llm")
    ap.add_argument("--call-log", help="Optional local JSONL per-call metrics (never contains the key)")
    ap.add_argument("recording")
    args = ap.parse_args()
    if args.real_provider and args.analyzer not in ("llm", "llm-explain", "llm-rolling"):
        ap.error("--real-provider requires selecting only this branch's LLM variant")
    if any(value < 0 for value in (args.score_every_s, args.max_calls_per_wearer,
                                   args.max_calls_session, args.fail_cooldown_s)):
        ap.error("cadence, budgets, and cooldown must be nonnegative")
    header, packets = load_recording(args.recording)
    print(f"recording: {header['source']}, {len(packets)} packets")
    if not args.real_provider:
        print("FAKE OFFLINE: software plumbing only; no model effectiveness, real token usage, latency or cost measured.")
    names = ["relay", "deterministic", "llm-explain", "llm-rolling"] if args.analyzer == "all" else [args.analyzer]
    summaries, logs = [], []
    for name in names:
        analyzer = build_analyzer(name, args.real_provider, variant=args.variant,
                                  score_every_s=args.score_every_s,
                                  max_calls_per_wearer=args.max_calls_per_wearer,
                                  max_calls_session=args.max_calls_session,
                                  fail_cooldown_s=args.fail_cooldown_s)
        flags = []
        for t, pkt in packets:
            flags.extend(analyzer.ingest(pkt, t))
        print(f"\nanalyzer: {analyzer.name}\n{len(flags)} flag(s) raised")
        for flag in flags:
            print(f"  t={flag.t:4d} {flag.device_id} {flag.kind}: {flag.message}")
        result = score(flags, getattr(analyzer, "events", ()))
        result["analyzer"] = name
        if isinstance(analyzer, LLMAnalyzer):
            client = analyzer.client
            result.update(calls=client.calls, prompt_tokens=client.prompt_tokens,
                          completion_tokens=client.completion_tokens,
                          measured_call_latency_s=sum(item["latency_s"] for item in client.logs),
                          latency_scope="local fake plumbing" if client.fake else "provider wall clock",
                          analysis_unavailable_events=sum(e["kind"] == "analysis unavailable" for e in analyzer.events),
                          budget_exhausted_events=sum(e["kind"] == "budget_exhausted" for e in analyzer.events),
                          model_deferred_events=sum(e["kind"] == "model_deferred" for e in analyzer.events),
                          score_every_s=analyzer.score_every_s,
                          max_calls_per_wearer=analyzer.max_calls_per_wearer,
                          max_calls_session=analyzer.max_calls_session,
                          fail_cooldown_s=analyzer.fail_cooldown_s,
                          live_safety_ceiling=LIVE_SAFETY_CEILING)
            logs.extend(dict(log, variant=analyzer.variant) for log in client.logs)
            for event in analyzer.events:
                print(f"  t={event['t']} {event['device_id']}: {event['kind']} {event['message']}")
        summaries.append(result)
    print("\n-- comparison summary (same recording) --")
    for result in summaries:
        print(json.dumps(result, sort_keys=True))
    if args.call_log:
        with open(args.call_log, "w") as target:
            for log in logs:
                target.write(json.dumps(log) + "\n")
    return int(any(row["missed_expected_events"] or row["flags_in_negative_windows"] or
                   (row.get("analysis_unavailable_events", 0) or row.get("budget_exhausted_events", 0)) for row in summaries))


if __name__ == "__main__":
    raise SystemExit(main())
