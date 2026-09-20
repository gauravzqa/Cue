"""The tool table. One instance, populated at import of `daa.tools`.

The registry is deliberately dumb: no dispatch, no policy, no LLM. It hands out
ToolSpecs to the Jev router and Tool objects to the loop, and it refuses to hold
two tools under one name -- a duplicate registration means two different pieces
of code answer to the same spoken intent, and only one of them was reviewed.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from daa.contracts import RiskTier, Tool, ToolSpec


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    # -- population ------------------------------------------------------

    def register(self, tool: Tool) -> Tool:
        spec = getattr(tool, "spec", None)
        if not isinstance(spec, ToolSpec):
            raise TypeError(f"{tool!r} has no ToolSpec")
        if not isinstance(tool, Tool):
            raise TypeError(f"{spec.name} does not satisfy the Tool protocol")
        if spec.name in self._tools:
            raise ValueError(f"duplicate tool name: {spec.name}")
        if not spec.activation_hint:
            # The semantic router sees ONLY this string. A tool without one is
            # unreachable by voice, which is a silent failure at runtime and a
            # loud one here.
            raise ValueError(f"{spec.name} has no activation_hint")
        self._tools[spec.name] = tool
        return tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    # -- lookup ----------------------------------------------------------

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            near = ", ".join(sorted(self._tools)) or "none"
            raise KeyError(f"no tool named {name!r}; registered: {near}") from None

    def specs(self) -> list[ToolSpec]:
        return [self._tools[name].spec for name in sorted(self._tools)]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def by_floor(self, minimum: RiskTier) -> list[ToolSpec]:
        return [s for s in self.specs() if s.floor >= minimum]

    def by_tag(self, tag: str) -> list[ToolSpec]:
        return [s for s in self.specs() if tag in s.tags]

    def activation_corpus(self) -> dict[str, str]:
        """name -> the only text the router is allowed to match against."""
        return {s.name: s.activation_hint for s in self.specs()}

    # -- container protocol ----------------------------------------------

    def __contains__(self, item: object) -> bool:
        if isinstance(item, str):
            return item in self._tools
        spec = getattr(item, "spec", None)
        return isinstance(spec, ToolSpec) and spec.name in self._tools

    def __iter__(self) -> Iterator[Tool]:
        return (self._tools[name] for name in sorted(self._tools))

    def __len__(self) -> int:
        return len(self._tools)

    def __repr__(self) -> str:
        return f"ToolRegistry({', '.join(sorted(self._tools))})"


def build_registry(tools: Sequence[Tool]) -> ToolRegistry:
    reg = ToolRegistry()
    for tool in tools:
        reg.register(tool)
    return reg


# Populated by daa/tools/__init__.py. Importing `daa.tools.registry` imports the
# parent package first, so this is never observed empty from outside.
REGISTRY = ToolRegistry()
