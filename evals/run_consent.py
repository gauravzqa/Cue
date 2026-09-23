"""Score the consent parser against evals/consent_cases.jsonl.

The two errors are not symmetric and are never averaged:

  FALSE CONSENT  -- a "no" or a correction read as a yes. This EXECUTES an
                    action the user declined. It is the expensive one.
  REJECTED YES   -- a clear "yes" read as unclear. The user is asked again and,
                    if the second reply also falls short, the action is
                    abandoned. Cheap per occurrence, but it is the failure the
                    user experiences, and at a high enough rate it makes every
                    confirmed action feel broken.

`confirm_yes` was 0.90, which was a guess. Measured over these 160 cases, a
clear spoken yes scores 0.79..0.95 (median 0.91) and a clear no scores
0.04..0.55, so 0.90 rejected 19/60 clear consents -- roughly one in three --
while buying no protection at all: nothing in the `no` or `unclear` slices
reaches even 0.6. The gap between the highest no (0.55) and the lowest yes
(0.79) is where the threshold belongs.

    .venv/bin/python -m evals.run_consent            # FakeJev, plumbing only
    TYPESAFE_API_KEY=... .venv/bin/python -m evals.run_consent --live
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor

from daa.config import Settings
from daa.jev import questions as Q
from daa.jev.client import build_provider

CASES = pathlib.Path(__file__).parent / "consent_cases.jsonl"


def load() -> list[dict]:
    return [json.loads(line) for line in CASES.read_text().splitlines() if line.strip()]


def score_one(provider, case: dict) -> float | None:
    desc = case["spoken_description"]
    state = {
        "reply": case["reply"],
        "proposed_action": {
            "tool": case["action"],
            "spoken_description": desc,
            "targets": [case["action"]],
        },
        "assistant_asked": f"Should I {desc}?",
    }
    try:
        return provider.ask(state, Q.consent_questions(), timeout_s=8).noul(Q.Q_CONSENT)
    except Exception:  # noqa: BLE001 -- a dead provider is a skipped case, not a crash
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="use the real Jev provider")
    ap.add_argument("--workers", type=int, default=12)
    args = ap.parse_args(argv)

    settings = Settings.load() if args.live else Settings(typesafe_api_key="")
    provider = build_provider(settings)
    cases = load()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        scored = list(ex.map(lambda c: (c, score_one(provider, c)), cases))

    by: dict[str, list[tuple[float, dict]]] = {"yes": [], "no": [], "unclear": []}
    for case, p in scored:
        if p is not None:
            by[case["label"]].append((p, case))

    if not any(by.values()):
        print("no scores -- provider unavailable")
        return 1

    print(f"{len(cases)} cases, provider={'live' if args.live else 'fake'}\n")
    for label in ("yes", "no", "unclear"):
        vs = sorted(p for p, _ in by[label])
        if not vs:
            continue
        print(f"  {label:8} n={len(vs):3}  min={vs[0]:.3f}  med={statistics.median(vs):.3f}  max={vs[-1]:.3f}")

    yes_v = [p for p, _ in by["yes"]]
    not_yes = [p for lab in ("no", "unclear") for p, _ in by[lab]]
    print(f"\n  at confirm_yes={settings.confirm_yes}, confirm_no={settings.confirm_no}:")
    rejected = [(p, c) for p, c in by["yes"] if p < settings.confirm_yes]
    false_consent = [(p, c) for lab in ("no", "unclear") for p, c in by[lab] if p >= settings.confirm_yes]
    print(f"    REJECTED YES  {len(rejected):3}/{len(yes_v)}")
    print(f"    FALSE CONSENT {len(false_consent):3}/{len(not_yes)}")
    for p, c in sorted(rejected, key=lambda r: r[0])[:8]:
        print(f"      {p:.3f}  {c['reply']!r:24} <- {c['spoken_description'][:48]}")

    near_miss = sorted(((p, c) for lab in ("no", "unclear") for p, c in by[lab]),
                       key=lambda r: -r[0])[:5]
    print("\n  highest-scoring replies that are NOT consent:")
    for p, c in near_miss:
        print(f"      {p:.3f}  [{c['label']}] {c['reply']!r:24} <- {c['spoken_description'][:44]}")

    if yes_v and not_yes:
        lo_yes, hi_not = min(yes_v), max(not_yes)
        print(f"\n  separation: highest non-yes {hi_not:.3f} .. lowest yes {lo_yes:.3f}")
        if lo_yes > hi_not:
            print(f"  any threshold in ({hi_not:.3f}, {lo_yes:.3f}] separates them perfectly; "
                  f"midpoint {((hi_not + lo_yes) / 2):.2f}")
        else:
            print("  the slices OVERLAP -- no threshold separates them; the question needs work")
    return 0


if __name__ == "__main__":
    sys.exit(main())
