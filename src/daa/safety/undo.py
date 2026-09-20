"""The undo journal: every mutation writes its inverse down before we boast.

Two properties make this file worth having:

1. It is written BEFORE the user is told the action succeeded. If the process
   dies between the mutation and the sentence "done", the undo still exists.
   A journal held in memory would lose exactly the cases it was built for.
2. A recorded inverse is re-checked against the world before it is run. An undo
   is a statement about a world that has since moved on; replaying
   "move it back to Downloads" after the user has already moved the file
   somewhere else is a second mutation wearing an undo's clothes. We detect
   that and report it rather than executing blindly.

Storage is append-only JSONL, and consuming an entry appends a tombstone rather
than rewriting the file: a crash mid-write can cost the last line, never the
history. Malformed lines are skipped on load for the same reason.

THE JOURNAL IS UNTRUSTED INPUT
------------------------------
This file lives on disk and `daa undo` reads a row out of it and executes the
tool it names with the arguments it carries. Anything running as the user can
append a row. So the journal is untrusted in exactly the way LLM output is
untrusted, and it gets the same treatment:

    * The file is 0600 inside a 0700 directory (safety/store.py), created that
      way and tightened if an older build left it at 0644.
    * Every row carries `produced_by`: the tool that performed the mutation
      this row claims to reverse. THE CALLER MUST CHECK IT before executing --
      `entry.tool in registry.get(entry.produced_by).spec.inverses` -- and this
      module cannot do that check for you, because safety/ may not import
      tools/. See UndoEntry.produced_by and UndoEntry.trusted.
    * A row with no parseable `produced_by` is hostile until proven otherwise:
      it loads with `trusted is False` rather than with a permissive default.

WHAT IS AND IS NOT REDACTED HERE
--------------------------------
`args` are stored verbatim, and they have to be: `set_clipboard`'s inverse is
literally the text that was on the clipboard before we overwrote it -- the
password copied ten seconds ago from that tool's own docstring. Redacting it
would leave a journal that describes an undo it cannot perform, which is the
worst of both worlds: the secret is gone AND the user's clipboard is not coming
back. So the tension is resolved by CONTAINMENT rather than by omission:

    * the value is stored, in the 0600 journal, and nowhere else;
    * `context` and `description` -- neither of which is needed to execute the
      undo -- are redacted on write with the audit log's own redactor;
    * every content-bearing argument key is listed in the row's `sensitive`
      field, and `UndoEntry.audit_payload()` is the ONLY shape that may be
      handed to the audit sink. The audit log never sees the args.

An entry is therefore as sensitive as the thing it can restore, and its
lifetime is the lifetime of the journal file. That is a real cost, accepted
knowingly: it is the price of "undo" meaning anything at all.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from daa.contracts import UndoAction
from daa.safety.audit import REDACTED_KEYS, redact
from daa.safety.store import harden_dir, harden_file, open_append, secure_dir

__all__ = ["DEFAULT_UNDO_PATH", "MAX_FINGERPRINTS", "UndoEntry", "UndoJournal"]

DEFAULT_UNDO_PATH = Path.home() / ".daa" / "undo.jsonl"

_KIND_RECORD = "undo"
_KIND_CONSUMED = "consumed"


@dataclass(frozen=True, slots=True)
class UndoEntry:
    """One recorded inverse, plus what the world looked like when we recorded it."""

    id: str
    at: float
    action: UndoAction
    context: Mapping[str, Any] = field(default_factory=dict)
    # path -> {"exists": bool, "size": int|None, "mtime_ns": int|None} at record
    # time. This is the whole staleness mechanism: cheap, local, no hashing of
    # file contents (we never read contents here -- see audit.py for why).
    fingerprints: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    # Filled in by peek()/pop() by re-statting `fingerprints`. None means "as we
    # left it". A caller that ignores this field and executes anyway is the bug
    # this field exists to make obvious.
    stale: str | None = None
    # The tool that PERFORMED the mutation this entry reverses -- not the tool
    # that undoes it. The two together are the only thing standing between
    # `daa undo` and a row somebody appended by hand:
    #
    #     spec = registry.get(entry.produced_by).spec
    #     if entry.tool not in spec.inverses: REFUSE
    #
    # THE CALLER MUST RUN THAT CHECK. safety/ may not import tools/, so this
    # module can hand over the claim and nothing more. None means the row did
    # not carry one, or carried something that was not a tool name.
    produced_by: str | None = None
    # Argument keys (dotted, e.g. "pairs.0.text") whose value is content rather
    # than a reference to content. Stored so a UI, the CLI and audit_payload()
    # can all agree on what must never be printed.
    sensitive: tuple[str, ...] = ()

    @property
    def trusted(self) -> bool:
        """False for any row we cannot attribute to a tool.

        Deliberately not a stored flag: a hostile row would simply set it.
        It is derived from whether the row named its producer at all, which a
        forger has to guess -- and guessing wrong is caught by the inverses
        check the caller runs on `produced_by`.

        `trusted is True` means ONLY "this row is well-formed enough to be
        validated". It is not permission to execute. The caller still has to
        check `tool in registry.get(produced_by).spec.inverses`.
        """
        return bool(
            isinstance(self.produced_by, str)
            and self.produced_by.strip()
            and isinstance(self.action.tool, str)
            and self.action.tool.strip()
        )

    @property
    def tool(self) -> str:
        return self.action.tool

    @property
    def args(self) -> Mapping[str, Any]:
        return self.action.args

    @property
    def description(self) -> str:
        return self.action.description

    def to_json(self) -> dict[str, Any]:
        """The row as written. `args` verbatim (the undo has to work), context
        and description redacted (neither is needed to execute)."""
        return {
            "kind": _KIND_RECORD,
            "id": self.id,
            "at": self.at,
            "produced_by": self.produced_by,
            "description": redact(_jsonable(self.action.description)),
            "tool": self.action.tool,
            "args": _jsonable(self.action.args),
            "sensitive": list(self.sensitive),
            "context": redact(_jsonable(self.context)),
            "fingerprints": _jsonable(self.fingerprints),
        }

    def audit_payload(self) -> dict[str, Any]:
        """The ONLY shape of this entry that may reach the audit log.

        `args` are replaced by their key names. The journal is 0600 and holds
        the previous clipboard; the audit log is a different file with a
        different lifetime and must never become a second copy of it.
        """
        return {
            "entry_id": self.id,
            "at": self.at,
            "tool": self.action.tool,
            "produced_by": self.produced_by,
            "trusted": self.trusted,
            "description": self.action.description,
            "arg_keys": sorted(str(k) for k in self.action.args),
            "sensitive": list(self.sensitive),
            "fingerprint_count": len(self.fingerprints),
            "stale": self.stale,
        }

    @classmethod
    def from_json(cls, row: Mapping[str, Any]) -> UndoEntry:
        """Parse one row of an untrusted file. Anything malformed is dropped,
        never defaulted into something permissive."""
        return cls(
            id=str(row["id"]),
            at=_float(row.get("at")),
            action=UndoAction(
                description=str(row.get("description", "")),
                tool=str(row["tool"]),
                args=dict(row.get("args") or {}),
            ),
            context=dict(row.get("context") or {}),
            fingerprints=dict(row.get("fingerprints") or {}),
            # A missing, empty, or non-string producer is not "probably fine":
            # it is a row we cannot attribute, and `trusted` reports False for
            # it. Coercing a dict to str() here would manufacture a tool name
            # out of a hostile row, so we do not.
            produced_by=_tool_name(row.get("produced_by")),
            sensitive=tuple(
                str(k) for k in (row.get("sensitive") or []) if isinstance(k, (str, int))
            ),
        )


class UndoJournal:
    """Append-only stack of inverses, persisted so undo survives a restart."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_UNDO_PATH
        self._entries: list[UndoEntry] = []
        self._consumed: set[str] = set()
        self._loaded_sig: tuple[int, int] | None = None
        self.reload()

    # --- writing ----------------------------------------------------------

    def record(
        self,
        action: UndoAction,
        context: Mapping[str, Any] | None = None,
        *,
        produced_by: str,
    ) -> UndoEntry:
        """Persist the inverse of a mutation that HAS ALREADY HAPPENED.

        Call this before telling the user it worked. `context` is free-form and
        is handed back at undo time; any paths in it (or in the undo args) are
        fingerprinted now so we can tell later whether the world moved.

        `produced_by` is REQUIRED and is the name of the tool that made the
        change -- `move_to_trash`, not the `move_files` that reverses it. It is
        what lets the caller check, at undo time, that the row names a
        legitimate inverse of a real mutation rather than an arbitrary tool
        call somebody appended to the file.

        An empty or non-string producer is recorded rather than refused: this
        runs AFTER the mutation, and dropping the row would trade a badly
        attributed undo for no undo at all. It lands with `trusted is False`,
        so the caller declines to execute it and the user is told why.
        """
        ctx = dict(context or {})
        entry = UndoEntry(
            id=uuid.uuid4().hex[:12],
            at=time.time(),
            action=action,
            context=ctx,
            fingerprints=_fingerprint_all(_candidate_paths(action.args, ctx)),
            produced_by=_tool_name(produced_by),
            sensitive=_sensitive_keys(action.args),
        )
        self._append(entry.to_json())
        self._entries.append(entry)
        return entry

    # --- reading ----------------------------------------------------------

    def peek(self) -> UndoEntry | None:
        """Most recent un-consumed entry, freshness-checked, LEFT IN PLACE.

        This is the call the loop wants. The sequence is:

            entry = journal.peek()
            validate: entry.trusted and entry.tool in inverses(entry.produced_by)
            check:    entry.stale
            run the tool
            journal.commit(entry)      # only if it actually ran

        Nothing about reading an entry should destroy it, because most of the
        reasons an undo does not run -- dry run, a failed validation, a stale
        world, the user saying no -- are reasons to keep it.
        """
        self._refresh()
        for entry in reversed(self._entries):
            if entry.id not in self._consumed:
                return self._checked(entry)
        return None

    def commit(self, entry: UndoEntry) -> bool:
        """Mark `entry` consumed. Call this ONLY once the undo actually ran.

        Returns False if it was already consumed, so a double-commit is a
        no-op rather than a second tombstone. Consuming after the fact is what
        stops a failed undo being retried forever by the next "undo that" --
        but it is the CALLER's decision, because only the caller knows whether
        anything happened.
        """
        self._refresh()
        if entry.id in self._consumed:
            return False
        self._consumed.add(entry.id)
        self._append({"kind": _KIND_CONSUMED, "id": entry.id, "at": time.time()})
        return True

    def pop(self) -> UndoEntry | None:
        """peek() and commit() in one call, for a caller that genuinely wants
        both -- `daa undo --discard`, or a test.

        THIS IS THE WRONG CALL FOR THE LOOP. It consumes the entry before
        anything has been executed, so under `dry_run` (the shipped default)
        it throws the real undo record away while undoing precisely nothing.
        Use peek -> run -> commit.
        """
        entry = self.peek()
        if entry is None:
            return None
        self.commit(entry)
        return entry

    def history(self, n: int = 10) -> list[UndoEntry]:
        """Up to `n` live entries, newest first. Consumed entries are excluded:
        they are history for the audit log, not for "undo that"."""
        if n <= 0:
            return []
        self._refresh()
        out: list[UndoEntry] = []
        for entry in reversed(self._entries):
            if entry.id in self._consumed:
                continue
            out.append(entry)
            if len(out) >= n:
                break
        return out

    def __len__(self) -> int:
        self._refresh()
        return sum(1 for e in self._entries if e.id not in self._consumed)

    # --- staleness --------------------------------------------------------

    def staleness(self, entry: UndoEntry) -> str | None:
        """A spoken sentence if the world moved under this undo, else None."""
        moved: list[str] = []
        gone: list[str] = []
        appeared: list[str] = []
        for raw, before in (entry.fingerprints or {}).items():
            now = _fingerprint(Path(raw))
            if before.get("exists") and not now["exists"]:
                gone.append(raw)
            elif not before.get("exists") and now["exists"]:
                appeared.append(raw)
            elif now["exists"] and (
                now["size"] != before.get("size") or now["mtime_ns"] != before.get("mtime_ns")
            ):
                moved.append(raw)
        # Spoken, count-only: the reason is read aloud, and a path read aloud is
        # both unintelligible and a privacy leak in a room with other people.
        if gone:
            return _plural(len(gone), "it's", "they're") + " not where I left " + (
                "it" if len(gone) == 1 else "them"
            ) + " any more."
        if appeared:
            return "Something is back at that spot already, so undoing could overwrite it."
        if moved:
            return _plural(len(moved), "it's", "they've") + " changed since I did that."
        return None

    def _checked(self, entry: UndoEntry) -> UndoEntry:
        return replace(entry, stale=self.staleness(entry))

    # --- persistence ------------------------------------------------------

    def reload(self) -> None:
        """Re-read the journal from disk. Called on construction (so undo
        survives a restart) and whenever the file changes underneath us (the
        CLI and the voice loop are separate processes)."""
        self._entries = []
        self._consumed = set()
        self._loaded_sig = _signature(self.path)
        if not self.path.exists():
            return
        # A journal written by a build that predates safety/store.py is sitting
        # there at 0644 inside a 0755 directory, world-readable and
        # user-writable-by-anything. Loading it is the first moment we can see
        # it, so it is the moment we fix it -- both the file and the directory,
        # since a 0755 ~/.daa lets anything list what is in there.
        harden_dir(self.path.parent)
        harden_file(self.path)
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    # A half-written last line from a crash. Losing one entry is
                    # survivable; refusing to load the whole journal is not.
                    continue
                kind = row.get("kind")
                if kind == _KIND_CONSUMED:
                    self._consumed.add(str(row.get("id")))
                elif kind == _KIND_RECORD:
                    try:
                        self._entries.append(UndoEntry.from_json(row))
                    except (KeyError, TypeError, ValueError):
                        continue

    def _refresh(self) -> None:
        if _signature(self.path) != self._loaded_sig:
            self.reload()

    def _append(self, row: Mapping[str, Any]) -> None:
        # 0700 dir, 0600 file, from the first byte: there must be no window in
        # which this file exists at the umask default. Anything that can write
        # here can make `daa undo` run a tool of its choosing.
        secure_dir(self.path.parent)
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        with open_append(self.path) as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())  # the crash we are insuring against is now
        self._loaded_sig = _signature(self.path)


# --- helpers ----------------------------------------------------------------


def _signature(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def _fingerprint(path: Path) -> dict[str, Any]:
    try:
        st = path.stat()
    except OSError:
        return {"exists": False, "size": None, "mtime_ns": None}
    return {"exists": True, "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _fingerprint_all(paths: Iterable[str]) -> dict[str, dict[str, Any]]:
    return {p: _fingerprint(Path(p)) for p in paths}


_PATH_KEYS = frozenset(
    {"path", "paths", "src", "dst", "source", "destination", "targets", "pairs", "files"}
)

# Fingerprinting stats every path it finds. A pathological args blob must not
# turn one undo record into ten thousand stat() calls.
MAX_FINGERPRINTS = 256
_MAX_DEPTH = 12


def _candidate_paths(*sources: Mapping[str, Any]) -> list[str]:
    """Paths worth fingerprinting, from explicit keys AND from the undo args.

    Walks nested lists, tuples and dicts to arbitrary depth. That is not
    generality for its own sake: every undo this product actually emits is
    shaped `{"pairs": [[src, dst], ...]}`, a list of lists, and the original
    one-level scan found exactly zero strings in it. The result was that no
    fingerprints were ever recorded, staleness() always returned None, and the
    entire "check the world before replaying an inverse" mechanism -- the
    second of the two reasons this file exists -- was dead code that passed its
    tests because the tests used the one shape the product never produces.

    Scanning the args too means a tool author who forgets to pass
    context={"paths": ...} still gets staleness detection. Forgetting is the
    normal case; the check should not depend on remembering.
    """
    found: list[str] = []
    for src in sources:
        _collect(src or {}, False, found)
    return found


def _collect(value: Any, explicit: bool, out: list[str], depth: int = 0) -> None:
    if depth > _MAX_DEPTH or len(out) >= MAX_FINGERPRINTS:
        return
    if isinstance(value, str):
        if (explicit or _looks_like_path(value)) and value not in out:
            out.append(value)
        return
    if explicit:
        # Under a key that names paths, everything below it is a path --
        # including the strings inside a list of [src, dst] pairs.
        for item in _strings(value):
            if item not in out and len(out) < MAX_FINGERPRINTS:
                out.append(item)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _collect(item, explicit or str(key).lower() in _PATH_KEYS, out, depth + 1)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _collect(item, explicit, out, depth + 1)


def _strings(value: Any) -> list[str]:
    """Every string anywhere in `value`, however deeply nested."""
    out: list[str] = []
    _strings_into(value, out, 0)
    return out


def _strings_into(value: Any, out: list[str], depth: int) -> None:
    if depth > _MAX_DEPTH:
        return
    if isinstance(value, str):
        out.append(value)
        return
    if isinstance(value, Mapping):
        for item in value.values():
            _strings_into(item, out, depth + 1)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _strings_into(item, out, depth + 1)


def _looks_like_path(value: str) -> bool:
    """Path-shaped wherever it appears, not merely under a key we anticipated.

    Absolute and home-relative only. A bare "notes.txt" would stat against the
    process's cwd, which at undo time is a different directory than it was at
    record time -- a fingerprint that means nothing is worse than none.
    """
    return value.startswith(("/", "~/", "./", "../"))


def _tool_name(value: Any) -> str | None:
    """A tool name from an untrusted row, or None.

    Never str()s a non-string: turning {"a": 1} into "{'a': 1}" would hand the
    caller a "tool name" that came out of nowhere. Not-a-string is not-a-name.
    """
    if not isinstance(value, str):
        return None
    name = value.strip()
    return name or None


def _sensitive_keys(args: Mapping[str, Any], prefix: str = "", depth: int = 0) -> tuple[str, ...]:
    """Dotted paths of the argument values that are content, not references.

    Uses the audit log's own key list, so "what counts as content" has exactly
    one definition in this package.
    """
    if depth > _MAX_DEPTH:
        return ()
    found: list[str] = []
    for key, value in (args or {}).items():
        k = str(key)
        dotted = f"{prefix}{k}"
        if k.lower() in REDACTED_KEYS:
            found.append(dotted)
        elif isinstance(value, Mapping):
            found.extend(_sensitive_keys(value, f"{dotted}.", depth + 1))
    return tuple(found)


def _float(value: Any) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _plural(n: int, one: str, many: str) -> str:
    return one.capitalize() if n == 1 else many.capitalize()


def _jsonable(value: Any) -> Any:
    """Coerce to something json.dumps accepts, never raising.

    record() runs after the mutation already happened, so throwing here would
    trade an un-loggable argument for a permanently un-undoable action. A
    stringified argument is worse than a perfect one and far better than none.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)) or (
        isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    ):
        return [_jsonable(v) for v in value]
    return str(value)
