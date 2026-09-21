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
    "VERBATIM_KEYS",
    "VERBATIM_MAX",
    "JsonlAudit",
    "MemoryAudit",
    "NullAudit",
    "checkpoint_event",
    "confirmation_event",
    "disposition_event",
    "execution_event",
    "grant_event",
    "grant_revoked_event",
    "job_event",
    "job_step_event",
    "judgment_event",
    "notice_event",
    "record",
    "redact",
    "rollback_event",
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
EVENT_KINDS = (
    "judgment",
    "disposition",
    "confirmation",
    "execution",
    "undo",
    # Added with scoped grants and background jobs. A grant is answered once
    # and spent many times, so the rows that reconstruct "who authorized what"
    # are spread across a whole run rather than sitting in one record.
    "grant",
    "grant_revoked",
    "job",
    "job_step",
    "checkpoint",
    "rollback",
    "notice",
)

# THE ONE EXEMPTION FROM LENGTH ELISION, and it is deliberate.
#
# `plan_summary` is the exact sentence the user heard before they said yes. A
# scope object answers "what was permitted"; only this answers "what did they
# consent to", which is the question actually asked six months later. It is
# also the only string in this log that daa WROTE and SPOKE rather than
# received: it is not tool output, not file contents, and not raw speech, so
# the usual reason for eliding a long string does not apply to it.
#
# What still applies: every shape-based rule. A card number or an API key in a
# readback is redacted exactly as it would be anywhere else, and the value is
# still capped -- at VERBATIM_MAX rather than MAX_STRING -- so a pathological
# grant cannot turn this into a way to dump a payload into the log.
#
# `goal` is NOT in here. A goal is the user's own words, and the user's own
# words are content. It goes through the ordinary scrub, which means a long one
# is elided and the readback beside it is what carries the meaning.
VERBATIM_KEYS = frozenset({"plan_summary"})
VERBATIM_MAX = 1000

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


def _redact(value: Any, *, content: bool, verbatim: bool = False) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _redact(
                item,
                content=content or str(key).lower() in REDACTED_KEYS,
                verbatim=verbatim or str(key).lower() in VERBATIM_KEYS,
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(v, content=content, verbatim=verbatim) for v in value]
    if content:
        # A key that is content-bearing wins over one that is verbatim. There
        # is no arrangement of keys that turns a redaction off.
        return _summarize(value)
    if isinstance(value, str):
        return scrub(value, limit=VERBATIM_MAX if verbatim else MAX_STRING)
    return value


def scrub(text: str, *, limit: int | None = None) -> str:
    """A string from a key we do NOT consider content-bearing, made safe(r).

    Length first -- anything past MAX_STRING is a payload however it is
    labelled -- then the handful of shapes that are secrets wherever they
    appear. This is the layer that catches a card number spoken aloud and
    carried into `targets` by a resolver that previews its own argument.
    """
    cap = MAX_STRING if limit is None else limit
    if len(text) > cap:
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


def _linkage(
    grant_id: str | None,
    warrant_id: str | None,
    job_id: str | None,
    step_index: int | None,
) -> dict[str, Any]:
    """The four fields that tie a row back to the bargain it ran under.

    Hoisted to the TOP LEVEL of every payload rather than nested, for the same
    reason `synthetic` is: the question asked six months later is "show me
    everything that happened under this grant", and it should not need a JSON
    path. All four are None on an ordinary foreground action, which is the
    honest answer -- that action ran under a fresh confirmation, not a grant.
    """
    return {
        "grant_id": grant_id,
        "warrant_id": warrant_id,
        "job_id": job_id,
        "step_index": step_index,
    }


def judgment_event(
    action: ResolvedAction,
    assessment: RiskAssessment | None,
    *,
    latency_ms: float | None = None,
    grant_id: str | None = None,
    warrant_id: str | None = None,
    job_id: str | None = None,
    step_index: int | None = None,
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
            **_linkage(grant_id, warrant_id, job_id, step_index),
        },
    )


def disposition_event(
    action: ResolvedAction,
    disposition: Disposition,
    *,
    grant_id: str | None = None,
    warrant_id: str | None = None,
    job_id: str | None = None,
    step_index: int | None = None,
) -> AuditEvent:
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
            **_linkage(grant_id, warrant_id, job_id, step_index),
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
    grant_id: str | None = None,
    warrant_id: str | None = None,
    job_id: str | None = None,
    step_index: int | None = None,
) -> AuditEvent:
    """One answer to one confirmation.

    `via` gained "grant" when scoped consent arrived. It is the difference
    between "the user said yes" and "a grant said yes on their behalf", and it
    is the single most important field in this record: a run that is all
    `via="grant"` rows under one `grant_id` is exactly the habituation failure
    the design is worried about, and this is what makes it countable.
    """
    return AuditEvent(
        kind="confirmation",
        payload={
            "tool": action.tool,
            "targets": list(action.targets),
            "tier": tier,
            "granted": granted,
            "via": via,          # "voice" | "visual" | "grant" | "implicit"
            "confidence": confidence,
            "synthetic": synthetic,
            **_linkage(grant_id, warrant_id, job_id, step_index),
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
    grant_id: str | None = None,
    warrant_id: str | None = None,
    job_id: str | None = None,
    step_index: int | None = None,
    checkpoint_id: str | None = None,
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
            "checkpoint_id": checkpoint_id,
            **_linkage(grant_id, warrant_id, job_id, step_index),
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


# --- grants, jobs, checkpoints ----------------------------------------------
# The reconstruction requirement these exist to satisfy is a single query on
# `grant_id`:
#
#     grant            id, goal, plan_summary (the EXACT spoken readback),
#                      ceiling, granted_via, scope, budget, parent_id
#       - job          job_id, grant_id, goal
#          - per step  judgment + disposition + execution, each carrying
#                      grant_id, warrant_id, job_id, step_index, tier, via
#          - confirmation rows for every mid-run re-confirm, carrying parent_id
#          - checkpoint / rollback rows
#       - grant_revoked  reason, at, steps_completed
#
# Storing the spoken readback verbatim on the grant row is the load-bearing
# part. "Who authorized what" is not answerable from a scope object; it is
# answerable from the sentence the user actually heard before they said yes.


def grant_event(grant: Any, *, granted: bool, tools: Sequence[str] = ()) -> AuditEvent:
    """A scoped go-ahead, asked for and answered.

    Written whether or not it was granted. A refused grant is as interesting as
    an accepted one -- more so, because "the assistant asked for more than it
    needed" is exactly the signal worth noticing, and it is invisible if only
    the accepted ones are logged.
    """
    scope = getattr(grant, "scope", None)
    budget = getattr(grant, "budget", None)
    return AuditEvent(
        kind="grant",
        payload={
            "grant_id": getattr(grant, "id", None),
            "parent_id": getattr(grant, "parent_id", None),
            "granted": granted,
            "goal": getattr(grant, "goal", ""),
            # The readback, verbatim. See VERBATIM_KEYS.
            "plan_summary": getattr(grant, "plan_summary", ""),
            "ceiling": getattr(grant, "ceiling", None),
            "granted_via": getattr(grant, "granted_via", None),
            "channel_cap": getattr(grant, "channel_cap", None),
            "max_satisfiable": getattr(grant, "max_satisfiable", None),
            "granted_at": getattr(grant, "granted_at", None),
            "expires_at": getattr(grant, "expires_at", None),
            "scope_tools": sorted(getattr(scope, "tools", ()) or ()),
            "scope_origins": sorted(getattr(scope, "origins", ()) or ()),
            "scope_apps": sorted(getattr(scope, "apps", ()) or ()),
            "scope_paths": list(getattr(scope, "path_prefixes", ()) or ()),
            "budget_steps": getattr(budget, "steps", None),
            "budget_seconds": getattr(budget, "seconds", None),
            "budget_spend_cents": getattr(budget, "spend_cents", None),
            "briefed_consequences": sorted(getattr(grant, "briefed_consequences", ()) or ()),
            # What was on the menu when the sentence was composed, so a reader
            # can tell whether the readback named the worst of it.
            "offered_tools": sorted(str(t) for t in tools),
        },
    )


def grant_revoked_event(
    *,
    grant_id: str,
    reason: str,
    at: float | None = None,
    steps_completed: int = 0,
    job_id: str | None = None,
    warrants_killed: int = 0,
) -> AuditEvent:
    """"Stop" landing. Always written, even when nothing was running, because
    the interesting question is how often people stop us, not how often they
    stop us successfully."""
    return AuditEvent(
        kind="grant_revoked",
        payload={
            "grant_id": grant_id,
            "job_id": job_id,
            "reason": reason,
            "steps_completed": steps_completed,
            "warrants_killed": warrants_killed,
        },
        at=at if at is not None else time.time(),
    )


def job_event(
    *,
    job_id: str,
    status: Any,
    goal: str = "",
    grant_id: str | None = None,
    steps_done: int = 0,
    phase: str = "",
    summary: str = "",
    error: str | None = None,
    at: float | None = None,
) -> AuditEvent:
    """A background job changing state. One row per transition, not per step."""
    return AuditEvent(
        kind="job",
        payload={
            "job_id": job_id,
            "grant_id": grant_id,
            "status": status,
            "goal": goal,
            "steps_done": steps_done,
            "phase": phase,
            "summary": summary,
            "error": error,
        },
        at=at if at is not None else time.time(),
    )


def job_step_event(
    *,
    job_id: str,
    step_index: int,
    tool: str,
    grant_id: str | None = None,
    warrant_id: str | None = None,
    authorized: bool = False,
    via: str = "",
    reason: str = "",
    tier: Any = None,
    at: float | None = None,
) -> AuditEvent:
    """One proposal from an agent generator, and what the loop did with it.

    Written for REFUSED steps too. A job whose steps were mostly refused is a
    job that was asking for things outside its grant, and that pattern only
    exists in the log if the refusals are in it.
    """
    return AuditEvent(
        kind="job_step",
        payload={
            "job_id": job_id,
            "grant_id": grant_id,
            "warrant_id": warrant_id,
            "step_index": step_index,
            "tool": tool,
            "authorized": authorized,
            "via": via,
            "reason": reason,
            "tier": tier,
        },
        at=at if at is not None else time.time(),
    )


def checkpoint_event(
    *,
    checkpoint_id: str,
    job_id: str | None = None,
    opened: bool = True,
    sealed: bool = False,
    sealed_by: str = "",
    entry_count: int = 0,
    at: float | None = None,
) -> AuditEvent:
    return AuditEvent(
        kind="checkpoint",
        payload={
            "checkpoint_id": checkpoint_id,
            "job_id": job_id,
            "opened": opened,
            "sealed": sealed,
            "sealed_by": sealed_by,
            "entry_count": entry_count,
        },
        at=at if at is not None else time.time(),
    )


def rollback_event(
    *,
    checkpoint_id: str,
    attempted: int,
    reversed_count: int,
    stopped_reason: str = "",
    sealed: bool = False,
    job_id: str | None = None,
    grant_id: str | None = None,
    at: float | None = None,
) -> AuditEvent:
    """Undoing a whole window.

    `attempted` and `reversed_count` are separate on purpose: a rollback that
    stops at the first stale row is the CORRECT behaviour, and a log that only
    recorded "rollback happened" could not tell that from one that reversed
    everything.
    """
    return AuditEvent(
        kind="rollback",
        payload={
            "checkpoint_id": checkpoint_id,
            "job_id": job_id,
            "grant_id": grant_id,
            "attempted": attempted,
            "reversed": reversed_count,
            "stopped_reason": stopped_reason,
            "sealed": sealed,
        },
        at=at if at is not None else time.time(),
    )


def notice_event(
    *,
    job_id: str,
    spoken: bool,
    urgency: str = "normal",
    reason: str = "",
    pending: int = 0,
    at: float | None = None,
) -> AuditEvent:
    """A deferred report, spoken or dropped.

    The TEXT is deliberately absent. A notice is written to be spoken, which
    means it quotes whatever the job was working on, which means it is content
    by the same rule that redacts `summary`.
    """
    return AuditEvent(
        kind="notice",
        payload={
            "job_id": job_id,
            "spoken": spoken,
            "urgency": urgency,
            "reason": reason,
            "pending": pending,
        },
        at=at if at is not None else time.time(),
    )
