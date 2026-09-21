"""Score the address gate against evals/address_gate_cases.jsonl.

Reports the two asymmetric error rates separately, because a single accuracy
number hides the only thing that matters: a false wake on `self_talk` can
execute a destructive command, while a miss on `direct_command` just means the
user repeats themselves. These costs are nowhere near equal and must never be
averaged into one figure.

    .venv/bin/python -m evals.run_address_gate            # FakeJev, plumbing only
    TYPESAFE_API_KEY=... .venv/bin/python -m evals.run_address_gate --live
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import statistics
import sys

from daa.config import Settings
from daa.jev.client import build_provider
from daa.jev.gate import AddressGate

CASES = pathlib.Path(__file__).parent / "address_gate_cases.jsonl"
# Slices where a false wake is expensive: the machine acts on speech that was
# never aimed at it. Tracked separately from overall specificity.
COSTLY_FALSE_WAKE = {"self_talk", "overheard", "media", "other_assistant", "mention_not_address"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="require a real TYPESAFE_API_KEY")
    ap.add_argument("--threshold", type=float, default=None, help="override Settings.address_gate")
    args = ap.parse_args()

    settings = Settings.load()
    if args.threshold is not None:
        settings = dataclasses_replace(settings, address_gate=args.threshold)
    if args.live and not settings.jev_live:
        print("--live given but TYPESAFE_API_KEY is unset", file=sys.stderr)
        return 2

    gate = AddressGate(build_provider(settings), settings)
    cases = [json.loads(line) for line in CASES.read_text().splitlines() if line.strip()]

    lat: list[float] = []
    by_cat: dict[str, list[bool]] = collections.defaultdict(list)
    misses, false_wakes, synthetic = [], [], False

    for c in cases:
        d = gate.should_wake(c["utterance"], c.get("ctx", {}))
        synthetic = synthetic or d.synthetic
        lat.append(d.latency_ms)
        correct = d.wake == c["addressed"]
        by_cat[c["category"]].append(correct)
        if not correct:
            (misses if c["addressed"] else false_wakes).append((c, d.addressed_p))

    if synthetic:
        print("!! FakeJev — these numbers measure plumbing, NOT gate quality.\n")

    n = len(cases)
    pos = sum(c["addressed"] for c in cases)
    print(f"threshold={settings.address_gate}  n={n}")
    print(f"  miss rate      {len(misses)}/{pos}       (assistant looks broken)")
    print(f"  false wake     {len(false_wakes)}/{n - pos}       (assistant acts uninvited)")
    costly = [c for c, _ in false_wakes if c["category"] in COSTLY_FALSE_WAKE]
    print(f"  ^ of which costly: {len(costly)}")
    if lat:
        print(f"  latency p50 {statistics.median(lat):.0f}ms  "
              f"p95 {sorted(lat)[int(len(lat) * 0.95)]:.0f}ms   (budget: 150ms)")

    print("\nper-slice accuracy")
    for cat, rs in sorted(by_cat.items(), key=lambda kv: sum(kv[1]) / len(kv[1])):
        print(f"  {cat:22} {sum(rs)}/{len(rs)}")

    if misses or false_wakes:
        print("\nerrors")
        for c, p in misses + false_wakes:
            want = "wake" if c["addressed"] else "ignore"
            print(f"  p={p:.2f} want={want:6} [{c['category']}] {c['utterance']!r}")
    return 0


def dataclasses_replace(s: Settings, **kw) -> Settings:
    import dataclasses

    return dataclasses.replace(s, **kw)


if __name__ == "__main__":
    raise SystemExit(main())
