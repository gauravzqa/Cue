"""Which tools does the conversational model get to see for this utterance?

Semantic activation, not keyword matching. The cost asymmetry drives the whole
design: handing the LLM two tools it does not need costs a few hundred tokens,
while withholding the one it does need makes the assistant look broken. So the
router activates the top tool plus everything in a photo-finish with it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from daa.contracts import JevProvider, ToolSpec
from daa.jev import questions as Q
from daa.jev.client import JevUnavailable


class ToolRouter:
    # A hard ceiling regardless of margin: past about five tools the LLM's own
    # selection accuracy falls faster than the router's does, so a wide tie is
    # better resolved by truncation than by passing the problem along.
    MAX_ACTIVE = 5

    def __init__(
        self,
        provider: JevProvider,
        *,
        margin: float = 0.15,
        max_active: int = MAX_ACTIVE,
    ) -> None:
        self._provider = provider
        self._margin = margin
        self._max_active = max(1, min(max_active, self.MAX_ACTIVE))

    def activate(
        self,
        utterance: str,
        specs: Sequence[ToolSpec],
        ctx: Mapping[str, Any] | None = None,
    ) -> list[ToolSpec]:
        if not specs:
            return []

        try:
            answers = self._provider.ask(
                Q.router_state(utterance, ctx or {}),
                {Q.Q_TOOL: Q.tool_choice(specs)},
            )
        except JevUnavailable:
            # No tools rather than all tools: a degraded turn where the
            # assistant says it cannot do something is recoverable, a turn where
            # it picks a destructive tool at random is not.
            return []

        return self._rank(answers.choice(Q.Q_TOOL), specs)

    def activate_next(
        self,
        utterance: str,
        specs: Sequence[ToolSpec],
        ctx: Mapping[str, Any] | None = None,
        *,
        done: Sequence[Mapping[str, Any]] = (),
    ) -> list[ToolSpec]:
        """What the task needs NEXT, given what it has already done.

        Same ranking, different question -- see `Q.next_tool_choice`. The agent
        loop calls this from step two onward and UNIONS the result onto the
        menu it already had, so this can only ever add reachability; it cannot
        take a tool away from a model that is mid-plan.
        """
        if not specs:
            return []
        try:
            answers = self._provider.ask(
                Q.next_step_state(utterance, ctx or {}, done),
                {Q.Q_TOOL: Q.next_tool_choice(specs)},
            )
        except JevUnavailable:
            # Nothing added. The menu the turn already had still stands, which
            # is why this degrades to "no new tools" rather than to "no tools".
            return []
        return self._rank(answers.choice(Q.Q_TOOL), specs)

    def _rank(self, answer: Any, specs: Sequence[ToolSpec]) -> list[ToolSpec]:
        probabilities = answer.probabilities

        # The explicit `none` option is a real answer, not a fallback. If it
        # wins outright the utterance was conversation, and activating the
        # runner-up tool anyway would defeat the point of offering it.
        if answer.choice == Q.NONE_OPTION:
            return []

        by_name = {spec.name: spec for spec in specs}
        ranked = sorted(
            ((name, p) for name, p in probabilities.items() if name in by_name),
            key=lambda item: (-item[1], item[0]),  # name breaks ties deterministically
        )
        if not ranked:
            return []

        top_p = ranked[0][1]
        cutoff = top_p - self._margin
        # `none` is skipped rather than allowed to cut the list short: if the
        # model is torn between "no tool" and "this tool", offering the tool
        # still leaves the LLM free to not use it.
        names = [name for name, p in ranked if p >= cutoff]
        # The declared choice always ships, even in the pathological case where
        # it is not the argmax of the distribution it came with. Second-guessing
        # the model's own stated answer is not this layer's job.
        if answer.choice in by_name and answer.choice not in names:
            names.insert(0, answer.choice)
        return [by_name[name] for name in names[: self._max_active]]
