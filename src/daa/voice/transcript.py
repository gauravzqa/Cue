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
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["user", "assistant", "system"]


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
    """

    def __init__(self, *, max_turns: int = 12, max_chars: int = 2_000, max_turn_chars: int = 600):
        self.max_turns = max_turns
        self.max_chars = max_chars
        self.max_turn_chars = max_turn_chars
        self._turns: deque[Turn] = deque(maxlen=max_turns)
        # Counts every turn ever added, including evicted ones. Lets a test
        # (and the audit log) prove the window is rolling rather than silently
        # dropping writes.
        self.total_turns = 0

    # -- writes ------------------------------------------------------------

    def add(self, role: Role, text: str) -> Turn:
        clipped = text.strip()
        if len(clipped) > self.max_turn_chars:
            # Clip the MIDDLE: the start carries the instruction and the end
            # carries the correction ("...actually, no, the other folder").
            head = self.max_turn_chars // 2
            tail = self.max_turn_chars - head - 1
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
            role = "assistant" if turn.role == "assistant" else "user"
            out.append({"role": role, "content": turn.text})
        return out
