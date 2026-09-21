"""Decides what is allowed to happen, and makes what happens reversible.

Three pieces, in the order the loop uses them:

    policy.decide(...)  -> Disposition   what must happen before we act
    grant.grant_satisfies -> bool, why   whether the user ALREADY answered it
    undo.UndoJournal    -> UndoAction    how to take it back afterwards
    audit.JsonlAudit    -> AuditSink     what we wrote down about all of it

plus store.py, which owns the one thing both files on disk have in common:
they are 0600 inside a 0700 directory, because the undo journal is executable
input and the audit log is a transcript of someone's day.

This package imports contracts.py and the standard library, and nothing else
from daa. Not jev/, not tools/, not voice/. The gate has to be reasonable about
actions it has never seen, from a judgment layer that may be down, so it works
on the contract types alone -- which is also why every rule in here is testable
without a microphone, an API key, or a filesystem.
"""

from __future__ import annotations

from daa.safety.audit import (
    DEFAULT_AUDIT_PATH,
    EVENT_KINDS,
    JsonlAudit,
    MemoryAudit,
    NullAudit,
    checkpoint_event,
    confirmation_event,
    disposition_event,
    execution_event,
    grant_event,
    grant_revoked_event,
    job_event,
    job_step_event,
    judgment_event,
    notice_event,
    redact,
    rollback_event,
    undo_event,
)
from daa.safety.grant import (
    GrantBook,
    GrantState,
    WarrantBook,
    action_digest,
    grant_satisfies,
    issue_grant,
    readback,
    visual_card,
)
from daa.safety.policy import MAX_POLICY_TIER, decide, derive_tier
from daa.safety.store import DIR_MODE, FILE_MODE
from daa.safety.undo import DEFAULT_UNDO_PATH, UndoEntry, UndoJournal

__all__ = [
    "DEFAULT_AUDIT_PATH",
    "DEFAULT_UNDO_PATH",
    "DIR_MODE",
    "EVENT_KINDS",
    "FILE_MODE",
    "MAX_POLICY_TIER",
    "GrantBook",
    "GrantState",
    "JsonlAudit",
    "MemoryAudit",
    "NullAudit",
    "UndoEntry",
    "UndoJournal",
    "WarrantBook",
    "action_digest",
    "checkpoint_event",
    "confirmation_event",
    "decide",
    "derive_tier",
    "disposition_event",
    "execution_event",
    "grant_event",
    "grant_revoked_event",
    "grant_satisfies",
    "issue_grant",
    "job_event",
    "job_step_event",
    "judgment_event",
    "notice_event",
    "readback",
    "redact",
    "rollback_event",
    "undo_event",
    "visual_card",
]
