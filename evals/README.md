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
