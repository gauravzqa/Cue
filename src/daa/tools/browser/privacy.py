"""What must never leave the page, and what must never leave the machine.

The browser is the first daa subsystem that routinely *holds credentials*. The
list below is exhaustive by intent: if it came from a page and it is not on the
allowed side, assume it is forbidden.

Never, under any key, in any log, journal, exception or tool `summary`:

  - cookie values, `Set-Cookie`, session ids, bearer tokens, CSRF, OAuth
    `code`/`state`/`id_token`
  - `localStorage`, `sessionStorage`, IndexedDB
  - any `<input>` / `<textarea>` / `contenteditable` value, EVER -- including
    the `value` argument to `fill_field`, including the value it replaces,
    including anything the page pre-filled
  - page text, extracted markdown, DOM, HTML or `aria_snapshot` output
  - screenshots, and the paths of screenshots
  - the CDP WebSocket URL
  - query strings and fragments (see `urls.log_url`)

Allowed, because they are references rather than content: origins, hosts,
paths, titles, character counts, field NAMES and TYPES, accessible names,
selectors and `dom_fingerprint`.

Enforcement is structural, not by filtering on the way out. Tools in this
package never put the forbidden things into `targets`, `consequences`, `args`,
`summary` or an `UndoAction` in the first place -- page content lives only in
`ToolResult.data`, which the event builders in `safety/audit.py` do not read.
tests/test_browser_privacy.py asserts that over every tool, driven off the tool
list, so a future tool cannot quietly opt out.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Shape redaction for the ONE payload that legitimately leaves the machine
# ---------------------------------------------------------------------------
#
# `daa.safety.audit.scrub()` already implements exactly these shapes and is the
# reviewed copy. It cannot be reused here: its first rule is a MAX_STRING
# backstop that replaces any string over 240 characters with "<elided N chars>",
# which is correct for a log field and would delete the entire page. So the
# patterns are restated, and tests/test_browser_privacy.py asserts this module
# and `audit.scrub` agree on a shared corpus, so the two cannot drift apart
# unnoticed.

_CARD_RE = re.compile(r"(?<![0-9])(?:[0-9][ -]?){12,18}[0-9](?![0-9])")
_ID_RE = re.compile(r"(?<![0-9])[0-9]{3}-[0-9]{2}-[0-9]{4}(?![0-9])")
_SECRET_RES = (
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{12,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _card_sub(match: re.Match[str]) -> str:
    digits = "".join(c for c in match.group(0) if c.isdigit())
    if not 13 <= len(digits) <= 19 or not _luhn(digits):
        return match.group(0)
    return "<redacted card number>"


def redact_shapes(text: str) -> str:
    """Strip the things that are secrets wherever they appear, keeping the prose.

    Runs over extracted page text BEFORE it is handed to the layer that will
    send it to a model -- not only on the way into the audit log. hermes found
    this the hard way: a page displaying environment variables leaks to the
    summarising model long before any general redaction layer runs.
    """
    out = _CARD_RE.sub(_card_sub, str(text))
    out = _ID_RE.sub("<redacted id number>", out)
    for pattern in _SECRET_RES:
        out = pattern.sub("<redacted secret>", out)
    return out


# ---------------------------------------------------------------------------
# Fields a voice assistant must never type into
# ---------------------------------------------------------------------------
#
# Not "confirm harder" -- refuse. A voice assistant should never be the thing
# that types a card number, and the enclosing tool has no tier at which it
# becomes acceptable, so the refusal lives in code that runs before and after
# the confirmation rather than in a floor.

CREDENTIAL_AUTOCOMPLETE = frozenset(
    {
        "cc-number", "cc-csc", "cc-exp", "cc-exp-month", "cc-exp-year",
        "cc-name", "cc-given-name", "cc-family-name", "cc-type",
        "current-password", "new-password", "one-time-code",
    }
)
CREDENTIAL_INPUT_TYPES = frozenset({"password"})

# Autocomplete hints that mean "money or identity is in this form", used for
# the spoken consequence rather than for a refusal.
PAYMENT_HINTS = frozenset(
    {"cc-number", "cc-csc", "cc-exp", "cc-exp-month", "cc-exp-year", "cc-type", "cc-name"}
)
OTP_HINTS = frozenset({"one-time-code"})
PASSWORD_HINTS = frozenset({"current-password", "new-password"})


def credential_reason(input_type: str | None, autocomplete: str | None) -> str | None:
    """Spoken reason this field may not be typed into, or None."""
    hint = (autocomplete or "").strip().lower()
    kind = (input_type or "").strip().lower()
    if kind in CREDENTIAL_INPUT_TYPES:
        return "that is a password field, and I never type into one"
    if hint in PAYMENT_HINTS:
        return "that is a payment card field, and I never type into one"
    if hint in OTP_HINTS:
        return "that is a one-time code field, and I never type into one"
    if hint in PASSWORD_HINTS:
        return "that is a password field, and I never type into one"
    return None


# ---------------------------------------------------------------------------
# aria_snapshot parsing
# ---------------------------------------------------------------------------
#
# `Locator.aria_snapshot()` renders an element as, e.g.
#
#     - button "Pay $412.00"
#     - textbox: a@b.c                 <-- everything after the colon is the VALUE
#     - heading "Order summary" [level=1]
#     - button                          <-- no accessible name at all
#
# The colon form is the reason this is a parser rather than a regex someone
# writes inline at a call site: the text after it is an input's current value,
# which this package may never read, never return and never log. So the parser
# stops at the first colon outside quotes, and it never invents a name for the
# bare form.

_ROLE_RE = re.compile(r"^\s*-\s*([A-Za-z][A-Za-z0-9 _-]*?)\s*(?=$|[\":\[])")


def parse_aria_line(snapshot: str) -> tuple[str, str]:
    """(role, accessible_name). An empty name means the element has none.

    Never returns anything after a `:` -- that is an input's value.
    """
    line = ""
    for candidate in str(snapshot or "").splitlines():
        if candidate.strip().startswith("-"):
            line = candidate
            break
    if not line:
        return "", ""
    match = _ROLE_RE.match(line)
    role = (match.group(1).strip() if match else "").lower()
    rest = line[match.end():] if match else ""
    name = ""
    if rest.lstrip().startswith('"'):
        rest = rest.lstrip()
        closing = rest.find('"', 1)
        if closing > 0:
            name = rest[1:closing]
    return role, name


__all__ = [
    "CREDENTIAL_AUTOCOMPLETE",
    "CREDENTIAL_INPUT_TYPES",
    "OTP_HINTS",
    "PASSWORD_HINTS",
    "PAYMENT_HINTS",
    "credential_reason",
    "parse_aria_line",
    "redact_shapes",
]
