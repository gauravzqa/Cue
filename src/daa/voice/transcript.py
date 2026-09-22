"""Rolling conversation state.

This object is passed as Jev `state` on every judgment and as message history
to DeepSeek, so its size is a direct multiplier on latency AND on cost: the
address gate alone fires on every segment of ambient speech in the room.

It is therefore bounded twice -- by turn count and by characters -- and the
bound is enforced on WRITE, not on read. An unbounded deque that is sliced at
call time still grows in memory all day and still tempts the next caller to
pass the whole thing. Truncating at the door means there is no long version to
accidentally send.

What it deliberately does NOT hold: audio, file paths, tool arguments. The
transcript is the conversation, not the audit log; safety/audit.py owns that,
and keeping them separate is why a Jev call can be logged verbatim without
leaking a directory listing into a prompt.

TOOL RESULTS LIVE HERE TOO, and they are the reason this file needed a second
look. The agent loop feeds every step's outcome back to the model so it can
react to what it read, and "what it read" is `read_page`'s page text,
`ui_describe`'s control labels, `get_clipboard`'s clipboard. Sending those to
DeepSeek is an egress nobody agreed to -- `summarise_page` is a separate
ANNOUNCE tool precisely because that send has to be announced -- so a tool
result turn is built under one rule:

    A TOOL RESULT MAY CARRY ONLY DAA'S OWN SENTENCE ABOUT THE STEP, PLUS THE
    SHAPE OF THE STRUCTURED DATA. Never the data itself.

`summary` is written BY the tool to be SPOKEN ALOUD: it is the same sentence
the user hears at ANNOUNCE, and at SILENT it is the sentence the tool chose as
safe to say. `data` is reduced by `shape_data` to counts, flags and numbers --
a string survives only under a key from a tiny allowlist of categories, and
`data["text"]` never survives at all. Both go through `safety/audit.py`'s
shape scrubber, so a card number or an `sk-` key is removed on this path as
well as on the way to the log, and the whole line is capped.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["user", "assistant", "system", "tool"]

# The prefix every tool-result turn carries into the prompt. The model sees
# these as ordinary conversation text (they are sent with role "user", because
# a real `role: "tool"` message is only legal after an assistant message that
# carries the matching tool_call_id, and this transcript does not keep those),
# so the marker has to be in the text itself.
TOOL_PREFIX = "[tool result]"

# Keys whose VALUE is a category rather than content. Everything else that is
# a string is dropped: a page title, a control label, a URL and a clipboard
# line are all content, and this is the list that decides it.
_SHAPE_STR_KEYS = frozenset(
    {"site", "app", "kind", "status", "state", "mode", "egress", "source", "verb"}
)
_SHAPE_MAX_KEYS = 6
_SHAPE_MAX_VALUE_CHARS = 48


@dataclass(frozen=True, slots=True)
class Turn:
    role: Role
    text: str
    at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {"role": self.role, "text": self.text}


class Transcript:
    """Bounded rolling window over the conversation.

    max_turns is small on purpose. A voice conversation has a short horizon --
    if the user's instruction depends on something said twelve turns ago, the
    right answer is to ask, not to pay for 4k tokens of context on every
    wake-word judgment.

    It is no longer as small as it was, and that is the agent loop's doing. One
    user turn can now be an instruction plus eight tool results plus whatever
    the model says between them, and a window of twelve evicts the INSTRUCTION
    half way through the task -- which is how an agent forgets what it was
    doing and starts again. The window is sized to the step budget
    (`Settings.agent_max_steps`, 8) with room over, and tool-result turns are
    held to `max_tool_chars` so eight of them cannot crowd out the sentence
    that started it all.
    """

    def __init__(
        self,
        *,
        max_turns: int = 24,
        max_chars: int = 3_200,
        max_turn_chars: int = 600,
        max_tool_chars: int = 240,
    ):
        self.max_turns = max_turns
        self.max_chars = max_chars
        self.max_turn_chars = max_turn_chars
        # Tool results are daa talking to itself about machinery. They get a
        # quarter of the budget a human sentence gets, because a step result
        # that needs 600 characters is a step result carrying content.
        self.max_tool_chars = max_tool_chars
        self._turns: deque[Turn] = deque(maxlen=max_turns)
        # Counts every turn ever added, including evicted ones. Lets a test
        # (and the audit log) prove the window is rolling rather than silently
        # dropping writes.
        self.total_turns = 0

    # -- writes ------------------------------------------------------------

    def add(self, role: Role, text: str) -> Turn:
        clipped = text.strip()
        limit = self.max_tool_chars if role == "tool" else self.max_turn_chars
        if len(clipped) > limit:
            # Clip the MIDDLE: the start carries the instruction and the end
            # carries the correction ("...actually, no, the other folder").
            head = limit // 2
            tail = limit - head - 1
            clipped = f"{clipped[:head]}…{clipped[-tail:]}"
        turn = Turn(role=role, text=clipped)
        self._turns.append(turn)
        self.total_turns += 1
        self._enforce_chars()
        return turn

    def add_user(self, text: str) -> Turn:
        return self.add("user", text)

    def add_assistant(self, text: str) -> Turn:
        return self.add("assistant", text)

    def add_tool_result(
        self,
        *,
        tool: str,
        status: str,
        summary: str = "",
        data: Mapping[str, Any] | None = None,
        dry_run: bool = False,
    ) -> Turn:
        """What happened when the loop tried one step. The model reads this.

        `status` is one of "ok", "failed" or "blocked". "blocked" is the whole
        reason this method is worth having: a refusal, a confirmation the user
        declined and "I could not find the ok button" never produce a
        ToolResult at all, and without a turn to put them in the model is told
        nothing and repeats itself.

        `dry_run` is not decoration either. With `dry_run=True` nothing changed,
        so a model that checks its own work finds the world unchanged and
        retries forever. Saying so in the result is the fix; the step budget is
        only the backstop.
        """
        return self.add("tool", tool_result_text(
            tool=tool, status=status, summary=summary, data=data, dry_run=dry_run
        ))

    def _enforce_chars(self) -> None:
        # Drop from the left until the window fits. Never drop the newest turn,
        # even if it alone exceeds the budget -- losing the thing the user just
        # said is worse than a slightly oversized state.
        while len(self._turns) > 1 and self.char_count > self.max_chars:
            self._turns.popleft()

    def clear(self) -> None:
        self._turns.clear()

    # -- reads -------------------------------------------------------------

    @property
    def char_count(self) -> int:
        return sum(len(t.text) for t in self._turns)

    def __len__(self) -> int:
        return len(self._turns)

    def __iter__(self) -> Iterator[Turn]:
        return iter(self._turns)

    @property
    def turns(self) -> list[Turn]:
        return list(self._turns)

    @property
    def last_user(self) -> str | None:
        for turn in reversed(self._turns):
            if turn.role == "user":
                return turn.text
        return None

    def recent(self, n: int | None = None) -> list[dict[str, Any]]:
        """Plain dicts, for the risk gate's `history` argument."""
        turns = self.turns if n is None else self.turns[-n:]
        return [t.as_dict() for t in turns]

    def as_state(self, **extra: Any) -> Mapping[str, Any]:
        """The Jev `state` payload.

        Flat, JSON-safe and small. `extra` is where the loop adds the one or
        two situational facts a given question needs (the pending action, the
        dry-run flag) rather than growing this class a field per caller.
        """
        state: dict[str, Any] = {
            "conversation": self.recent(),
            "turns_total": self.total_turns,
        }
        state.update({k: v for k, v in extra.items() if v is not None})
        return state

    def messages(self, system: str | None = None) -> list[dict[str, str]]:
        """OpenAI-shaped history for the DeepSeek call.

        System prompt first and unchanging, so the provider's prefix cache hits
        on every turn -- the same reason the router only activates a few tools.
        """
        out: list[dict[str, str]] = []
        if system:
            out.append({"role": "system", "content": system})
        for turn in self._turns:
            # A tool result is sent as "user" and NOT as role "tool": an
            # OpenAI-shaped `role: "tool"` message is only valid directly after
            # an assistant message carrying the matching tool_call_id, and this
            # transcript deliberately does not keep tool_call ids. The
            # `[tool result]` prefix carries the meaning instead, which also
            # keeps the whole thing readable in a log.
            role = "assistant" if turn.role == "assistant" else "user"
            out.append({"role": role, "content": turn.text})
        return out


# ---------------------------------------------------------------------------
# Tool results
# ---------------------------------------------------------------------------


def tool_result_text(
    *,
    tool: str,
    status: str,
    summary: str = "",
    data: Mapping[str, Any] | None = None,
    dry_run: bool = False,
) -> str:
    """One line describing one step, safe to put in front of a remote model."""
    head = f"{TOOL_PREFIX} {str(tool or 'unknown').strip()}: {str(status or 'ok').strip()}"
    if dry_run:
        # Said in words, not as a flag the model has to know to look for.
        head += " (DRY RUN — nothing actually changed, do not retry it)"
    sentence = _scrub(" ".join(str(summary or "").split()))
    if sentence:
        head += f" — {sentence}"
    shaped = shape_data(data)
    if shaped:
        head += " · " + " ".join(f"{k}={_render(v)}" for k, v in shaped.items())
    return head


def shape_data(data: Mapping[str, Any] | None) -> dict[str, Any]:
    """`ToolResult.data` reduced to shape. THIS IS THE PRIVACY BOUNDARY.

    Three rules, in this order, and every one of them is a deny by default:

    1. A key `safety/audit.py` already classifies as content-bearing is gone.
       That is the same list the audit log uses, so "what may be written down"
       and "what may be sent to DeepSeek" cannot drift apart.
    2. A container becomes a COUNT. `headings`, `matches`, `controls` and
       `alternates` are the useful ones and their length is the useful part;
       their contents are the page.
    3. A string survives only under a key from `_SHAPE_STR_KEYS`, and only if
       it is short after scrubbing. Numbers and flags survive as they are.

    Nothing recurses, so a nested mapping contributes a count and nothing else.
    """
    if not isinstance(data, Mapping):
        return {}
    banned = _redacted_keys()
    out: dict[str, Any] = {}
    for key in sorted(str(k) for k in data):
        if len(out) >= _SHAPE_MAX_KEYS:
            break
        if key.lower() in banned:
            continue
        value = data[key]
        if isinstance(value, (bool, int)):
            out[key] = value
        elif isinstance(value, float):
            out[key] = round(value, 3)
        elif isinstance(value, (list, tuple, set, frozenset, Mapping)):
            out[f"{key}_count"] = len(value)
        elif isinstance(value, str):
            if key.lower() not in _SHAPE_STR_KEYS:
                continue
            # Length is checked on the RAW value, before scrubbing. Scrubbing a
            # 400-character payload yields "<elided 400 chars>", which is short
            # -- so checking afterwards would let a long string through the
            # length rule by being caught by a different one.
            text = " ".join(value.split())
            if text and len(text) <= _SHAPE_MAX_VALUE_CHARS:
                out[key] = _scrub(text)
        # None and everything else: dropped. "absent" is not information the
        # model needs and a repr() is a payload wearing a type name.
    return out


def _render(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


_AUDIT: Any = None
_AUDIT_LOOKED = False


def _audit_module() -> Any:
    """safety/audit.py, imported lazily and tolerated absent.

    voice/ is allowed to know about safety/ -- loop.py already does -- but this
    module must still import in a half-built tree, so the scrubber degrades to
    the length cap rather than to an ImportError.
    """
    global _AUDIT, _AUDIT_LOOKED
    if not _AUDIT_LOOKED:
        _AUDIT_LOOKED = True
        try:
            from daa.safety import audit as module

            _AUDIT = module
        except ImportError:
            _AUDIT = None
    return _AUDIT


def _scrub(text: str) -> str:
    module = _audit_module()
    if module is None:
        return text[:_FALLBACK_MAX]
    return str(module.scrub(text, limit=_FALLBACK_MAX))


def _redacted_keys() -> frozenset[str]:
    module = _audit_module()
    keys = getattr(module, "REDACTED_KEYS", None) if module is not None else None
    if isinstance(keys, (set, frozenset)):
        # `summary` is on that list because a tool routinely quotes its input
        # into one, and the audit log outlives the session. Here the summary is
        # the WHOLE POINT -- it is the sentence daa speaks aloud -- so it is
        # handled by `tool_result_text` (scrubbed and capped) rather than by
        # `shape_data`, and a `data["summary"]` is still dropped below.
        return frozenset(keys) | _EXTRA_CONTENT_KEYS
    return _EXTRA_CONTENT_KEYS | frozenset({"text", "summary", "error", "clipboard", "body"})


# Content-bearing keys the audit list does not name, because nothing had ever
# put them in a log. A result fed to a remote model is a new exposure and gets
# its own list: a page title, a URL, a control label and a filename are all
# things the user did not agree to send anywhere.
_EXTRA_CONTENT_KEYS = frozenset(
    {
        "title", "url", "href", "link", "path", "paths", "overflow_path",
        "filename", "filenames", "label", "labels", "query", "selector",
        "placeholder", "value", "values", "target", "targets", "heading",
    }
)
_FALLBACK_MAX = 200
