"""`PageFacts`: the readback is derived from the live DOM and nothing else.

This is the stage that had to land before any clicking. If `click_element` had
shipped first it would have shipped with a selector-derived readback, that
readback would have been a lie in exactly the way the design exists to prevent,
and it would then have been load-bearing and hard to change.
"""

from __future__ import annotations

import pytest

from daa.contracts import RiskTier
from daa.tools.browser import facts as factlib
from daa.tools.browser.privacy import parse_aria_line
from test_browser_fakes import SEARCH_FORM, checkout_page, elem, form


def facts_for(page, selector: str, **kwargs):
    raw = page.element_facts(selector)
    assert len(raw) == 1, f"{selector} matched {len(raw)}"
    return factlib.facts_from_raw(
        raw[0],
        page_url=page.url(),
        page_title=page.title(),
        logged_in=kwargs.get("logged_in", page.has_session()),
        pending_dialog=kwargs.get("pending_dialog", page.pending_dialog()),
    )


# ---------------------------------------------------------------------------
# aria_snapshot parsing: the colon form is an input's VALUE
# ---------------------------------------------------------------------------


def test_the_accessible_name_comes_out_of_the_browser_snapshot():
    assert parse_aria_line('- button "Pay $412.00"') == ("button", "Pay $412.00")
    assert parse_aria_line('- heading "Order summary" [level=1]') == ("heading", "Order summary")


def test_an_unnamed_control_parses_as_unnamed_rather_than_as_something():
    assert parse_aria_line("- button") == ("button", "")


def test_the_parser_never_returns_what_is_typed_into_a_field():
    """`- textbox: a@b.c` -- everything after the colon is the field's VALUE."""
    role, name = parse_aria_line("- textbox: bob@example.com")
    assert role == "textbox"
    assert name == ""
    assert "bob@example.com" not in (role + name)


def test_a_named_textbox_keeps_the_name_and_drops_the_value():
    role, name = parse_aria_line('- textbox "Email": bob@example.com')
    assert (role, name) == ("textbox", "Email")
    assert "bob@example.com" not in name


# ---------------------------------------------------------------------------
# the verb table
# ---------------------------------------------------------------------------


def test_a_post_submit_reads_as_submit_and_a_get_submit_as_search():
    page = checkout_page()
    assert factlib.verb_for(facts_for(page, "#pay")) == "submit"
    _, raw = elem("#go", name="Search", input_type="submit", submits=True,
                  form_facts=SEARCH_FORM)
    get_facts = factlib.facts_from_raw(raw, page_url="https://shop.example.com/")
    assert factlib.verb_for(get_facts) == "search with"


def test_a_link_off_the_site_says_so_in_the_verb_itself():
    page = checkout_page()
    assert factlib.verb_for(facts_for(page, "#offsite")) == "leave this site and open"


def test_a_link_on_the_same_site_is_just_open():
    _, raw = elem("#same", tag="A", role="link", name="Next",
                  href="https://stripe.com/checkout/next")
    same = factlib.facts_from_raw(raw, page_url="https://stripe.com/checkout/order")
    assert factlib.verb_for(same) == "open"


def test_a_checkbox_reads_its_current_state_not_a_generic_toggle():
    _, on = elem("#c", role="checkbox", name="Email me", checked=True)
    _, off = elem("#c", role="checkbox", name="Email me", checked=False)
    assert factlib.verb_for(factlib.facts_from_raw(on, page_url="https://x.com/")) == "turn off"
    assert factlib.verb_for(factlib.facts_from_raw(off, page_url="https://x.com/")) == "turn on"


# ---------------------------------------------------------------------------
# targets: names, never selectors, never ordinals
# ---------------------------------------------------------------------------


def test_the_target_is_the_name_the_site_wrote_plus_the_site():
    page = checkout_page()
    assert factlib.target_phrase(facts_for(page, "#pay")) == (
        "the 'Pay $412.00' button on stripe.com"
    )


def test_an_unnamed_control_is_not_given_a_name_to_buy_a_cheaper_tier():
    page = checkout_page()
    nameless = facts_for(page, "#nameless")
    phrase = factlib.target_phrase(nameless)
    assert phrase == "an unlabelled button on stripe.com"
    assert "#nameless" not in phrase
    assert "unnamed" in factlib.consequences_for(nameless)
    # Not voice-confirmable: there is no honest sentence, so it goes to a card.
    assert factlib.floor_hint_for(nameless) is RiskTier.CONFIRM_VISUAL


@pytest.mark.parametrize("selector", ["#pay", "#email", "#nameless", "#offsite"])
def test_no_selector_and_no_ordinal_ever_reaches_the_spoken_phrase(selector):
    page = checkout_page()
    phrase = factlib.target_phrase(facts_for(page, selector))
    assert selector not in phrase
    assert "#" not in phrase and "nth" not in phrase
    for ordinal in ("first", "second", "third", "element 0", "@e"):
        assert ordinal not in phrase


# ---------------------------------------------------------------------------
# consequences: computed, spoken, content-free
# ---------------------------------------------------------------------------


def test_the_checkout_fixture_produces_the_sentence_the_design_promised():
    page = checkout_page()
    pay = facts_for(page, "#pay")
    spoken = factlib.consequences_for(pay)
    assert spoken["sends"] == "sending 4 fields to checkout.stripe.com"
    assert spoken["payment"] == "including a card number"
    assert spoken["amount"] == "for $412.00"
    assert spoken["logged_in"] == "where you are signed in"


def test_a_form_that_posts_somewhere_else_is_the_phishing_visible_case():
    _, raw = elem("#pay", name="Pay", input_type="submit", submits=True,
                  form_facts=form(action="https://checkout-xyz.io/take",
                                  method="post", names=("a", "b"), types=("text", "text")))
    off = factlib.facts_from_raw(raw, page_url="https://shop.example.com/cart")
    assert off.posts_cross_origin
    assert factlib.consequences_for(off)["sends"] == "sending 2 fields to checkout-xyz.io"


def test_password_and_one_time_code_forms_say_so():
    login = form(action="https://x.com/in", method="post",
                 names=("u", "p"), types=("text", "password"), hints=("", "current-password"))
    _, raw = elem("#in", name="Sign in", input_type="submit", submits=True, form_facts=login)
    spoken = factlib.consequences_for(factlib.facts_from_raw(raw, page_url="https://x.com/"))
    assert spoken["password"] == "including a password field"

    otp = form(action="https://x.com/in", method="post", names=("code",),
               types=("text",), hints=("one-time-code",))
    _, raw2 = elem("#ok", name="Continue", input_type="submit", submits=True, form_facts=otp)
    spoken2 = factlib.consequences_for(factlib.facts_from_raw(raw2, page_url="https://x.com/"))
    assert spoken2["otp"] == "including a one-time code"


def test_no_field_value_can_reach_a_consequence_because_none_is_collected():
    page = checkout_page()
    for selector, _ in page.elements:
        found = facts_for(page, selector)
        blob = " ".join(factlib.consequences_for(found).values())
        assert "@" not in blob          # no email address
        assert "4111" not in blob       # no card digits
    # And the record itself has nowhere to put one.
    assert not any(f.name == "value" for f in factlib.PageFacts.__dataclass_fields__.values())


def test_signed_in_is_appended_to_every_acting_readback_when_true():
    page = checkout_page()
    for selector, _ in page.elements:
        assert "logged_in" in factlib.consequences_for(facts_for(page, selector))


# ---------------------------------------------------------------------------
# the name table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,group",
    [
        ("Pay $412.00", "spends"),
        ("Place order", "spends"),
        ("Delete this review", "destroys"),
        ("Transfer funds", "moves money"),
        ("Cancel subscription", "unwinds"),
        ("Send", "sends"),
        ("Approve", "commits"),
        ("Show more details", None),
        ("Deleted items", None),
        ("", None),
    ],
)
def test_the_control_label_is_the_highest_signal_string_on_the_page(name, group):
    assert factlib.name_group(name) == group


def test_the_english_only_limit_is_real_and_the_other_signals_cover_it():
    """A German pay button does not match the table. The POST does."""
    german = form(action="https://shop.de/pay", method="post",
                  names=("cc",), types=("text",), hints=("cc-number",))
    _, raw = elem("#b", name="Kostenpflichtig bestellen", input_type="submit",
                  submits=True, form_facts=german)
    found = factlib.facts_from_raw(raw, page_url="https://shop.de/kasse")
    assert found.cost_group is None                      # the table missed it
    assert factlib.must_use_submit(found) is True        # the structure did not
    assert factlib.floor_hint_for(found) is RiskTier.CONFIRM_VISUAL


# ---------------------------------------------------------------------------
# must_use_submit: the categorical route
# ---------------------------------------------------------------------------


def test_a_post_submit_control_must_route_to_the_form_tool():
    assert factlib.must_use_submit(facts_for(checkout_page(), "#pay")) is True


def test_a_plain_search_button_does_not_need_the_expensive_tool():
    _, raw = elem("#go", name="Search", input_type="submit", submits=True,
                  form_facts=SEARCH_FORM)
    found = factlib.facts_from_raw(raw, page_url="https://shop.example.com/")
    assert factlib.must_use_submit(found) is False
    assert factlib.floor_hint_for(found) is None


def test_a_non_form_button_is_never_routed_because_there_is_no_form_to_submit():
    _, raw = elem("#more", name="Show more details")
    assert factlib.must_use_submit(
        factlib.facts_from_raw(raw, page_url="https://shop.example.com/")
    ) is False


def test_a_destructive_button_with_no_form_escalates_instead_of_routing():
    _, raw = elem("#del", name="Delete this review")
    found = factlib.facts_from_raw(raw, page_url="https://shop.example.com/p/42")
    assert factlib.must_use_submit(found) is False
    assert factlib.floor_hint_for(found) is RiskTier.CONFIRM_VISUAL


def test_a_get_form_whose_button_says_pay_still_routes():
    """Signal 1 is enough on its own when the label says it costs money."""
    _, raw = elem("#b", name="Pay now", input_type="submit", submits=True,
                  form_facts=form(action="https://x.com/p", method="get",
                                  names=("a",), types=("text",)))
    assert factlib.must_use_submit(factlib.facts_from_raw(raw, page_url="https://x.com/")) is True


# ---------------------------------------------------------------------------
# floor_hint: one-way, and never SILENT
# ---------------------------------------------------------------------------


def test_the_hint_never_claims_a_cheaper_tier_than_no_claim_at_all():
    """`SILENT` would read as a claim. None is "nothing to say"."""
    _, raw = elem("#more", name="Show more details")
    found = factlib.facts_from_raw(raw, page_url="https://shop.example.com/")
    hint = factlib.floor_hint_for(found)
    assert hint is None
    assert hint is not RiskTier.SILENT


@pytest.mark.parametrize(
    "kwargs,url,expected",
    [
        ({"name": "Show more"}, "https://shop.example.com/p", None),
        ({"name": "Show more"}, "https://shop.example.com/checkout", RiskTier.CONFIRM_VISUAL),
        ({"name": "Continue"}, "https://stripe.com/x", RiskTier.CONFIRM_VISUAL),
        ({"name": "Continue"}, "https://monzo.com/x", RiskTier.CONFIRM_VISUAL),
        ({"name": ""}, "https://shop.example.com/p", RiskTier.CONFIRM_VISUAL),
        ({"name": "Next", "visible": False}, "https://shop.example.com/p",
         RiskTier.CONFIRM_VOICE),
        ({"name": "Next", "in_dialog": True}, "https://shop.example.com/p",
         RiskTier.CONFIRM_VOICE),
        ({"name": "Next", "nearby_amount": "$9.99"}, "https://shop.example.com/p",
         RiskTier.CONFIRM_VISUAL),
    ],
)
def test_the_graduated_escalation_covers_the_long_tail(kwargs, url, expected):
    _, raw = elem("#x", **kwargs)
    assert factlib.floor_hint_for(factlib.facts_from_raw(raw, page_url=url)) is expected


def test_being_signed_in_alone_is_worth_a_spoken_confirmation():
    _, raw = elem("#more", name="Show more details")
    found = factlib.facts_from_raw(raw, page_url="https://shop.example.com/p", logged_in=True)
    assert factlib.floor_hint_for(found) is RiskTier.CONFIRM_VOICE


# ---------------------------------------------------------------------------
# dom_fingerprint
# ---------------------------------------------------------------------------


def test_the_fingerprint_is_stable_when_nothing_moved():
    page = checkout_page()
    assert factlib.dom_fingerprint(facts_for(page, "#pay")) == factlib.dom_fingerprint(
        facts_for(page, "#pay")
    )


@pytest.mark.parametrize(
    "change",
    [
        {"name": "Pay $941.00"},
        {"role": "link"},
        {"submits": False},
        {"in_dialog": True},
        {"enabled": False},
        {"box": (10, 900, 120, 40)},
        {"form_facts": form(action="https://elsewhere.example/pay", method="post",
                            names=("email",), types=("email",))},
    ],
)
def test_any_change_that_changes_what_the_button_is_changes_the_fingerprint(change):
    page = checkout_page()
    before = factlib.dom_fingerprint(facts_for(page, "#pay"))
    if "form_facts" in change:
        page.mutate("#pay", form=change.pop("form_facts"))
    page.mutate("#pay", **change)
    assert factlib.dom_fingerprint(facts_for(page, "#pay")) != before


def test_navigating_is_a_mismatch_by_construction():
    page = checkout_page()
    before = factlib.dom_fingerprint(facts_for(page, "#pay"))
    page.page_url = "https://stripe.com/checkout/confirm"
    assert factlib.dom_fingerprint(facts_for(page, "#pay")) != before
    page.page_url = "https://evil.example.net/checkout/order"
    assert factlib.dom_fingerprint(facts_for(page, "#pay")) != before


def test_a_sub_pixel_reflow_does_not_abort_a_confirmation():
    """Erring toward abort is safe; erring toward it on every carousel tick
    trains the user to re-ask until it works, which is the same disease."""
    page = checkout_page()
    before = factlib.dom_fingerprint(facts_for(page, "#pay"))
    page.mutate("#pay", box=(11, 101, 121, 41))
    assert factlib.dom_fingerprint(facts_for(page, "#pay")) == before


def test_the_fingerprint_is_not_the_selector():
    """Two different nodes reachable by the same selector hash differently."""
    page = checkout_page()
    first = factlib.dom_fingerprint(facts_for(page, "#pay"))
    page.mutate("#pay", name="Delete account", submits=False, form=None)
    assert factlib.dom_fingerprint(facts_for(page, "#pay")) != first
