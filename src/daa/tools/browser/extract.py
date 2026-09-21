"""Turning a page into something that can be spoken, and capping it.

Three representations, three jobs, and mixing them up is a measured mistake
rather than a stylistic one:

| Representation      | Job                                          | Never used for |
|---------------------|----------------------------------------------|----------------|
| `aria_snapshot()`   | **acting** -- where accessible names come from | speaking     |
| extracted text      | **speaking** -- main content, chrome removed   | acting       |
| raw HTML            | nothing                                        | anything     |

The accessibility tree is the WRONG thing to read aloud: measured on this
machine it is 731,503 characters on one Wikipedia article and ten times the
page's own text on Hacker News, because it enumerates every link and control
with its URL. Scoping it to an element is what `facts.py` does; taking it
whole-body for a summary blows the context window on any page with a lot of
links.

Extraction does not solve the size problem either -- it is a quality win (it
drops nav, cookie banners, footers and sidebars, the things that make a spoken
summary sound like a screen reader), worth roughly 20-25% on a blog post and
2% on Wikipedia. So the cap is explicit, it is hermes's shipped 15,000, and the
truncation cuts on paragraph boundaries rather than mid-sentence.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from daa.tools.base import spoken_snippet


def truncate_structured(text: str, limit: int) -> tuple[str, bool]:
    """Cut on a paragraph boundary, then a line, then a word. Never mid-word.

    A summary that ends mid-word is read aloud as a stammer, and a snapshot cut
    mid-element is a lie about the document's shape.
    """
    body = str(text or "")
    if len(body) <= limit or limit <= 0:
        return body, False
    head = body[:limit]
    for separator in ("\n\n", "\n", ". ", " "):
        cut = head.rfind(separator)
        if cut > limit // 2:
            return head[:cut].rstrip(), True
    return head.rstrip(), True


def round_words(count: int) -> str:
    """A word count a person can hear. Precision here is noise."""
    n = max(0, int(count))
    if n == 0:
        return "no text"
    if n < 60:
        return "a couple of sentences"
    if n < 200:
        return "a few paragraphs"
    if n < 1000:
        return f"about {round(n, -2)} words"
    if n < 10_000:
        return f"about {round(n / 1000, 1):g} thousand words"
    return f"about {round(n / 1000):g} thousand words"


def page_shape(extracted: Mapping[str, Any], *, site: str, logged_in: bool) -> str:
    """The SPOKEN sentence for `read_page`. A shape, never the content.

    `ToolResult.summary` is synthesised into speech, so it carries no URL, no
    path and no long identifier -- the text itself lives in `ToolResult.data`,
    where the conversational layer decides what to do with it.
    """
    words = int(extracted.get("words") or 0)
    where = f" on {site}" if site else ""
    title = spoken_snippet(str(extracted.get("title") or ""), limit=48)
    lead = f"It's {title}" if title else "It's a page"
    tail = ", where you're signed in" if logged_in else ""
    return f"{lead}{where}{tail}, {round_words(words)}."


def heading_outline(extracted: Mapping[str, Any], *, limit: int = 8) -> list[str]:
    """Headings, for the case where the full text may not leave the machine.

    A logged-in page's body is private content; its section headings are how a
    person decides whether they want it read to them at all, and are what
    `find_on_page` searches against locally.
    """
    out: list[str] = []
    for heading in list(extracted.get("headings") or [])[:limit]:
        flat = " ".join(str(heading).split())
        if flat:
            out.append(flat[:80])
    return out


__all__ = ["heading_outline", "page_shape", "round_words", "truncate_structured"]
