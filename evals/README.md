# Evals

The address gate is the riskiest assumption in daa: an always-on mic is only
viable if "is this addressed to me?" is both fast and accurate. Everything else
degrades gracefully; this one fails as either a dead assistant (misses) or a
creepy one (false wakes).

`address_gate_cases.jsonl` is a hand-labelled set biased HARD toward the
failure modes, not the easy cases. A gate that scores well on "hey daa, open
safari" tells you nothing. The cases that matter are the ones where a human
is talking near the machine and not to it.

Fields:
  utterance   what STT produced (including plausible STT errors)
  addressed   ground truth
  category    for per-slice accuracy — misses in `self_talk` are cheap,
              misses in `direct_command` are the product not working
  note        why this case is here

Run: `.venv/bin/python -m evals.run_address_gate`  (needs TYPESAFE_API_KEY)
Without a key it runs against FakeJev and only exercises plumbing.

## What to measure
Not raw accuracy — the classes are imbalanced and the costs are asymmetric.
  - false wake rate on `overheard` + `self_talk`  (privacy + annoyance cost)
  - miss rate on `direct_command`                 (product-is-broken cost)
  - p50 / p95 latency                             (must stay under ~150ms)
Then pick `Settings.address_gate` from the ROC, rather than keeping 0.85
because it looked like a reasonable number.

## consent (`run_consent.py`)

160 replies × 5 actions, labelled `yes` / `no` / `unclear`, including the ones
a keyword parser gets backwards: "yeah no", "sure but not that one", "no, the
other one".

    .venv/bin/python -m evals.run_consent --live

The two errors are never averaged. A **false consent** executes something the
user declined. A **rejected yes** makes daa ask again and then abandon the
action — cheap once, and the thing the user actually experiences.

Measured against live Jev: a clear yes scores 0.79–0.95, everything that is
not consent tops out at 0.64, and the gap between them is where the threshold
belongs. `confirm_yes` was 0.90 — above ordinary consent — which rejected 23 of
60 clear yeses and caught nothing in exchange.
