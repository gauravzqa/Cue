"""The conversational model: DeepSeek over the OpenAI-compatible endpoint.

Two non-obvious things happen here, both about latency, which in a voice loop
is the only quality metric the user can feel:

1. `extra_body={"thinking": {"type": "disabled"}}`. DeepSeek turns reasoning
   ON by default. For "what's on my calendar" that buys nothing and costs
   several seconds of silence, which reads as a broken assistant. The judgment
   that actually needs care already happened in Jev, against calibrated
   probabilities, before this call was made.

2. Only the tools the router activated are sent. The system prompt plus tool
   schemas is the prefix the provider caches; if it changes shape every turn
   nothing caches and every turn pays full price. A stable small prefix beats
   a complete one.

This module knows about ToolSpec (a contract type) and nothing else from the
subsystems -- it never imports tools/, and it cannot run anything.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from daa.contracts import ToolSpec

SYSTEM_PROMPT = (
    "You are daa, a voice assistant on the user's Mac. You are being SPOKEN to and "
    "your reply will be SPOKEN aloud.\n"
    "Rules:\n"
    "- One or two short sentences. No markdown, no lists, no file paths, no IDs.\n"
    "- If a tool can do what was asked, call it. Do not describe what you would do.\n"
    "- Never claim you did something a tool did not report doing.\n"
    "- If the request is ambiguous, ask one short question instead of guessing."
)


@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    args: Mapping[str, Any]
    id: str = ""


@dataclass(frozen=True, slots=True)
class LLMTurn:
    """What the model wants to happen next: say something, call tools, or both."""

    text: str = ""
    tool_calls: Sequence[ToolCall] = ()
    latency_ms: float = 0.0
    model: str = ""


class LLM(Protocol):
    def respond(
        self,
        messages: Sequence[Mapping[str, str]],
        specs: Sequence[ToolSpec],
    ) -> LLMTurn:
        ...


def tool_schema(spec: ToolSpec) -> dict[str, Any]:
    """ToolSpec.params -> OpenAI function schema.

    Accepts either a full JSON schema object or the shorthand
    {"name": {"type": "string", ...}} so tool authors are not forced to write
    the `{"type": "object", "properties": ...}` envelope by hand.
    """
    params = dict(spec.params or {})
    if params.get("type") == "object" and "properties" in params:
        schema = params
    else:
        required = [k for k, v in params.items() if isinstance(v, Mapping) and v.get("required")]
        schema = {
            "type": "object",
            "properties": {
                k: {kk: vv for kk, vv in dict(v).items() if kk != "required"}
                if isinstance(v, Mapping)
                else {"type": "string", "description": str(v)}
                for k, v in params.items()
            },
            "required": required,
        }
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": schema,
        },
    }


@dataclass(slots=True)
class FakeLLM:
    """Scripted replies, keyed by nothing -- consumed in order.

    Deliberately dumb: a test that wants "utterance X produces tool call Y"
    should say so directly, not encode it in a fake's matching rules.
    """

    turns: list[LLMTurn] = field(default_factory=list)
    default: LLMTurn = field(default_factory=lambda: LLMTurn(text="Okay."))
    # Every (messages, spec names) pair seen, so a test can assert the router's
    # narrowing actually reached the model.
    seen: list[tuple[list[Mapping[str, str]], list[str]]] = field(default_factory=list)

    def respond(
        self,
        messages: Sequence[Mapping[str, str]],
        specs: Sequence[ToolSpec],
    ) -> LLMTurn:
        self.seen.append(([dict(m) for m in messages], [s.name for s in specs]))
        if self.turns:
            return self.turns.pop(0)
        return self.default


class DeepSeekLLM:
    """Real client. `openai` is imported lazily so `daa doctor` stays instant."""

    def __init__(self, settings: Any, *, system_prompt: str = SYSTEM_PROMPT) -> None:
        self.settings = settings
        self.system_prompt = system_prompt
        self._client: Any = None

    @property
    def name(self) -> str:
        return getattr(self.settings, "voice_model", "deepseek-chat")

    def available(self) -> bool:
        return bool(getattr(self.settings, "deepseek_api_key", None))

    def _ensure(self) -> Any:
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self.settings.deepseek_api_key,
                base_url=getattr(self.settings, "deepseek_base_url", None),
            )
        return self._client

    def respond(
        self,
        messages: Sequence[Mapping[str, str]],
        specs: Sequence[ToolSpec],
    ) -> LLMTurn:
        import time

        client = self._ensure()
        kwargs: dict[str, Any] = {
            "model": self.settings.voice_model,
            "messages": list(messages),
            # Short, because it is going to be read aloud. A model that writes
            # a paragraph has already failed regardless of content.
            "max_tokens": 300,
            "temperature": 0.3,
            # THE line. See the module docstring.
            "extra_body": {"thinking": {"type": "disabled"}},
        }
        if specs:
            kwargs["tools"] = [tool_schema(s) for s in specs]
            kwargs["tool_choice"] = "auto"

        started = time.monotonic()
        response = client.chat.completions.create(**kwargs)
        latency = (time.monotonic() - started) * 1000
        message = response.choices[0].message
        calls: list[ToolCall] = []
        for raw in getattr(message, "tool_calls", None) or []:
            try:
                args = json.loads(raw.function.arguments or "{}")
            except json.JSONDecodeError:
                # A malformed arg blob is not a reason to crash mid-sentence;
                # resolve() will reject the empty call and the user gets asked.
                args = {}
            calls.append(ToolCall(name=raw.function.name, args=args, id=raw.id or ""))
        return LLMTurn(
            text=(message.content or "").strip(),
            tool_calls=tuple(calls),
            latency_ms=latency,
            model=response.model,
        )


def build_llm(settings: Any) -> LLM:
    """Real model when there is a key, a fake that says something honest when not."""
    llm = DeepSeekLLM(settings)
    if llm.available():
        return llm
    return FakeLLM(default=LLMTurn(text="I heard you, but I have no model key configured."))
