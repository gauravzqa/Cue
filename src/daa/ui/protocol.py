"""The wire format, and nothing else.

Newline-delimited JSON, UTF-8, one object per line, both directions. No
Content-Length headers, no embedded raw newlines (JSON escaping guarantees
this, and `encode` asserts it the way the Swift encoder does).

    {"t":"req","id":"r7","m":"method.name","p":{}}
    {"t":"res","id":"r7","ok":true,"p":{}}
    {"t":"res","id":"r7","ok":false,"err":{"code":"x","message":"y"}}
    {"t":"ev","m":"event.name","p":{}}

Requests flow BOTH ways: Python raises `confirm.request` at the dock exactly
as the dock raises `undo.last` at Python. Ids are opaque strings, unique per
sender.

Two rules the Swift decoder enforces and this file mirrors, because both are
about consent rather than about parsing:

    A `res` with no `ok` field decodes as an ERROR, never as success. The only
    thing a response ever authorises is an action, and an unreadable answer is
    not an approval.

    An unparseable line raises `FrameError`, which the reader logs to stderr
    and skips. It is never fatal: one corrupt line must not take the bridge
    down, because a bridge that exits is a dock that silently stops updating.

This module has no threads, no I/O and no knowledge of the loop. It is pure
enough to test line-by-line, which is what `tests/test_bridge_protocol.py`
does.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "CHATTY_KINDS",
    "DISPLAY_KEYS",
    "LOAD_BEARING_KINDS",
    "PROTOCOL_VERSION",
    "Event",
    "Frame",
    "FrameError",
    "Method",
    "Request",
    "Response",
    "audit_payload",
    "card_payload",
    "decode",
    "encode",
    "event",
    "request",
    "response",
]

# Bumped only for a breaking change. The dock sends its own in `session.hello`
# and a mismatched MAJOR is answered with an error rather than a guess.
PROTOCOL_VERSION = 1


class FrameError(ValueError):
    """An unreadable line. Logged and skipped, never fatal."""


# ---------------------------------------------------------------------------
# Method names
# ---------------------------------------------------------------------------


class Method:
    """Every method name in one place, so a typo is an AttributeError rather
    than a frame nobody handles. Mirrors `Method` in WireProtocol.swift."""

    # dock -> daa
    HELLO = "session.hello"
    SHUTDOWN = "session.shutdown"
    MIC_UTTERANCE = "mic.utterance"
    MIC_ONSET = "mic.onset"
    CONTROL_ALWAYS_ON = "control.alwaysOn"
    CONTROL_CANCEL = "control.cancel"
    CONTROL_TEXT = "control.text"
    UNDO_LAST = "undo.last"
    DOCTOR = "doctor"

    # daa -> dock
    READY = "ready"
    STATE = "state"
    AUDIT = "audit"
    SPEAK = "speak"
    CONFIRM_REQUEST = "confirm.request"
    CONFIRM_CANCEL = "confirm.cancel"
    TASK_UPDATE = "task.update"
    TASK_DONE = "task.done"


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Request:
    id: str
    method: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Response:
    id: str
    ok: bool
    params: dict[str, Any] = field(default_factory=dict)
    code: str = ""
    message: str = ""


@dataclass(frozen=True, slots=True)
class Event:
    method: str
    params: dict[str, Any] = field(default_factory=dict)


Frame = Request | Response | Event


def request(id: str, method: str, **params: Any) -> Request:
    return Request(id=id, method=method, params=dict(params))


def response(id: str, ok: bool, params: Any = None) -> Response:
    """`params` is a dict rather than **kwargs on purpose: `undo.last` answers
    with a payload that has its own `ok` field, and a keyword form would have
    made that a TypeError at exactly the wrong moment."""
    return Response(id=id, ok=ok, params=dict(params or {}))


def event(method: str, **params: Any) -> Event:
    return Event(method=method, params=dict(params))


def error(id: str, code: str, message: str) -> Response:
    return Response(id=id, ok=False, code=code, message=message)


def encode(frame: Frame) -> bytes:
    """One line, newline-terminated, no embedded raw newlines.

    `separators` matches the Swift encoder's compactness; `ensure_ascii=False`
    keeps a script's non-ASCII text readable in the log while still producing
    valid UTF-8. Control characters are escaped by `json.dumps`, which is what
    makes the newline assertion below a tripwire rather than a hope.
    """
    if isinstance(frame, Request):
        root: dict[str, Any] = {
            "t": "req",
            "id": frame.id,
            "m": frame.method,
            "p": frame.params,
        }
    elif isinstance(frame, Event):
        root = {"t": "ev", "m": frame.method, "p": frame.params}
    elif isinstance(frame, Response):
        root = {"t": "res", "id": frame.id, "ok": bool(frame.ok)}
        if frame.ok:
            root["p"] = frame.params
        else:
            root["err"] = {"code": frame.code or "error", "message": frame.message}
    else:  # pragma: no cover - the union is closed
        raise FrameError(f"not a frame: {type(frame).__name__}")
    line = json.dumps(root, ensure_ascii=False, separators=(",", ":"))
    # The Swift side preconditions on this. So do we: a corrupt stream looks
    # like a protocol bug on the other end and costs an evening to find.
    assert "\n" not in line, "frame contains a raw newline"
    return line.encode("utf-8") + b"\n"


def decode(line: str | bytes) -> Frame:
    """Parse one line. Raises `FrameError` for anything unreadable."""
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FrameError(f"not utf-8: {exc}") from exc
    text = line.strip()
    if not text:
        raise FrameError("empty line")
    try:
        root = json.loads(text)
    except Exception as exc:
        raise FrameError(f"not json: {exc}") from exc
    if not isinstance(root, dict):
        raise FrameError("top level is not an object")

    kind = root.get("t")
    params = root.get("p")
    params = dict(params) if isinstance(params, dict) else {}

    if kind == "req":
        fid, method = root.get("id"), root.get("m")
        if not isinstance(fid, str) or not fid:
            raise FrameError("req without id")
        if not isinstance(method, str) or not method:
            raise FrameError("req without m")
        return Request(id=fid, method=method, params=params)

    if kind == "res":
        fid = root.get("id")
        if not isinstance(fid, str) or not fid:
            raise FrameError("res without id")
        # Absent `ok` is NOT a permissive default. See the module docstring.
        ok = root.get("ok")
        if ok is True:
            return Response(id=fid, ok=True, params=params)
        err = root.get("err")
        err = err if isinstance(err, dict) else {}
        return Response(
            id=fid,
            ok=False,
            code=str(err.get("code") or "error"),
            message=str(err.get("message") or "the other side said no"),
        )

    if kind == "ev":
        method = root.get("m")
        if not isinstance(method, str) or not method:
            raise FrameError("ev without m")
        return Event(method=method, params=params)

    raise FrameError(f"unknown t={kind!r}")


# ---------------------------------------------------------------------------
# The audit tee's vocabulary
# ---------------------------------------------------------------------------

# Display-only kinds. Under backpressure these are dropped FIRST, because the
# dock uses them to look alive and the disk log keeps them regardless.
# Mirrors `AuditRecord.chatty` in Payloads.swift.
CHATTY_KINDS = frozenset({"heard", "spoke", "barge_in", "buffered"})

# Kinds that are a record of a DECISION. Never dropped, whatever the queue
# looks like. Mirrors `AuditRecord.loadBearing` in Payloads.swift; a test
# asserts the two sets are disjoint on both sides.
LOAD_BEARING_KINDS = frozenset(
    {
        "disposition",
        "confirmation",
        "confirmation_event",
        "execution",
        "undo",
        "undo_rejected",
        "undo_retained",
        "refused",
        "dry_run",
        "visual_confirm",
        "deferred_visual",
        "abandoned",
        "error",
        "judgment",
    }
)

# The dock renders a transcript, and a transcript of "<redacted 24 chars>" is
# not a transcript. These payload keys are restored for these kinds only, and
# they are the only ones: `safety/audit.py` redacts `text` and `summary` BY
# KEY because the file they land in is a record of someone's whole day that
# outlives the session. The dock is not that file. It is the screen in front
# of the person who just said the sentence, and it already receives daa's own
# words verbatim over `speak`; `woke` is the user's own utterance, which the
# dock transcribed in the first place and sent to us.
#
# Restored through `scrub()`, never raw: the SHAPE rules -- card numbers, IDs,
# API keys, the length cap -- still apply, so a spoken card number is still
# redacted on its way to the screen. Only the by-key rule is lifted, and only
# for the four kinds that project into a transcript line.
DISPLAY_KEYS: dict[str, tuple[str, ...]] = {
    "woke": ("text",),
    "spoke": ("text",),
    "execution": ("summary",),
    "dry_run": ("summary",),
}


def audit_payload(event: Any, *, record: Any, scrub: Any) -> dict[str, Any]:
    """One `audit` event's payload: `{kind, id, at, payload}`.

    `record` and `scrub` are `safety.audit.record` / `.scrub`, injected rather
    than imported so this module stays free of the rest of the tree (and so a
    half-built checkout still imports it). The record is the EXACT dict
    `JsonlAudit` writes -- same redaction, same `synthetic` hoist -- with the
    display keys of `DISPLAY_KEYS` put back, scrubbed.
    """
    out = record(event)
    payload_out = out.get("payload")
    if isinstance(payload_out, dict) and "synthetic" not in payload_out:
        # `record` hoists `synthetic` to the top level; the dock reads it off
        # the payload to badge a row SYNTHETIC. Copy it rather than ask the
        # dock to look in two places, and never overwrite a real one.
        payload_out["synthetic"] = bool(out.get("synthetic"))
    keys = DISPLAY_KEYS.get(str(out.get("kind", "")))
    if keys:
        raw = event.payload if isinstance(event.payload, dict) else dict(event.payload or {})
        payload = out.get("payload")
        if isinstance(payload, dict):
            for key in keys:
                value = raw.get(key)
                if isinstance(value, str) and value:
                    payload[key] = scrub(value)
    return out


# ---------------------------------------------------------------------------
# The approval card
# ---------------------------------------------------------------------------


def card_payload(
    action: Any,
    disposition: Any,
    *,
    dry_run: bool,
    phrase: str,
    script_keys: tuple[str, ...],
    expires_in_ms: int = 90_000,
) -> dict[str, Any]:
    """`confirm.request`'s payload, built from the two objects `present` gets.

    Every argument in full, never summarised and never truncated: the tier
    exists to put the real thing in front of a person, and a card assembled
    out of an elision is a consent record for something nobody saw. The dock
    enforces the same rule from its side -- a frame it cannot render in full
    is answered `granted:false` and no card is shown.

    `phrase` and `script_keys` are passed in rather than imported for the same
    reason `record` is above: `_phrase` and `_SCRIPT_KEYS` live in loop.py and
    this module does not import loop.py.
    """
    args = dict(getattr(action, "args", {}) or {})
    assessment = getattr(disposition, "assessment", None)
    tier = getattr(disposition, "tier", None)
    return {
        "tool": str(getattr(action, "tool", "") or ""),
        "tier": getattr(tier, "name", str(tier)),
        "reason": str(getattr(disposition, "reason", "") or ""),
        "phrase": phrase,
        "verb": str(getattr(action, "verb", "") or ""),
        "explicit": bool(getattr(action, "explicit", False)),
        "dryRun": bool(dry_run),
        "targets": [str(t) for t in (getattr(action, "targets", ()) or ())],
        "args": [
            {"key": str(k), "isProgram": str(k) in script_keys, "value": _as_text(v)}
            for k, v in args.items()
        ],
        "consequences": {
            str(k): str(v) for k, v in dict(getattr(action, "consequences", {}) or {}).items()
        },
        "assessment": _assessment_payload(assessment),
        "expiresInMs": int(expires_in_ms),
    }


def _as_text(value: Any) -> str:
    """Arguments reach the card as strings, because that is what is rendered.

    A dict or a list is JSON-dumped rather than `repr`'d: a Python repr of a
    nested structure is not something a person can read and decide about, and
    deciding about it is the entire point of the screen.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, ensure_ascii=False, indent=2, default=str)
        except Exception:  # noqa: BLE001 -- unrenderable is still showable
            return str(value)
    return str(value)


def _assessment_payload(assessment: Any) -> dict[str, Any]:
    """The judgment, or the worst case said out loud.

    A missing assessment is not a mild one. `synthetic: True` here means the
    card says "daa could not get a real judgment, so it is assuming the
    worst", which is the honest sentence and the same default the dock
    applies when the field is absent entirely.
    """
    if assessment is None:
        return {
            "blastRadius": 3.0,
            "unrecoverable": 1.0,
            "explicitlyRequested": 0.0,
            "targetConfidence": "guessing",
            "confidence": 0.0,
            "synthetic": True,
        }
    return {
        "blastRadius": _number(getattr(assessment, "blast_radius", None), 3.0),
        "unrecoverable": _number(getattr(assessment, "unrecoverable", None), 1.0),
        "explicitlyRequested": _number(getattr(assessment, "explicitly_requested", None), 0.0),
        "targetConfidence": str(getattr(assessment, "target_confidence", "") or "guessing"),
        "confidence": _number(getattr(assessment, "confidence", None), 0.0),
        "synthetic": bool(getattr(assessment, "synthetic", True)),
    }


def _number(value: Any, fallback: float) -> float:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    # NaN and the infinities are not JSON, and `json.dumps` would emit bare
    # `NaN`, which the Swift decoder rejects -- taking the whole card with it.
    if math.isnan(out) or math.isinf(out):
        return fallback
    return out
