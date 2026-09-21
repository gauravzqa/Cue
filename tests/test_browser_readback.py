"""The sentence the user answers must describe what will actually happen.

The browser mirror of `tests/test_tools_readback.py`, plus the three rules that
are specific to a page:

1. The readback is composed from the live DOM -- the accessible name, the
   form's method and action host, the field types, the origin -- and never from
   the selector, an ordinal, or anything the model wrote.
2. An action with no honest name does not get a made-up one; it escalates.
3. Two physically different actions never read back identically.
"""

from __future__ import annotations

import pytest

from daa.config import Settings
from daa.contracts import ResolvedAction, RiskTier
from daa.tools.browser import BROWSER_TOOL_CLASSES, make_browser_tools
from daa.tools.browser import facts as factlib
from test_browser_fakes import (
    TEST_OPTIONS,
    FakeBackend,
    checkout_page,
    elem,
    form,
    shop_page,
)

DRY = Settings(dry_run=True)


def tools(*pages):
    backend = FakeBackend(pages=list(pages) or [shop_page()], options=TEST_OPTIONS)
    return {t.spec.name: t for t in make_browser_tools(DRY, session=backend, options=TEST_OPTIONS)}


# One resolving and one non-resolving call for every browser tool. Both matter:
# the path that finds nothing still has to say what it was going to do.
RESOLVE_ARGS: dict[str, tuple[dict, dict]] = {
    "list_tabs": ({}, {}),
    "read_page": ({}, {"tab_id": "nope"}),
    "find_on_page": ({"text": "lamp"}, {"text": ""}),
    "scroll_page": ({"direction": "down"}, {}),
    "open_tab": ({"url": "https://example.com/x"}, {"url": "javascript:alert(1)"}),
    "close_tab": ({}, {"tab_id": "nope"}),
    "go_back": ({}, {"tab_id": "nope"}),
    "summarise_page": ({}, {"tab_id": "nope"}),
    "click_element": ({"text": "Show more details"}, {"text": "nothing-like-this"}),
    "fill_field": ({"text": "Search products", "value": "lamp"}, {"text": "nothing-like-this"}),
    "submit_form": ({"text": "Search"}, {"text": "nothing-like-this"}),
}


def test_the_arg_table_covers_every_browser_tool():
    assert set(RESOLVE_ARGS) == {cls.spec.name for cls in BROWSER_TOOL_CLASSES}


@pytest.mark.parametrize("name", sorted(RESOLVE_ARGS), ids=str)
def test_every_tool_declares_a_spoken_verb(name):
    tool = tools()[name]
    assert tool.verb, f"{name} has no verb: its readback is a bare noun phrase"
    assert tool.verb == tool.verb.strip() and tool.verb[0].islower()


@pytest.mark.parametrize("name", sorted(RESOLVE_ARGS), ids=str)
def test_every_resolve_path_carries_the_verb(name):
    for kwargs in RESOLVE_ARGS[name]:
        action = tools()[name].resolve(**kwargs)
        assert isinstance(action, ResolvedAction)
        assert action.verb, f"{name}.resolve({kwargs}) produced a verb-less readback"
        assert action.describe().startswith(action.verb)


@pytest.mark.parametrize("name", sorted(RESOLVE_ARGS), ids=str)
def test_resolve_never_touches_the_page(name):
    """resolve() runs before the risk gate, so it may not act."""
    page = shop_page()
    kit = tools(page)
    for kwargs in RESOLVE_ARGS[name]:
        kit[name].resolve(**kwargs)
    assert page.clicked == [] and page.filled == [] and page.submitted == []
    assert page.scrolls == [] and page.page_url.startswith("https://shop.example.com/")


# ---------------------------------------------------------------------------
# the readback comes from the DOM
# ---------------------------------------------------------------------------


def test_the_click_readback_names_the_control_the_site_named():
    action = tools()["click_element"].resolve(text="Show more details")
    assert action.targets == ("the 'Show more details' button on shop.example.com",)
    assert action.describe().startswith("press the 'Show more details' button")


def test_the_readback_does_not_come_from_what_the_caller_typed():
    """Same physical control, three different spellings, one sentence."""
    kit = tools()
    said = [
        kit["click_element"].resolve(text="show more details"),
        kit["click_element"].resolve(text="Show more"),
        kit["click_element"].resolve(selector="#more"),
    ]
    assert len({a.describe() for a in said}) == 1


def test_a_selector_never_reaches_the_spoken_sentence():
    action = tools()["click_element"].resolve(selector="#more")
    assert "#more" not in action.describe()
    assert action.args["selector"] == "#more"     # still there for run()


def test_two_different_controls_never_read_back_identically():
    kit = tools()
    more = kit["click_element"].resolve(text="Show more details")
    delete = kit["click_element"].resolve(text="Delete this review")
    assert more.describe() != delete.describe()


def test_the_same_control_under_two_tools_does_not_read_back_identically():
    """The headline bug: one sentence that meant either of two things."""
    kit = tools()
    pressed = kit["click_element"].resolve(text="Search")
    submitted = kit["submit_form"].resolve(text="Search")
    assert pressed.describe() != submitted.describe()


def test_the_payment_fixture_produces_the_sentence_the_design_promised():
    kit = tools(checkout_page())
    action = kit["submit_form"].resolve(text="Pay $412.00")
    spoken = action.describe()
    assert spoken.startswith("submit the payment form on stripe.com")
    assert "by pressing 'Pay $412.00'" in spoken
    assert "sending 4 fields to checkout.stripe.com" in spoken
    assert "including a card number" in spoken
    assert "for $412.00" in spoken
    assert "where you are signed in" in spoken
    assert "I will not be able to undo this" in spoken


def test_leaving_the_site_is_stated_in_the_verb_and_in_the_consequences():
    kit = tools(checkout_page())
    action = kit["click_element"].resolve(text="Continue")
    spoken = action.describe()
    assert spoken.startswith("leave this site and open")
    assert "evil-checkout.example.net" in spoken


# ---------------------------------------------------------------------------
# no invented names
# ---------------------------------------------------------------------------


def test_an_unnamed_control_escalates_rather_than_being_described_as_something():
    page = checkout_page()
    action = tools(page)["click_element"].resolve(selector="#nameless")
    assert "unlabelled" in action.describe()
    assert "the control has no label I can read to you" in action.describe()
    assert action.floor_hint is RiskTier.CONFIRM_VISUAL


def test_the_resolver_cannot_buy_a_cheaper_tier_by_inventing_a_name(monkeypatch):
    """Mutation-revert: put the selector in where the name should be.

    With the honest resolver the action escalates to a card because there is
    nothing to say. Substituting the selector for the missing name produces a
    voice-confirmable sentence that reads as if the control were labelled -- so
    the escalation above is caused by the missing name, not by something else
    on that element.
    """
    # A plain page, so that nothing ELSE on it can be the cause: shop.example.com
    # is not a payment host, the control has no form and no money near it.
    page = shop_page()
    honest = tools(page)["click_element"].resolve(selector="#nameless")
    assert honest.floor_hint is RiskTier.CONFIRM_VISUAL

    real_from_raw = factlib.facts_from_raw

    def lying(raw, **kwargs):
        patched = dict(raw)
        patched["name"] = "#nameless"
        return real_from_raw(patched, **kwargs)

    monkeypatch.setattr(factlib, "facts_from_raw", lying)
    lied = tools(page)["click_element"].resolve(selector="#nameless")
    assert lied.floor_hint is not RiskTier.CONFIRM_VISUAL
    assert "unlabelled" not in lied.describe()


# ---------------------------------------------------------------------------
# no content in targets
# ---------------------------------------------------------------------------

SECRET = "hunter2-correct-horse-battery-staple"


def test_a_typed_value_never_reaches_the_targets_that_get_logged():
    action = tools()["fill_field"].resolve(text="Search products", value=SECRET)
    assert SECRET not in " ".join(action.targets)
    assert SECRET not in action.describe()
    assert SECRET not in " ".join(action.consequences.values())
    assert action.args["value"] == SECRET       # still there for the tool to use


@pytest.mark.parametrize("name", sorted(RESOLVE_ARGS), ids=str)
def test_no_tool_puts_a_content_argument_into_targets(name):
    marker = "zzcontentmarkerzz"
    content_args = {
        "fill_field": {"text": "Search products", "value": marker},
        "find_on_page": {"text": marker},
    }
    kwargs = content_args.get(name)
    if kwargs is None:
        pytest.skip(f"{name} takes no free-text content")
    action = tools()[name].resolve(**kwargs)
    if name == "find_on_page":
        # The needle is the user's own words rather than page content, so it is
        # spoken back -- capped, because a pasted paragraph in `targets` is a
        # pasted paragraph in the audit log.
        assert len(" ".join(action.targets)) <= 60
    else:
        assert marker not in " ".join(action.targets), f"{name} leaked content into targets"


def test_a_pasted_paragraph_is_described_by_its_shape_not_quoted():
    action = tools()["find_on_page"].resolve(text="x " * 200)
    assert action.targets == ("that phrase",)


# ---------------------------------------------------------------------------
# origin, for grant scoping
# ---------------------------------------------------------------------------


def test_acting_actions_carry_a_machine_readable_origin_and_never_speak_it():
    kit = tools(checkout_page())
    action = kit["submit_form"].resolve(text="Pay $412.00")
    assert action.origin == "https://stripe.com"
    assert action.origin not in action.describe()


def test_open_tab_carries_the_origin_of_where_it_is_going():
    action = tools()["open_tab"].resolve(url="https://amazon.co.uk/gp/buy?token=abc")
    assert action.origin == "https://amazon.co.uk"
    assert "token=abc" not in action.describe()
    assert "token=abc" not in " ".join(action.targets)


# ---------------------------------------------------------------------------
# the refusal is in the readback, not only in the result
# ---------------------------------------------------------------------------


def test_a_refusal_says_so_before_the_user_answers():
    action = tools(checkout_page())["click_element"].resolve(text="Pay $412.00")
    spoken = action.describe()
    assert "that button submits a form" in spoken
    assert action.args["route_to"] == "submit_form"


def test_an_ambiguous_name_offers_the_runners_up_instead_of_guessing():
    page = shop_page()
    page.elements.append(elem("#go2", name="Search", input_type="submit", submits=True,
                              form_facts=form(action="https://shop.example.com/s",
                                              method="get", names=("q",), types=("search",))))
    action = tools(page)["click_element"].resolve(text="Search")
    assert "refused" in action.args
    assert len(action.args["candidates"]) >= 2
