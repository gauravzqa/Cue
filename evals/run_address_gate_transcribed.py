"""Score the address gate on what STT ACTUALLY HEARD, not on what was said.

`run_address_gate.py` feeds the gate the hand-written utterance text. That
measures the gate in isolation, which is useful and also optimistic: in the
real pipeline the gate never sees those strings. It sees whatever the
transcriber produced, and macOS Speech alters the words in 13 of our 40 cases.

Some of those alterations move the label rather than just the wording:

    said : "ugh why is this so slow"      (self_talk, NOT addressed)
    heard: "Why is this so slow?"         -- the mutter is now a clean question

    said : "alexa turn off the lights"    (other_assistant, NOT addressed)
    heard: "lakes are turn off the lights" -- the wake word is gone, and what
                                             is left is a bare imperative

The ground-truth label belongs to what the SPEAKER DID, so it is carried over
unchanged. A gate that gets these wrong is wrong in production no matter how
well it scores on clean text.

    .venv/bin/python -m evals.run_address_gate_transcribed            # FakeJev
    .venv/bin/python -m evals.run_address_gate_transcribed --live     # real Jev
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import re
import statistics
import sys

from daa.config import Settings
from daa.jev.client import build_provider
from daa.jev.gate import AddressGate

HERE = pathlib.Path(__file__).parent
CASES = HERE / "address_gate_cases.jsonl"
# Written by ui/experiments/speech/run.sh -- one row per case, in order.
TRANSCRIPTS = {
    "samantha": HERE.parent / "ui/experiments/speech/results-samantha.json",
    "alex": HERE.parent / "ui/experiments/speech/results-alex.json",
}
COSTLY = {"self_talk", "overheard", "media", "other_assistant", "mention_not_address"}

_norm = lambda s: re.sub(r"[^a-z0-9 ]", " ", s.lower()).split()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--voice", default="samantha", choices=sorted(TRANSCRIPTS))
    ap.add_argument("--threshold", type=float, default=None)
    args = ap.parse_args()

    path = TRANSCRIPTS[args.voice]
    if not path.exists():
        print(f"no transcripts at {path}; run ui/experiments/speech/run.sh", file=sys.stderr)
        return 2

    cases = [json.loads(l) for l in CASES.read_text().splitlines() if l.strip()]
    heard = json.loads(path.read_text())
    if len(heard) != len(cases):
        print(f"transcript count {len(heard)} != case count {len(cases)}", file=sys.stderr)
        return 2

    settings = Settings.load()
    if args.threshold is not None:
        settings = dataclasses.replace(settings, address_gate=args.threshold)
    if args.live and not settings.jev_live:
        print("--live given but TYPESAFE_API_KEY is unset", file=sys.stderr)
        return 2

    gate = AddressGate(build_provider(settings), settings)

    lat: list[float] = []
    misses, false_wakes, synthetic = [], [], False
    altered = 0

    for case, row in zip(cases, heard, strict=True):
        text = (row.get("text") or "").strip()
        if not text:
            # STT returned nothing. The loop never calls the gate on silence,
            # so scoring it as a gate error would blame the wrong component.
            continue
        if _norm(text) != _norm(case["utterance"]):
            altered += 1
        d = gate.should_wake(text, case.get("ctx", {}))
        synthetic = synthetic or d.synthetic
        lat.append(d.latency_ms)
        if d.wake != case["addressed"]:
            (misses if case["addressed"] else false_wakes).append((case, text, d.addressed_p))

    if synthetic:
        print("!! FakeJev -- plumbing only, NOT gate quality.\n")

    scored = len([r for r in heard if (r.get("text") or "").strip()])
    pos = sum(c["addressed"] for c, r in zip(cases, heard, strict=True) if (r.get("text") or "").strip())
    print(f"voice={args.voice}  threshold={settings.address_gate}  scored={scored}/{len(cases)}")
    print(f"  words changed by STT   {altered}/{scored}")
    print(f"  miss rate              {len(misses)}/{pos}")
    print(f"  false wake             {len(false_wakes)}/{scored - pos}")
    costly = [c for c, _, _ in false_wakes if c["category"] in COSTLY]
    print(f"  ^ of which costly      {len(costly)}")
    if lat:
        print(f"  gate latency p50 {statistics.median(lat):.0f}ms")

    if misses or false_wakes:
        print("\nerrors -- 'said' carries the label, 'heard' is what the gate judged")
        for case, text, p in misses + false_wakes:
            want = "wake" if case["addressed"] else "ignore"
            print(f"  p={p:.2f} want={want:6} [{case['category']}]")
            print(f"       said : {case['utterance']!r}")
            print(f"       heard: {text!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
