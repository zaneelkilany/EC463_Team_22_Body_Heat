"""Replay identical packets through baselines and this branch's model variant.

Fake results establish software plumbing only; they say nothing about Qwen/model
effectiveness, real token usage, latency, or cost. No provider calls by default.
"""
import argparse
import json

from .analyzer import PodRelayAnalyzer
from .deterministic_analyzer import DeterministicAnalyzer
from .llm_analyzer import LLMAnalyzer, FakeLLMClient, LLMClient, VARIANT
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


def build_analyzer(name, real=False):
    if name == "relay":
        return PodRelayAnalyzer()
    if name == "deterministic":
        return DeterministicAnalyzer()
    if name in ("llm", "llm-" + VARIANT):
        return LLMAnalyzer(LLMClient() if real else FakeLLMClient())
    raise ValueError(f"unknown analyzer {name!r}")


def score(flags):
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
        print(f"  no temperature flags {dev} t={t0}..{t1}: {'PASS' if not bad else str(len(bad)) + ' unexpected'}")
    rise = [f.t for f in flags if f.device_id == "pod-11" and f.t >= 15 and
            f.kind in ("trend", "above_limit", "absolute")]
    return {"first_rise_t": min(rise) if rise else None,
            "time_to_first_flag_s": min(rise) - 15 if rise else None,
            "missed_expected_events": missed, "flags_in_negative_windows": negative}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--analyzer", choices=["all", "relay", "deterministic", "llm", "llm-" + VARIANT], default="relay")
    ap.add_argument("--real-provider", action="store_true", help="Explicit paid-network opt-in; only with --analyzer llm")
    ap.add_argument("--call-log", help="Optional local JSONL per-call metrics (never contains the key)")
    ap.add_argument("recording")
    args = ap.parse_args()
    if args.real_provider and args.analyzer not in ("llm", "llm-" + VARIANT):
        ap.error("--real-provider requires selecting only this branch's LLM variant")
    header, packets = load_recording(args.recording)
    print(f"recording: {header['source']}, {len(packets)} packets")
    if not args.real_provider:
        print("FAKE OFFLINE: software plumbing only; no model effectiveness, real token usage, latency or cost measured.")
    names = ["relay", "deterministic", "llm-" + VARIANT] if args.analyzer == "all" else [args.analyzer]
    summaries, logs = [], []
    for name in names:
        analyzer = build_analyzer(name, args.real_provider)
        flags = []
        for t, pkt in packets:
            flags.extend(analyzer.ingest(pkt, t))
        print(f"\nanalyzer: {analyzer.name}\n{len(flags)} flag(s) raised")
        for flag in flags:
            print(f"  t={flag.t:4d} {flag.device_id} {flag.kind}: {flag.message}")
        result = score(flags)
        result["analyzer"] = name
        if isinstance(analyzer, LLMAnalyzer):
            client = analyzer.client
            result.update(calls=client.calls, prompt_tokens=client.prompt_tokens,
                          completion_tokens=client.completion_tokens,
                          measured_call_latency_s=sum(item["latency_s"] for item in client.logs),
                          latency_scope="local fake plumbing" if client.fake else "provider wall clock",
                          analysis_unavailable_events=len(analyzer.events))
            logs.extend(client.logs)
            for event in analyzer.events:
                print(f"  t={event['t']} {event['device_id']}: analysis unavailable")
        summaries.append(result)
    print("\n-- comparison summary (same recording) --")
    for result in summaries:
        print(json.dumps(result, sort_keys=True))
    if args.call_log:
        with open(args.call_log, "w") as target:
            for log in logs:
                target.write(json.dumps(log) + "\n")
    return int(any(row["missed_expected_events"] or row["flags_in_negative_windows"] or
                   row.get("analysis_unavailable_events", 0) for row in summaries))


if __name__ == "__main__":
    raise SystemExit(main())
