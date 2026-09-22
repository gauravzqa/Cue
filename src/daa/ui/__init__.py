"""The dock protocol: the Python half of the line between `daa` and the app.

This module imports NOTHING but `os` and `sys`, and it must stay that way.
`steal_stdout` lives here rather than in `bridge.py` for exactly one reason:
it has to run before anything else is imported, and a package `__init__` that
pulled in half the tree would itself be "anything else". The import chain to
reach this function is `daa` (empty) then `daa.ui` (this file), and neither
can print.

Everything with behaviour lives in `protocol` and `bridge`, imported
explicitly by `daa bridge` after fd 1 is safe.
"""

from __future__ import annotations

import os
import sys
from typing import Any

__all__ = ["steal_stdout"]


def steal_stdout() -> Any:
    """Take fd 1 for the protocol and send everyone else to stderr.

    Call this FIRST, before importing anything that might print. fd 1 is the
    protocol and nothing else: one stray `print()` anywhere in the tree --
    ours, a dependency's, at import time, on a machine we have never seen --
    corrupts the stream, and the symptom is a dock that silently stops
    updating rather than an error anyone can act on.

    The order is the whole trick:

    1. `dup(1)` -- a private copy of the real pipe to the dock. Nothing else
       in the process has a handle on it, so nothing else can write to it.
    2. `dup2(2, 1)` -- fd 1 now *is* stderr, so every write to it lands in
       the log: ours, a C extension's, a child process's.
    3. `sys.stdout = sys.stderr` -- and so does anything already holding the
       Python object, including a library that cached it at import time.

    Returns the private, unbuffered binary handle the writer thread owns.
    """
    real = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return real
