"""Adversarial tests for the risk gate.

The bar for this file: a missing test here is a deleted file on someone's
machine. So the one-way rule is asserted over the WHOLE tier enum rather than a
sampled example, and every escalation rule has a test that fails if the rule is
deleted -- not merely one that passes while it exists.
"""

from __future__ import annotations

import math

import pytest

from daa.config import Settings
from daa.contracts import Disposition, ResolvedAction, RiskAssessment, RiskTier, ToolSpec
from daa.safety import policy

LIVE = Settings(dry_run=False)
DRY = Settings(dry_run=True)

ALL_TIERS = tuple(RiskTier)


def spec(floor: RiskTier = RiskTier.ANNOUNCE, **kw) -> ToolSpec:
    return ToolSpec(
        name=kw.pop("name", "move_to_trash"),
        description="",
        params={},
        floor=floor,
        **kw,
    )


def action(explicit: bool = True, targets=("Screenshot 2026-09-20.png",)) -> ResolvedAction:
    return ResolvedAction(tool="move_to_trash", args={}, targets=targets, explicit=explicit)


def assess(
    blast: float = 0.0,
    unrecoverable: float = 0.0,
    requested: float = 1.0,
    target: str = "certain",
    confidence: float = 1.0,
    synthetic: bool = False,
) -> RiskAssessment:
    return RiskAssessment(blast, unrecoverable, requested, target, confidence, synthetic)


# The assessment a maximally over-confident Jev could produce: nothing at
# stake, perfectly recoverable, explicitly asked for, certain of the target,
# certain of itself. If anything can push a tier below the floor, it is this.
SAFEST = assess(blast=0.0, unrecoverable=0.0, requested=1.0, target="certain", confidence=1.0)


# --- the one-way rule -------------------------------------------------------


@pytest.mark.parametrize("floor", ALL_TIERS, ids=[t.name for t in ALL_TIERS])
def test_safest_possible_assessment_never_goes_below_the_floor(floor: RiskTier) -> None:
    d = policy.decide(action(), spec(floor), SAFEST, LIVE)
    assert d.tier >= floor, f"{floor.name} was lowered to {d.tier.name}"


@pytest.mark.parametrize("floor", ALL_TIERS, ids=[t.name for t in ALL_TIERS])
@pytest.mark.parametrize("settings", [LIVE, DRY], ids=["live", "dry_run"])
def test_no_assessment_at_all_never_goes_below_the_floor(floor, settings) -> None:
    d = policy.decide(action(), spec(floor), None, settings)
    assert d.tier >= floor


@pytest.mark.parametrize("floor", ALL_TIERS, ids=[t.name for t in ALL_TIERS])
def test_floor_holds_across_the_whole_assessment_space(floor: RiskTier) -> None:
    """Sweep the corners of every input at once -- not one rule at a time."""
    for blast in (0.0, 0.4, 1.0, 1.6, 2.4, 3.0, -5.0, 99.0):
        for unrec in (0.0, 0.5, 0.51, 1.0, -1.0):
            for req in (0.0, 0.49, 0.5, 1.0):
                for target in ("certain", "probable", "guessing", "", "CERTAIN", "nonsense"):
                    for conf in (0.0, 0.49, 0.5, 1.0):
                        for explicit in (True, False):
                            d = policy.decide(
                                action(explicit=explicit),
                                spec(floor),
                                assess(blast, unrec, req, target, conf),
                                LIVE,
                            )
                            assert d.tier >= floor


def test_refuse_floor_is_absorbing() -> None:
    """A REFUSE tool stays REFUSE however good the news is."""
    assert policy.decide(action(), spec(RiskTier.REFUSE), SAFEST, LIVE).tier is RiskTier.REFUSE
    assert policy.decide(action(), spec(RiskTier.REFUSE), None, DRY).tier is RiskTier.REFUSE


def test_policy_never_invents_a_refusal() -> None:
    """Escalation tops out at CONFIRM_VISUAL: REFUSE is a human's decision."""
    worst = assess(blast=3.0, unrecoverable=1.0, requested=0.0, target="guessing", confidence=0.0)
    for floor in (RiskTier.SILENT, RiskTier.ANNOUNCE, RiskTier.CONFIRM_VOICE):
        d = policy.decide(action(explicit=False), spec(floor), worst, LIVE)
        assert d.tier is RiskTier.CONFIRM_VISUAL


# --- rule 1: low confidence escalates ---------------------------------------


@pytest.mark.parametrize("confidence", [0.0, 0.1, 0.49])
def test_low_confidence_forces_at_least_confirm_voice(confidence: float) -> None:
    d = policy.decide(action(), spec(RiskTier.SILENT), assess(confidence=confidence), LIVE)
    assert d.tier >= RiskTier.CONFIRM_VOICE


def test_confident_and_tiny_stays_cheap() -> None:
    """The counterpart: if the rule fired on everything it would be useless."""
    d = policy.decide(action(), spec(RiskTier.SILENT), assess(confidence=0.99), LIVE)
    assert d.tier is RiskTier.SILENT


def test_confidence_threshold_boundary() -> None:
    low = policy.decide(action(), spec(RiskTier.SILENT), assess(confidence=0.4999), LIVE)
    at = policy.decide(action(), spec(RiskTier.SILENT), assess(confidence=0.5), LIVE)
    assert low.tier >= RiskTier.CONFIRM_VOICE
    assert at.tier is RiskTier.SILENT


# --- rule 2: guessing at the target -----------------------------------------


def test_guessing_forces_confirm_voice_even_with_zero_blast_radius() -> None:
    """Deleting ONE wrong file is a tiny blast radius and a total failure."""
    a = assess(blast=0.0, unrecoverable=0.0, requested=1.0, target="guessing", confidence=1.0)
    d = policy.decide(action(), spec(RiskTier.SILENT), a, LIVE)
    assert d.tier >= RiskTier.CONFIRM_VOICE


@pytest.mark.parametrize("target", ["certain", "probable"])
def test_known_good_target_confidence_does_not_escalate(target: str) -> None:
    d = policy.decide(action(), spec(RiskTier.SILENT), assess(target=target), LIVE)
    assert d.tier is RiskTier.SILENT


@pytest.mark.parametrize("target", ["", "unsure", "GUESSING", "certain-ish", "probably"])
def test_unrecognised_target_confidence_fails_closed(target: str) -> None:
    """A model update that renames the labels must not open the gate."""
    d = policy.decide(action(), spec(RiskTier.SILENT), assess(target=target), LIVE)
    assert d.tier >= RiskTier.CONFIRM_VOICE


def test_target_confidence_is_case_and_space_insensitive_for_known_values() -> None:
    d = policy.decide(action(), spec(RiskTier.SILENT), assess(target="  Certain "), LIVE)
    assert d.tier is RiskTier.SILENT


# --- rule 3: unrecoverable needs eyes ---------------------------------------


@pytest.mark.parametrize("unrec", [0.51, 0.8, 1.0])
def test_unrecoverable_forces_confirm_visual(unrec: float) -> None:
    d = policy.decide(action(), spec(RiskTier.SILENT), assess(unrecoverable=unrec), LIVE)
    assert d.tier >= RiskTier.CONFIRM_VISUAL


def test_unrecoverable_beats_a_perfect_assessment_everywhere_else() -> None:
    """'yes' is easy to mishear; voice alone can never authorize this."""
    a = assess(blast=0.0, unrecoverable=0.9, requested=1.0, target="certain", confidence=1.0)
    assert policy.decide(action(), spec(), a, LIVE).tier is RiskTier.CONFIRM_VISUAL


def test_unrecoverable_boundary_is_strictly_greater_than() -> None:
    at = policy.decide(action(), spec(RiskTier.SILENT), assess(unrecoverable=0.5), LIVE)
    over = policy.decide(action(), spec(RiskTier.SILENT), assess(unrecoverable=0.5001), LIVE)
    assert at.tier < RiskTier.CONFIRM_VISUAL
    assert over.tier is RiskTier.CONFIRM_VISUAL


# --- rule 4: we inferred it --------------------------------------------------


@pytest.mark.parametrize(
    ("requested", "base_blast", "expected"),
    [
        (0.0, 0.0, RiskTier.ANNOUNCE),          # SILENT      -> ANNOUNCE
        (0.2, 1.0, RiskTier.CONFIRM_VOICE),     # ANNOUNCE    -> CONFIRM_VOICE
        (0.49, 2.0, RiskTier.CONFIRM_VISUAL),   # VOICE       -> VISUAL
        (0.0, 3.0, RiskTier.CONFIRM_VISUAL),    # VISUAL      -> capped
    ],
)
def test_inferred_action_escalates_exactly_one_tier(requested, base_blast, expected) -> None:
    a = assess(blast=base_blast, requested=requested)
    assert policy.decide(action(), spec(RiskTier.SILENT), a, LIVE).tier is expected


def test_resolver_explicit_flag_escalates_even_when_jev_disagrees() -> None:
    """ResolvedAction.explicit is a hard input: the resolver knows things Jev
    never sees, so either signal saying 'we inferred this' is enough."""
    a = assess(blast=0.0, requested=1.0)
    assert policy.decide(action(explicit=True), spec(RiskTier.SILENT), a, LIVE).tier is (
        RiskTier.SILENT
    )
    assert policy.decide(action(explicit=False), spec(RiskTier.SILENT), a, LIVE).tier is (
        RiskTier.ANNOUNCE
    )


def test_explicit_request_does_not_de_escalate_anything() -> None:
    """The bump is one-way too: being asked outright is not a discount."""
    a = assess(blast=2.0, requested=1.0)
    assert policy.decide(action(), spec(RiskTier.SILENT), a, LIVE).tier is RiskTier.CONFIRM_VOICE


# --- rule 5: no assessment means fail closed --------------------------------


def test_missing_assessment_raises_the_floor_by_one() -> None:
    d = policy.decide(action(), spec(RiskTier.CONFIRM_VOICE), None, LIVE)
    assert d.tier is RiskTier.CONFIRM_VISUAL


def test_missing_assessment_on_a_mutating_tool_floors_at_confirm_voice() -> None:
    d = policy.decide(action(), spec(RiskTier.ANNOUNCE), None, LIVE)
    assert d.tier is RiskTier.CONFIRM_VOICE


def test_missing_assessment_on_a_read_only_tool_stays_cheap() -> None:
    """A Jev outage should degrade the assistant, not brick it: reads still run."""
    d = policy.decide(action(), spec(RiskTier.SILENT), None, LIVE)
    assert d.tier is RiskTier.ANNOUNCE


def test_missing_assessment_on_a_silent_but_mutating_tool_still_confirms() -> None:
    d = policy.decide(action(), spec(RiskTier.SILENT, tags=("mutates",)), None, LIVE)
    assert d.tier is RiskTier.CONFIRM_VOICE


def test_missing_assessment_never_means_proceed_for_any_mutating_floor() -> None:
    for floor in ALL_TIERS:
        s = spec(floor, tags=("mutates",))
        assert policy.decide(action(), s, None, LIVE).tier >= RiskTier.CONFIRM_VOICE


def test_disposition_carries_the_assessment_through() -> None:
    a = assess(synthetic=True)
    d = policy.decide(action(), spec(), a, LIVE)
    assert d.assessment is a
    assert policy.decide(action(), spec(), None, LIVE).assessment is None


# --- rule 6: dry run --------------------------------------------------------


def test_dry_run_says_so_in_the_spoken_reason() -> None:
    d = policy.decide(action(), spec(), SAFEST, DRY)
    assert d.reason.startswith(policy.DRY_RUN_PREFIX)
    assert "nothing will actually change" in d.reason


def test_dry_run_does_not_relax_the_gate() -> None:
    """A dry run that lowered the tier would leave the live path untested, and
    the flag is honoured by the executor, which safety/ does not control."""
    a = assess(blast=3.0, unrecoverable=1.0, target="guessing", confidence=0.1)
    assert policy.decide(action(), spec(), a, DRY).tier == policy.decide(
        action(), spec(), a, LIVE
    ).tier


def test_live_run_reason_has_no_dry_run_prefix() -> None:
    assert not policy.decide(action(), spec(), SAFEST, LIVE).reason.startswith(
        policy.DRY_RUN_PREFIX
    )


# --- rule 7: the reason is spoken -------------------------------------------


def _all_dispositions() -> list[Disposition]:
    out = []
    for floor in ALL_TIERS:
        for settings in (LIVE, DRY):
            out.append(policy.decide(action(), spec(floor), None, settings))
            for blast in (0.0, 1.0, 2.0, 3.0):
                for unrec in (0.0, 1.0):
                    for req in (0.0, 1.0):
                        for target in ("certain", "guessing"):
                            for conf in (0.2, 1.0):
                                out.append(
                                    policy.decide(
                                        action(),
                                        spec(floor),
                                        assess(blast, unrec, req, target, conf),
                                        settings,
                                    )
                                )
    return out


def test_every_reachable_reason_is_a_short_spoken_sentence() -> None:
    for d in _all_dispositions():
        r = d.reason
        assert r and r[0].isupper(), r
        assert r.endswith("."), r
        assert len(r) <= 140, r
        # Spoken means no log furniture: no paths, no ids, no markdown, no
        # tool names the user has never heard.
        for junk in ("/", "_", "`", "*", "{", "RiskTier", "None"):
            assert junk not in r, (junk, r)


def test_the_reason_names_the_escalation_that_actually_won() -> None:
    cases = [
        (assess(unrecoverable=0.9), "undone"),
        (assess(target="guessing"), "guessing"),
        (assess(confidence=0.1), "confident"),
        (assess(requested=0.0), "didn't ask"),
    ]
    for a, needle in cases:
        d = policy.decide(action(), spec(RiskTier.SILENT), a, LIVE)
        assert needle in d.reason, (needle, d.reason)
    assert "couldn't check" in policy.decide(action(), spec(), None, LIVE).reason


def test_reason_explains_the_floor_when_the_floor_is_what_won() -> None:
    d = policy.decide(action(), spec(RiskTier.CONFIRM_VISUAL), SAFEST, LIVE)
    assert d.tier is RiskTier.CONFIRM_VISUAL
    assert "always" in d.reason


# --- garbage in ------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "at_least"),
    [
        # A NaN means the judgment layer misbehaved, which is a reason to be
        # MORE careful. Each field fails closed at its own worst value, so the
        # minimum each one guarantees differs -- but none of them is SILENT.
        (assess(blast=math.nan), RiskTier.CONFIRM_VISUAL),
        (assess(unrecoverable=math.nan), RiskTier.CONFIRM_VISUAL),
        (assess(confidence=math.nan), RiskTier.CONFIRM_VOICE),
        (assess(requested=math.nan), RiskTier.ANNOUNCE),
        (assess(blast=float("inf")), RiskTier.CONFIRM_VISUAL),
        (
            assess(blast=-99.0, unrecoverable=-99.0, requested=-99.0, confidence=-99.0),
            RiskTier.CONFIRM_VISUAL,
        ),
    ],
    ids=["nan_blast", "nan_unrec", "nan_conf", "nan_req", "inf_blast", "negatives"],
)
def test_garbage_numbers_resolve_in_the_cautious_direction(a, at_least) -> None:
    d = policy.decide(action(), spec(RiskTier.SILENT), a, LIVE)
    assert d.tier > RiskTier.SILENT
    assert d.tier >= at_least


def test_non_numeric_assessment_fields_do_not_crash_the_loop() -> None:
    a = RiskAssessment("lots", None, "yes", "certain", "high")  # type: ignore[arg-type]
    d = policy.decide(action(), spec(RiskTier.SILENT), a, LIVE)
    assert d.tier >= RiskTier.CONFIRM_VOICE


# --- purity ----------------------------------------------------------------


def test_decide_is_deterministic() -> None:
    a = assess(blast=1.7, unrecoverable=0.3, requested=0.8, target="probable", confidence=0.7)
    first = policy.decide(action(), spec(), a, LIVE)
    for _ in range(5):
        again = policy.decide(action(), spec(), a, LIVE)
        assert (again.tier, again.reason) == (first.tier, first.reason)


def test_safety_package_does_not_import_the_other_subsystems() -> None:
    """Run in a subprocess: mutating this interpreter's sys.modules to prove a
    point would leave every other test importing a different daa."""
    import subprocess
    import sys

    probe = (
        "import daa.safety, sys; "
        "print([m for m in sys.modules if m.startswith(('daa.jev','daa.tools','daa.voice'))])"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]", out.stdout


# --- the matrix -------------------------------------------------------------


@pytest.mark.parametrize(
    ("floor", "blast", "unrec", "req", "target", "conf", "explicit", "expected"),
    [
        # floor                 blast unrec req  target      conf explicit expected
        (RiskTier.SILENT,       0.0,  0.0,  1.0, "certain",  1.0, True,  RiskTier.SILENT),
        (RiskTier.SILENT,       0.6,  0.0,  1.0, "certain",  1.0, True,  RiskTier.ANNOUNCE),
        (RiskTier.SILENT,       1.6,  0.0,  1.0, "certain",  1.0, True,  RiskTier.CONFIRM_VOICE),
        (RiskTier.SILENT,       2.6,  0.0,  1.0, "certain",  1.0, True,  RiskTier.CONFIRM_VISUAL),
        (RiskTier.SILENT,       0.0,  0.0,  1.0, "guessing", 1.0, True,  RiskTier.CONFIRM_VOICE),
        (RiskTier.SILENT,       0.0,  0.0,  1.0, "certain",  0.3, True,  RiskTier.CONFIRM_VOICE),
        (RiskTier.SILENT,       0.0,  0.9,  1.0, "certain",  1.0, True,  RiskTier.CONFIRM_VISUAL),
        (RiskTier.SILENT,       0.0,  0.0,  0.1, "certain",  1.0, True,  RiskTier.ANNOUNCE),
        (RiskTier.SILENT,       0.0,  0.0,  1.0, "certain",  1.0, False, RiskTier.ANNOUNCE),
        (RiskTier.ANNOUNCE,     0.0,  0.0,  1.0, "certain",  1.0, True,  RiskTier.ANNOUNCE),
        (RiskTier.CONFIRM_VOICE, 0.0, 0.0,  1.0, "certain",  1.0, True,  RiskTier.CONFIRM_VOICE),
        (RiskTier.CONFIRM_VOICE, 3.0, 1.0,  0.0, "guessing", 0.0, False, RiskTier.CONFIRM_VISUAL),
        (RiskTier.CONFIRM_VISUAL, 0.0, 0.0, 1.0, "certain",  1.0, True,  RiskTier.CONFIRM_VISUAL),
        (RiskTier.REFUSE,       0.0,  0.0,  1.0, "certain",  1.0, True,  RiskTier.REFUSE),
        # the mishearing this module exists for: "trash the screenshots" heard
        # as something else, Jev unsure of the target, small blast radius.
        (RiskTier.ANNOUNCE,     0.7,  0.2,  0.9, "guessing", 0.8, True,  RiskTier.CONFIRM_VOICE),
        # inferred AND unrecoverable: already at the cap, stays at the cap.
        (RiskTier.ANNOUNCE,     0.0,  1.0,  0.0, "certain",  1.0, True,  RiskTier.CONFIRM_VISUAL),
    ],
)
def test_tier_matrix(floor, blast, unrec, req, target, conf, explicit, expected) -> None:
    d = policy.decide(
        action(explicit=explicit),
        spec(floor),
        assess(blast, unrec, req, target, conf),
        LIVE,
    )
    assert d.tier is expected


def test_a_resolver_that_guessed_escalates_at_every_tier_it_can() -> None:
    """The dead clause.

    policy.decide() has read `action.explicit` since it was written, but every
    resolver in tools/ hardcoded it to True, so the branch had never once
    fired in production. It is being set honestly now -- False when a resolver
    had to guess which thing the user meant -- and this is the test that says
    what must happen when it is.

    Jev is told the opposite here (`requested=1.0`, "they asked for this
    outright") precisely so the assertion cannot pass through the
    explicitly_requested path: the resolver's own flag has to carry it alone.
    """
    a = assess(requested=1.0, target="certain", confidence=1.0)
    for base_blast, expected in (
        (0.0, RiskTier.ANNOUNCE),          # SILENT       -> ANNOUNCE
        (0.6, RiskTier.CONFIRM_VOICE),     # ANNOUNCE     -> CONFIRM_VOICE
        (1.6, RiskTier.CONFIRM_VISUAL),    # CONFIRM_VOICE-> CONFIRM_VISUAL
        (2.6, RiskTier.CONFIRM_VISUAL),    # already at the cap
    ):
        guessed = assess(blast=base_blast, requested=1.0)
        d = policy.decide(action(explicit=False), spec(RiskTier.SILENT), guessed, LIVE)
        assert d.tier is expected, f"blast={base_blast}"
        if expected is not RiskTier.CONFIRM_VISUAL or base_blast < 2.5:
            assert "didn't ask for this directly" in d.reason

    # ... and the same inputs with explicit=True do not move at all, so the
    # test fails if the clause is deleted rather than passing either way.
    assert policy.decide(action(explicit=True), spec(RiskTier.SILENT), a, LIVE).tier is (
        RiskTier.SILENT
    )


def test_an_inferred_action_never_escalates_past_the_policy_cap() -> None:
    """Even a guessed target on an unrecoverable action cannot become REFUSE:
    "never automate this" stays a human's decision, written in the registry."""
    worst = assess(blast=3.0, unrecoverable=1.0, requested=0.0, target="guessing", confidence=0.0)
    d = policy.decide(action(explicit=False), spec(RiskTier.CONFIRM_VISUAL), worst, LIVE)
    assert d.tier is RiskTier.CONFIRM_VISUAL
