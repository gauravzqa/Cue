"""The twelve clauses, one test each, plus the things that make them stick.

Every test here flips EXACTLY ONE input away from a baseline that satisfies all
twelve. That shape is deliberate: a test that changes two things cannot tell you
which clause caught it, and a clause nobody can point at is a clause that can be
deleted in a refactor without a failure.

The readback tests are the other half. `grant_satisfies` bounds the WORST case;
the readback is the only thing standing between the design and the TYPICAL case,
which is a user who has learned the shape of the sentence and says yes at clause
two. So the four clauses are asserted structurally, exactly the way
test_tools_readback pins the action readback.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from daa.contracts import (
    Budget,
    Disposition,
    Grant,
    GrantScope,
    ResolvedAction,
    RiskAssessment,
    RiskTier,
    ToolSpec,
)
from daa.safety.grant import (
    GrantBook,
    GrantState,
    WarrantBook,
    action_digest,
    budget_exhausted,
    grant_satisfies,
    issue_grant,
    readback,
    visual_card,
)

NOW = 1010.0

SAFE = RiskAssessment(
    blast_radius=1.0,
    unrecoverable=0.1,
    explicitly_requested=1.0,
    target_confidence="certain",
    confidence=0.9,
)

MOVE = ToolSpec(
    name="move_files",
    description="Move files between folders",
    params={},
    floor=RiskTier.ANNOUNCE,
    inverses=("move_files",),
)


def _grant(**over: Any) -> Grant:
    base: dict[str, Any] = {
        "id": "g1",
        "goal": "clear out downloads",
        "plan_summary": "(the sentence they heard)",
        "ceiling": RiskTier.CONFIRM_VOICE,
        "granted_via": "voice",
        "scope": GrantScope(
            tools=frozenset({"move_files"}), path_prefixes=("/Users/x/Downloads",)
        ),
        "budget": Budget(steps=10, seconds=60.0, spend_cents=0),
        "granted_at": 1000.0,
        "expires_at": 1300.0,
    }
    base.update(over)
    return Grant(**base)  # type: ignore[arg-type]


def _action(**over: Any) -> ResolvedAction:
    base: dict[str, Any] = {
        "tool": "move_files",
        "args": {"src": "/Users/x/Downloads/a.txt", "dst": "/Users/x/Downloads/old/a.txt"},
        "targets": ("a.txt",),
        "verb": "move",
    }
    base.update(over)
    return ResolvedAction(**base)  # type: ignore[arg-type]


def _ask(
    *,
    grant: Grant | None = None,
    state: GrantState | None = None,
    action: ResolvedAction | None = None,
    spec: ToolSpec | None = None,
    disposition: Disposition | None = None,
    now: float = NOW,
) -> tuple[bool, str]:
    return grant_satisfies(
        _grant() if grant is None else grant,
        GrantState(grant_id="g1") if state is None else state,
        _action() if action is None else action,
        MOVE if spec is None else spec,
        Disposition(tier=RiskTier.CONFIRM_VOICE, reason="x", assessment=SAFE)
        if disposition is None
        else disposition,
        now,
    )


# ---------------------------------------------------------------------------
# The baseline, and then one broken thing at a time
# ---------------------------------------------------------------------------


def test_the_baseline_is_satisfied_or_none_of_the_others_mean_anything():
    ok, why = _ask()
    assert ok, why
    assert "clear out downloads" in why, "the reason is SPOKEN and names the bargain"


def test_1_revocation_beats_everything():
    state = GrantState(grant_id="g1")
    state.revoke(NOW, "stop")
    ok, why = _ask(state=state)
    assert not ok
    assert "stop" in why


def test_2_consent_decays():
    ok, why = _ask(now=1301.0)
    assert not ok
    assert "run out" in why
    # And the boundary is exclusive: expiring exactly now is expired.
    assert not _ask(now=1300.0)[0]


def test_3_the_ceiling_caps_the_tier():
    grant = _grant(ceiling=RiskTier.ANNOUNCE)
    ok, why = _ask(grant=grant)
    assert not ok
    assert "bigger than" in why


def test_3_a_spoken_grant_can_never_answer_a_visual_confirmation():
    """The non-negotiable. A ceiling of CONFIRM_VISUAL on a VOICE grant is
    capped by the channel, not by the ceiling, and no combination of the two
    lets speech answer a question that exists because speech is not enough."""
    grant = _grant(ceiling=RiskTier.CONFIRM_VISUAL, granted_via="voice")
    assert grant.max_satisfiable is RiskTier.CONFIRM_VOICE
    ok, why = _ask(
        grant=grant,
        disposition=Disposition(
            tier=RiskTier.CONFIRM_VISUAL, reason="script", assessment=SAFE
        ),
    )
    assert not ok
    assert "on screen" in why


def test_3_a_visual_grant_may_answer_a_visual_confirmation():
    grant = _grant(ceiling=RiskTier.CONFIRM_VISUAL, granted_via="visual")
    ok, why = _ask(
        grant=grant,
        disposition=Disposition(
            tier=RiskTier.CONFIRM_VISUAL, reason="script", assessment=SAFE
        ),
    )
    assert ok, why


def test_3_an_unrecognised_channel_fails_closed():
    """`Grant.channel_cap` maps every unknown string to CONFIRM_VISUAL, which is
    the PERMISSIVE reading. This is the clause that stops a typo widening a
    grant past every tier in the system."""
    grant = _grant(granted_via="keyboard-ish")
    assert grant.channel_cap is RiskTier.CONFIRM_VISUAL, "the contract's permissive default"
    ok, why = _ask(grant=grant)
    assert not ok, "grant.py must not trust granted_via without checking it"
    assert "how you okayed" in why


def test_4_refuse_is_never_satisfiable():
    ok, _ = _ask(
        grant=_grant(ceiling=RiskTier.REFUSE),
        disposition=Disposition(tier=RiskTier.REFUSE, reason="never", assessment=SAFE),
    )
    assert not ok


def test_5_grantable_false_is_absolute():
    spec = replace(MOVE, grantable=False)
    ok, why = _ask(spec=spec)
    assert not ok
    assert "individually" in why


def test_5_grantable_false_holds_at_every_ceiling_and_every_channel():
    spec = replace(MOVE, grantable=False)
    for ceiling in (RiskTier.CONFIRM_VOICE, RiskTier.CONFIRM_VISUAL):
        for via in ("voice", "visual"):
            ok, _ = _ask(spec=spec, grant=_grant(ceiling=ceiling, granted_via=via))
            assert not ok, f"{ceiling.name}/{via} found a way through grantable=False"


def test_6_the_irreversible_tag_is_belt_and_braces():
    spec = replace(MOVE, tags=("irreversible",))
    ok, why = _ask(spec=spec)
    assert not ok
    assert "undone" in why


def test_7_a_runtime_unrecoverable_raise_is_always_allowed():
    risky = replace(SAFE, unrecoverable=0.9)
    ok, why = _ask(
        disposition=Disposition(tier=RiskTier.CONFIRM_VOICE, reason="x", assessment=risky)
    )
    assert not ok
    assert "undoable" in why


def test_7_no_assessment_at_all_fails_closed():
    ok, why = _ask(
        disposition=Disposition(tier=RiskTier.CONFIRM_VOICE, reason="x", assessment=None)
    )
    assert not ok, "an unchecked action is not a checked-and-fine one"
    assert "couldn't check" in why


def test_8_an_empty_tool_allowlist_covers_nothing():
    ok, why = _ask(grant=_grant(scope=GrantScope(path_prefixes=("/Users/x/Downloads",))))
    assert not ok
    assert "isn't one of the things" in why


def test_9_a_new_origin_re_confirms():
    grant = _grant(
        scope=GrantScope(
            tools=frozenset({"move_files"}),
            origins=frozenset({"https://mail.google.com"}),
            path_prefixes=("/Users/x/Downloads",),
        )
    )
    ok, why = _ask(grant=grant, action=_action(origin="https://evil.example"))
    assert not ok
    assert "didn't mention" in why
    assert _ask(grant=grant, action=_action(origin="https://mail.google.com"))[0]


def test_9_an_unlabelled_action_is_not_an_origin_free_one():
    """A place-scoped grant plus a resolver that forgot to set `origin` is the
    permissive-default bug this clause exists to prevent."""
    grant = _grant(
        scope=GrantScope(
            tools=frozenset({"move_files"}),
            origins=frozenset({"https://mail.google.com"}),
            path_prefixes=("/Users/x/Downloads",),
        )
    )
    ok, _ = _ask(grant=grant, action=_action(origin=None))
    assert not ok


def test_9_a_long_running_tool_must_say_where_it_is():
    """For an agent-driving tool a missing origin is a bug, not an absence, so
    it is never covered -- even by a grant that names no places at all."""
    spec = replace(MOVE, long_running=True)
    ok, why = _ask(spec=spec, action=_action(origin=None))
    assert not ok
    assert "where" in why


def test_10_paths_must_be_under_an_allowed_prefix():
    ok, why = _ask(action=_action(args={"src": "/Users/x/Documents/taxes.pdf"}))
    assert not ok
    assert "outside the folder" in why


def test_10_a_grant_with_no_prefixes_covers_no_paths():
    grant = _grant(scope=GrantScope(tools=frozenset({"move_files"})))
    ok, _ = _ask(grant=grant)
    assert not ok, "empty means empty for paths too"


def test_10_prefix_matching_respects_component_boundaries():
    grant = _grant(
        scope=GrantScope(tools=frozenset({"move_files"}), path_prefixes=("/Users/x/Down",))
    )
    ok, _ = _ask(grant=grant)
    assert not ok, "/Users/x/Down must not contain /Users/x/Downloads/a.txt"


def test_10_a_traversal_cannot_climb_out_of_the_scope():
    ok, _ = _ask(action=_action(args={"src": "/Users/x/Downloads/../.ssh/id_rsa"}))
    assert not ok


def test_10_an_action_with_no_paths_passes_the_path_clause():
    grant = _grant(scope=GrantScope(tools=frozenset({"move_files"})))
    ok, why = _ask(grant=grant, action=_action(args={"note": "hello"}, targets=("a note",)))
    assert ok, why


def test_11_an_undescribed_consequence_was_never_consented_to():
    ok, why = _ask(action=_action(consequences={"overwrite": "replacing one file"}))
    assert not ok
    assert "didn't mention" in why
    briefed = _grant(briefed_consequences=("overwrite",))
    assert _ask(
        grant=briefed, action=_action(consequences={"overwrite": "replacing one file"})
    )[0]


def test_12_the_step_budget_is_finite():
    state = GrantState(grant_id="g1", steps_used=10)
    ok, why = _ask(state=state)
    assert not ok
    assert "many steps" in why


def test_12_the_wall_clock_budget_is_finite():
    ok, why = _ask(now=1061.0)
    assert not ok
    assert "as long as" in why


def test_12_zero_spend_means_may_not_spend():
    ok, why = _ask(action=_action(consequences={"charge": "it costs four dollars"}))
    assert not ok
    # Clause 11 fires first on an unbriefed consequence, so brief it and check
    # that the MONEY clause is what stops it.
    ok, why = _ask(
        grant=_grant(briefed_consequences=("charge",)),
        action=_action(consequences={"charge": "it costs four dollars"}),
    )
    assert not ok
    assert "costs money" in why


def test_12_a_spend_budget_permits_a_declared_spend():
    grant = _grant(
        briefed_consequences=("charge",),
        budget=Budget(steps=10, seconds=60.0, spend_cents=500),
    )
    ok, why = _ask(grant=grant, action=_action(consequences={"charge": "four dollars"}))
    assert ok, why


def test_a_missing_grant_is_not_a_permission():
    ok, _ = grant_satisfies(
        None,
        None,
        _action(),
        MOVE,
        Disposition(tier=RiskTier.CONFIRM_VOICE, reason="x", assessment=SAFE),
        NOW,
    )
    assert not ok


def test_budget_exhausted_allows_a_zero_spend_against_a_zero_budget():
    assert budget_exhausted(
        Budget(steps=5, seconds=10.0), steps_used=0, elapsed_s=0.0, spent_cents=0
    ) == ""
    assert budget_exhausted(
        Budget(steps=5, seconds=10.0), steps_used=0, elapsed_s=0.0, spent_cents=1
    )


# ---------------------------------------------------------------------------
# The readback
# ---------------------------------------------------------------------------

SEND = ToolSpec(
    name="send_email",
    description="Send an email to someone",
    params={},
    floor=RiskTier.CONFIRM_VISUAL,
    tags=("irreversible",),
    grantable=False,
)
TRASH = ToolSpec(
    name="move_to_trash",
    description="Move files to the Trash",
    params={},
    floor=RiskTier.CONFIRM_VOICE,
)


def test_the_readback_has_all_four_required_clauses():
    grant = _grant()
    sentence = readback(grant, [MOVE, TRASH])
    assert grant.goal in sentence, "clause 1: the goal, in the user's own words"
    assert "most it could do" in sentence, "clause 2: the plan, named concretely"
    assert "steps" in sentence, "clause 3: the bounds must name a step count"
    assert "seconds" in sentence or "minutes" in sentence, "clause 3: ...and a duration"
    assert "only in Downloads" in sentence, "clause 3: ...and a place"
    assert "Say stop any time" in sentence, "clause 4: the stop line"
    assert sentence.rstrip().endswith("Okay?")


def test_the_readback_names_the_most_destructive_thing_in_scope():
    """THE anti-habituation clause. "I'll open the three invoices and rename
    them" is a lie of framing if the rename can overwrite, and a sentence that
    describes the average thing in scope sounds identical every time."""
    grant = _grant(scope=GrantScope(tools=frozenset({"move_files", "move_to_trash"})))
    sentence = readback(grant, [MOVE, TRASH])
    assert "Trash" in sentence, "the worst tool in scope is unnamed"
    assert "Move files between folders".lower() not in sentence.lower()


def test_the_readback_names_what_will_still_be_asked_about():
    grant = _grant(scope=GrantScope(tools=frozenset({"move_files", "send_email"})))
    sentence = readback(grant, [MOVE, SEND])
    assert "still stop and ask" in sentence
    assert "send email" in sentence


def test_an_empty_scope_says_so_out_loud():
    sentence = readback(_grant(scope=GrantScope()), [MOVE])
    assert "nothing" in sentence


def test_the_readback_can_say_what_it_cannot_promise():
    sentence = readback(
        _grant(), [MOVE], uncertain="I can't see the page until I'm in it"
    )
    assert "can't see the page" in sentence


def test_the_visual_card_prints_everything():
    grant = _grant(
        scope=GrantScope(
            tools=frozenset({"move_files", "send_email"}),
            origins=frozenset({"https://mail.google.com"}),
            path_prefixes=("/Users/x/Downloads",),
        ),
        granted_via="visual",
        ceiling=RiskTier.CONFIRM_VISUAL,
        briefed_consequences=("overwrite",),
    )
    card = visual_card(grant, [MOVE, SEND])
    for needle in (
        grant.goal,
        "CONFIRM_VISUAL",
        "https://mail.google.com",
        "/Users/x/Downloads",
        "10 steps",
        "0 cents",
        "overwrite",
        "still asked individually",
        "Move files between folders",
        "Send an email to someone",
    ):
        assert needle in card, f"the card hides {needle!r}, which downgrades the tier"


# ---------------------------------------------------------------------------
# Warrants
# ---------------------------------------------------------------------------


def test_the_digest_covers_what_the_user_heard():
    base = _action()
    assert action_digest(base) == action_digest(_action())
    for changed in (
        _action(args={"src": "/Users/x/Downloads/b.txt"}),
        _action(targets=("b.txt",)),
        _action(verb="delete"),
        _action(origin="https://x.test"),
        _action(consequences={"overwrite": "one file"}),
        _action(explicit=False),
        _action(floor_hint=RiskTier.CONFIRM_VISUAL),
    ):
        assert action_digest(changed) != action_digest(base)


def test_a_warrant_is_single_use():
    book = WarrantBook()
    warrant = book.issue(
        _action(),
        Disposition(tier=RiskTier.CONFIRM_VOICE, reason="x"),
        via="voice",
        now=NOW,
    )
    assert book.spend(warrant.id) is not None
    assert book.spend(warrant.id) is None, "a spent warrant must not pay twice"


def test_a_grant_issued_warrant_inherits_the_grants_channel():
    book = WarrantBook()
    spoken = book.issue(
        _action(),
        Disposition(tier=RiskTier.CONFIRM_VOICE, reason="x"),
        via="grant",
        now=NOW,
        grant=_grant(granted_via="voice"),
    )
    typed = book.issue(
        _action(),
        Disposition(tier=RiskTier.CONFIRM_VISUAL, reason="x"),
        via="grant",
        now=NOW,
        grant=_grant(granted_via="visual"),
    )
    assert book.spend(spoken.id).visual_ok is False
    assert book.spend(typed.id).visual_ok is True


def test_revoking_a_grant_invalidates_its_unspent_warrants():
    book = WarrantBook()
    grant = _grant()
    live = book.issue(
        _action(), Disposition(tier=RiskTier.CONFIRM_VOICE, reason="x"),
        via="grant", now=NOW, grant=grant,
    )
    assert book.revoke_grant(grant.id) == 1
    assert book.spend(live.id) is None, "a revoked grant's warrants are worthless"


def test_grants_are_not_persisted_anywhere(tmp_path):
    """Short expiries and NO persistence is one of the two mitigations for
    consent fatigue. A grant that survives the process is a setting."""
    book = GrantBook()
    grant = issue_grant(
        goal="g",
        plan_summary="p",
        ceiling=RiskTier.CONFIRM_VOICE,
        granted_via="voice",
        scope=GrantScope(),
        budget=Budget(),
        now=NOW,
    )
    book.add(grant)
    assert list(tmp_path.iterdir()) == []
    assert GrantBook().get(grant.id) == (None, None), "a fresh book knows nothing"


def test_expiries_are_short_by_default():
    from daa.safety.grant import DEFAULT_GRANT_TTL_S, DEFAULT_WARRANT_TTL_S

    assert DEFAULT_GRANT_TTL_S <= 600, "a grant that outlives the conversation is a setting"
    assert DEFAULT_WARRANT_TTL_S <= 60, "a stale warrant is a confirmation that became a lie"


@pytest.mark.parametrize("bad", [None, 123, object()])
def test_a_malformed_disposition_is_not_a_permission(bad: Any):
    ok, _ = grant_satisfies(_grant(), GrantState("g1"), _action(), MOVE, bad, NOW)
    assert not ok
