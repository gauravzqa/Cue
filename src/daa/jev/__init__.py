"""Jev -- the judgment layer.

Unstructured state and typed questions in, typed probabilistic answers out, in
one parallel pass. Nothing here may import daa.tools or daa.voice: judgment must
stay testable with no machine to touch and no microphone to open.
"""

from daa.jev.client import (
    FakeJev,
    JevUnavailable,
    RealJev,
    ReplayJev,
    build_provider,
)
from daa.jev.confirm import ConfirmParser, Verdict
from daa.jev.gate import AddressGate, WakeDecision
from daa.jev.risk import RiskGate
from daa.jev.router import ToolRouter

__all__ = [
    "AddressGate",
    "ConfirmParser",
    "FakeJev",
    "JevUnavailable",
    "RealJev",
    "ReplayJev",
    "RiskGate",
    "ToolRouter",
    "Verdict",
    "WakeDecision",
    "build_provider",
]
