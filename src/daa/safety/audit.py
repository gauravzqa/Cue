"""Append-only audit log.

This file will end up holding a record of someone's whole day: what they asked
for, what we thought about it, what we did. Two consequences drive the design.

First, PRIVACY. We log paths, names, counts, tiers and reasons -- never the
thing itself. Not clipboard contents, not file contents, not the body of a
message. If it is the payload rather than the reference, it is redacted here,
by key, by shape and by length, on the way out. A log you would not want read
aloud is a log that should not exist.

Redaction runs at three levels, because a leak only needs one of them to be
missing:

    BY KEY    -- `clipboard`, `text`, `before`, `after`, `reply`, ... Matched
                 exactly on the lowercased key.
    BY SCOPE  -- everything UNDER a redacted key, however deep. A list whose
                 elements are content is redacted element by element rather
                 than swapped for a count, so the log keeps its shape and
                 loses its substance.
    BY SHAPE  -- a small set of things that are secrets wherever they appear:
                 payment card numbers (Luhn-checked), national ID numbers,
                 API keys, private key blocks. This is the layer that catches
                 content sitting under a key nobody classified -- a spoken
                 card number arriving inside `targets` because a resolver put
                 a preview of the clipboard text there.

Length is the backstop under all three: a string longer than MAX_STRING is a
payload whatever it is called.

Second, PROVENANCE. Anything derived from a Jev answer carries `synthetic`.
An offline replay or an eval sweep writes the same event kinds as a live
session, and six months later nobody will remember which log was which. So the
flag is hoisted to the top level of every record, found wherever it is nested,
and written even when false.

The sink is a plain Callable[[AuditEvent], None], i.e. contracts.AuditSink, so
tests can pass NullAudit or MemoryAudit and nothing else changes.
"""

from __future__ import annotations

import enum
import json
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any

from daa.contracts import AuditEvent, Disposition, ResolvedAction, RiskAssessment, UndoAction
from daa.safety.store import harden_dir, harden_file, open_append, secure_dir

__all__ = [
    "DEFAULT_AUDIT_PATH",
    "EVENT_KINDS",
    "MAX_STRING",
    "REDACTED_KEYS",
    "JsonlAudit",
    "MemoryAudit",
    "NullAudit",
    "confirmation_event",
    "disposition_event",
    "execution_event",
    "judgment_event",
    "record",
    "redact",
    "undo_event",
]

DEFAULT_AUDIT_PATH = Path.home() / ".daa" / "audit.jsonl"

# Keys whose VALUE is content rather than a reference to content. Matched
# exactly on the lowercased key, because substring matching turns "context"
# into a redaction and hides the very fields we need when reading the log back.
REDACTED_KEYS = frozenset(
    {
        "clipboard",
        "clipboard_text",
        "content",
        "contents",
        "file_content",
        "file_contents",
        "text",
        "body",
        "message",
        "snippet",
        "preview",
        "excerpt",
        "transcript",
        "utterance",
        "stdout",
        "stderr",
        "image",
        "screenshot",
        "bytes",
        "blob",
        "password",
        "secret",
        "token",
        "api_key",
        # --- added after a spoken credit card number was found in audit.jsonl
        # under every one of these. `before`/`after` are the raw transcript on
        # either side of a cloud re-score; `reply` is the user's answer to a
        # confirmation, which is whatever they happened to say next; `summary`
        # and `error` are written BY tools and routinely quote their input
        # ("I could not write 4111... to the clipboard"). `input_text`,
        # `script` and `purpose` are arguments to run_shortcut and
        # run_applescript, i.e. arbitrary user text and arbitrary code.
        "before",
        "after",
        "reply",
        "summary",
        "error",
        "input_text",
        "script",
        "purpose",
    }
)

# Long strings are how contents leak through keys we did not think of. A reason
# or a summary is a sentence; anything much longer is a payload.
MAX_STRING = 240

# The event kinds the loop is expected to write. Not enforced -- an unknown
# kind is still logged -- but named here so `daa audit` and the tests have one
# list to read, and so adding a stage to the loop means adding it here first.
EVENT_KINDS = ("judgment", "disposition", "confirmation", "execution", "undo")

# --- shape-based redaction --------------------------------------------------
# Things that are secrets wherever they turn up, including under a key we never
# classified. Deliberately a SHORT list of high-signal patterns: a broad
# "anything with digits" rule would eat the filenames and paths that are the
# whole point of keeping the log.

# 13-19 digits, optionally in groups separated by a space or a dash. Confirmed
# by Luhn before anything is redacted, which is what keeps a 14-digit
# screenshot timestamp out of the way most of the time. It does not keep it out
# ALL of the time -- roughly one long digit run in ten passes Luhn by accident
# -- and losing a timestamp from a filename is the right side of that trade.
_CARD_RE = re.compile(r"(?<![0-9])(?:[0-9][ -]?){12,18}[0-9](?![0-9])")
# US SSN and the same shape used by several national ID schemes.
_ID_RE = re.compile(r"(?<![0-9])[0-9]{3}-[0-9]{2}-[0-9]{4}(?![0-9])")
_SECRET_RES = (
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{12,}"),          # OpenAI-style
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),               # GitHub
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                       # AWS access key
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),             # Slack
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),         # PEM
)


class JsonlAudit:
    """Append-only JSONL sink. Satisfies contracts.AuditSink (it is callable)."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_AUDIT_PATH
        # Write failures are counted, not raised. The audit log is an observer;
        # a full disk must not be able to abort a mutation half-way, or worse,
        # prevent the undo journal from being written.
        self.errors = 0
        self.last_error: str | None = None

    def __call__(self, event: AuditEvent) -> None:
        line = json.dumps(record(event), ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            # 0700 dir, 0600 file, applied on creation AND to whatever is
            # already there. This file is a transcript of someone's day; it has
            # no business being readable by every process on the machine.
            secure_dir(self.path.parent)
            with open_append(self.path) as fh:
                fh.write(line)
                fh.flush()
        except OSError as exc:  # pragma: no cover - exercised via a read-only dir
            self.errors += 1
            self.last_error = str(exc)

    def read_all(self) -> list[dict[str, Any]]:
        """Parsed records, oldest first. For tests and for `daa audit`."""
        if not self.path.exists():
            return []
        # Reading is also the moment to tighten a log written by an older
        # build: a file created at 0644 must be fixed, not merely deprecated.
        harden_dir(self.path.parent)
        harden_file(self.path)
        out: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out


class NullAudit:
    """Drops everything. The default in tests that do not care about logging."""

    def __call__(self, event: AuditEvent) -> None:
        return None


class MemoryAudit:
    """Keeps redacted records in memory so a test can assert on them.

    Deliberately stores the same redacted dict JsonlAudit would write, not the
    raw event: a test that passes here must also be true on disk.
    """

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def __call__(self, event: AuditEvent) -> None:
        self.records.append(record(event))

    def kinds(self) -> list[str]:
        return [r["kind"] for r in self.records]


def record(event: AuditEvent) -> dict[str, Any]:
    """The exact dict written to disk: redacted, with `synthetic` hoisted."""
    payload = redact(_plain(event.payload))
    return {
        "at": event.at,
        "id": event.id,
        "kind": event.kind,
        # Hoisted so a grep for '"synthetic":true' over a year of logs is enough
        # to separate replayed judgments from ones that touched a real machine.
        "synthetic": _synthetic(payload),
        "payload": payload,
    }


def redact(value: Any) -> Any:
    """Strip content out of a payload, keeping every reference to it.

    Once a key is recognised as content-bearing, EVERYTHING beneath it is
    content: `{"body": {"parts": ["...", "..."]}}` must not survive just
    because the secret is two containers down. The container itself is kept so
    the record still says what shape the thing was.
    """
    return _redact(value, content=False)


def _redact(value: Any, *, content: bool) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _redact(item, content=content or str(key).lower() in REDACTED_KEYS)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(v, content=content) for v in value]
    if content:
        return _summarize(value)
    if isinstance(value, str):
        return scrub(value)
    return value


def scrub(text: str) -> str:
    """A string from a key we do NOT consider content-bearing, made safe(r).

    Length first -- anything past MAX_STRING is a payload however it is
    labelled -- then the handful of shapes that are secrets wherever they
    appear. This is the layer that catches a card number spoken aloud and
    carried into `targets` by a resolver that previews its own argument.
    """
    if len(text) > MAX_STRING:
        return f"<elided {len(text)} chars>"
    out = _CARD_RE.sub(_card_sub, text)
    out = _ID_RE.sub("<redacted id number>", out)
    for pattern in _SECRET_RES:
        out = pattern.sub("<redacted secret>", out)
    return out


def _card_sub(match: re.Match[str]) -> str:
    digits = [c for c in match.group(0) if c.isdigit()]
    if not 13 <= len(digits) <= 19 or not _luhn(digits):
        return match.group(0)
    return "<redacted card number>"


def _luhn(digits: Sequence[str]) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _summarize(value: Any) -> str:
    """Shape without substance: enough to debug, useless to a snoop."""
    if value is None:
        return "<redacted none>"
    if isinstance(value, str):
        return f"<redacted {len(value)} chars>"
    if isinstance(value, (list, tuple, set, frozenset)):
        return f"<redacted {len(value)} items>"
    if isinstance(value, Mapping):
        return f"<redacted {len(value)} keys>"
    return f"<redacted {type(value).__name__}>"


def _synthetic(value: Any) -> bool:
    """True if ANY judgment anywhere in this payload came from a fake provider.

    Searched recursively rather than read from a known key, because the flag
    rides along inside RiskAssessment and Answers, and the caller assembling an
    event should not have to remember to copy it up.
    """
    if isinstance(value, Mapping):
        if value.get("synthetic") is True:
            return True
        return any(_synthetic(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_synthetic(v) for v in value)
    return False


def _plain(value: Any) -> Any:
    """Dataclasses/enums/paths -> json-safe primitives, never raising.

    Enums become their NAME: an audit log read a year later should say
    "CONFIRM_VISUAL", not "3", because the numbers can be renumbered and the
    log cannot.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        # bool/int first: RiskTier is an IntEnum, so check enum before this.
        return value.name if isinstance(value, enum.Enum) else value
    if isinstance(value, enum.Enum):
        return value.name
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_plain(v) for v in value]
    return str(value)


# --- event builders ---------------------------------------------------------
# The loop owns WHEN to log; these own WHAT a record looks like, so every call
# site spells the same event the same way and nobody hand-rolls a payload that
# happens to include a file's contents.


def judgment_event(
    action: ResolvedAction,
    assessment: RiskAssessment | None,
    *,
    latency_ms: float | None = None,
) -> AuditEvent:
    """What Jev said about this invocation, in full.

    Every field of the assessment is hoisted to the top of the payload as well
    as being nested, because the questions asked six months later are "how
    often did we act on a `guessing` target?" and "which of these came off a
    fake provider?", and neither should need a JSON path to answer. An
    assessment that is logged only as the tier it produced cannot answer
    either: by then the numbers that authorized the action are gone.
    """
    return AuditEvent(
        kind="judgment",
        payload={
            "tool": action.tool,
            "verb": action.verb,
            "targets": list(action.targets),
            "target_count": len(action.targets),
            "explicit": action.explicit,
            "consequences": dict(action.consequences),
            "assessment": assessment,
            "blast_radius": getattr(assessment, "blast_radius", None),
            "unrecoverable": getattr(assessment, "unrecoverable", None),
            "explicitly_requested": getattr(assessment, "explicitly_requested", None),
            "target_confidence": getattr(assessment, "target_confidence", None),
            "confidence": getattr(assessment, "confidence", None),
            "latency_ms": latency_ms,
            "synthetic": bool(assessment.synthetic) if assessment else False,
        },
    )


def disposition_event(action: ResolvedAction, disposition: Disposition) -> AuditEvent:
    return AuditEvent(
        kind="disposition",
        payload={
            "tool": action.tool,
            "verb": action.verb,
            "targets": list(action.targets),
            "target_count": len(action.targets),
            "explicit": action.explicit,
            "consequences": dict(action.consequences),
            "tier": disposition.tier,
            "reason": disposition.reason,
            # The assessment that authorized the tier, kept alongside it: the
            # tier alone says what we did, never what we believed.
            "assessment": disposition.assessment,
        },
    )


def confirmation_event(
    action: ResolvedAction,
    *,
    tier: Any,
    granted: bool,
    via: str,
    confidence: float | None = None,
    synthetic: bool = False,
) -> AuditEvent:
    return AuditEvent(
        kind="confirmation",
        payload={
            "tool": action.tool,
            "targets": list(action.targets),
            "tier": tier,
            "granted": granted,
            "via": via,          # "voice" | "visual" | "implicit"
            "confidence": confidence,
            "synthetic": synthetic,
        },
    )


def execution_event(
    action: ResolvedAction,
    *,
    ok: bool,
    summary: str = "",
    error: str | None = None,
    dry_run: bool = False,
    undo: UndoAction | None = None,
    tier: Any = None,
    undo_id: str | None = None,
) -> AuditEvent:
    """What actually happened. `summary` and `error` are redacted on the way
    out -- both are written by tools, and tools quote their arguments."""
    return AuditEvent(
        kind="execution",
        payload={
            "tool": action.tool,
            "verb": action.verb,
            "targets": list(action.targets),
            "target_count": len(action.targets),
            "ok": ok,
            "summary": summary,
            "error": error,
            "dry_run": dry_run,
            # The tier that authorized this run, so an execution line can be
            # read without hunting for the disposition it came from.
            "tier": tier,
            "undo_tool": undo.tool if undo else None,
            "undo_id": undo_id,
            "undoable": undo is not None,
        },
    )


def undo_event(
    *,
    tool: str,
    ok: bool,
    stale: str | None = None,
    entry_id: str | None = None,
    produced_by: str | None = None,
    trusted: bool | None = None,
    description: str = "",
    committed: bool | None = None,
    dry_run: bool = False,
    error: str | None = None,
    at: float | None = None,
) -> AuditEvent:
    """One attempt to reverse a recorded mutation.

    The journal is untrusted input, so the record has to say more than "we ran
    move_files". `produced_by` is the tool the journal CLAIMS made the change,
    `trusted` is whether that claim survived validation against the registry,
    and `committed` is whether the entry was actually consumed -- which is
    false for a dry run or a refused undo, and is the difference between an
    undo that is gone and one that can still be retried.
    """
    payload = {
        "tool": tool,
        "ok": ok,
        "stale": stale,
        "entry_id": entry_id,
        "produced_by": produced_by,
        "trusted": trusted,
        "description": description,
        "committed": committed,
        "dry_run": dry_run,
        "error": error,
    }
    return AuditEvent(kind="undo", payload=payload, at=at if at is not None else time.time())
