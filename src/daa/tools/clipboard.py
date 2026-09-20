"""Reading and writing the general pasteboard.

Writing the clipboard looks harmless and is not: the previous contents are gone
the instant NSPasteboard is cleared, and the thing people keep on their
clipboard is usually the thing they were in the middle of. So set_clipboard
snapshots first and hands back an UndoAction even though ANNOUNCE does not
require one -- the cost is one extra read, and the alternative is a user who
lost a password they had copied ten seconds ago.
"""

from __future__ import annotations

from typing import Any

from daa.contracts import ResolvedAction, RiskTier, ToolResult, UndoAction
from daa.tools.base import BaseTool, Degraded, as_sentence, param, spec, spoken_snippet

# Long clipboard contents are summarised rather than spoken in full.
SPOKEN_PREVIEW_CHARS = 60


def _pasteboard() -> Any:
    from AppKit import NSPasteboard  # type: ignore

    return NSPasteboard.generalPasteboard()


def read_clipboard_text() -> tuple[str | None, str]:
    """Returns (text, error). None means 'nothing readable as text'."""
    try:
        from AppKit import NSPasteboardTypeString  # type: ignore

        board = _pasteboard()
        value = board.stringForType_(NSPasteboardTypeString)
        return (str(value) if value is not None else None), ""
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def describe_text(text: str) -> str:
    """A speakable IDENTIFIER for a piece of text -- never the text itself.

    `targets` is copied verbatim into ~/.daa/audit.jsonl and into the state Jev
    scores, so putting sixty characters of clipboard content there writes the
    password the user just copied to a file on disk. The content stays in
    `data`/`args`; what the user hears is how much of it there is.
    """
    flat = " ".join(text.split())
    if not flat:
        return "nothing"
    if len(flat) <= SPOKEN_PREVIEW_CHARS:
        return "the text you dictated"
    return f"{len(flat)} characters of text"


class GetClipboard(BaseTool):
    verb = "read the clipboard"

    spec = spec(
        "get_clipboard",
        "Read the current text contents of the clipboard.",
        {},
        floor=RiskTier.SILENT,
        activation_hint="""
            what did I just copy, what is on my clipboard, read the clipboard, what is in
            my paste buffer, use the thing I copied, summarise what I copied, translate
            what's on my clipboard. Read-only. Use whenever the user refers to something
            they copied rather than something they said.
        """,
        tags=("clipboard", "read"),
        # Reading takes nothing away, so there is nothing to give back.
        inverses=(),
    )

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        return self.action(targets=[], explicit=bool(kwargs.get("explicit", True)))

    def run(self, action: ResolvedAction) -> ToolResult:
        text, error = read_clipboard_text()
        if error:
            return Degraded("pasteboard", "I need the macOS app frameworks to read that.", error).as_result(
                "I could not read the clipboard."
            )
        if text is None:
            return ToolResult(
                ok=True, summary="There is nothing text-like on the clipboard.",
                data={"text": None, "empty": True},
            )
        if not text.strip():
            return ToolResult(
                ok=True, summary="The clipboard is empty.", data={"text": text, "empty": True}
            )
        # The text itself goes in `data`, not the sentence: a copied URL or API
        # key read aloud is noise at best and a leak at worst.
        snippet = spoken_snippet(text, limit=SPOKEN_PREVIEW_CHARS)
        words = len(text.split())
        summary = (
            f"The clipboard has {snippet}"
            if snippet
            else f"The clipboard has about {words} words of text"
        )
        return ToolResult(
            ok=True,
            summary=as_sentence(summary),
            data={"text": text, "length": len(text), "words": words, "empty": False},
        )


class SetClipboard(BaseTool):
    mutates = True
    verb = "copy to the clipboard"

    spec = spec(
        "set_clipboard",
        "Replace the clipboard contents with a piece of text.",
        {"text": param("string", "Text to place on the clipboard", required=True)},
        floor=RiskTier.ANNOUNCE,
        activation_hint="""
            copy that put this on my clipboard so I can paste it, copy the answer, copy
            this address link code snippet phone number, save that to paste later, give it
            to me on the clipboard. Replaces whatever is currently copied. Use when the
            user wants text in hand rather than spoken aloud.
        """,
        tags=("clipboard", "undoable"),
        # Restoring the old clipboard is another set_clipboard.
        inverses=("set_clipboard",),
    )

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        text = kwargs.get("text")
        text = "" if text is None else str(text)
        return self.action(
            # The content itself never reaches targets -- see describe_text.
            targets=[describe_text(text)] if text else [],
            explicit=bool(kwargs.get("explicit", True)),
            consequences=(
                {"replaces": "replacing whatever is on the clipboard now"} if text else {}
            ),
            text=text,
        )

    def run(self, action: ResolvedAction) -> ToolResult:
        text = str(action.args.get("text", ""))
        if self.dry_run:
            return self.dry("I would copy that to the clipboard.", length=len(text))

        previous, read_error = read_clipboard_text()
        try:
            from AppKit import NSPasteboardTypeString  # type: ignore

            board = _pasteboard()
            board.clearContents()
            wrote = bool(board.setString_forType_(text, NSPasteboardTypeString))
        except Exception as exc:  # noqa: BLE001
            return Degraded(
                "pasteboard", "I need the macOS app frameworks to do that.", str(exc)
            ).as_result("I could not write to the clipboard.")
        if not wrote:
            return self.failed("I could not write to the clipboard.", "setString returned false")

        # If the old clipboard held only an image or a file promise we cannot
        # restore it, and the undo says so rather than pretending.
        undo = (
            UndoAction(
                description="put the previous clipboard text back",
                tool="set_clipboard",
                args={"text": previous},
            )
            if previous is not None
            else None
        )
        return ToolResult(
            ok=True,
            summary="Copied.",
            data={
                "length": len(text),
                "replaced_text": previous is not None,
                "previous_unrecoverable": previous is None,
                "read_error": read_error or None,
            },
            undo=undo,
        )


__all__ = ["GetClipboard", "SetClipboard", "describe_text", "read_clipboard_text"]
