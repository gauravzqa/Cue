"""The seam between the tools and a real browser.

Every tool in this package talks to a `PageHandle` and a `BrowserBackend`, never
to Playwright. Three things fall out of that, and the third is the reason:

1. The whole safety surface -- readback, fingerprint, escalation, refusal --
   is testable with no browser, no profile, no Chrome and no network.
2. Swapping Playwright for something else is one file.
3. **A capability can be removed by removing a method.** `PageHandle` is the
   only thing `run()` is handed, and a read-only tool is handed a
   `ReadOnlyPage`, which has no `click`, no `fill` and no `submit` -- so a
   prompt injection on page 2 saying "now click Pay" reaches code that has no
   `click`. That is enforced by the type, not by asking a model nicely.

The `page.evaluate` snippet below is the one piece of JavaScript this package
runs, it is a constant rather than model-authored, and it is reviewed for one
property above all others: **it never returns the value of any input,
textarea or contenteditable.** It returns whether a field is empty, never what
is in it. Same for cookies: names only, never values.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# The vendored JavaScript
# ---------------------------------------------------------------------------

# Per-element facts. Mirrors the schema in facts.facts_from_raw().
#
# NEVER add a field that returns `el.value`, `localStorage`, `document.cookie`
# or `innerHTML` to this snippet. `value_empty` is the only thing this package
# is allowed to know about what is typed into a page.
FACTS_JS = r"""
(args) => {
  const CURRENCY = /(?:[$£€¥]\s?\d[\d.,]*(?:\.\d{2})?|\d[\d.,]*\s?(?:USD|GBP|EUR|CAD|AUD))/;
  const limit = args.limit || 8;
  const nodes = Array.from(document.querySelectorAll(args.selector)).slice(0, limit);

  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none') return false;
    if (Number(st.opacity || '1') <= 0.01) return false;
    if (el.closest('[aria-hidden="true"]')) return false;
    return r.width > 0 && r.height > 0;
  };

  const isEnabled = (el) => !(el.disabled || el.getAttribute('aria-disabled') === 'true');

  // The accessible name, as a FALLBACK only: the authoritative value comes
  // from the browser's own computation via Locator.aria_snapshot() on the
  // Python side. `value` is consulted for submit/button/reset inputs ONLY,
  // where the value attribute IS the visible label the site authored -- never
  // for a text field, where it is whatever the user typed.
  const nameOf = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria && aria.trim()) return aria.trim();
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const parts = by.split(/\s+/).map(id => document.getElementById(id))
        .filter(Boolean).map(n => (n.textContent || '').trim()).filter(Boolean);
      if (parts.length) return parts.join(' ');
    }
    const tag = el.tagName;
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'INPUT' && ['submit', 'button', 'reset'].includes(type)) {
      const v = (el.getAttribute('value') || '').trim();
      if (v) return v;
    }
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag)) {
      if (el.labels && el.labels.length) {
        const t = Array.from(el.labels).map(l => (l.textContent || '').trim())
          .filter(Boolean).join(' ');
        if (t) return t;
      }
      const ph = (el.getAttribute('placeholder') || '').trim();
      if (ph) return ph;
    } else {
      const t = (el.textContent || '').replace(/\s+/g, ' ').trim();
      if (t) return t.slice(0, 120);
    }
    const title = (el.getAttribute('title') || '').trim();
    if (title) return title;
    const alt = (el.getAttribute('alt') || '').trim();
    if (alt) return alt;
    return '';
  };

  const roleOf = (el) => {
    const explicit = (el.getAttribute('role') || '').trim().toLowerCase();
    if (explicit) return explicit;
    const tag = el.tagName;
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'A') return el.hasAttribute('href') ? 'link' : '';
    if (tag === 'BUTTON') return 'button';
    if (tag === 'SELECT') return 'combobox';
    if (tag === 'TEXTAREA') return 'textbox';
    if (tag === 'INPUT') {
      if (['submit', 'button', 'reset', 'image'].includes(type)) return 'button';
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (type === 'search') return 'searchbox';
      return 'textbox';
    }
    return '';
  };

  const submitsFor = (el) => {
    const tag = el.tagName;
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'BUTTON') return type === '' || type === 'submit';
    if (tag === 'INPUT') return type === 'submit' || type === 'image';
    return false;
  };

  const formOf = (form) => {
    const els = Array.from(form.elements || []);
    return {
      action: form.getAttribute('action') ? form.action : location.href,
      method: (form.getAttribute('method') || 'get').toLowerCase(),
      field_names: els.map(e => e.getAttribute('name') || ''),
      field_types: els.map(e => ((e.getAttribute('type') || e.tagName) || '').toLowerCase()),
      autocomplete_hints: els.map(e => (e.getAttribute('autocomplete') || '')),
      field_count: els.length
    };
  };

  const amountNear = (el, form, dialog) => {
    const scope = dialog || form || el;
    const text = (scope.innerText || scope.textContent || '').replace(/\s+/g, ' ');
    const m = CURRENCY.exec(text);
    return m ? m[0].trim() : '';
  };

  const emptiness = (el) => {
    const tag = el.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA') return !(el.value || '').length;
    if (el.isContentEditable) return !((el.textContent || '').trim().length);
    return null;
  };

  return nodes.map((el, index) => {
    const r = el.getBoundingClientRect();
    const form = el.form || el.closest('form');
    const dialog = el.closest('[role="dialog"], [role="alertdialog"], dialog');
    const type = (el.getAttribute('type') || '').toLowerCase();
    return {
      index: index,
      tag: el.tagName,
      role: roleOf(el),
      name: nameOf(el),
      input_type: type,
      autocomplete: el.getAttribute('autocomplete') || '',
      visible: isVisible(el),
      enabled: isEnabled(el),
      in_dialog: !!dialog,
      checked: (typeof el.checked === 'boolean') ? el.checked : null,
      value_empty: emptiness(el),
      box: [Math.round(r.left + window.scrollX), Math.round(r.top + window.scrollY),
            Math.round(r.width), Math.round(r.height)],
      href: el.tagName === 'A' ? (el.href || '') : '',
      target: el.getAttribute('target') || '',
      download: el.hasAttribute('download'),
      submits: !!form && submitsFor(el),
      form: form ? formOf(form) : null,
      nearby_amount: amountNear(el, form, dialog)
    };
  });
}
"""

# Text extraction. ONE function, behind ONE call site, exactly so that swapping
# it for vendored Mozilla Readability 0.6.0 (~88 KB, Apache-2.0) or for
# `readability-lxml` over `page.content()` stays a ~20-line change. This
# version drops the chrome -- nav, header, footer, aside, script, style, forms
# -- which is the thing that makes a spoken summary sound like a screen reader,
# and does not try to be a content-extraction benchmark winner.
EXTRACT_JS = r"""
() => {
  const DROP = 'script,style,noscript,nav,header,footer,aside,form,svg,iframe,' +
               '[role="navigation"],[role="banner"],[role="contentinfo"],[aria-hidden="true"]';
  const article = document.querySelector('article, main, [role="main"]') || document.body;
  const clone = article.cloneNode(true);
  clone.querySelectorAll(DROP).forEach(n => n.remove());
  const text = (clone.innerText || clone.textContent || '')
    .split('\n').map(l => l.trim()).filter(Boolean).join('\n');
  const headings = Array.from(document.querySelectorAll('h1,h2,h3'))
    .map(h => (h.textContent || '').replace(/\s+/g, ' ').trim())
    .filter(Boolean).slice(0, 40);
  return {
    title: document.title || '',
    text: text,
    headings: headings,
    chars: text.length,
    words: text ? text.split(/\s+/).length : 0,
    links: document.querySelectorAll('a[href]').length
  };
}
"""

FIND_JS = r"""
(args) => {
  const needle = (args.needle || '').toLowerCase();
  if (!needle) return {count: 0, matches: []};
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const matches = [];
  let count = 0;
  let node;
  while ((node = walker.nextNode())) {
    const parent = node.parentElement;
    if (!parent || ['SCRIPT', 'STYLE', 'NOSCRIPT'].includes(parent.tagName)) continue;
    const text = (node.nodeValue || '').replace(/\s+/g, ' ');
    let from = text.toLowerCase().indexOf(needle);
    while (from !== -1) {
      count += 1;
      if (matches.length < (args.limit || 10)) {
        matches.push({
          snippet: text.slice(Math.max(0, from - 60), from + needle.length + 60).trim(),
          heading: (parent.closest('section,article,div') || {}).id || ''
        });
      }
      from = text.toLowerCase().indexOf(needle, from + needle.length);
    }
  }
  return {count: count, matches: matches};
}
"""

SCROLL_JS = r"""
(args) => {
  const step = args.amount === 'page' ? window.innerHeight * 0.9
             : args.amount === 'all' ? document.body.scrollHeight
             : window.innerHeight * 0.4;
  const dir = args.direction === 'up' ? -1 : 1;
  if (args.direction === 'top') window.scrollTo(0, 0);
  else if (args.direction === 'bottom') window.scrollTo(0, document.body.scrollHeight);
  else window.scrollBy(0, dir * step);
  const y = window.scrollY;
  const height = Math.max(1, document.body.scrollHeight - window.innerHeight);
  return {
    y: Math.round(y),
    fraction: Math.min(1, Math.max(0, y / height)),
    at_top: y <= 2,
    at_bottom: y >= height - 2
  };
}
"""

# Interactive controls, for ranking an ambiguous target and for nothing else.
# Names and roles only; no hrefs, no values, no text content beyond the name.
CONTROLS_JS = r"""
(args) => {
  const sel = 'a[href],button,input,select,textarea,[role="button"],[role="link"],' +
              '[role="checkbox"],[role="switch"],[role="tab"],[role="menuitem"]';
  const esc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : s;
  // A CSS path, so that a control found by NAME can be handed to the acting
  // tools without anybody inventing an ordinal like "the third button". The
  // path is only a handle: identity is re-established from the live DOM via
  // the fingerprint, so a path that goes stale aborts rather than misfires.
  const cssPath = (el) => {
    if (el.id && document.querySelectorAll('#' + esc(el.id)).length === 1) {
      return '#' + esc(el.id);
    }
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 8) {
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (node.id) { parts.unshift('#' + esc(node.id)); break; }
      if (!parent) { parts.unshift(part); break; }
      const sibs = Array.from(parent.children).filter(c => c.tagName === node.tagName);
      if (sibs.length > 1) part += ':nth-of-type(' + (sibs.indexOf(node) + 1) + ')';
      parts.unshift(part);
      node = parent;
    }
    return parts.join(' > ');
  };
  return Array.from(document.querySelectorAll(sel)).slice(0, args.limit || 60).map((el, i) => {
    const aria = (el.getAttribute('aria-label') || '').trim();
    let name = aria;
    if (!name) {
      const tag = el.tagName;
      const type = (el.getAttribute('type') || '').toLowerCase();
      if (tag === 'INPUT' && ['submit', 'button', 'reset'].includes(type)) {
        name = (el.getAttribute('value') || '').trim();
      } else if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag)) {
        name = (el.getAttribute('placeholder') || '').trim();
        if (!name && el.labels && el.labels.length) {
          name = (el.labels[0].textContent || '').trim();
        }
      } else {
        name = (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 80);
      }
    }
    return {
      index: i,
      name: name,
      role: (el.getAttribute('role') || el.tagName).toLowerCase(),
      selector: cssPath(el)
    };
  }).filter(c => c.name);
}
"""


# ---------------------------------------------------------------------------
# The protocols
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TabInfo:
    id: str
    title: str
    safe_url: str          # scheme://host/path -- ALREADY normalised for logs
    host: str
    active: bool = False


@runtime_checkable
class ReadOnlyPage(Protocol):
    """Everything a reading tool may do. Mechanically incapable of acting.

    There is no `click`, no `fill`, no `submit`, no `evaluate` and no `goto`
    here, and that absence is the point.
    """

    @property
    def id(self) -> str: ...

    def url(self) -> str: ...

    def title(self) -> str: ...

    def element_facts(self, selector: str, *, limit: int = 8) -> list[dict[str, Any]]: ...

    def controls(self, *, limit: int = 60) -> list[dict[str, Any]]: ...

    def extract(self) -> dict[str, Any]: ...

    def find(self, needle: str, *, limit: int = 10) -> dict[str, Any]: ...

    def cookie_names(self) -> list[str]: ...

    def has_session(self) -> bool: ...

    def pending_dialog(self) -> str: ...


@runtime_checkable
class PageHandle(ReadOnlyPage, Protocol):
    """A page a tool may also act on. Handed only to the acting tools."""

    def scroll(self, direction: str, amount: str) -> dict[str, Any]: ...

    def click(self, selector: str) -> None: ...

    def fill(self, selector: str, value: str) -> None: ...

    def submit(self, selector: str) -> None: ...

    def go_back(self) -> bool: ...


@runtime_checkable
class BrowserBackend(Protocol):
    def tabs(self) -> Sequence[TabInfo]: ...

    def page(self, tab_id: str | None = None) -> PageHandle | None: ...

    def open_tab(self, url: str) -> PageHandle: ...

    def close_tab(self, tab_id: str) -> bool: ...


# ---------------------------------------------------------------------------
# Session detection, from cookie NAMES
# ---------------------------------------------------------------------------

# Names that mean "there is a login here". Matched on the lowercased name only
# -- a cookie's VALUE is never read, never returned and never logged.
_SESSION_NAME_MARKERS = (
    "session", "sess", "sid", "auth", "login", "logged", "token", "jwt",
    "identity", "account", "user", "remember", "oauth", "passport",
)
# httpOnly is a strong signal for a server-set session cookie, but analytics
# vendors set httpOnly cookies too, so the obvious ones are excluded by name.
_ANALYTICS_PREFIXES = ("_ga", "_gid", "_gcl", "_fbp", "_fbc", "__utm", "_hj", "ajs_", "amp_")


def looks_signed_in(cookies: Sequence[Mapping[str, Any]]) -> bool:
    """Heuristic, and deliberately biased toward True.

    A false "you are signed in" adds a clause to the readback and raises the
    floor; a false "you are not" removes the one fact that changes what a yes
    means. So the asymmetry is on purpose, and it is the safe direction.
    """
    for cookie in cookies:
        name = str(cookie.get("name") or "").lower()
        if not name or name.startswith(_ANALYTICS_PREFIXES):
            continue
        if any(marker in name for marker in _SESSION_NAME_MARKERS):
            return True
        if cookie.get("httpOnly") and cookie.get("secure"):
            return True
    return False


__all__ = [
    "CONTROLS_JS",
    "EXTRACT_JS",
    "FACTS_JS",
    "FIND_JS",
    "SCROLL_JS",
    "BrowserBackend",
    "PageHandle",
    "ReadOnlyPage",
    "TabInfo",
    "looks_signed_in",
]
