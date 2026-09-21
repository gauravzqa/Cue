"""Nothing a page holds may reach a file on disk.

Driven off the tool list rather than a hand-written set of cases, like
`test_undo_coverage.py`, so a twelfth browser tool cannot quietly opt out.

The surfaces under test are exactly the ones that get written down:

  `ResolvedAction.targets` / `.verb` / `.consequences` / `.origin`
      -> copied verbatim into `judgment` and `disposition` audit events
  `ToolResult.summary` / `.error`
      -> copied into the `execution` event
  `UndoAction.description` / `.args`
      -> written to `~/.daa/undo.jsonl` and replayed later

`ToolResult.data` is deliberately NOT on that list: it is the one field no
audit event builder reads, which is why page text lives there and nowhere else.
"""

from __future__ import annotations

import json

import pytest

from daa.config import Settings
from daa.safety import audit
from daa.tools.browser import BROWSER_TOOL_CLASSES, make_browser_tools, urls
from test_browser_fakes import TEST_OPTIONS, FakeBackend, FakePage, elem, form

LIVE = Settings(dry_run=False)

# Every marker is a thing a page can hold that must never be written down.
CARD = "4012 8888 8888 1881"          # Luhn-valid
TOKEN = "tokabc123SECRETxyz"          # a magic-link token in a query string
COOKIE_VALUE = "cookievalueSECRET"
FIELD_VALUE = "fieldvalueSECRET"
PAGE_SECRET = "pagetextSECRET"
API_KEY = "sk-livedeadbeefdeadbeef"
MARKERS = (CARD, CARD.replace(" ", ""), TOKEN, COOKIE_VALUE, FIELD_VALUE, PAGE_SECRET, API_KEY)

LOGIN_FORM = form(
    action="https://portal.example.com/pay?session=" + TOKEN,
    method="post",
    names=("email", "cardnumber", "password"),
    types=("email", "text", "password"),
    hints=("email", "cc-number", "current-password"),
)


def poisoned_page() -> FakePage:
    """A page carrying one of everything this package must not write down."""
    return FakePage(
        page_url=f"https://portal.example.com/statement?token={TOKEN}#{PAGE_SECRET}",
        page_title="Statement",
        text=f"Your card {CARD} and key {API_KEY}. {PAGE_SECRET} appears here.",
        headings=("Statement",),
        cookies=[{"name": "sessionid", "httpOnly": True, "secure": True,
                  "value": COOKIE_VALUE}],
        elements=[
            elem("#pay", name="Pay now", input_type="submit", submits=True,
                 form_facts=LOGIN_FORM, nearby_amount="$412.00"),
            elem("#email", tag="INPUT", role="textbox", name="Email", input_type="email",
                 value_empty=False, form_facts=LOGIN_FORM, box=(0, 0, 100, 20)),
            elem("#note", tag="TEXTAREA", role="textbox", name="Note",
                 value_empty=False, box=(0, 40, 100, 20)),
            elem("#more", name="Show more", box=(0, 80, 100, 20)),
        ],
    )


# Every tool, with a call that actually reaches the poisoned page.
CALLS: dict[str, dict] = {
    "list_tabs": {},
    "read_page": {},
    # The needle is the USER's own words, not page content, so it is spoken
    # back -- see the dedicated test below. A page marker here would be
    # testing the wrong thing.
    "find_on_page": {"text": "Statement"},
    "scroll_page": {"direction": "down"},
    "open_tab": {"url": f"https://portal.example.com/login?token={TOKEN}"},
    "close_tab": {"tab_id": "t1"},
    "go_back": {},
    "summarise_page": {},
    "click_element": {"text": "Pay now"},
    "fill_field": {"text": "Note", "value": FIELD_VALUE},
    "submit_form": {"text": "Pay now"},
}


def tools_for(page):
    backend = FakeBackend(pages=[page], options=TEST_OPTIONS)
    return {
        t.spec.name: t for t in make_browser_tools(LIVE, session=backend, options=TEST_OPTIONS)
    }


def written_surfaces(action, result) -> str:
    """Everything that leaves a tool and ends up in a file, as one blob."""
    blob = {
        "targets": list(action.targets),
        "verb": action.verb,
        "consequences": dict(action.consequences),
        "origin": action.origin,
        "summary": result.summary,
        "error": result.error,
        "undo": (
            {"description": result.undo.description, "args": dict(result.undo.args)}
            if result.undo else None
        ),
    }
    return json.dumps(blob, default=str)


def audit_records(action, result) -> str:
    """The exact dicts `JsonlAudit` would write to disk."""
    events = [
        audit.judgment_event(action, None),
        audit.disposition_event(action, __import__(
            "daa.contracts", fromlist=["Disposition"]
        ).Disposition(tier=action.floor_hint or 0, reason="test")),
        audit.confirmation_event(action, tier=2, granted=True, via="voice"),
        audit.execution_event(
            action, ok=result.ok, summary=result.summary, error=result.error, undo=result.undo
        ),
    ]
    return json.dumps([audit.record(e) for e in events], default=str)


def test_the_call_table_covers_every_browser_tool():
    assert set(CALLS) == {cls.spec.name for cls in BROWSER_TOOL_CLASSES}


@pytest.mark.parametrize("name", sorted(CALLS), ids=str)
def test_no_tool_writes_page_content_a_value_a_cookie_or_a_query_string(name):
    page = poisoned_page()
    page.history.append("https://portal.example.com/previous")
    tool = tools_for(page)[name]
    action = tool.resolve(**CALLS[name])
    result = tool.run(action)
    blob = written_surfaces(action, result)
    for marker in MARKERS:
        assert marker not in blob, f"{name} wrote {marker!r} into {blob}"
    assert "?" not in blob.replace("\\", "")
    assert "#" not in blob


@pytest.mark.parametrize("name", sorted(CALLS), ids=str)
def test_nothing_survives_into_the_audit_log_either(name):
    page = poisoned_page()
    page.history.append("https://portal.example.com/previous")
    tool = tools_for(page)[name]
    action = tool.resolve(**CALLS[name])
    result = tool.run(action)
    blob = audit_records(action, result)
    for marker in MARKERS:
        assert marker not in blob, f"{name} put {marker!r} in the audit log"


@pytest.mark.parametrize("name", sorted(CALLS), ids=str)
def test_no_undo_row_carries_a_value_a_cookie_or_a_query_string(name):
    page = poisoned_page()
    tool = tools_for(page)[name]
    result = tool.run(tool.resolve(**CALLS[name]))
    if result.undo is None:
        pytest.skip(f"{name} produced no undo on this path")
    args = dict(result.undo.args)
    assert "value" not in args and "values" not in args
    for marker in MARKERS:
        assert marker not in json.dumps(args, default=str)
    for key, raw in args.items():
        if key == "url":
            assert raw == urls.log_url(raw)


def test_the_needle_is_echoed_but_the_page_around_it_is_not():
    """`find_on_page` speaks back what the USER said, never what it found."""
    page = poisoned_page()
    tool = tools_for(page)["find_on_page"]
    action = tool.resolve(text=PAGE_SECRET)
    result = tool.run(action)
    assert PAGE_SECRET in result.summary                      # the user's words
    assert CARD not in written_surfaces(action, result)       # the page's
    assert API_KEY not in written_surfaces(action, result)
    assert result.data["matches"], "the surrounding text belongs in data"


def test_the_page_body_reaches_data_and_only_data():
    """`ToolResult.data` is the one field no audit event builder reads."""
    page = poisoned_page()
    page.cookies = []                       # a public page, so the text comes back
    tool = tools_for(page)["read_page"]
    action = tool.resolve()
    result = tool.run(action)
    assert PAGE_SECRET in result.data["text"]
    assert PAGE_SECRET not in written_surfaces(action, result)
    assert PAGE_SECRET not in audit_records(action, result)
    # And the shapes that are secrets anywhere are gone even from `data`.
    assert CARD not in result.data["text"]
    assert API_KEY not in result.data["text"]


def test_the_typed_value_lives_in_args_for_the_tool_and_nowhere_that_is_logged():
    page = poisoned_page()
    tool = tools_for(page)["fill_field"]
    action = tool.resolve(text="Note", value=FIELD_VALUE)
    assert action.args["value"] == FIELD_VALUE          # the tool needs it
    assert FIELD_VALUE not in audit_records(action, tool.run(action))


def test_a_cookie_value_is_never_even_read_into_the_tool():
    page = poisoned_page()
    handle = tools_for(page)["read_page"].page_for(None)
    assert handle.cookie_names() == ["sessionid"]
    assert handle.has_session() is True


# ---------------------------------------------------------------------------
# mutation-reverts: each rule is load-bearing
# ---------------------------------------------------------------------------


def test_mutation_revert_a_raw_url_in_a_target_leaks_the_magic_link(monkeypatch):
    """Sabotage `log_url` and `open_tab`'s undo starts carrying the token."""
    page = poisoned_page()
    tools = tools_for(page)
    honest = tools["close_tab"]
    clean = honest.run(honest.resolve(tab_id="t1"))
    assert TOKEN not in json.dumps(dict(clean.undo.args))

    from daa.tools.browser import reading

    monkeypatch.setattr(reading.urls, "log_url", lambda raw: str(raw))
    page2 = poisoned_page()
    leaky = tools_for(page2)["close_tab"]
    dirty = leaky.run(leaky.resolve(tab_id="t1"))
    assert TOKEN in json.dumps(dict(dirty.undo.args))


def test_mutation_revert_returning_a_private_page_body_puts_it_one_step_from_a_model(
    monkeypatch,
):
    page = poisoned_page()
    tool = tools_for(page)["read_page"]
    assert "text" not in tool.run(tool.resolve()).data

    monkeypatch.setattr(type(page), "has_session", lambda self: False)
    leaky = tools_for(poisoned_page())["read_page"]
    assert PAGE_SECRET in leaky.run(leaky.resolve()).data["text"]


# ---------------------------------------------------------------------------
# the duplicated shape rules must not drift from safety/audit.py
# ---------------------------------------------------------------------------

SHAPE_CORPUS = [
    "my card is 4012 8888 8888 1881 ok",
    "4012888888881881",
    "ssn 123-45-6789",
    "key sk-livedeadbeefdeadbeef here",
    "ghp_aaaaaaaaaaaaaaaaaaaa",
    "AKIAIOSFODNN7EXAMPLE",
    "xoxb-1234567890-abcdef",
    "-----BEGIN RSA PRIVATE KEY-----",
    "nothing secret at all",
    "order 12345 shipped on 2026-09-21",
]


@pytest.mark.parametrize("sample", SHAPE_CORPUS, ids=range(len(SHAPE_CORPUS)))
def test_the_local_shape_redactor_agrees_with_the_audit_one(sample):
    """`audit.scrub` cannot be reused -- its MAX_STRING backstop would delete a
    whole page -- so the patterns are restated. This pins them together."""
    from daa.tools.browser.privacy import redact_shapes

    assert redact_shapes(sample) == audit.scrub(sample)


def test_the_local_redactor_keeps_a_long_page_that_audit_would_elide():
    from daa.tools.browser.privacy import redact_shapes

    page = "word " * 2000
    assert len(redact_shapes(page)) == len(page)
    assert audit.scrub(page).startswith("<elided")
