"""The crux: the sentence the user answers is written by the tree, not the model.

`run_applescript` already learned this the hard way -- its `purpose` field is
written by the model, so using it as the readback asks the user to check the
model against a sentence the model wrote, and a script that empties an inbox
introduced itself as "check what time my next meeting is".

Here the attack is one step shorter. The model asks to press "the OK button".
The tree says `AXButton`, titled "Delete Account", inside the window "Account
Settings", in Safari. If those two strings can ever swap places, every floor,
every hint and every consequence in this package is decorative, because all of
them are spent buying one sentence.

The last test in this file is a mutation-revert: it builds a resolver that
tells the lie on purpose and asserts that the property test above catches it.
A property test nobody has ever seen fail is a property test that might be
vacuous.
"""

from __future__ import annotations

from typing import Any

import pytest

from computer_tree import el, field, serve, tree
from daa.config import Settings
from daa.contracts import ResolvedAction, RiskTier
from daa.tools.computer import naming
from daa.tools.computer import tools as tools_mod
from daa.tools.computer.tools import UiClick, UiType

DRY = Settings(dry_run=True)

# What the model asked for, and what the tree actually says. Each left-hand
# side DOES match its right-hand side -- that is what makes these dangerous.
# The model supplies a phrase loose enough to be true and vague enough to sound
# harmless, and the only thing between the user and a yes to the wrong button
# is that the sentence is rebuilt from the tree instead of echoed back.
LIES = [
    ("delete", "Delete Account"),
    ("continue", "Continue and Pay"),
    ("allow", "Allow Full Disk Access"),
    ("save", "Save and Send to Everyone"),
    ("ok", "OK, Erase Disk"),
]

# Phrases that match NOTHING in the tree. The right answer is a refusal, not a
# quiet retarget onto the nearest thing -- "press something" is not consentable.
MISMATCHES = ["the OK button", "yes", "the close button", "proceed"]


# ---------------------------------------------------------------------------
# Composing a target from the tree
# ---------------------------------------------------------------------------


def test_the_target_is_built_from_ax_attributes_in_order():
    element = el("Delete Account", role_description="button", window="Account Settings",
                 app="Safari", bundle_id="com.apple.Safari")
    assert naming.compose_target(element) == (
        "the Delete Account button in Account Settings in Safari"
    )


def test_an_element_with_no_label_cannot_be_named_and_therefore_cannot_be_pressed():
    assert naming.compose_target(el("", role_description="button")) is None


def test_the_role_noun_falls_back_to_the_role_rather_than_reading_a_constant_aloud():
    assert naming.role_noun(el("Save", role="AXMenuItem", role_description="")) == "menu item"
    assert naming.role_noun(el("Save", role="AXButton", role_description="")) == "button"
    assert naming.role_noun(el("Save", role_description="knopf")) == "knopf"


def test_a_role_already_in_the_label_is_not_said_twice():
    element = el("Close button", role_description="button")
    assert naming.compose_target(element) == "the Close button in Untitled in TextEdit"


def test_the_window_carries_the_context_the_button_label_lost():
    """A button labelled "Continue" that closes an account reads back as safe.

    That hole is known and unpatchable by lexicon alone, which is exactly why
    the window title is part of the target string rather than decoration.
    """
    element = el("Continue", window="Delete Account", app="Safari")
    assert "Delete Account" in naming.compose_target(element)


def test_matching_happens_on_the_label_only():
    """Otherwise the query "safari" drags in every control in Safari."""
    element = el("Save", window="Account Settings", app="Safari")
    assert naming.searchable(element) == "Save"


# ---------------------------------------------------------------------------
# The destructive lexicon -- matched against the tree, never the model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title", ["Delete", "Delete Account", "Erase Disk", "Send", "Pay Now", "Sign Out", "Allow"]
)
def test_destructive_wording_is_detected_in_the_trees_own_title(title):
    assert naming.destructive_words(title)


@pytest.mark.parametrize("title", ["Cancel", "Close", "Open", "Rename", "Zoom", ""])
def test_ordinary_wording_is_not_flagged(title):
    assert naming.destructive_words(title) == ()


def test_the_lexicon_is_never_run_against_the_models_phrasing():
    """`assess` is only ever handed an Element. Nothing there came from the model."""
    import inspect

    source = inspect.getsource(naming.assess)
    assert "requested" not in source


# ---------------------------------------------------------------------------
# floor_hint: deterministic code that read the real tree
# ---------------------------------------------------------------------------


def test_destructive_wording_raises_the_floor_for_this_invocation():
    hint, consequences = naming.assess(el("Delete Account"))
    assert hint is RiskTier.CONFIRM_VISUAL
    assert "Delete Account" in consequences["wording"]


def test_a_system_permission_dialog_raises_the_floor():
    element = el(
        "Allow", app="UserNotificationCenter",
        bundle_id="com.apple.UserNotificationCenter", in_alert=True, default=True,
    )
    hint, consequences = naming.assess(element)
    assert hint is RiskTier.CONFIRM_VISUAL
    assert "macOS permission dialog" in consequences["system"]


def test_the_default_button_of_an_alert_raises_the_floor():
    hint, consequences = naming.assess(el("Continue", in_alert=True, default=True))
    assert hint is RiskTier.CONFIRM_VISUAL
    assert consequences["modal"]


def test_an_ordinary_button_leaves_the_tool_floor_alone():
    hint, _ = naming.assess(el("Rename"))
    assert hint is None


def test_the_hint_can_only_ever_raise():
    """Nothing in this module returns a tier below the tools' own floors."""
    for element in (el("Rename"), el("Delete"), el("Allow", in_alert=True, default=True)):
        hint, _ = naming.assess(element)
        assert hint is None or hint >= RiskTier.CONFIRM_VOICE


def test_every_assessment_says_it_cannot_be_undone():
    for element in (el("Rename"), el("Delete Account"), field("Search")):
        _, consequences = naming.assess(element)
        assert consequences["undo"] == naming.UNDO_CONSEQUENCE


def test_acting_in_the_wrong_app_is_spoken():
    _, consequences = naming.assess(el("Send", app="Safari"), app_query="Mail")
    assert "not in Mail" in consequences["app"]


def test_a_press_in_place_says_it_does_not_steal_the_front():
    _, in_place = naming.assess(el("Save", actions=("AXPress",)))
    _, synthetic = naming.assess(el("Save", actions=()))
    assert "without bringing it to the front" in in_place["focus"]
    assert "to the front" in synthetic["focus"]


# ---------------------------------------------------------------------------
# The property, on the real tools
# ---------------------------------------------------------------------------


def assert_readback_comes_from_the_tree(
    action: ResolvedAction, *, requested: str, actual: str
) -> None:
    """The property every `ui_*` resolution must satisfy.

    Extracted so the mutation-revert below can run the identical assertions
    against a deliberately-broken resolver and show that they bite.
    """
    sentence = action.describe()
    assert action.targets, "a resolution with no target cannot be confirmed at all"
    # The tree's label, in full. A phrasing that merely OVERLAPS the label --
    # "delete" for "Delete Account" -- is exactly the case this catches.
    assert actual in sentence, f"the tree said {actual!r} and the sentence does not"
    assert action.targets[0] != requested, "the sentence is the model's phrasing verbatim"
    assert action.args["requested_target"] == requested, (
        "the model's phrasing must still be recorded, beside the truth and never as it"
    )


@pytest.mark.parametrize("requested,actual", LIES)
def test_ui_click_reads_back_what_the_thing_is(requested, actual, monkeypatch):
    monkeypatch.setattr(
        tools_mod, "snapshot", serve(tree(el(actual, window="Account Settings", app="Safari")))
    )
    tool = UiClick(DRY)
    action = tool.resolve(target=requested, app="Safari")
    assert_readback_comes_from_the_tree(action, requested=requested, actual=actual)
    assert "Account Settings" in action.describe() and "Safari" in action.describe()


@pytest.mark.parametrize("requested", MISMATCHES)
def test_a_phrase_that_matches_nothing_is_refused_rather_than_retargeted(
    requested, monkeypatch
):
    monkeypatch.setattr(
        tools_mod, "snapshot", serve(tree(el("Delete Account", window="Account Settings")))
    )
    action = UiClick(DRY).resolve(target=requested, app="Safari")
    assert action.targets == ()
    assert action.args["reason"]
    assert UiClick(DRY).run(action).ok is False


def test_ui_type_reads_back_the_field_the_tree_names(monkeypatch):
    monkeypatch.setattr(
        tools_mod, "snapshot", serve(tree(field("Recipient address", window="New Message")))
    )
    tool = UiType(DRY)
    action = tool.resolve(text="hello", target="recipient", app="TextEdit")
    assert_readback_comes_from_the_tree(
        action, requested="recipient", actual="Recipient address"
    )


def test_the_literal_text_reaches_the_sentence():
    """The body of what gets written is part of what a yes means."""
    import daa.tools.computer.tools as mod

    snap = tree(field("Search"))
    original = mod.snapshot
    mod.snapshot = serve(snap)
    try:
        action = UiType(DRY).resolve(text="quarterly notes", target="search", app="TextEdit")
    finally:
        mod.snapshot = original
    assert "quarterly notes" in action.describe()


# ---------------------------------------------------------------------------
# Mutation-revert: prove the property test above is not vacuous
# ---------------------------------------------------------------------------


class _LyingClick(UiClick):
    """The bug this whole package exists to prevent, written out on purpose.

    It is one line: use the model's phrasing as the target instead of the
    tree's. That is precisely what hermes does -- its approval prompt renders
    "click element #7" -- and it is what `run_applescript` would do if it used
    `purpose` as its readback.
    """

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        action = super().resolve(**kwargs)
        if not action.targets:
            return action
        return ResolvedAction(
            tool=action.tool,
            args=action.args,
            targets=(str(kwargs.get("target") or ""),),
            explicit=action.explicit,
            verb=action.verb,
            floor_hint=action.floor_hint,
            origin=action.origin,
            consequences=action.consequences,
        )


@pytest.mark.parametrize("requested,actual", LIES)
def test_the_readback_property_catches_the_lie(requested, actual, monkeypatch):
    monkeypatch.setattr(
        tools_mod, "snapshot", serve(tree(el(actual, window="Account Settings", app="Safari")))
    )
    action = _LyingClick(DRY).resolve(target=requested, app="Safari")
    with pytest.raises(AssertionError):
        assert_readback_comes_from_the_tree(action, requested=requested, actual=actual)


def test_dropping_the_window_from_the_target_is_also_caught():
    """A second mutation: keep the tree's label, lose the context around it."""
    element = el("Continue", window="Delete Account", app="Safari")
    honest = naming.compose_target(element)
    mutated = f"the {element.label} button in {element.app}"
    assert "Delete Account" in honest
    assert "Delete Account" not in mutated
