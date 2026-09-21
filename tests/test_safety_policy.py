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


def action(
    explicit: bool = True,
    targets=("Screenshot 2026-09-20.png",),
    floor_hint: RiskTier | None = None,
    consequences=None,
) -> ResolvedAction:
    return ResolvedAction(
        tool="move_to_trash",
        args={},
        targets=targets,
        explicit=explicit,
        floor_hint=floor_hint,
        consequences=consequences or {},
    )


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
#
# Guessing escalates only where guessing WRONG costs something. Measured on
# live Jev, `reveal_in_finder` came back "guessing" at 0.74 confidence, which
# under the old unconditional rule pinned a read-only Finder reveal at
# CONFIRM_VOICE forever. Revealing the wrong file costs one more sentence;
# trashing the wrong file costs the file.


def test_guessing_forces_confirm_voice_even_with_zero_blast_radius() -> None:
    """Deleting ONE wrong file is a tiny blast radius and a total failure, so
    for a tool that MUTATES, blast radius must not talk us out of confirming."""
    a = assess(blast=0.0, unrecoverable=0.0, requested=1.0, target="guessing", confidence=1.0)
    d = policy.decide(action(), spec(RiskTier.ANNOUNCE), a, LIVE)
    assert d.tier >= RiskTier.CONFIRM_VOICE
    assert "guessing" in d.reason


def test_guessing_does_not_escalate_a_read_only_action() -> None:
    """The measured regression. A tool whose author declared it read-only --
    by tag and by a SILENT floor -- cannot be damaged by resolving to the wrong
    target, so an unsure resolution is not a reason to interrupt anyone."""
    a = assess(blast=0.02, unrecoverable=0.02, requested=0.69, target="guessing", confidence=0.96)
    d = policy.decide(action(), spec(RiskTier.SILENT, tags=("files", "read")), a, LIVE)
    assert d.tier is RiskTier.SILENT


def test_a_guessed_target_still_confirms_when_the_model_says_it_is_big() -> None:
    """Blast radius is a second way IN to the guessing rule, never a way out.
    A tool claiming to be read-only while Jev insists this particular call is
    substantial is a disagreement to resolve with the user, not silently."""
    a = assess(blast=0.6, unrecoverable=0.0, requested=1.0, target="guessing", confidence=1.0)
    d = policy.decide(action(), spec(RiskTier.SILENT, tags=("read",)), a, LIVE)
    assert d.tier >= RiskTier.CONFIRM_VOICE


def test_the_guessing_gate_reads_the_registry_not_the_model() -> None:
    """Same numbers, two tools. The only difference is what a human wrote down
    in the registry about what the tool can reach."""
    a = assess(blast=0.1, unrecoverable=0.0, requested=1.0, target="guessing", confidence=1.0)
    read_only = policy.decide(action(), spec(RiskTier.SILENT, tags=("read",)), a, LIVE)
    mutating = policy.decide(action(), spec(RiskTier.SILENT, tags=("mutates",)), a, LIVE)
    assert read_only.tier is RiskTier.SILENT
    assert mutating.tier is RiskTier.CONFIRM_VOICE


@pytest.mark.parametrize("target", ["certain", "probable"])
def test_known_good_target_confidence_does_not_escalate(target: str) -> None:
    d = policy.decide(action(), spec(RiskTier.SILENT), assess(target=target), LIVE)
    assert d.tier is RiskTier.SILENT


@pytest.mark.parametrize("target", ["", "unsure", "GUESSING", "certain-ish", "probably"])
def test_unrecognised_target_confidence_fails_closed(target: str) -> None:
    """A model update that renames the labels must not open the gate: anything
    unrecognised is read as "guessing", and on a mutating tool that confirms."""
    d = policy.decide(action(), spec(RiskTier.ANNOUNCE), assess(target=target), LIVE)
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
        # ANNOUNCE rather than SILENT: "guessing" only escalates where guessing
        # wrong can cost something, so a read-only spec would never reach the
        # branch whose wording this test is pinning.
        d = policy.decide(action(), spec(RiskTier.ANNOUNCE), a, LIVE)
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
        # guessing on a read-only tool (SILENT floor, no tags) is free now
        (RiskTier.SILENT,       0.0,  0.0,  1.0, "guessing", 1.0, True,  RiskTier.SILENT),
        # ... and still confirms the moment the tool can actually change things
        (RiskTier.ANNOUNCE,     0.0,  0.0,  1.0, "guessing", 1.0, True,  RiskTier.CONFIRM_VOICE),
        # ... or the moment the model says this particular call is not small
        (RiskTier.SILENT,       0.6,  0.0,  1.0, "guessing", 1.0, True,  RiskTier.CONFIRM_VOICE),
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


# --- the five tools, with the numbers live Jev actually returned ------------
#
# Every assessment below is hardcoded from a measured run against the real
# TYPESAFE_API_KEY. Nothing here calls the API: the point is to pin the
# intended product behaviour so it survives without a key, and so a future
# tweak to a threshold has to argue with a real measurement rather than with
# a number somebody invented to make a test pass.
#
# `spec_of` mirrors what the registry declares. test_the_table_matches_the_real
# _registry below fails if tools/ ever drifts from these assumptions, which is
# what stops this table quietly testing a world that no longer exists.

_REGISTRY_SHAPE = {
    # name                 floor                   tags
    "get_clipboard":      (RiskTier.SILENT,        ("clipboard", "read")),
    "spotlight_search":   (RiskTier.SILENT,        ("files", "search", "read")),
    "reveal_in_finder":   (RiskTier.SILENT,        ("files", "finder", "read")),
    "move_to_trash":      (RiskTier.CONFIRM_VOICE, ("files", "destructive", "undoable")),
    "run_shortcut":       (RiskTier.CONFIRM_VOICE, ("shortcuts", "automation", "irreversible")),
    "run_applescript":    (RiskTier.CONFIRM_VISUAL, ("applescript", "escape-hatch",
                                                     "irreversible")),
}


def spec_of(name: str) -> ToolSpec:
    floor, tags = _REGISTRY_SHAPE[name]
    return ToolSpec(name=name, description="", params={}, floor=floor, tags=tags)


@pytest.mark.parametrize(
    ("name", "a", "allowed"),
    [
        # "what's on my clipboard" -- read-only, certain, asked for outright.
        (
            "get_clipboard",
            assess(blast=0.01, unrecoverable=0.01, requested=0.94, target="certain",
                   confidence=0.97),
            {RiskTier.SILENT},
        ),
        # "find my invoice". THE measured regression: both danger answers are
        # ~0.02 at ~0.97 confidence, and all the doubt is in "did they ask for
        # this" (0.38) and "is this the right target" (0.42). Under a flat min
        # over four answers this asked permission before running a search.
        (
            "spotlight_search",
            assess(blast=0.02, unrecoverable=0.02, requested=0.69, target="probable",
                   confidence=0.96),
            {RiskTier.SILENT},
        ),
        # Same shape, but Jev returned "guessing" on the target at 0.74. Under
        # the old unconditional guessing rule this was CONFIRM_VOICE forever.
        (
            "reveal_in_finder",
            assess(blast=0.05, unrecoverable=0.02, requested=0.61, target="guessing",
                   confidence=0.95),
            {RiskTier.SILENT, RiskTier.ANNOUNCE},
        ),
        # The one that must NOT get quieter. Its floor says so, and the floor
        # is the whole point: a reassuring assessment cannot lower it.
        (
            "move_to_trash",
            assess(blast=0.9, unrecoverable=0.2, requested=0.97, target="certain",
                   confidence=0.93),
            {RiskTier.CONFIRM_VOICE},
        ),
        # A shortcut carrying a body of text does something we cannot inspect.
        (
            "run_shortcut",
            assess(blast=1.1, unrecoverable=0.45, requested=0.88, target="certain",
                   confidence=0.71),
            {RiskTier.CONFIRM_VOICE, RiskTier.CONFIRM_VISUAL},
        ),
        # The escape hatch. Voice alone never authorizes it.
        (
            "run_applescript",
            assess(blast=1.4, unrecoverable=0.6, requested=0.9, target="certain",
                   confidence=0.66),
            {RiskTier.CONFIRM_VISUAL},
        ),
    ],
    ids=list(_REGISTRY_SHAPE),
)
def test_live_shaped_assessments_land_on_the_intended_tier(name, a, allowed) -> None:
    d = policy.decide(action(), spec_of(name), a, LIVE)
    names = [t.name for t in allowed]
    assert d.tier in allowed, f"{name}: {d.tier.name} not in {names} -- {d.reason}"


def test_a_read_only_search_is_silent_and_a_trash_is_not() -> None:
    """The one-line summary of both fixes: the quiet ones got quiet, and
    nothing that can destroy something moved a millimetre."""
    searching = assess(blast=0.02, unrecoverable=0.02, requested=0.69, target="probable",
                       confidence=0.96)
    assert policy.decide(action(), spec_of("spotlight_search"), searching, LIVE).tier is (
        RiskTier.SILENT
    )
    assert policy.decide(action(), spec_of("move_to_trash"), SAFEST, LIVE).tier is (
        RiskTier.CONFIRM_VOICE
    )
    assert policy.decide(action(), spec_of("run_applescript"), SAFEST, LIVE).tier is (
        RiskTier.CONFIRM_VISUAL
    )


def test_the_table_matches_the_real_registry() -> None:
    """The table above is only meaningful if tools/ still agrees with it.

    safety/ may not import tools/, so this is a TEST-only cross-check: it skips
    where the registry cannot be built, and fails loudly where a floor or a tag
    has moved underneath the numbers.
    """
    registry = pytest.importorskip("daa.tools.registry")
    real = {s.name: s for s in registry.REGISTRY.specs()}
    for name, (floor, tags) in _REGISTRY_SHAPE.items():
        assert name in real, f"{name} has left the registry"
        assert real[name].floor is floor, f"{name} floor moved to {real[name].floor.name}"
        assert tuple(real[name].tags) == tags, f"{name} tags moved to {real[name].tags}"


# --- the sweep, extended -----------------------------------------------------


def test_the_whole_assessment_space_over_both_kinds_of_tool() -> None:
    """The full cross-product, run over a read-only spec AND a mutating one,
    and crossed with every value `floor_hint` can take.

    Rule 2 reads the spec, so a sweep that only ever saw one kind of tool would
    only ever test half of it; `floor_hint` is a second floor, so a sweep that
    never set one would never test it at all.

    Asserts the three invariants that may never bend:
      * the tool's floor is never lowered,
      * the resolver's floor_hint is never lowered,
      * policy never invents a REFUSE -- one may only ever come from a human,
        i.e. from spec.floor or from a resolver's explicit hint.
    """
    checked = 0
    violations: list[str] = []
    hints = (None, *ALL_TIERS)
    for floor in ALL_TIERS:
        for tags in ((), ("read",), ("mutates",)):
            tool = spec(floor, tags=tags)
            for hint in hints:
                floor_of_hint = hint if hint is not None else RiskTier.SILENT
                for blast in (0.0, 0.4, 0.5, 1.0, 1.6, 2.4, 3.0, -5.0, 99.0, float("nan")):
                    for unrec in (0.0, 0.5, 0.51, 1.0, -1.0):
                        for req in (0.0, 0.49, 0.5, 1.0):
                            for target in ("certain", "probable", "guessing", "", "nonsense"):
                                for conf in (0.0, 0.49, 0.5, 1.0):
                                    for explicit in (True, False):
                                        a = assess(blast, unrec, req, target, conf)
                                        act = action(explicit=explicit, floor_hint=hint)
                                        d = policy.decide(act, tool, a, LIVE)
                                        checked += 1
                                        if d.tier < floor:
                                            violations.append(
                                                f"floor {floor.name} -> {d.tier.name}"
                                            )
                                        if d.tier < floor_of_hint:
                                            violations.append(
                                                f"hint {floor_of_hint.name} -> {d.tier.name}"
                                            )
                                        if (
                                            d.tier is RiskTier.REFUSE
                                            and floor is not RiskTier.REFUSE
                                            and hint is not RiskTier.REFUSE
                                        ):
                                            violations.append(
                                                f"invented REFUSE from {floor.name}/{hint}"
                                            )
    assert checked >= 52_800, f"only swept {checked} combinations"
    assert violations == [], violations[:5]


def test_is_mutating_believes_the_tag_over_the_floor_heuristic() -> None:
    """`is_mutating` used to be a tiebreaker for the Jev-is-down path only, and
    it guessed from the floor. Rule 2 now leans on it, so an author's explicit
    tag has to beat the guess in BOTH directions -- otherwise the floor is the
    only thing that ever speaks and the tags are decoration.

    "read" is the spelling the registry actually uses; it was not in the list.
    """
    read_tagged = spec(RiskTier.ANNOUNCE, tags=("files", "read"))
    assert policy.is_mutating(read_tagged) is False

    mutating_tagged = spec(RiskTier.SILENT, tags=("mutates",))
    assert policy.is_mutating(mutating_tagged) is True

    # No tags at all: fall back to the floor, exactly as before.
    assert policy.is_mutating(spec(RiskTier.SILENT)) is False
    assert policy.is_mutating(spec(RiskTier.ANNOUNCE)) is True


def test_a_read_tagged_tool_above_the_silent_floor_is_not_confirmed_for_guessing() -> None:
    """The case where the tag is the only signal: a read-only tool that is
    noisy enough to deserve an ANNOUNCE floor still must not demand a
    confirmation just because the target resolution was unsure."""
    a = assess(blast=0.1, unrecoverable=0.0, requested=1.0, target="guessing", confidence=1.0)
    d = policy.decide(action(), spec(RiskTier.ANNOUNCE, tags=("files", "read")), a, LIVE)
    assert d.tier is RiskTier.ANNOUNCE


# --- the resolver's floor_hint ----------------------------------------------
#
# `spec.floor` is per-TOOL. A click on a nav link and a click on a
# `Pay $412.00` submit button are the same tool at the same floor, and until
# `floor_hint` existed the only thing that could tell them apart was a judgment
# model -- which is the thing floors exist to defend against. The hint is
# raised by the RESOLVER: deterministic code that read the form's method, the
# button's accessible name, the overwrite collision that is actually on disk.


@pytest.mark.parametrize("hint", ALL_TIERS, ids=[t.name for t in ALL_TIERS])
@pytest.mark.parametrize("floor", ALL_TIERS, ids=[t.name for t in ALL_TIERS])
def test_floor_hint_can_only_ever_raise(floor: RiskTier, hint: RiskTier) -> None:
    """Every (floor, hint) pair, including every pair where the hint is
    strictly LOWER than the floor. A hint below the floor must be inert: if it
    could pull a tier down, a resolver bug would be a silent execution, and the
    field would have become the opposite of what it is for."""
    with_hint = policy.decide(action(floor_hint=hint), spec(floor), SAFEST, LIVE)
    without = policy.decide(action(), spec(floor), SAFEST, LIVE)

    assert with_hint.tier >= without.tier, "a hint lowered a tier"
    assert with_hint.tier >= floor, "a hint went under the tool's floor"
    assert with_hint.tier >= hint, "a hint did not reach its own level"
    assert with_hint.tier is RiskTier(max(int(floor), int(hint), int(without.tier)))


@pytest.mark.parametrize("floor", ALL_TIERS, ids=[t.name for t in ALL_TIERS])
def test_an_unset_hint_changes_nothing_at_all(floor: RiskTier) -> None:
    """The overwhelmingly common case has to be exactly free."""
    for a in (SAFEST, assess(blast=2.0, unrecoverable=0.9, requested=0.0, target="guessing"), None):
        plain = policy.decide(action(), spec(floor), a, LIVE)
        none_hint = policy.decide(action(floor_hint=None), spec(floor), a, LIVE)
        silent_hint = policy.decide(action(floor_hint=RiskTier.SILENT), spec(floor), a, LIVE)
        assert plain.tier is none_hint.tier is silent_hint.tier
        assert plain.reason == none_hint.reason == silent_hint.reason


def test_a_payment_button_is_confirmed_on_a_tool_that_usually_is_not() -> None:
    """THE case this field was added for. Same tool, same floor, same
    reassuring judgment -- one of them charges the user $412."""
    nav_link = action(targets=("Pricing",))
    pay = action(
        targets=("Pay $412.00",),
        floor_hint=RiskTier.CONFIRM_VISUAL,
        consequences={"payment": "that button charges you $412.00"},
    )
    click = spec(RiskTier.ANNOUNCE, name="browser_click", tags=("browser",))
    reassuring = assess(blast=0.3, unrecoverable=0.1, requested=0.95, target="certain",
                        confidence=0.9)

    assert policy.decide(nav_link, click, reassuring, LIVE).tier is RiskTier.ANNOUNCE
    assert policy.decide(pay, click, reassuring, LIVE).tier is RiskTier.CONFIRM_VISUAL


def test_the_reason_says_what_the_resolver_found_not_a_generic_line() -> None:
    """"Why are you asking?" about a payment button has to be answered with
    "because it is a payment button". Policy does not know why the hint was
    raised, but the resolver's own `consequences` do, so the reason is built
    from those rather than from a stock sentence."""
    pay = action(
        targets=("Pay $412.00",),
        floor_hint=RiskTier.CONFIRM_VISUAL,
        consequences={"payment": "that button charges you $412.00"},
    )
    d = policy.decide(pay, spec(RiskTier.ANNOUNCE), assess(), LIVE)
    assert "charges you $412.00" in d.reason
    assert "kind of thing" not in d.reason, "the generic floor line won instead"


def test_the_hint_explains_itself_even_when_it_outranks_everything_else() -> None:
    """The hint takes ties: it is the only one of the three claims that looked
    at THIS invocation, so it out-explains both the tool's floor and the
    model's 'this can't be undone' when all three land on the same tier."""
    unrecoverable = assess(blast=0.0, unrecoverable=0.9, requested=1.0, target="certain")
    pay = action(
        floor_hint=RiskTier.CONFIRM_VISUAL,
        consequences={"payment": "that button charges you $412.00"},
    )
    d = policy.decide(pay, spec(RiskTier.CONFIRM_VISUAL), unrecoverable, LIVE)
    assert d.tier is RiskTier.CONFIRM_VISUAL
    assert "charges you" in d.reason


def test_a_hint_raised_without_saying_why_still_explains_the_tier() -> None:
    """A resolver that raises the floor and sets no consequences is a resolver
    bug -- but going quiet about it would be a worse one."""
    d = policy.decide(action(floor_hint=RiskTier.CONFIRM_VOICE), spec(RiskTier.SILENT),
                      SAFEST, LIVE)
    assert d.tier is RiskTier.CONFIRM_VOICE
    assert "more care than usual" in d.reason


def test_a_bigger_judgment_still_out_explains_a_smaller_hint() -> None:
    """The hint takes ties, not everything. If the model found something worse
    than the resolver did, the model's reason is the honest one."""
    a = assess(blast=0.0, unrecoverable=0.9, requested=1.0, target="certain")
    d = policy.decide(action(floor_hint=RiskTier.ANNOUNCE), spec(RiskTier.SILENT), a, LIVE)
    assert d.tier is RiskTier.CONFIRM_VISUAL
    assert "undone" in d.reason


def test_the_hint_survives_a_jev_outage() -> None:
    """A resolver that determined this is a payment form does not stop being
    right because the judgment model is down. If anything, that is when it
    matters most: fail-closed raises the FLOOR by one, which knows nothing
    about this particular invocation."""
    blind = policy.decide(action(), spec(RiskTier.SILENT), None, LIVE)
    hinted = policy.decide(
        action(floor_hint=RiskTier.CONFIRM_VISUAL,
               consequences={"payment": "that button charges you $412.00"}),
        spec(RiskTier.SILENT),
        None,
        LIVE,
    )
    assert blind.tier < RiskTier.CONFIRM_VISUAL
    assert hinted.tier is RiskTier.CONFIRM_VISUAL
    assert "charges you" in hinted.reason


@pytest.mark.parametrize("floor", ALL_TIERS, ids=[t.name for t in ALL_TIERS])
@pytest.mark.parametrize("hint", ALL_TIERS, ids=[t.name for t in ALL_TIERS])
def test_the_hint_holds_with_no_assessment_at_every_floor(floor, hint) -> None:
    d = policy.decide(action(floor_hint=hint), spec(floor), None, LIVE)
    assert d.tier >= hint and d.tier >= floor


def test_a_hint_and_a_guessed_target_do_not_disagree_silently() -> None:
    """Where floor_hint and is_mutating overlap.

    `is_mutating` asks "can this TOOL do damage", which for a browser click is
    the same answer for a nav link and for a Pay button. `floor_hint` asks "did
    THIS ONE turn out to be dangerous". If the resolver says yes, then being
    unsure which element we resolved to is suddenly worth a sentence -- so a
    raised hint is a third way into the guessing rule. Without that the two
    would disagree quietly: the hint lifts the floor to ANNOUNCE and the
    guessing rule, reading only the tool, declines to take it further.
    """
    guessing = assess(blast=0.1, unrecoverable=0.0, requested=1.0, target="guessing",
                      confidence=1.0)
    read_only_tool = spec(RiskTier.SILENT, name="browser_click", tags=("browser", "read"))

    assert policy.decide(action(), read_only_tool, guessing, LIVE).tier is RiskTier.SILENT
    hinted = policy.decide(action(floor_hint=RiskTier.ANNOUNCE), read_only_tool, guessing, LIVE)
    assert hinted.tier is RiskTier.CONFIRM_VOICE
    assert "guessing" in hinted.reason


@pytest.mark.parametrize("raw", ["payment", object(), float("nan"), (), {"tier": 3}])
def test_an_unreadable_hint_is_treated_as_a_warning_not_as_silence(raw) -> None:
    """A hint exists because a resolver looked at the world and concluded this
    one is worse than it looks. Dropping a malformed one turns a resolver bug
    into a silent execution -- the exact failure the field was added to stop.
    Same rule as an unrecognised target_confidence label becoming "guessing".
    """
    bad = ResolvedAction(tool="browser_click", args={}, targets=("x",), floor_hint=raw)
    d = policy.decide(bad, spec(RiskTier.SILENT), SAFEST, LIVE)
    assert d.tier >= RiskTier.CONFIRM_VOICE


@pytest.mark.parametrize(("raw", "expected"), [(0, RiskTier.SILENT), (2, RiskTier.CONFIRM_VOICE),
                                               (4, RiskTier.REFUSE), (99, RiskTier.CONFIRM_VISUAL),
                                               (-3, RiskTier.SILENT)])
def test_a_numeric_hint_is_clamped_into_the_scale(raw, expected) -> None:
    """Off the top of the scale is still a claim that this is serious; below
    the bottom is not a claim at all."""
    bad = ResolvedAction(tool="browser_click", args={}, targets=("x",), floor_hint=raw)
    assert policy.floor_hint(bad) is expected


def test_a_hint_is_the_only_way_policy_may_reach_refuse() -> None:
    """REFUSE stays a human's decision. A resolver IS a human's decision --
    deterministic code somebody wrote and reviewed -- so a hint of REFUSE is
    honoured, exactly as spec.floor=REFUSE is. What policy still may not do is
    invent one from a score."""
    worst = assess(blast=3.0, unrecoverable=1.0, requested=0.0, target="guessing", confidence=0.0)
    assert policy.decide(action(explicit=False), spec(RiskTier.SILENT), worst, LIVE).tier is (
        RiskTier.CONFIRM_VISUAL
    )
    assert policy.decide(action(floor_hint=RiskTier.REFUSE), spec(RiskTier.SILENT),
                         SAFEST, LIVE).tier is RiskTier.REFUSE
