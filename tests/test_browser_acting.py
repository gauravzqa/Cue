"""Stage 4: the three tools that touch a page, and what stops each one.

Three properties, each with a mutation-revert beside it so that the assertion
is shown to be caused by the guard rather than by an accident of the fixture:

1. `click_element` cannot press a submitting control -- checked in `resolve()`
   AND again in `run()` against separately re-read facts.
2. The `dom_fingerprint` taken in `resolve()` is recomputed in `run()`, and a
   mismatch aborts rather than retrying.
3. `fill_field` refuses a password, a card number or a one-time code outright,
   in both phases.
"""

from __future__ import annotations

import pytest

from daa.config import Settings
from daa.contracts import ResolvedAction, RiskTier, UndoAction
from daa.tools.browser import facts as factlib
from daa.tools.browser import make_browser_tools
from daa.tools.browser.acting import ROUTE_TO_SUBMIT
from daa.tools.browser.base import STALE_SUMMARY
from test_browser_fakes import (
    TEST_OPTIONS,
    FakeBackend,
    checkout_page,
    elem,
    form,
    shop_page,
)

DRY = Settings(dry_run=True)
LIVE = Settings(dry_run=False)


def kit(*pages, settings=LIVE):
    backend = FakeBackend(pages=list(pages), options=TEST_OPTIONS)
    return backend, {
        t.spec.name: t
        for t in make_browser_tools(settings, session=backend, options=TEST_OPTIONS)
    }


# ---------------------------------------------------------------------------
# 1. click_element cannot submit a form
# ---------------------------------------------------------------------------


def test_clicking_a_pay_button_is_refused_and_routed():
    page = checkout_page()
    _, tools = kit(page)
    tool = tools["click_element"]
    action = tool.resolve(text="Pay $412.00")
    assert ROUTE_TO_SUBMIT in action.describe()
    result = tool.run(action)
    assert result.ok is False
    assert result.data["route_to"] == "submit_form"
    assert page.clicked == []


def test_the_refusal_survives_a_resolver_that_got_it_wrong(monkeypatch):
    """The second check, against re-read facts, is what actually stops it.

    A hand-built action -- or a resolver bug, or a page that changed shape --
    reaches run() claiming an ordinary click. `run()` re-reads the DOM and
    refuses anyway, so the guarantee does not rest on resolve() being right.
    """
    page = checkout_page()
    _, tools = kit(page)
    tool = tools["click_element"]
    forged = ResolvedAction(
        tool="click_element",
        args={
            "selector": "#pay",
            "tab_id": None,
            "dom_fingerprint": factlib.dom_fingerprint(
                factlib.facts_from_raw(
                    page.element_facts("#pay")[0], page_url=page.url(),
                    page_title=page.title(), logged_in=True,
                )
            ),
        },
        targets=("a harmless looking button",),
        verb="press",
    )
    result = tool.run(forged)
    assert result.ok is False
    assert ROUTE_TO_SUBMIT in result.summary
    assert page.clicked == []


def test_mutation_revert_removing_the_run_side_check_would_let_the_pay_click_through(
    monkeypatch,
):
    """Sabotage: make `must_use_submit` always false and the click lands.

    That is the whole point of the check -- nothing else on the path stops a
    submitting control from being pressed by the cheap tool.
    """
    page = checkout_page()
    _, tools = kit(page)
    tool = tools["click_element"]
    action = tool.resolve(selector="#pay")
    monkeypatch.setattr(factlib, "must_use_submit", lambda facts: False)
    sabotaged = tool.resolve(selector="#pay")
    assert "refused" in action.args and "refused" not in sabotaged.args
    assert tool.run(sabotaged).ok is True
    assert page.clicked == ["#pay"]


def test_an_ordinary_button_is_pressed_without_ceremony():
    page = shop_page()
    _, tools = kit(page)
    tool = tools["click_element"]
    result = tool.run(tool.resolve(text="Show more details"))
    assert result.ok and page.clicked == ["#more"]
    assert result.summary == "Pressed Show more details."


def test_a_search_button_is_still_an_ordinary_click():
    page = shop_page()
    _, tools = kit(page)
    tool = tools["click_element"]
    action = tool.resolve(text="Search")
    assert "refused" not in action.args
    assert action.verb == "search with"
    assert tool.run(action).ok


def test_a_dry_run_presses_nothing():
    page = checkout_page()
    _, tools = kit(page, settings=DRY)
    tool = tools["click_element"]
    result = tool.run(tool.resolve(text="Continue"))
    assert result.ok and result.data["dry_run"] is True
    assert page.clicked == []


# ---------------------------------------------------------------------------
# 2. the fingerprint
# ---------------------------------------------------------------------------


def test_the_page_moving_between_the_question_and_the_yes_aborts_the_click():
    page = shop_page()
    _, tools = kit(page)
    tool = tools["click_element"]
    action = tool.resolve(text="Show more details")
    # Two seconds of spoken readback is enough for an SPA to replace a subtree.
    page.mutate("#more", name="Delete my account")
    result = tool.run(action)
    assert result.ok is False
    assert result.summary == STALE_SUMMARY
    assert page.clicked == []


def test_navigating_between_the_question_and_the_yes_aborts_the_click():
    page = shop_page()
    _, tools = kit(page)
    tool = tools["click_element"]
    action = tool.resolve(text="Show more details")
    page.page_url = "https://evil.example.net/products/42"
    result = tool.run(action)
    assert result.ok is False and result.summary == STALE_SUMMARY
    assert page.clicked == []


def test_the_control_vanishing_aborts_rather_than_clicking_its_replacement():
    page = shop_page()
    _, tools = kit(page)
    tool = tools["click_element"]
    action = tool.resolve(text="Show more details")
    page.elements = [(sel, raw) for sel, raw in page.elements if sel != "#more"]
    assert tool.run(action).summary == STALE_SUMMARY
    assert page.clicked == []


def test_a_second_element_appearing_under_the_same_selector_aborts():
    page = shop_page()
    _, tools = kit(page)
    tool = tools["click_element"]
    action = tool.resolve(text="Show more details")
    page.elements.append(elem("#more", name="Show more details"))
    assert tool.run(action).summary == STALE_SUMMARY
    assert page.clicked == []


@pytest.mark.parametrize("name", ["click_element", "fill_field", "submit_form"])
def test_every_acting_tool_checks_the_fingerprint(name):
    page = checkout_page()
    _, tools = kit(page)
    tool = tools[name]
    args = {"click_element": {"text": "Continue"},
            "fill_field": {"text": "Email", "value": "x"},
            "submit_form": {"text": "Pay $412.00"}}[name]
    action = tool.resolve(**args)
    assert action.args.get("dom_fingerprint")
    page.mutate(action.args["selector"], name="Something else entirely")
    result = tool.run(action)
    assert result.ok is False and result.summary == STALE_SUMMARY


def test_mutation_revert_a_constant_fingerprint_lets_the_changed_page_through(monkeypatch):
    """Sabotage: freeze the hash and the swapped control gets clicked."""
    page = shop_page()
    _, tools = kit(page)
    tool = tools["click_element"]
    action = tool.resolve(text="Show more details")
    page.mutate("#more", name="Delete my account")
    monkeypatch.setattr(factlib, "dom_fingerprint", lambda facts: action.args["dom_fingerprint"])
    result = tool.run(action)
    assert result.ok is True
    assert page.clicked == ["#more"]
    assert result.summary == "Pressed Delete my account."     # not what was confirmed


# ---------------------------------------------------------------------------
# 3. fill_field
# ---------------------------------------------------------------------------


def test_a_card_number_field_is_refused_rather_than_confirmed_harder():
    page = checkout_page()
    _, tools = kit(page)
    tool = tools["fill_field"]
    action = tool.resolve(text="Card number", value="4111111111111111")
    assert "never type into one" in action.describe()
    result = tool.run(action)
    assert result.ok is False
    assert page.filled == []
    assert "4111111111111111" not in result.summary


@pytest.mark.parametrize(
    "kwargs",
    [
        {"input_type": "password"},
        {"autocomplete": "current-password"},
        {"autocomplete": "new-password"},
        {"autocomplete": "one-time-code"},
        {"autocomplete": "cc-csc"},
        {"autocomplete": "cc-exp"},
    ],
)
def test_every_credential_shaped_field_is_refused(kwargs):
    page = shop_page()
    page.elements.append(
        elem("#secret", tag="INPUT", role="textbox", name="Secret", value_empty=True, **kwargs)
    )
    _, tools = kit(page)
    tool = tools["fill_field"]
    assert tool.run(tool.resolve(selector="#secret", value="hunter2")).ok is False
    assert page.filled == []


def test_a_field_that_becomes_a_password_field_after_the_question_is_still_refused():
    """The credential check runs again in run(), against the re-read DOM."""
    page = shop_page()
    _, tools = kit(page)
    tool = tools["fill_field"]
    action = tool.resolve(text="Search products", value="lamp")
    assert "refused" not in action.args
    page.mutate("#q", input_type="password")
    result = tool.run(action)
    assert result.ok is False
    assert page.filled == []


def test_typing_into_an_empty_field_can_be_taken_back_and_typing_over_cannot():
    page = shop_page()
    _, tools = kit(page)
    tool = tools["fill_field"]

    first = tool.run(tool.resolve(text="Search products", value="lamp"))
    assert first.ok and isinstance(first.undo, UndoAction)
    assert first.undo.args == {
        "selector": "#q", "tab_id": None, "restore": "empty",
        "expires_at": first.undo.args["expires_at"],
    }
    assert "lamp" not in str(first.undo.args)

    second = tool.resolve(text="Search products", value="lamps")
    assert "which I will not be able to put back" in second.describe()
    assert tool.run(second).undo is None


def test_the_undo_is_runnable_and_names_a_tool_its_own_spec_allows():
    page = shop_page()
    _, tools = kit(page)
    tool = tools["fill_field"]
    result = tool.run(tool.resolve(text="Search products", value="lamp"))
    assert result.undo.tool in tool.spec.inverses
    replay = tool.run(tool.resolve(**dict(result.undo.args)))
    assert replay.ok
    assert page.filled[-1] == ("#q", "")


def test_a_stale_fill_undo_is_inert():
    page = shop_page()
    _, tools = kit(page)
    tool = tools["fill_field"]
    stale = tool.resolve(selector="#q", restore="empty", expires_at=1.0)
    assert tool.run(stale).ok is False
    assert page.filled == []


def test_the_value_never_reaches_the_summary_or_the_data():
    page = shop_page()
    _, tools = kit(page)
    tool = tools["fill_field"]
    secret = "correct horse battery staple"
    result = tool.run(tool.resolve(text="Search products", value=secret))
    assert secret not in result.summary
    assert secret not in str(result.data)
    assert result.data["chars"] == len(secret)      # the count, never the characters


# ---------------------------------------------------------------------------
# submit_form
# ---------------------------------------------------------------------------


def test_submit_form_is_the_only_statically_expensive_tool_here():
    _, tools = kit(shop_page())
    spec = tools["submit_form"].spec
    assert spec.floor is RiskTier.CONFIRM_VISUAL
    assert spec.inverses == ()
    assert spec.grantable is False
    assert tools["submit_form"].irreversible is True
    assert "irreversible" in spec.tags
    assert "irreversible" in spec.activation_hint


def test_the_floor_is_static_so_a_buggy_resolver_cannot_make_a_post_cheap(monkeypatch):
    """Mutation-revert: break every computed escalation and the floor holds."""
    monkeypatch.setattr(factlib, "floor_hint_for", lambda facts: None)
    monkeypatch.setattr(factlib, "must_use_submit", lambda facts: False)
    _, tools = kit(checkout_page())
    action = tools["submit_form"].resolve(text="Pay $412.00")
    assert action.floor_hint is RiskTier.CONFIRM_VISUAL     # set in the tool, not computed
    assert tools["submit_form"].spec.floor is RiskTier.CONFIRM_VISUAL


def test_the_card_carries_field_names_and_types_and_never_values():
    _, tools = kit(checkout_page())
    card = tools["submit_form"].resolve(text="Pay $412.00").args["card"]
    assert card["field_names"] == ["email", "cardnumber", "cvc", ""]
    assert card["field_types"] == ["email", "text", "text", "submit"]
    assert card["action_origin"] == "https://checkout.stripe.com/pay"
    assert card["amount"] == "$412.00"
    assert card["signed_in"] is True
    assert card["own_browser"] is True
    assert "value" not in card and "values" not in card


def test_submitting_a_non_form_control_routes_back_to_the_cheap_tool():
    page = shop_page()
    _, tools = kit(page)
    tool = tools["submit_form"]
    action = tool.resolve(text="Show more details")
    assert action.args["route_to"] == "click_element"
    assert tool.run(action).ok is False
    assert page.submitted == []


def test_a_form_that_stops_being_a_form_is_not_submitted():
    page = checkout_page()
    _, tools = kit(page)
    tool = tools["submit_form"]
    action = tool.resolve(text="Pay $412.00")
    page.mutate("#pay", submits=False)
    assert tool.run(action).ok is False
    assert page.submitted == []


def test_a_real_submission_records_no_undo_at_all():
    page = shop_page()
    _, tools = kit(page)
    tool = tools["submit_form"]
    result = tool.run(tool.resolve(text="Search"))
    assert result.ok and result.undo is None
    assert page.submitted == ["#go"]


# ---------------------------------------------------------------------------
# dialogs: the site is asking the USER a question
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["click_element", "fill_field", "submit_form"])
def test_nothing_acts_while_the_page_is_waiting_on_a_dialog(name):
    page = shop_page()
    page.dialog = "confirm"
    _, tools = kit(page)
    tool = tools[name]
    args = {"click_element": {"text": "Show more details"},
            "fill_field": {"text": "Search products", "value": "x"},
            "submit_form": {"text": "Search"}}[name]
    action = tool.resolve(**args)
    assert "dialog" in action.describe()
    assert tool.run(action).ok is False
    assert page.clicked == [] and page.filled == [] and page.submitted == []


# ---------------------------------------------------------------------------
# ambiguity
# ---------------------------------------------------------------------------


def test_two_controls_with_one_name_produce_a_list_rather_than_a_coin_flip():
    page = shop_page()
    page.elements.append(elem("#more2", name="Show more details"))
    _, tools = kit(page)
    tool = tools["click_element"]
    action = tool.resolve(text="Show more details")
    result = tool.run(action)
    assert result.ok is False
    assert sorted(result.data["candidates"]) == ["Show more details", "Show more details"]
    assert page.clicked == []


def test_a_name_that_matches_nothing_says_so_instead_of_pressing_the_nearest_thing():
    page = shop_page()
    _, tools = kit(page)
    tool = tools["click_element"]
    action = tool.resolve(text="Launch the missiles")
    assert "could not find" in action.describe()
    assert tool.run(action).ok is False
    assert page.clicked == []


def test_pointing_at_nothing_at_all_is_refused_rather_than_guessed():
    page = shop_page()
    _, tools = kit(page)
    tool = tools["click_element"]
    action = tool.resolve()
    assert "did not say which control" in action.describe()
    assert tool.run(action).ok is False


# ---------------------------------------------------------------------------
# a submitting control reached through an odd shape still routes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "form_kwargs,expected",
    [
        ({"method": "post", "names": ("a",), "types": ("text",)}, True),
        ({"method": "get", "names": ("q",), "types": ("search",)}, False),
        ({"method": "get", "names": ("p",), "types": ("password",)}, True),
        ({"method": "get", "names": ("c",), "types": ("text",),
          "hints": ("one-time-code",)}, True),
    ],
)
def test_the_structural_signals_decide_the_route_not_the_label(form_kwargs, expected):
    page = shop_page()
    page.elements.append(
        elem("#x", name="Continue", input_type="submit", submits=True,
             form_facts=form(action="https://shop.example.com/x", **form_kwargs))
    )
    _, tools = kit(page)
    action = tools["click_element"].resolve(selector="#x")
    assert ("refused" in action.args) is expected
