"""Finding, revealing, trashing and moving files.

Two rules shape this module.

Spotlight over walking: `mdfind` answers "the screenshots from today" against an
index macOS already maintains, in milliseconds, across the whole disk. Walking
the filesystem to answer the same question is slower, misses metadata-only facts
like kMDItemIsScreenCapture, and burns the user's patience while they wait for a
spoken reply.

Trash over delete: nothing here ever calls `rm`. Deletion goes through
NSFileManager's trashItemAtURL, which returns the RESULTING url inside the
Trash -- and that return value is the only reason an honest undo is possible.
`rm` would make the confirmation a lie, because there would be nothing to put
back when the user says "no, wait".
"""

from __future__ import annotations

import glob as globlib
import os
import shutil
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from daa.contracts import ResolvedAction, RiskTier, ToolResult, UndoAction
from daa.tools.base import BaseTool, ShellTool, param, run_argv, spec

MDFIND = "/usr/bin/mdfind"
OPEN = "/usr/bin/open"
OSASCRIPT = "/usr/bin/osascript"

# Spoken content categories -> the UTI trees Spotlight actually indexes.
KIND_QUERIES: dict[str, str] = {
    "image": 'kMDItemContentTypeTree == "public.image"',
    "screenshot": "kMDItemIsScreenCapture == 1",
    "pdf": 'kMDItemContentTypeTree == "com.adobe.pdf"',
    "document": 'kMDItemContentTypeTree == "public.content"',
    "text": 'kMDItemContentTypeTree == "public.text"',
    "audio": 'kMDItemContentTypeTree == "public.audio"',
    "video": 'kMDItemContentTypeTree == "public.movie"',
    "folder": 'kMDItemContentTypeTree == "public.folder"',
    "archive": 'kMDItemContentTypeTree == "public.archive"',
    "presentation": 'kMDItemContentTypeTree == "public.presentation"',
    "spreadsheet": 'kMDItemContentTypeTree == "public.spreadsheet"',
    "application": 'kMDItemContentTypeTree == "com.apple.application"',
}

# Spoken time windows -> mdfind's $time vocabulary. Doing this with literal
# timestamps would drift the moment the query is cached or replayed.
WHEN_QUERIES: dict[str, str] = {
    "today": "kMDItemContentModificationDate >= $time.today",
    "yesterday": (
        "kMDItemContentModificationDate >= $time.yesterday && "
        "kMDItemContentModificationDate < $time.today"
    ),
    "this week": "kMDItemContentModificationDate >= $time.this_week",
    "last week": (
        "kMDItemContentModificationDate >= $time.this_week(-1) && "
        "kMDItemContentModificationDate < $time.this_week"
    ),
    "this month": "kMDItemContentModificationDate >= $time.this_month",
    "last month": (
        "kMDItemContentModificationDate >= $time.this_month(-1) && "
        "kMDItemContentModificationDate < $time.this_month"
    ),
    "this year": "kMDItemContentModificationDate >= $time.this_year",
}

# Trashing any of these is never what a voice command meant, no matter how
# confidently it resolved. The gate is here rather than in safety/ because it is
# a fact about the filesystem, not a judgment call.
PROTECTED = {
    Path("/"),
    Path("/System"),
    Path("/Applications"),
    Path("/Library"),
    Path("/Users"),
    Path("/Volumes"),
    Path.home(),
    Path.home() / "Desktop",
    Path.home() / "Documents",
    Path.home() / "Downloads",
    Path.home() / "Library",
    Path.home() / "Pictures",
    Path.home() / "Movies",
    Path.home() / "Music",
}


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def expand(raw: str | os.PathLike[str]) -> Path:
    return Path(os.path.expandvars(str(raw))).expanduser()


def speakable(path: str | os.PathLike[str]) -> str:
    """Filenames are spoken; directories and extensions are noise."""
    return Path(str(path)).name or str(path)


def protected_label(path: str | os.PathLike[str]) -> str:
    """A protected path said the way a person would say it.

    speakable(~) is the account's short name, which nobody recognises as their
    own home folder when they hear it in a confirmation.
    """
    candidate = Path(str(path))
    try:
        resolved = candidate.resolve()
    except OSError:
        resolved = candidate
    if resolved == Path.home():
        return "your home folder"
    if resolved.parent == Path.home():
        return f"your {resolved.name} folder"
    return speakable(resolved) or str(resolved)


def expand_inputs(values: Iterable[str]) -> tuple[list[Path], list[str]]:
    """Expand ~, $VARS and globs into concrete existing paths.

    Returns (found, missing). Glob expansion belongs in resolve(): a `*` that
    silently matched nothing, or matched forty files instead of four, must be
    visible in the confirmation.
    """
    found: list[Path] = []
    missing: list[str] = []
    seen: set[Path] = set()
    for raw in values:
        text = str(raw).strip()
        if not text:
            continue
        candidate = expand(text)
        if any(ch in text for ch in "*?[") and not candidate.exists():
            matches = [Path(m) for m in sorted(globlib.glob(str(candidate)))]
            if not matches:
                missing.append(text)
            for match in matches:
                if match not in seen:
                    seen.add(match)
                    found.append(match)
            continue
        if candidate.exists() or candidate.is_symlink():
            if candidate not in seen:
                seen.add(candidate)
                found.append(candidate)
        else:
            missing.append(text)
    return found, missing


GLOB_CHARS = "*?["

# A folder is counted, not walked forever: a spoken confirmation that waits on
# a 400k-file tree is worse than one that says "more than 20,000 items".
COUNT_BUDGET = 20_000


def has_glob(values: Iterable[str]) -> bool:
    """True when the user gave a PATTERN rather than naming files.

    A pattern is the resolver guessing at which files were meant, which is
    exactly what `ResolvedAction.explicit=False` is for.
    """
    return any(ch in str(v) for v in values for ch in GLOB_CHARS)


def count_inside(path: Path, budget: int = COUNT_BUDGET) -> tuple[int, bool]:
    """(items inside this folder, hit_the_budget). 0 for a file or a symlink.

    "myapp" read back as one target while 401 things go in the Trash is a
    confirmation the user cannot possibly have meant to give.
    """
    if path.is_symlink() or not path.is_dir():
        return 0, False
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    total += 1
                    if total >= budget:
                        return total, True
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                    except OSError:
                        continue
        except OSError:
            continue  # an unreadable subfolder costs us that subfolder
    return total, False


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, os.PathLike)):
        return [str(value)]
    return [str(v) for v in value]


def _stat_row(path: Path) -> dict[str, Any]:
    row: dict[str, Any] = {"path": str(path), "name": path.name, "is_dir": path.is_dir()}
    try:
        st = path.stat()
        row["size"] = st.st_size
        row["modified"] = datetime.fromtimestamp(st.st_mtime, tz=UTC).isoformat()
        row["modified_ts"] = st.st_mtime
    except OSError:
        row["size"] = None
        row["modified_ts"] = 0.0
    return row


def _count_phrase(n: int, singular: str, plural: str | None = None) -> str:
    plural = plural or singular + "s"
    return f"{n} {singular if n == 1 else plural}"


# ---------------------------------------------------------------------------
# spotlight_search
# ---------------------------------------------------------------------------


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def build_mdfind_query(
    text: str | None = None,
    *,
    kind: str | None = None,
    when: str | None = None,
    within_days: int | None = None,
    extension: str | None = None,
    name_only: bool = False,
) -> str:
    clauses: list[str] = []
    if text:
        needle = _escape(text)
        if name_only:
            clauses.append(f'kMDItemFSName == "*{needle}*"cd')
        else:
            clauses.append(
                f'(kMDItemDisplayName == "*{needle}*"cd || kMDItemTextContent == "*{needle}*"cd)'
            )
    if extension:
        ext = _escape(extension.lstrip("."))
        clauses.append(f'kMDItemFSName == "*.{ext}"cd')
    if kind:
        key = kind.strip().lower()
        if key in KIND_QUERIES:
            clauses.append(KIND_QUERIES[key])
        else:
            clauses.append(f'kMDItemKind == "*{_escape(kind)}*"cd')
    if within_days is not None:
        days = max(0, int(within_days))
        clauses.append(f"kMDItemContentModificationDate >= $time.today(-{days})")
    elif when:
        key = " ".join(str(when).strip().lower().split())
        if key in WHEN_QUERIES:
            clauses.append(WHEN_QUERIES[key])
        elif key.startswith("last ") and key.endswith((" days", " day")):
            try:
                days = int(key.split()[1])
                clauses.append(f"kMDItemContentModificationDate >= $time.today(-{days})")
            except (IndexError, ValueError):
                pass
    if not clauses:
        # mdfind with an empty query returns the entire index; refuse instead.
        clauses.append('kMDItemFSName == "*"cd')
    return " && ".join(clauses)


def _describe_query(
    text: str | None, kind: str | None, when: str | None, within_days: int | None, scopes: list[Path]
) -> str:
    parts: list[str] = []
    parts.append(f"{kind}s" if kind else "files")
    if text:
        parts.append(f"matching {text}")
    if within_days is not None:
        parts.append("from today" if within_days == 0 else f"from the last {within_days} days")
    elif when:
        parts.append(f"from {when}")
    if scopes:
        parts.append("in " + ", ".join(p.name or str(p) for p in scopes))
    return " ".join(parts)


class SpotlightSearch(ShellTool):
    binary = MDFIND
    timeout_s = 15.0
    verb = "search for"

    spec = spec(
        "spotlight_search",
        "Find files with Spotlight, optionally scoped to a folder, kind and date range.",
        {
            "text": param("string", "Words to look for in the name or contents"),
            "scope": param("array", "Folders to search inside, e.g. ~/Downloads"),
            "kind": param("string", "image, screenshot, pdf, document, audio, video, folder..."),
            "when": param("string", "today, yesterday, this week, last week, this month"),
            "within_days": param("integer", "Modified within this many days"),
            "extension": param("string", "File extension filter, e.g. png"),
            "name_only": param("boolean", "Match filenames only, not contents"),
            "limit": param("integer", "Maximum results to return"),
        },
        floor=RiskTier.SILENT,
        activation_hint="""
            find locate search look for where is that file document pdf screenshot photo
            invoice spreadsheet download I saved earlier. Handles time-scoped asks --
            the screenshots from today, files I changed yesterday, what did I download
            this week, anything from last month. Searches filenames and file contents
            across the whole disk or inside one folder. Read-only; returns paths for a
            later move, reveal or trash.
        """,
        tags=("files", "search", "read"),
        # Reading the index changes nothing, so there is nothing to invert.
        inverses=(),
    )

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        text = kwargs.get("text") or kwargs.get("query") or None
        kind = kwargs.get("kind") or None
        when = kwargs.get("when") or None
        within_days = kwargs.get("within_days")
        within_days = int(within_days) if within_days is not None else None
        extension = kwargs.get("extension") or None
        name_only = bool(kwargs.get("name_only", False))
        limit = int(kwargs.get("limit") or 50)

        scopes = [expand(s) for s in _as_list(kwargs.get("scope") or kwargs.get("scopes"))]
        scopes = [s for s in scopes if s.is_dir()]

        query = build_mdfind_query(
            text, kind=kind, when=when, within_days=within_days,
            extension=extension, name_only=name_only,
        )
        argv = [self.binary]
        for scope in scopes:
            argv += ["-onlyin", str(scope)]
        argv.append(query)

        # A search with no needle ("the screenshots from today") resolves to a
        # CLASS of files the user never named one by one. Whatever comes back
        # is the resolver's inference, and a later trash of those results must
        # inherit that -- so we never let a caller-supplied flag raise a guess
        # to explicit, only lower it.
        named_the_files = bool(text)
        return self.action(
            targets=[_describe_query(text, kind, when, within_days, scopes)],
            explicit=bool(kwargs.get("explicit", True)) and named_the_files,
            argv=argv, query=query, limit=limit,
            scopes=[str(s) for s in scopes], kind=kind, text=text,
        )

    def run(self, action: ResolvedAction) -> ToolResult:
        argv = list(action.args.get("argv") or [])
        if not argv:
            return self.failed("I could not build that search.", "missing argv")
        # Read-only, so it runs under dry_run too: resolve() for a later move or
        # trash depends on real search output, not a placeholder.
        result = self.sh(argv, mutating=False)
        if not result.ok:
            return self.failed("Spotlight could not run that search.", result.failure_reason)

        limit = int(action.args.get("limit") or 50)
        rows = [_stat_row(Path(line)) for line in result.lines() if Path(line).exists()]
        rows.sort(key=lambda r: r.get("modified_ts") or 0.0, reverse=True)
        truncated = len(rows) > limit
        rows = rows[:limit]

        if not rows:
            return ToolResult(ok=True, summary="I did not find anything.", data={"results": [], "count": 0})
        first = rows[0]["name"]
        if len(rows) == 1:
            summary = f"I found one file, {first}."
        else:
            more = "at least " if truncated else ""
            summary = f"I found {more}{len(rows)} files, the newest is {first}."
        return ToolResult(
            ok=True,
            summary=summary,
            data={
                "results": rows,
                "paths": [r["path"] for r in rows],
                "count": len(rows),
                "truncated": truncated,
            },
        )


# ---------------------------------------------------------------------------
# reveal_in_finder
# ---------------------------------------------------------------------------


class RevealInFinder(ShellTool):
    binary = OPEN
    verb = "show you"

    spec = spec(
        "reveal_in_finder",
        "Show files or folders in a Finder window without opening them.",
        {"paths": param("array", "Paths to reveal", required=True)},
        floor=RiskTier.SILENT,
        activation_hint="""
            show me reveal where that file lives, open the folder containing it, bring it
            up in finder, take me to it, show in finder, open enclosing folder, point at
            it. Selects the item in Finder instead of opening the document. Use right
            after a search when the user wants to see the file rather than act on it.
        """,
        tags=("files", "finder", "read"),
        # Nothing moves, so nothing needs putting back.
        inverses=(),
    )

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        raw = _as_list(kwargs.get("paths") or kwargs.get("path"))
        found, missing = expand_inputs(raw)
        return self.action(
            targets=[speakable(p) for p in found],
            # A glob picked these files, not the user.
            explicit=bool(kwargs.get("explicit", True)) and not has_glob(raw),
            paths=[str(p) for p in found],
            missing=missing,
        )

    def run(self, action: ResolvedAction) -> ToolResult:
        paths = [str(p) for p in action.args.get("paths") or []]
        if not paths:
            return self.failed(
                "I could not find those files.", "nothing to reveal",
                missing=action.args.get("missing", []),
            )
        # Revealing changes nothing on disk, so it is not gated by dry_run.
        result = self.sh([self.binary, "-R", *paths], mutating=False)
        if not result.ok:
            return self.failed("Finder would not open that.", result.failure_reason)
        label = speakable(paths[0]) if len(paths) == 1 else _count_phrase(len(paths), "file")
        return ToolResult(ok=True, summary=f"Showing {label} in Finder.", data={"paths": paths})


# ---------------------------------------------------------------------------
# move_to_trash
# ---------------------------------------------------------------------------


def _trash_via_appkit(path: Path) -> tuple[bool, str | None, str]:
    """Returns (ok, resulting_trash_path, error)."""
    try:
        from Foundation import NSURL, NSFileManager  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return False, None, f"pyobjc unavailable: {exc}"
    try:
        url = NSURL.fileURLWithPath_(str(path))
        ok, resulting, err = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
            url, None, None
        )
        if not ok:
            return False, None, str(err.localizedDescription()) if err else "trashItemAtURL failed"
        # The resulting URL is what makes undo exact: the Trash renames on
        # collision, so the original basename is not enough to find it again.
        return True, (str(resulting.path()) if resulting else None), ""
    except Exception as exc:  # noqa: BLE001
        return False, None, str(exc)


def _trash_via_finder(path: Path, *, dry_run: bool = False) -> tuple[bool, str | None, str]:
    """Fallback when pyobjc is unavailable. Finder, never `rm`."""
    script = (
        'on run argv\n'
        '  set p to POSIX file (item 1 of argv)\n'
        '  tell application "Finder" to set t to (delete p)\n'
        '  return POSIX path of (t as alias)\n'
        'end run'
    )
    result = run_argv(
        [OSASCRIPT, "-", str(path)],
        timeout=20.0,
        input_text=script,
        mutating=True,
        dry_run=dry_run,
    )
    if result.skipped:
        return False, None, "dry run"
    if not result.ok:
        return False, None, result.failure_reason
    trashed = result.stdout.strip()
    return True, (trashed or None), ""


def trash(path: Path, *, dry_run: bool = False) -> tuple[bool, str | None, str]:
    """Move one item to the Trash and say where it landed.

    The resulting path is the whole point: the Trash renames on collision, so
    without it "put it back" is a guess. Used by move_to_trash AND by
    move_files, which displaces whatever it is about to overwrite rather than
    destroying it -- see MoveFiles.run.
    """
    ok, trashed, err = _trash_via_appkit(path)
    if ok:
        return ok, trashed, err
    return _trash_via_finder(path, dry_run=dry_run)


def _inside_counts(paths: list[Path]) -> tuple[dict[str, int], int, bool]:
    """(per-folder counts, total items that will actually move, truncated)."""
    counts: dict[str, int] = {}
    truncated = False
    total = 0
    for path in paths:
        inside, hit_budget = count_inside(path)
        truncated = truncated or hit_budget
        if inside:
            counts[str(path)] = inside
        total += 1 + inside
    return counts, total, truncated


class MoveToTrash(ShellTool):
    binary = OSASCRIPT
    mutates = True
    verb = "move to the Trash"

    spec = spec(
        "move_to_trash",
        "Move files or folders to the Trash so they can be put back.",
        {"paths": param("array", "Paths to trash", required=True)},
        floor=RiskTier.CONFIRM_VOICE,
        activation_hint="""
            delete remove get rid of throw away bin trash chuck these files, clear out the
            old downloads, clean up the screenshots on my desktop, junk that folder. Moves
            items to the Trash -- recoverable, never an erase. Use for any request to
            delete a file, folder, download or screenshot. Not for emptying the Trash and
            not for uninstalling apps.
        """,
        tags=("files", "destructive", "undoable"),
        # Putting something back is a move, and move_files is the only tool
        # that may perform it.
        inverses=("move_files",),
    )

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        raw = _as_list(kwargs.get("paths") or kwargs.get("path"))
        found, missing = expand_inputs(raw)
        protected: list[str] = []
        safe: list[Path] = []
        for path in found:
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if resolved in PROTECTED or resolved.parent == resolved:
                protected.append(str(path))
            else:
                safe.append(path)
        rows = [_stat_row(p) for p in safe]
        total = sum(r["size"] or 0 for r in rows)
        # A folder is ONE spoken target and hundreds of trashed items. Both the
        # sentence the user answers and the count Jev scores its blast radius
        # against have to know that.
        counts, item_count, truncated = _inside_counts(safe)

        consequences: dict[str, str] = {}
        if counts:
            inside = sum(counts.values())
            about = "more than " if truncated else ""
            where = (
                speakable(next(iter(counts)))
                if len(counts) == 1
                else _count_phrase(len(counts), "folder")
            )
            consequences["contents"] = (
                f"including {about}{_count_phrase(inside, 'item')} inside {where}"
            )
        if protected:
            # resolve() dropped these, but run() refuses outright when any were
            # named -- so a yes here buys the user nothing at all. Say that,
            # rather than let them consent to something that will not happen.
            consequences["protected"] = (
                f"but {protected_label(protected[0])} is protected, so nothing will be moved"
            )

        return self.action(
            targets=[speakable(p) for p in safe],
            # A glob chose these files; the user named a pattern.
            explicit=bool(kwargs.get("explicit", True)) and not has_glob(raw),
            consequences=consequences,
            paths=[str(p) for p in safe],
            items=rows,
            missing=missing,
            protected=protected,
            total_bytes=total,
            # What Jev sees. len(targets) says 1 for a folder; this does not.
            item_count=item_count,
            items_inside=counts,
            count_truncated=truncated,
        )

    def run(self, action: ResolvedAction) -> ToolResult:
        paths = [Path(p) for p in action.args.get("paths") or []]
        protected = list(action.args.get("protected") or [])
        if protected:
            return self.failed(
                "I will not put that folder in the Trash.",
                f"protected path: {protected[0]}", protected=protected,
            )
        if not paths:
            return self.failed(
                "I could not find those files.", "nothing to trash",
                missing=action.args.get("missing", []),
            )
        if self.dry_run:
            return self.dry(
                f"I would move {_count_phrase(len(paths), 'item')} to the Trash.",
                paths=[str(p) for p in paths],
            )

        moved: list[tuple[str, str]] = []   # (trash_path, original_path)
        failures: list[dict[str, str]] = []
        for path in paths:
            ok, trashed, err = trash(path, dry_run=self.dry_run)
            if ok and trashed:
                moved.append((trashed, str(path)))
            elif ok:
                # Trashed but unlocatable: better to say so than to hand back an
                # undo that would fail when the user takes us up on it.
                failures.append({"path": str(path), "error": "trashed but could not be located"})
            else:
                failures.append({"path": str(path), "error": err})

        undo = (
            UndoAction(
                description=f"put {_count_phrase(len(moved), 'item')} back where they were",
                tool="move_files",
                args={"pairs": [[trashed, original] for trashed, original in moved]},
            )
            if moved
            else None
        )
        data = {"trashed": [o for _, o in moved], "failures": failures, "count": len(moved)}
        if failures and not moved:
            return ToolResult(
                ok=False, summary="I could not move those to the Trash.",
                data=data, error=failures[0]["error"],
            )
        label = speakable(moved[0][1]) if len(moved) == 1 else _count_phrase(len(moved), "item")
        if failures:
            return ToolResult(
                ok=False,
                summary=f"I trashed {label} but {len(failures)} would not move.",
                data=data, undo=undo, error=failures[0]["error"],
            )
        return ToolResult(ok=True, summary=f"Moved {label} to the Trash.", data=data, undo=undo)


# ---------------------------------------------------------------------------
# move_files
# ---------------------------------------------------------------------------


def _plan_pairs(kwargs: dict[str, Any]) -> tuple[list[list[str]], list[str], list[str]]:
    """Normalise both call shapes into (pairs, missing, collisions).

    `pairs` is the canonical form precisely because it is its own inverse:
    move_to_trash's undo and move_files' undo are both just a reversed pair list.
    """
    explicit_pairs = kwargs.get("pairs")
    missing: list[str] = []
    pairs: list[list[str]] = []

    if explicit_pairs:
        for entry in explicit_pairs:
            src_raw, dst_raw = (entry[0], entry[1]) if not isinstance(entry, dict) else (
                entry["source"], entry["destination"]
            )
            src = expand(src_raw)
            dst = expand(dst_raw)
            if not (src.exists() or src.is_symlink()):
                missing.append(str(src_raw))
                continue
            if dst.is_dir():
                dst = dst / src.name
            pairs.append([str(src), str(dst)])
    else:
        sources, missing = expand_inputs(_as_list(kwargs.get("sources") or kwargs.get("source")))
        destination = kwargs.get("destination") or kwargs.get("to")
        if destination is None:
            return [], missing, []
        dest = expand(destination)
        if len(sources) == 1 and not dest.is_dir() and dest.suffix:
            pairs.append([str(sources[0]), str(dest)])   # a rename, not a move
        else:
            for src in sources:
                pairs.append([str(src), str(dest / src.name)])

    collisions = [dst for _, dst in pairs if Path(dst).exists()]
    return pairs, missing, collisions


class MoveFiles(BaseTool):
    mutates = True
    verb = "move"

    spec = spec(
        "move_files",
        "Move or rename files and folders, and put trashed items back.",
        {
            "sources": param("array", "Paths to move"),
            "destination": param("string", "Destination folder, or new path for a rename"),
            "pairs": param("array", "Explicit [source, destination] pairs"),
            "overwrite": param("boolean", "Replace an existing file at the destination"),
            "create_destination": param("boolean", "Create the destination folder if absent"),
        },
        floor=RiskTier.CONFIRM_VOICE,
        activation_hint="""
            move put file these into that folder, rename this to, drag them over to
            desktop documents downloads, tidy sort organise my downloads into folders,
            stick the invoices in the invoices folder, put it back where it was, restore
            undo a delete. Also renames a single file; it is the undo for trashing too.
            Reversible, but only because anything it would replace is put in the Trash
            first rather than destroyed. Not for copying or duplicating.
        """,
        tags=("files", "destructive", "undoable"),
        # Its own inverse: every undo it hands back is a reversed pair list.
        inverses=("move_files",),
    )

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        pairs, missing, collisions = _plan_pairs(dict(kwargs))
        targets = [
            f"{speakable(src)} to {Path(dst).parent.name or Path(dst).parent}"
            for src, dst in pairs
        ]
        overwrite = bool(kwargs.get("overwrite", False))

        # Collisions were already computed here and then dropped on the floor,
        # so "move report.pdf to Documents" read back identically whether or not
        # it was about to land on top of another report.pdf.
        consequences: dict[str, str] = {}
        if collisions and overwrite:
            n = len(collisions)
            it = "it" if n == 1 else "them"
            consequences["overwrite"] = (
                f"replacing {_count_phrase(n, 'file')} already there, which I will put "
                f"in the Trash first so you can get {it} back"
            )
        elif collisions:
            consequences["blocked"] = (
                f"but something is already called {speakable(collisions[0])} there, "
                "so nothing will move"
            )

        raw_sources = _as_list(kwargs.get("sources") or kwargs.get("source"))
        return self.action(
            targets=targets,
            # A glob chose these files; the user named a pattern.
            explicit=bool(kwargs.get("explicit", True)) and not has_glob(raw_sources),
            consequences=consequences,
            pairs=pairs,
            missing=missing,
            collisions=collisions,
            overwrite=overwrite,
            create_destination=bool(kwargs.get("create_destination", True)),
        )

    def run(self, action: ResolvedAction) -> ToolResult:
        pairs = [list(p) for p in action.args.get("pairs") or []]
        if not pairs:
            return self.failed(
                "I could not find anything to move.", "no source files resolved",
                missing=action.args.get("missing", []),
            )
        overwrite = bool(action.args.get("overwrite", False))
        collisions = [dst for _, dst in pairs if Path(dst).exists()]
        if collisions and not overwrite:
            return self.failed(
                f"Something is already called {speakable(collisions[0])} there.",
                "destination exists", collisions=collisions,
            )
        if self.dry_run:
            return self.dry(
                f"I would move {_count_phrase(len(pairs), 'item')}.", pairs=pairs
            )

        done: list[list[str]] = []
        # (path inside the Trash, the destination it was displaced from)
        replaced: list[list[str]] = []
        failures: list[dict[str, str]] = []
        for src, dst in pairs:
            try:
                target = Path(dst)
                if overwrite and (target.exists() or target.is_symlink()):
                    # `shutil.move` onto an existing file destroys it, and no
                    # undo can bring it back -- so the thing being replaced goes
                    # to the Trash first. If it cannot be put somewhere
                    # recoverable, this pair does not move at all: refusing one
                    # move is survivable, an unrecoverable overwrite is not.
                    ok, trashed, err = trash(target, dry_run=self.dry_run)
                    if not ok or not trashed:
                        failures.append({
                            "path": dst,
                            "error": f"could not put the file already there in the Trash: {err}",
                        })
                        continue
                    replaced.append([trashed, dst])
                if action.args.get("create_destination", True):
                    target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(src, dst)
                done.append([src, dst])
            except (OSError, shutil.Error) as exc:
                failures.append({"path": src, "error": str(exc)})

        # The inverse is produced even on partial failure -- what moved is
        # exactly what must be movable back. Displaced files are restored AFTER
        # the moved ones have vacated, which is why the order of `pairs` matters.
        undone_pairs = [[dst, src] for src, dst in done] + [
            [trashed, dst] for trashed, dst in replaced
        ]
        undo = (
            UndoAction(
                description=(
                    f"move {_count_phrase(len(done), 'item')} back"
                    + (
                        f" and put {_count_phrase(len(replaced), 'replaced file')} back"
                        if replaced
                        else ""
                    )
                ),
                tool="move_files",
                args={"pairs": undone_pairs, "overwrite": True},
            )
            if done
            else None
        )
        data = {
            "moved": done,
            "replaced": replaced,
            "failures": failures,
            "count": len(done),
        }
        if not done:
            return ToolResult(
                ok=False, summary="I could not move those.", data=data,
                error=failures[0]["error"] if failures else "nothing moved",
            )
        where = Path(done[0][1]).parent.name or "there"
        label = speakable(done[0][0]) if len(done) == 1 else _count_phrase(len(done), "item")
        if failures:
            return ToolResult(
                ok=False, summary=f"I moved {label} but {len(failures)} would not go.",
                data=data, undo=undo, error=failures[0]["error"],
            )
        return ToolResult(ok=True, summary=f"Moved {label} to {where}.", data=data, undo=undo)


__all__ = [
    "COUNT_BUDGET",
    "KIND_QUERIES",
    "PROTECTED",
    "WHEN_QUERIES",
    "MoveFiles",
    "MoveToTrash",
    "RevealInFinder",
    "SpotlightSearch",
    "build_mdfind_query",
    "count_inside",
    "expand",
    "expand_inputs",
    "has_glob",
    "protected_label",
    "speakable",
    "trash",
]
