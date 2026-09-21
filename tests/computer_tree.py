"""Recorded accessibility trees, so every decision is testable with zero grants.

The same trick `test_undo_coverage` uses for `_trash_via_appkit`: the only part
that needs a real machine is stubbed, and everything that decides what the user
hears runs on a fixture. A machine with no Accessibility grant -- which is every
CI machine and, right now, this one -- still exercises the ranking, the
destructive lexicon, the floor hints and every refusal path.
"""

from __future__ import annotations

from collections.abc import Sequence

from daa.tools.computer.ax import Element, Snapshot


def el(
    label: str = "",
    *,
    role: str = "AXButton",
    role_description: str = "button",
    subrole: str = "",
    window: str = "Untitled",
    app: str = "TextEdit",
    bundle_id: str = "com.apple.TextEdit",
    enabled: bool = True,
    actions: Sequence[str] = ("AXPress",),
    in_alert: bool = False,
    default: bool = False,
    description: str = "",
    placeholder: str = "",
    identifier: str = "",
    position: tuple[float, float] | None = (100.0, 100.0),
    size: tuple[float, float] | None = (80.0, 24.0),
    path: tuple[int, ...] = (0, 0),
) -> Element:
    return Element(
        role=role,
        subrole=subrole,
        role_description=role_description,
        title=label,
        description=description,
        placeholder=placeholder,
        identifier=identifier,
        enabled=enabled,
        actions=tuple(actions),
        window_title=window,
        in_alert=in_alert,
        is_default_button=default,
        app=app,
        bundle_id=bundle_id,
        pid=4242,
        depth=len(path),
        path=path,
        position=position,
        size=size,
        ref=object(),
    )


def field(
    label: str = "Search",
    *,
    secure: bool = False,
    app: str = "TextEdit",
    bundle_id: str = "com.apple.TextEdit",
    window: str = "Untitled",
    enabled: bool = True,
) -> Element:
    role = "AXSecureTextField" if secure else "AXTextField"
    return el(
        label,
        role=role,
        role_description="secure text field" if secure else "text field",
        actions=(),
        app=app,
        bundle_id=bundle_id,
        window=window,
        enabled=enabled,
    )


def tree(
    *elements: Element,
    app: str = "TextEdit",
    bundle_id: str = "com.apple.TextEdit",
    snapshot_id: str = "snap0001",
) -> Snapshot:
    return Snapshot(
        app=app,
        bundle_id=bundle_id,
        pid=4242,
        elements=tuple(elements),
        snapshot_id=snapshot_id,
        nodes_visited=len(elements) + 1,
        elapsed_ms=12.0,
    )


def serve(snapshot: Snapshot):
    """A stand-in for `ax.snapshot` that always answers with this tree."""

    def _snapshot(app_query: str, **_kwargs: object) -> Snapshot:
        return snapshot

    return _snapshot


def serving(*snapshots: Snapshot):
    """A stand-in that answers with each tree in turn, then repeats the last.

    How "the screen changed between resolve() and yes" is tested: resolve()
    sees the first tree, run() sees the second.
    """
    queue = list(snapshots)

    def _snapshot(app_query: str, **_kwargs: object) -> Snapshot:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return _snapshot
