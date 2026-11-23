#!/usr/bin/env python3
"""
Site monitor: watches web pages, WordPress REST endpoints, sitemaps and RSS feeds,
and sends a Discord message when something changes.

    python monitor.py run [--only NAME] [--dry-run]
    python monitor.py test NAME            # show what a watcher currently sees
    python monitor.py list                 # show all watchers and whether they're enabled
    python monitor.py match KEYWORD TEXT   # check how a keyword matches a piece of text

Configuration lives in config.yaml, state in state/*.json (committed back by the workflow).
"""
from __future__ import annotations

import argparse
import difflib
import gzip
import hashlib
import html as htmllib
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup, Comment
from lxml import etree
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("MONITOR_CONFIG", ROOT / "config.yaml"))
STATE_DIR = Path(os.environ.get("MONITOR_STATE_DIR", ROOT / "state"))

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 site-monitor"
)
MAX_STORED_TEXT = 60_000
COLORS = {"page": 0xF1C40F, "wordpress": 0x3498DB, "rss": 0xE67E22, "sitemap": 0x2ECC71, "pdf": 0x9B59B6,
          "error": 0xE74C3C}


def now() -> datetime:
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    print(f"[{now():%H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------- helpers

@dataclass
class Note:
    title: str
    body: str = ""
    url: str | None = None
    color: int | None = None


def label(w: dict) -> str:
    """Name shown in Discord. `label:` if set, otherwise `name:`."""
    return str(w.get("label") or w["name"])


html_unescape = htmllib.unescape


def clean(s, limit: int = 200) -> str:
    """Strip HTML/whitespace and make text safe to embed in a Discord markdown link."""
    if s is None:
        return ""
    s = htmllib.unescape(re.sub(r"<[^>]+>", "", str(s)))
    s = " ".join(s.split()).replace("[", "(").replace("]", ")")
    return s[:limit]


def md_link(text: str, url: str | None) -> str:
    if not url:
        return text
    return f"[{text}]({url.replace(' ', '%20').replace(')', '%29')})"


def bullets(prefix: str, items: list[str], limit: int = 15) -> list[str]:
    out = [f"{prefix} {x}" for x in items[:limit]]
    if len(items) > limit:
        out.append(f"… and {len(items) - limit} more")
    return out


def short_ts(ts: str | None) -> str:
    """Compact display of a lastmod/modified value. Handles ISO ('2026-11-12T16:29:00+00:00'),
    '2026-11-12 16:29 +00:00' and free-form values like '16 May 2026 20:14'."""
    if not ts:
        return "–"
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?\s*(Z|[+-]\d{2}:?\d{2})?", ts)
    if m:
        return " ".join(x for x in (m.group(1), m.group(2), m.group(3)) if x)
    return ts[:40]


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def diff_lines(old: str, new: str, limit: int = 6, width: int = 160) -> list[str]:
    d = [l for l in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0)
         if not l.startswith(("---", "+++", "@@"))]
    shown = [x.replace("```", "'''")[:width] for x in d[:limit]]
    if len(d) > limit:
        shown.append(f"… {len(d) - limit} more changed lines")
    return shown


def diff_block(lines: list[str]) -> str:
    return "```diff\n" + "\n".join(lines) + "\n```"


# --------------------------------------------------------------------------- keywords

_TRANSLIT = str.maketrans({"ø": "o", "ł": "l", "đ": "d", "ð": "d", "æ": "ae", "œ": "oe", "ı": "i", "þ": "th"})


def norm(s: str) -> str:
    """Lowercase and strip accents, so 'Eurovīzija', 'EUROVIZIJA' and 'eurovizija' look the same."""
    s = unicodedata.normalize("NFKD", str(s).casefold()).translate(_TRANSLIT)
    return "".join(ch for ch in s if not unicodedata.combining(ch))


class _AllOf:
    """A keyword like 'festival+kenges': every part must match somewhere in the text."""

    def __init__(self, pats: list[re.Pattern]):
        self.pats = pats

    def search(self, hay: str):
        return all(p.search(hay) for p in self.pats)


def _kw_pattern(k: str) -> re.Pattern | None:
    k = k.strip()
    if k.startswith("="):
        body, pat = k[1:].strip(), None
        pat = r"(?<!\w)" + re.escape(body) + r"(?!\w)"
    elif k.startswith("*"):
        body = k[1:].strip()
        pat = re.escape(body)
    else:
        body = k
        pat = r"(?<!\w)" + re.escape(body)
    return re.compile(pat) if body else None


def compile_keywords(w: dict) -> list[re.Pattern] | None:
    """keywords: ['eurovizij', '*songfestival', '=esc', 'festival+kenges']
        plain word   → matches any word that STARTS with it (any ending): eurovizij → Eurovizijos, Eurovizijoje
        *word        → matches anywhere inside a word:  *songfestival → Eurosongfestival
        =word        → matches only that exact whole word: =esc → ESC, but not 'escalator'
        a+b          → ALL parts must appear (any order, anything in between); each part follows the rules above
    A keyword may be several words ("festivali i kenges" = that phrase). Case and accents are ignored."""
    kws = w.get("keywords")
    if not kws:
        return None
    if isinstance(kws, str):
        kws = [kws]
    pats = []
    for k in kws:
        parts = [x for x in norm(k).split("+") if x.strip()]
        compiled = [c for c in (_kw_pattern(x) for x in parts) if c]
        if len(compiled) == 1:
            pats.append(compiled[0])
        elif compiled:
            pats.append(_AllOf(compiled))
    return pats or None


def kw_ok(pats: list[re.Pattern] | None, *texts) -> bool:
    """True if there are no keywords, or any keyword matches any of the texts."""
    if not pats:
        return True
    hay = norm(" ".join(str(t) for t in texts if t)).replace("_", " ")
    return any(p.search(hay) for p in pats)


class Ctx:
    """Shared HTTP session."""

    def __init__(self, defaults: dict):
        self.defaults = defaults
        self.timeout = defaults.get("timeout", 30)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": defaults.get("user_agent", DEFAULT_UA),
            "Accept-Language": "en,*;q=0.5",
            "Cache-Control": "no-cache",
        })
        retry = Retry(total=2, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504],
                      allowed_methods=["GET"])
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))
        self.dead_hosts: dict[str, str] = {}              # host -> short cause, for sites we could not connect to this run
        self.skipped: dict[str, int] = {}                  # host -> number of checks skipped because of that
        self.fail_alerts: list[tuple[dict, int, str]] = []  # (watcher, failures in a row, short cause), sent as one message
        self.recovered: list[dict] = []                    # watchers that work again after an alert
        self._tr_key = os.environ.get("GOOGLE_TRANSLATE_API_KEY", "").strip()
        self._tr_cache: dict[str, str] | None = None  # "lang|text" -> translation ("" = no translation needed)
        self._tr_new = False
        self._tr_off = False   # set after a quota/key error so we stop asking for the rest of the run
        self._tr_warned = False
        self._tr_count = 0

    # ---- translation (Google Cloud Translation Basic v2) -------------------------------------------
    def _tr_target(self, w: dict) -> str | None:
        v = w.get("translate", self.defaults.get("translate"))
        if not v:
            return None
        return "en" if v is True else str(v)

    def _tr_load(self) -> dict[str, str]:
        if self._tr_cache is None:
            try:
                self._tr_cache = json.loads((STATE_DIR / "_translations.json").read_text("utf-8"))
            except (OSError, ValueError):
                self._tr_cache = {}
        return self._tr_cache

    def tr(self, w: dict, text: str) -> str | None:
        """Translation of a short text (a headline) into the language set by `translate:`, or None
        (translation off, already in that language, nothing to translate, or the service failed)."""
        target = self._tr_target(w)
        text = " ".join((text or "").split())[:300]
        if not target or not text or not any(ch.isalpha() for ch in text):
            return None
        cache = self._tr_load()
        key = f"{target}|{text}"
        if key in cache:
            return cache[key] or None
        if self._tr_off:
            return None
        if not self._tr_key:
            if not self._tr_warned:
                log("  ! translate is on but GOOGLE_TRANSLATE_API_KEY is not set: sending without translations")
                self._tr_warned = True
            return None
        try:
            r = requests.post("https://translation.googleapis.com/language/translate/v2",
                              headers={"X-goog-api-key": self._tr_key},  # not in the URL, so it can't leak into logs
                              json={"q": text, "target": target, "format": "text"}, timeout=20)
        except requests.RequestException as e:
            log(f"  ! translation request failed ({type(e).__name__}): sending without translations")
            return None
        if r.status_code != 200:
            try:
                msg = r.json().get("error", {}).get("message", "")
            except ValueError:
                msg = ""
            log(f"  ! translation service answered {r.status_code}: {msg[:200]}")
            if r.status_code in (400, 401, 403, 429):  # bad key, API not enabled, quota reached: don't hammer it
                self._tr_off = True
            return None
        try:
            t = r.json()["data"]["translations"][0]
            out, det = html_unescape(t["translatedText"]).strip(), str(t.get("detectedSourceLanguage", ""))
        except (ValueError, KeyError, IndexError):
            return None
        self._tr_count += len(text)
        same = det.split("-")[0].lower() == target.split("-")[0].lower() or norm(out) == norm(text)
        cache[key] = "" if same else out
        self._tr_new = True
        return None if same else out

    def tline(self, w: dict, text: str) -> str:
        """'\n↳ translation' to append to a headline line, or '' when there is nothing to add."""
        t = self.tr(w, text)
        return f"\n↳ {clean(t, 220)}" if t else ""

    def save_translations(self) -> None:
        if not self._tr_new or self._tr_cache is None:
            return
        cache = dict(list(self._tr_cache.items())[-3000:])
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        (STATE_DIR / "_translations.json").write_text(json.dumps(cache, ensure_ascii=False, indent=0), "utf-8")

    def get(self, url: str, w: dict | None = None, params: dict | None = None) -> requests.Response:
        headers = {}
        timeout = self.timeout
        if w:
            headers.update(w.get("headers", {}))
            if w.get("user_agent"):
                headers["User-Agent"] = w["user_agent"]
            timeout = w.get("timeout", timeout)
        r = self.session.get(url, params=params, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r


# --------------------------------------------------------------------------- sitemap

def parse_sitemap(content: bytes) -> tuple[str, dict[str, str | None]]:
    """Return ('urlset' | 'sitemapindex', {loc: lastmod})."""
    if content[:2] == b"\x1f\x8b":  # .xml.gz
        content = gzip.decompress(content)
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=True, huge_tree=True)
    root = etree.fromstring(content, parser)
    if root is None:
        raise ValueError("response is not XML")
    kind = etree.QName(root).localname
    if kind not in ("urlset", "sitemapindex"):
        raise ValueError(f"unexpected root <{kind}>: not a sitemap (blocked or redirected to an HTML page?)")
    entries: dict[str, str | None] = {}
    for el in root:
        if not isinstance(el.tag, str):
            continue
        loc = lastmod = None
        for c in el:
            if not isinstance(c.tag, str):
                continue
            name = etree.QName(c).localname
            if name == "loc":
                loc = (c.text or "").strip()
            elif name == "lastmod":
                lastmod = (c.text or "").strip() or None
        if loc:
            entries[loc] = lastmod
    return kind, entries


def page_text(ctx: Ctx, w: dict, url: str) -> str:
    """Fetch one page and return its visible text (used for sitemap snapshots)."""
    pw = {
        "name": w["name"], "url": url, "mode": "text",
        "selector": w.get("snapshot_selector"),
        "exclude_selectors": w.get("snapshot_exclude_selectors", ["header", "footer", "nav", "aside"]),
        "ignore_patterns": w.get("snapshot_ignore_patterns", []),
    }
    text = extract(ctx.get(url, w).content, pw)
    return text[: int(w.get("snapshot_max_text", 5000))]


def check_sitemap(w: dict, prev: dict | None, ctx: Ctx):
    deep = w.get("deep", True)
    inc = [re.compile(p) for p in w.get("include_children", [])]
    exc = [re.compile(p) for p in w.get("exclude_children", [])]
    ign = [re.compile(p) for p in w.get("ignore_urls", [])]
    max_children = w.get("max_children", 80)
    notify_removed = w.get("notify_removed", False)
    kw = compile_keywords(w)
    snap_on = bool(w.get("snapshots")) and deep
    snap_pat = [re.compile(p) for p in w.get("snapshot_urls", [])]
    ignored = lambda u: any(p.search(u) for p in ign)  # noqa: E731
    snappable = lambda u: not ignored(u) and (not snap_pat or any(p.search(u) for p in snap_pat))  # noqa: E731

    old_children = (prev or {}).get("children", {})
    kind, top = parse_sitemap(ctx.get(w["url"], w).content)
    cur: dict[str, dict] = {}
    if kind == "urlset":
        cur[w["url"]] = {"lastmod": None, "urls": top}
    else:
        locs = [(loc, lm) for loc, lm in top.items()
                if (not inc or any(p.search(loc) for p in inc)) and not any(p.search(loc) for p in exc)]
        if len(locs) > max_children:
            log(f"  ! {len(locs)} child sitemaps, only checking the first {max_children} (max_children)")
            locs = locs[:max_children]
        for loc, lm in locs:
            old = old_children.get(loc)
            if not deep:
                cur[loc] = {"lastmod": lm, "urls": {}}
            elif old and lm and old.get("lastmod") == lm:
                cur[loc] = old  # child unchanged: no need to download it again
            else:
                try:
                    _, urls = parse_sitemap(ctx.get(loc, w).content)
                    cur[loc] = {"lastmod": lm, "urls": urls}
                except Exception as e:  # one broken child must not break the whole watcher
                    log(f"  ! child sitemap {loc}: {e}")
                    if old:
                        cur[loc] = old

    # ---- page snapshots (optional): remember the text of pages so a change can be shown as a diff
    snaps: dict[str, str] = dict((prev or {}).get("snaps", {})) if snap_on else {}
    had_snaps = bool(snaps)

    def remember(u: str, text: str) -> None:
        snaps.pop(u, None)
        snaps[u] = text
        keep = int(w.get("snapshot_keep", 100))
        while len(snaps) > keep:
            snaps.pop(next(iter(snaps)))

    def baseline_snaps(skip=()) -> None:
        """Silently snapshot the most recently modified pages, so the next change has a 'before'."""
        rows = sorted(((m or "", u) for c in cur.values() for u, m in c["urls"].items() if snappable(u)),
                      reverse=True)
        for _, u in rows[: int(w.get("snapshot_limit", 30))]:
            if u in skip or u in snaps:
                continue
            try:
                remember(u, page_text(ctx, w, u))
            except Exception as e:
                log(f"  ! snapshot {u}: {e}")
            time.sleep(0.3)

    def state_out() -> dict:
        st = {"children": cur}
        if snap_on:
            st["snaps"] = snaps
        return st

    if prev is None:
        if snap_on:
            baseline_snaps()
        return state_out(), []

    # ---- what changed, per child sitemap
    has_urls = deep or kind == "urlset"
    changes = []
    for loc, c in cur.items():
        old = old_children.get(loc)
        old_urls = old["urls"] if old else {}
        ch = {"loc": loc, "name": loc.rsplit("/", 1)[-1] if loc != w["url"] else "sitemap",
              "new": [], "upd": [], "gone": [], "info": [], "extra_upd": 0, "fresh": old is None and has_urls}
        lastmod_changed = old is None or old.get("lastmod") != c.get("lastmod")
        if has_urls:
            ch["new"] = [u for u in c["urls"] if u not in old_urls and not ignored(u) and kw_ok(kw, u)]
            ch["upd"] = [(u, old_urls[u], m) for u, m in c["urls"].items()
                         if u in old_urls and m != old_urls[u] and not ignored(u) and kw_ok(kw, u)]
            if notify_removed:
                ch["gone"] = [u for u in old_urls if u not in c["urls"] and not ignored(u) and kw_ok(kw, u)]
            if not (ch["new"] or ch["upd"] or ch["gone"]) and old is not None and not kw \
                    and c["urls"] == old_urls and lastmod_changed:
                ch["info"].append(f"ℹ️ sitemap lastmod changed ({short_ts(old.get('lastmod'))} → "
                                  f"{short_ts(c.get('lastmod'))}) but no URL differences found")
        elif lastmod_changed and not kw:
            ch["info"].append(f"✏️ lastmod {short_ts(old.get('lastmod') if old else None)} → {short_ts(c.get('lastmod'))}"
                              + (" (new sitemap)" if old is None else ""))
            ch["extra_upd"] = 1
        changes.append(ch)

    # ---- fetch the changed pages and compare with their snapshots
    detail: dict[str, tuple] = {}
    skipped = 0
    if snap_on:
        targets = [u for ch in changes for u in ch["new"] + [x[0] for x in ch["upd"]] if snappable(u)]
        budget = int(w.get("snapshot_max_fetch", 15))
        skipped = max(0, len(targets) - budget)
        for u in targets[:budget]:
            try:
                text = page_text(ctx, w, u)
            except Exception as e:
                detail[u] = ("err", str(e)[:80])
                continue
            finally:
                time.sleep(0.3)
            old_text = snaps.get(u)
            remember(u, text)
            if old_text is None:
                detail[u] = ("first", text)
            elif old_text == text:
                detail[u] = ("same", None)
            else:
                detail[u] = ("diff", diff_lines(old_text, text))
        if not had_snaps:  # snapshots were just switched on for an existing watcher
            baseline_snaps(skip=set(detail))

    def annotate(u: str, is_new: bool) -> list[str]:
        d = detail.get(u)
        if not d:
            return []
        kind_, val = d
        if kind_ == "diff":
            return [diff_block(val)]
        if kind_ == "same":
            return ["└ _no visible text change on the page_"]
        if kind_ == "first":
            if is_new:
                ex = re.sub(r"[_*`]", " ", " ".join(val.split()))[:200]
                return [f"└ _{ex}_"] if ex else []
            return ["└ _no earlier snapshot, saved now_"]
        return [f"└ _couldn't open the page ({clean(val, 80)})_"]

    sections = []  # (name, lines, n_new, n_upd, single_url, loc, fresh)
    for ch in changes:
        lines: list[str] = []
        for u in ch["new"][:15]:
            lines.append(f"🆕 {u}")
            lines += annotate(u, True)
        if len(ch["new"]) > 15:
            lines.append(f"… and {len(ch['new']) - 15} more new")
        for u, a, b in ch["upd"][:15]:
            lines.append(f"✏️ {u} ({short_ts(a)} → {short_ts(b)})")
            lines += annotate(u, False)
        if len(ch["upd"]) > 15:
            lines.append(f"… and {len(ch['upd']) - 15} more updated")
        lines += bullets("🗑️", ch["gone"])
        lines += ch["info"]
        if not lines:
            continue
        one = (ch["new"] + [x[0] for x in ch["upd"]]) if len(ch["new"]) + len(ch["upd"]) == 1 else []
        sections.append((ch["name"], lines, len(ch["new"]), len(ch["upd"]) + ch["extra_upd"],
                         one[0] if one else None, ch["loc"], ch["fresh"]))
    if notify_removed:
        for loc in old_children:
            if loc not in cur and loc not in top:  # no longer listed in the index at all
                sections.append((loc.rsplit("/", 1)[-1], ["🗑️ sitemap removed"], 0, 0, None, loc, False))
    if not sections:
        return state_out(), []

    def summary(n_new: int, n_upd: int) -> str:
        parts = []
        if n_new:
            parts.append(f"{n_new} new")
        if n_upd:
            parts.append(f"{n_upd} updated")
        return ", ".join(parts)

    if snap_on:  # one message per child sitemap, so page diffs get room
        notes = []
        for name, lines, n_new, n_upd, one, loc, _fresh in sections:
            s = summary(n_new, n_upd)
            notes.append(Note(f"{name} · {s}" if s else name, "\n".join(lines), one or loc))
        if skipped:
            notes[-1].body += f"\n… page diffs skipped for {skipped} more page(s) (snapshot_max_fetch)"
        return state_out(), notes

    total_new = sum(s[2] for s in sections)
    total_upd = sum(s[3] for s in sections)
    singles = [s[4] for s in sections if s[4]]
    link = singles[0] if (total_new + total_upd == 1 and singles) else w["url"]
    body = "\n\n".join(f"**{name}**" + (" _(new sitemap)_" if fresh else "") + "\n" + "\n".join(lines)
                       for name, lines, _n, _u, _o, _l, fresh in sections)
    return state_out(), [Note(summary(total_new, total_upd) or "Sitemap changed", body, link)]


# --------------------------------------------------------------------------- WordPress REST

WP_FIELDS = "id,date,modified,modified_gmt,link,slug,title,status,source_url,mime_type"


def wp_text(it: dict, fields: list[str]) -> str:
    parts = []
    for f in fields:
        v = it.get(f)
        if isinstance(v, dict):
            v = v.get("rendered")
        if v:
            soup = BeautifulSoup(str(v), "lxml")
            for t in soup(["script", "style"]):
                t.decompose()
            parts.append(block_text(soup.body or soup))
    return "\n".join(parts)


def check_wp(w: dict, prev: dict | None, ctx: Ctx):
    base, _, qs = w["url"].partition("?")
    diff_on = bool(w.get("diff"))
    dfields = list(w.get("diff_fields", ["content"]))
    fields = w.get("fields", WP_FIELDS) + ("," + ",".join(dfields) if diff_on else "")
    params = {"per_page": 100, "orderby": "modified", "order": "desc", "_fields": fields}
    params.update(dict(parse_qsl(qs)))
    params.update(w.get("params", {}))
    per_page = int(params["per_page"])
    max_text = int(w.get("snapshot_max_text", 6000))
    keep_text = int(w.get("snapshot_keep", 50))
    kw = compile_keywords(w)
    ign = [re.compile(p) for p in w.get("ignore_patterns", [])]
    cur: dict[str, dict] = {}
    total = None
    for page in range(1, int(w.get("pages", 1)) + 1):
        p = dict(params, page=page)
        if w.get("cache_bust"):
            p["_cb"] = str(int(time.time()))
        r = ctx.get(base, w, params=p)
        try:
            data = r.json()
        except ValueError:
            raise ValueError(f"response is not JSON (content-type {r.headers.get('content-type')}); "
                             "REST API disabled or blocked?") from None
        if isinstance(data, dict):
            if "code" in data and "id" not in data:
                raise ValueError(f"WordPress error {data.get('code')}: {data.get('message')}")
            data = [data]
        total = r.headers.get("X-WP-Total", total)
        for it in data:
            if not isinstance(it, dict) or "id" not in it:
                continue
            t = it.get("title")
            t = t.get("rendered") if isinstance(t, dict) else t
            cur[str(it["id"])] = {
                "modified": it.get("modified_gmt") or it.get("modified"),
                "link": it.get("link") or it.get("source_url"),
                "title": clean(t, 100) or it.get("slug") or "",
            }
            if diff_on:
                txt = "\n".join(l for l in wp_text(it, dfields).splitlines() if not any(x.search(l) for x in ign))
                cur[str(it["id"])]["text"] = txt[:max_text]
        if len(data) < per_page:
            break

    seen = dict((prev or {}).get("items", {}))
    merged = {**seen, **cur}
    max_tracked = w.get("max_tracked", 3000)
    if len(merged) > max_tracked:
        merged = dict(sorted(merged.items(), key=lambda kv: kv[1].get("modified") or "", reverse=True)[:max_tracked])
    if diff_on:  # keep text only for the most recently modified items, so the state file stays small
        order = sorted(merged, key=lambda i: merged[i].get("modified") or "", reverse=True)
        for i in order[keep_text:]:
            merged[i].pop("text", None)
    state = {"items": merged, "total": total}
    if prev is None:
        return state, []

    new = [(i, v) for i, v in cur.items() if i not in seen and kw_ok(kw, v["title"], v["link"], v.get("text"))]
    mod = [(i, v, seen[i]) for i, v in cur.items()
           if i in seen and v["modified"] != seen[i].get("modified") and kw_ok(kw, v["title"], v["link"], v.get("text"))]
    limit = 10 if diff_on else 15
    lines: list[str] = []
    for i, v in new[:limit]:
        lines.append(f"🆕 {md_link(v['title'] or '#' + i, v['link'])} · #{i}" + ctx.tline(w, v["title"]))
        if diff_on and v.get("text"):
            ex = re.sub(r"[_*`]", " ", " ".join(v["text"].split()))[:200]
            lines.append(f"└ _{ex}_")
    if len(new) > limit:
        lines.append(f"… and {len(new) - limit} more new")
    for i, v, old in mod[:limit]:
        lines.append(f"✏️ {md_link(v['title'] or '#' + i, v['link'])} · #{i} "
                     f"({short_ts(old.get('modified'))} → {short_ts(v['modified'])})")
        if diff_on:
            if old.get("text") is None:
                lines.append("└ _no earlier snapshot of the text_")
            elif old["text"] == v.get("text"):
                lines.append("└ _no visible text change_")
            else:
                lines.append(diff_block(diff_lines(old["text"], v.get("text", ""), 5, 140)))
    if len(mod) > limit:
        lines.append(f"… and {len(mod) - limit} more modified")
    prev_total = prev.get("total")
    if total and prev_total and total != prev_total and not kw:
        lines.append(f"🔢 total items: {prev_total} → {total}")
    if not lines:
        return state, []
    parts = []
    if new:
        parts.append(f"{len(new)} new")
    if mod:
        parts.append(f"{len(mod)} modified")
    if not parts:
        parts.append("item count changed")
    link = (new[0][1]["link"] if len(new) == 1 and not mod else mod[0][1]["link"] if len(mod) == 1 and not new else w["url"])
    return state, [Note(", ".join(parts), "\n".join(lines), link)]


# --------------------------------------------------------------------------- RSS / Atom

def rss_text(e) -> str:
    parts = [e.get("title"), e.get("summary")]
    parts += [c.get("value") for c in (e.get("content") or [])]
    parts += [t.get("term") for t in (e.get("tags") or [])]
    return " ".join(clean(p, 20000) for p in parts if p)


def _with_param(url: str, key: str, value) -> str:
    u = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k != key] + [(key, str(value))]
    return urlunparse(u._replace(query=urlencode(q)))


def check_rss(w: dict, prev: dict | None, ctx: Ctx):
    feed = feedparser.parse(ctx.get(w["url"], w).content)
    if not feed.entries:
        if feed.get("bozo"):
            raise ValueError(f"cannot parse feed: {feed.get('bozo_exception')}")
        return (prev or {"seen": []}), []
    all_entries = list(feed.entries)
    # pages: N reads ?paged=2..N as well (WordPress feeds with one item per page). Page 1 must work; if a later
    # page fails or is empty, we stop and use what we have.
    for n in range(2, int(w.get("pages", 1)) + 1):
        try:
            more = feedparser.parse(ctx.get(_with_param(w["url"], w.get("page_param", "paged"), n), w).content).entries
        except requests.RequestException:
            break
        if not more:
            break
        all_entries += more
    kw = compile_keywords(w)
    ids, entries = [], {}
    for e in all_entries:
        eid = e.get("id") or e.get("link") or e.get("title")
        if eid and eid not in entries:
            ids.append(eid)
            entries[eid] = e
    seen = (prev or {}).get("seen", [])
    seen_set, id_set = set(seen), set(ids)
    new_ids = [i for i in ids if i not in seen_set]
    state = {"seen": (ids + [s for s in seen if s not in id_set])[:1000]}  # every item counts as seen, matching or not
    if prev is None or not new_ids:
        return state, []
    new_ids = [i for i in new_ids if kw_ok(kw, rss_text(entries[i]))]
    if not new_ids:
        return state, []
    max_n = w.get("max_per_run", 10)
    notes = []
    for eid in reversed(new_ids[:max_n]):  # oldest first
        e = entries[eid]
        body = clean(e.get("summary"), 350)
        tl = ctx.tline(w, clean(e.get("title"), 250))
        if tl:
            body = tl.strip() + ("\n" + body if body else "")
        if e.get("published"):
            body += f"\n_{e['published']}_"
        notes.append(Note(clean(e.get("title"), 250) or "(untitled)", body.strip(), e.get("link")))
    if len(new_ids) > max_n:
        notes.append(Note(f"… and {len(new_ids) - max_n} more new items", "", w["url"]))
    return state, notes


# --------------------------------------------------------------------------- generic page / element

def _kill(tags) -> None:
    for t in tags:
        if not getattr(t, "decomposed", False):
            t.decompose()


_BLOCKS = ["p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "td", "th", "table",
           "blockquote", "br", "section", "article", "header", "footer", "main", "aside", "nav", "figure",
           "figcaption", "dt", "dd", "pre", "form"]


_BLOCKS_ROWS = [b for b in _BLOCKS if b not in ("td", "th")]


def block_text(node, rows: bool = False) -> str:
    """Visible text with one line per block (paragraph, list item, heading…). Inline tags like <b> or <a>
    stay inside their line, and line breaks in the HTML source don't split sentences.
    rows=True keeps each table row on ONE line, cells separated by ' | ' (so '11:00 | Name' stays together)."""
    if rows:
        for tr in node.find_all("tr"):
            cells = tr.find_all(["td", "th"], recursive=False)
            for c in cells[:-1]:
                c.insert_after(" | ")
    for t in node.find_all(_BLOCKS_ROWS if rows else _BLOCKS):
        t.insert_before("\u2029")
        t.insert_after("\u2029")
    raw = node.get_text("").replace("\n", " ").replace("\u2029", "\n")
    return "\n".join(l for l in (" ".join(x.split()) for x in raw.splitlines()) if l)


def extract(content: bytes, w: dict):
    """Return text (mode text/html) or {href: text} (mode links) for the selected part of the page."""
    soup = BeautifulSoup(content, "lxml")
    _kill(soup(["script", "style", "noscript", "template", "svg"]))
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    for sel in w.get("exclude_selectors", []):
        _kill(soup.select(sel))
    sel = w.get("selector")
    nodes = soup.select(sel) if sel else [soup.body or soup]
    if not nodes:
        raise ValueError(f"selector {sel!r} matched nothing. Content loaded by JavaScript? "
                         f"Try: python monitor.py test {w['name']!r}")
    ign = [re.compile(p) for p in w.get("ignore_patterns", [])]
    mode = w.get("mode", "text")
    if mode == "links":
        links: dict[str, str] = {}
        for n in nodes:
            anchors = ([n] if n.name == "a" and n.has_attr("href") else []) + n.find_all("a", href=True)
            for a in anchors:
                href = a["href"].strip()
                if href.startswith(("#", "javascript:", "mailto:", "tel:")):
                    continue
                href = urljoin(w["url"], href)
                text = " ".join(a.get_text(" ", strip=True).split())[:120]
                if any(p.search(f"{href} {text}") for p in ign):
                    continue
                if not links.get(href):
                    links[href] = text
        return links
    if mode == "html":
        raw = "\n".join(n.prettify() for n in nodes)
    else:
        raw = "\n".join(block_text(n, bool(w.get("table_rows"))) for n in nodes)
    lines = [" ".join(l.split()) for l in raw.splitlines()]
    return "\n".join(l for l in lines if l and not any(p.search(l) for p in ign))


def _item_info(content: bytes, base: str) -> tuple[str, str, str]:
    """(title, link, text) of a small fetched item: the first link in it (or the first line) is the title."""
    soup = BeautifulSoup(content, "lxml")
    _kill(soup(["script", "style", "noscript", "template", "svg"]))
    text = block_text(soup.body or soup)
    a = soup.find("a", href=True)
    link = urljoin(base, a["href"].strip()) if a else base
    title = " ".join(a.get_text(" ", strip=True).split()) if a else ""
    return clean(title or next(iter(text.splitlines()), ""), 120), link, text


def _unjson(v: str) -> str:
    """Decode text taken from JSON inside a script: \\u0119 -> ę and \\/ -> /."""
    if "\\" not in v:
        return v.strip()
    try:
        return json.loads(f'"{v}"').strip()
    except ValueError:
        return v.replace("\\/", "/").strip()


def check_matches(w: dict, prev: dict | None, ctx: Ctx):
    """mode: matches. Finds items in the page's RAW source (scripts and HTML comments included) with a regex
    and reports new ones. The pattern may use the groups (?P<id>...), (?P<title>...) and (?P<url>...); without
    names, group 1 is the ID. The ID defaults to the url, then to the title. With item_url, new IDs are opened
    to get a title/link and to apply `keywords`."""
    pat = w.get("pattern")
    if not pat:
        raise ValueError("mode: matches needs a `pattern:` (a regex; group 1, or named groups id/title/url)")
    rx = re.compile(pat)
    names = set(rx.groupindex)
    html = ctx.get(w["url"], w).text
    items: dict[str, tuple[str, str]] = {}  # id -> (title, url)
    for m in rx.finditer(html):
        g = (lambda n: _unjson(m.group(n) or "") if n in names else "")
        title, url = g("title"), g("url")
        if url:
            url = urljoin(w["url"], url)
        iid = g("id") or url or title or _unjson(m.group(1) if rx.groups else m.group(0))
        if iid and iid not in items:
            items[iid] = (title, url)
    if not items:
        raise ValueError(f"pattern {pat!r} matched nothing in the page source "
                         f"(is it loaded by JavaScript? Try: python monitor.py test {w['name']!r})")
    seen = dict((prev or {}).get("ids", {}))
    if prev is None:  # first run: everything that exists now is the baseline, silently
        return {"ids": {i: t for i, (t, _) in items.items()}}, []
    tmpl, kw = w.get("item_url"), compile_keywords(w)
    fetch = bool(tmpl) and w.get("fetch_items", True)
    budget, found = int(w.get("max_fetch", 15)), []
    for i, (title, url) in items.items():
        if i in seen:
            continue
        link, text = url or (tmpl.replace("{id}", i) if tmpl else w["url"]), ""
        if fetch and not title:
            if budget <= 0:
                continue  # picked up on the next run
            budget -= 1
            try:
                title, link2, text = _item_info(ctx.get(link, w).content, link)
                link = url or link2
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code in (404, 410):
                    seen[i] = ""  # item doesn't exist: stop retrying
                continue  # any other error: try again next run
            except requests.RequestException:
                continue
        seen[i] = title
        if kw_ok(kw, text, title, i):
            found.append(md_link(clean(title) or i, link) + (ctx.tline(w, title) if len(found) < 20 else ""))
    if len(seen) > 3000:
        seen = dict(list(seen.items())[-3000:])
    state = {"ids": seen}
    if not found:
        return state, []
    return state, [Note(f"{len(found)} new item(s)", "\n".join(bullets("🆕", found, 20)), w["url"])]


def check_page(w: dict, prev: dict | None, ctx: Ctx):
    if w.get("mode") == "matches":
        return check_matches(w, prev, ctx)
    cur = extract(ctx.get(w["url"], w).content, w)
    kw = compile_keywords(w)
    if w.get("mode") == "links":
        seen = dict((prev or {}).get("links", {}))
        new = {u: t for u, t in cur.items() if u not in seen}
        merged = {**seen, **new}
        # opt-in: same URL, different link text (e.g. a live blog that gets a new title)
        changed: dict[str, tuple[str, str]] = {}
        if w.get("notify_title_changes"):
            for u, t in cur.items():
                old = seen.get(u)
                if u in seen and old and t and old != t:  # ignore links that had/have no text
                    changed[u] = (old, t)
                    merged[u] = t
        if len(merged) > 3000:
            merged = dict(list(merged.items())[-3000:])
        state = {"links": merged}  # every link counts as seen, matching the keywords or not
        new = {u: t for u, t in new.items() if kw_ok(kw, t, u)}
        changed = {u: ot for u, ot in changed.items() if kw_ok(kw, ot[0], ot[1], u)}
        if prev is None or not (new or changed):
            return state, []
        lines = bullets("🆕", [md_link(t or u, u) + (ctx.tline(w, t) if i < 20 else "")
                               for i, (u, t) in enumerate(new.items())], 20)
        lines += bullets("✏️", [f"{clean(o, 100)} → {md_link(clean(n, 100), u)}" + (ctx.tline(w, n) if i < 10 else "")
                                for i, (u, (o, n)) in enumerate(changed.items())], 10)
        parts = ([f"{len(new)} new link(s)"] if new else []) + ([f"{len(changed)} title change(s)"] if changed else [])
        return state, [Note(", ".join(parts), "\n".join(lines), w["url"])]

    digest = hashlib.sha256(cur.encode()).hexdigest()
    state = {"hash": digest, "text": cur[:MAX_STORED_TEXT]}
    if prev is None or prev.get("hash") == digest:
        return state, []
    diff = [l for l in difflib.unified_diff(prev.get("text", "").splitlines(), cur.splitlines(), lineterm="", n=0)
            if not l.startswith(("---", "+++", "@@"))]
    if kw:  # only report changed lines that contain a keyword
        diff = [l for l in diff if kw_ok(kw, l)]
        if not diff:
            return state, []
    shown = [d.replace("```", "'''")[:200] for d in diff[:25]]
    body = "```diff\n" + "\n".join(shown) + "\n```" if shown else "Content changed."
    if len(diff) > 25:
        body += f"\n… {len(diff) - 25} more changed lines"
    return state, [Note("Page changed", body, w["url"])]


# --------------------------------------------------------------------------- PDF

def pdf_text(content: bytes, w: dict) -> tuple[str, int]:
    """Readable text of a PDF as one sentence per line, so re-wrapped paragraphs don't look like changes.
    Returns (text, number_of_pages). Text is '' for scanned PDFs or if the file can't be read."""
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("the 'pypdf' package is not installed: add a line 'pypdf' to requirements.txt") from None
    import io
    import logging
    from collections import Counter
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            reader.decrypt("")
        n_pages = len(reader.pages)
        parts = []
        for page in list(reader.pages)[: int(w.get("max_pages", 300))]:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                parts.append("")
    except Exception as e:
        log(f"  ! could not read the PDF text ({e}); only file changes can be reported")
        return "", 0

    page_lines = [[l.strip() for l in p.splitlines() if l.strip()] for p in parts]
    key = lambda l: re.sub(r"\d+", "#", l)  # noqa: E731
    if len(page_lines) >= 3:  # drop headers/footers/page numbers: short lines at the very top or bottom of a
        # page that repeat (ignoring digits) on at least half of the pages
        edge = lambda lines: lines[:2] + lines[-2:]  # noqa: E731
        counts = Counter(k for lines in page_lines for k in {key(l) for l in edge(lines)})
        cut = max(2, len(page_lines) * 0.5)
        page_lines = [[l for i, l in enumerate(lines)
                       if not ((i < 2 or i >= len(lines) - 2) and len(l) < 80 and counts[key(l)] >= cut)]
                      for lines in page_lines]
    raw = "\n".join("\n".join(lines) for lines in page_lines)
    raw = re.sub(r"(?<=[a-z])-\n(?=[a-z])", "", raw)  # words hyphenated at the end of a line
    raw = " ".join(raw.split())

    ign = [re.compile(p) for p in w.get("ignore_patterns", [])]
    out, carry = [], ""
    for piece in re.split(r"(?<=[.!?;:])\s+", raw):
        piece = (carry + " " + piece).strip()
        carry = ""
        if not piece:
            continue
        if re.fullmatch(r"[\(\[]?\w{1,4}(?:\.\d+)*[.\)\]:;]?", piece):  # a bare clause number like "2.1." or "(a)"
            carry = piece
            continue
        if not any(p.search(piece) for p in ign):
            out.append(piece)
    if carry and not any(p.search(carry) for p in ign):
        out.append(carry)
    return "\n".join(out), n_pages


def size_str(n: int | None) -> str:
    n = n or 0
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{n / 1024:.0f} KB"


def check_pdf(w: dict, prev: dict | None, ctx: Ctx):
    r = ctx.get(w["url"], w)
    content = r.content
    if len(content) > int(w.get("max_size_mb", 25)) * 1048576:
        raise ValueError(f"file is {size_str(len(content))}, over max_size_mb")
    if b"%PDF-" not in content[:1024]:
        raise ValueError(f"not a PDF (content-type {r.headers.get('content-type')}); "
                         "blocked, or redirected to a web page?")
    text, n_pages = pdf_text(content, w)
    keep = text[: int(w.get("snapshot_max_text", 60000))]
    state = {"hash": hashlib.sha256(content).hexdigest(), "text_hash": hashlib.sha256(text.encode()).hexdigest(),
             "text": keep, "pages": n_pages, "size": len(content)}
    if prev is None:
        if not text:
            log("  ! no readable text in this PDF (a scanned image?): only file changes can be reported")
        return state, []
    if prev.get("hash") == state["hash"]:
        return state, []

    facts = f"Size {size_str(prev.get('size'))} → {size_str(len(content))}"
    if prev.get("pages") != n_pages:
        facts += f" · pages {prev.get('pages')} → {n_pages}"
    old_text = prev.get("text", "")
    if not text and not old_text:
        return state, [Note("PDF file changed", "This PDF has no readable text (a scanned image?), so only the file "
                            f"change can be reported. {facts}", w["url"])]
    if prev.get("text_hash") == state["text_hash"]:
        if w.get("text_only") or w.get("keywords"):  # a re-exported file with the same text: not interesting
            return state, []
        return state, [Note("PDF file changed (text identical)",
                            f"The file was replaced, but its readable text is the same. {facts}", w["url"])]

    kw = compile_keywords(w)
    d = [l for l in difflib.unified_diff(old_text.splitlines(), keep.splitlines(), lineterm="", n=0)
         if not l.startswith(("---", "+++", "@@"))]
    if kw:  # only report changed lines that contain a keyword
        d = [l for l in d if kw_ok(kw, l)]
        if not d:
            return state, []
    if not d:
        return state, [Note("PDF changed", "The text changed beyond the part that is stored for comparison "
                            f"(snapshot_max_text). {facts}", w["url"])]
    shown = [x.replace("```", "'''")[:200] for x in d[:25]]
    if len(d) > 25:
        shown.append(f"… {len(d) - 25} more changed lines")
    return state, [Note("PDF changed", f"{diff_block(shown)}\n{facts}", w["url"])]


# --------------------------------------------------------------------------- pages/files that come and go

def excerpt(data: dict | None, limit: int = 300) -> str:
    t = (data or {}).get("text") or ""
    return re.sub(r"[_*`]", " ", " ".join(t.split()))[:limit]


def missing_aware(fn):
    """`allow_missing: true` makes a 404/410 answer a normal state ('not online'), not an error.
    You get a message when the page or file appears, and (unless `notify_removed: false`) when it disappears.
    The last version we saw is kept, so when it returns you can see whether it changed in the meantime."""

    def run(w: dict, prev: dict | None, ctx: Ctx):
        if not w.get("allow_missing"):
            return fn(w, prev, ctx)
        was_missing = bool(prev and prev.get("missing"))
        last = {k: v for k, v in (prev or {}).items() if k != "missing"} or None
        try:
            data, notes = fn(w, last, ctx)
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else None
            if code not in (404, 410):
                raise
            notes = []
            if last and not was_missing and w.get("notify_removed", True):
                notes.append(Note(f"🔴 No longer online ({code})",
                                  f"It used to be available, but now answers '{code}'. "
                                  "The last version we saw is kept for comparison.", w["url"]))
            elif prev is None:
                log(f"  ! {w['name']}: answers {code}, treated as 'not online yet' (allow_missing)")
            return {**(last or {}), "missing": True}, notes
        if was_missing:
            body = ""
            if "size" in data:
                body = f"{size_str(data.get('size'))}, {data.get('pages')} page(s)"
            elif "links" in data:
                body = f"{len(data['links'])} links on the page"
            if last:
                body += ("\nSame content as the last version we saw." if not notes else
                         "\nIt changed while it was offline, see the next message.")
            ex = excerpt(data)
            if ex:
                body += f"\n_{ex}_"
            notes.insert(0, Note("🟢 Online again" if last else "🟢 Now online", body.strip(), w["url"]))
        return data, notes

    return run


CHECKERS = {"sitemap": check_sitemap, "wordpress": check_wp, "rss": check_rss,
            "page": missing_aware(check_page), "pdf": missing_aware(check_pdf)}


# --------------------------------------------------------------------------- Discord

def fit(body: str, limit: int = 3900) -> str:
    if len(body) <= limit:
        return body
    body = body[:limit].rsplit("\n", 1)[0] + "\n…"
    if body.count("```") % 2:
        body += "\n```"
    return body


def send_discord(webhook: str, source: str, wtype: str, notes: list[Note]) -> None:
    embeds = []
    for n in notes:
        e = {
            "author": {"name": source[:256]},
            "title": n.title[:256],
            "description": fit(n.body),
            "color": n.color or COLORS.get(wtype, 0x95A5A6),
            "timestamp": now().isoformat(),
        }
        if n.url:
            e["url"] = n.url
        embeds.append(e)
    # Discord allows max 10 embeds and ~6000 characters in total per message
    batches, cur, size = [], [], 0
    for e in embeds:
        s = len(e["title"]) + len(e["description"]) + len(e["author"]["name"])
        if cur and (len(cur) >= 10 or size + s > 5500):
            batches.append(cur)
            cur, size = [], 0
        cur.append(e)
        size += s
    if cur:
        batches.append(cur)
    for batch in batches:
        for _ in range(4):
            r = requests.post(webhook, json={"embeds": batch, "username": "Tall Top Tipster",
                                             "avatar_url": "https://i.ibb.co/YTXt6c1x/mg.png",
                                             "allowed_mentions": {"parse": []}}, timeout=20)
            if r.status_code == 429:
                time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
                continue
            r.raise_for_status()
            break
        else:
            raise RuntimeError("Discord rate limit: gave up")
        time.sleep(0.4)


def deliver(w: dict, notes: list[Note], dry_run: bool) -> None:
    if not notes:
        return
    if dry_run:
        for n in notes:
            print(f"\n--- would notify: [{label(w)}] {n.title}\n{n.url or ''}\n{n.body}\n")
        return
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        raise RuntimeError("DISCORD_WEBHOOK_URL is not set")
    send_discord(webhook, label(w), w["type"], notes)


# --------------------------------------------------------------------------- state + runner

def state_path(name: str) -> Path:
    return STATE_DIR / f"{slug(name)}.json"


def load_state(name: str) -> dict | None:
    p = state_path(name)
    if p.exists():
        try:
            return json.loads(p.read_text("utf-8"))
        except ValueError:
            log(f"  ! corrupt state for {name}, starting over")
    return None


def save_state(name: str, st: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_path(name).write_text(json.dumps(st, ensure_ascii=False, separators=(",", ":")), "utf-8")


class HostDown(Exception):
    """Raised instead of a request when the same site already failed to connect in this run."""


def host_of(w: dict) -> str:
    return urlparse(w["url"]).netloc.lower()


def short_error(e: Exception, timeout=30) -> tuple[str, bool]:
    """(short readable cause, True if no connection to the site could be made at all: DNS, refused, SSL, connect timeout)."""
    s = str(e)
    low = s.lower()
    if isinstance(e, requests.HTTPError) and e.response is not None:
        code = e.response.status_code
        what = {401: "login required", 403: "access denied (the site may block this server)", 404: "page not found",
                410: "page removed", 429: "too many requests (rate limited)"}.get(code, "server error" if code >= 500 else "error")
        return f"🚫 HTTP {code}: {what}", False
    m = re.search(r"too many (\d{3}) error", low)
    if isinstance(e, requests.exceptions.RetryError) or m:
        return f"🛠️ Server error (HTTP {m.group(1) if m else '5xx'})", False
    if isinstance(e, requests.exceptions.ConnectTimeout) or "connecttimeout" in low:
        return f"⏱️ Could not connect (no answer within {timeout} s)", True
    if isinstance(e, requests.Timeout) or "timed out" in low or "timeouterror" in low:
        return f"⏱️ Timed out (no answer within {timeout} s)", False
    if any(x in low for x in ("name or service not known", "nameresolution", "getaddrinfo", "name resolution")):
        return "🌐 Site not found (DNS error)", True
    if re.search(r"\bssl|certificate", low):
        return "🔒 SSL / certificate problem", True
    if "connection refused" in low:
        return "🔌 Connection refused", True
    if isinstance(e, HostDown):
        return s, True
    if isinstance(e, requests.ConnectionError) or "connection" in low:
        return "🔌 Connection failed or dropped", False
    return (clean(s, 220) if isinstance(e, ValueError) else f"{type(e).__name__}: {clean(s, 200)}"), False


def run_watcher(w: dict, ctx: Ctx, dry_run: bool) -> None:
    name = w["name"]
    st = load_state(name) or {}
    prev = st.get("data")
    fails = st.get("fails", 0)
    threshold = w.get("fail_alert_after", ctx.defaults.get("fail_alert_after", 3))
    host = host_of(w)
    if host in ctx.dead_hosts:
        # No connection to this site could be made a moment ago: don't wait for it again. This is neutral: it is
        # not counted as a failure of this watcher, and its state is left alone.
        log(f"  - {name}: skipped, {host} could not be reached in this run ({ctx.dead_hosts[host]})")
        ctx.skipped[host] = ctx.skipped.get(host, 0) + 1
        return
    try:
        data, notes = CHECKERS[w["type"]](w, prev, ctx)
    except Exception as e:
        fails += 1
        short, unreachable = short_error(e, ctx.timeout)
        log(f"  ✗ {name}: {short} (failure #{fails})  [{clean(str(e), 300)}]")
        if unreachable:
            ctx.dead_hosts[host] = short
        if dry_run:
            return
        if fails == threshold:
            ctx.fail_alerts.append((w, fails, short))
        save_state(name, {**st, "fails": fails})
        return

    if prev is None:
        log(f"  • {name}: baseline recorded")
    elif notes:
        log(f"  ✓ {name}: {len(notes)} notification(s)")
    else:
        log(f"  · {name}: no changes")
    try:
        deliver(w, notes, dry_run)
    except Exception as e:
        log(f"  ✗ {name}: notification failed ({e}); will retry next run")
        return  # state not saved, so the change is reported again next time
    if not dry_run:
        save_state(name, {"v": 1, "fails": 0, "checked": now().isoformat(timespec="seconds"), "data": data})
        if fails >= threshold:  # an alert was sent earlier: tell that it works again
            ctx.recovered.append(w)


def flush_alerts(ctx: Ctx) -> None:
    """Send all failure alerts and 'working again' messages of this run, grouped per site and cause."""
    notes: list[Note] = []
    groups: dict[tuple[str, str], list[tuple[dict, int]]] = {}
    for w, fails, short in ctx.fail_alerts:
        groups.setdefault((host_of(w), short), []).append((w, fails))
    for (host, short), items in groups.items():
        runs = max(f for _, f in items)
        extra = (f"\n{ctx.skipped[host]} other check(s) on this site were skipped" if ctx.skipped.get(host) else "")
        if len(items) == 1:
            w = items[0][0]
            notes.append(Note(f"⚠️ {label(w)}: check failing", f"{short}\nFailed {runs}× in a row{extra}", w["url"], COLORS["error"]))
        else:
            lines = [f"• {label(w)}" for w, _ in items[:15]] + ([f"… and {len(items) - 15} more"] if len(items) > 15 else [])
            notes.append(Note(f"⚠️ {host}: {len(items)} checks failing",
                              f"{short}\nFailed {runs}× in a row\n" + "\n".join(lines) + extra, items[0][0]["url"], COLORS["error"]))
    ok: dict[str, list[dict]] = {}
    for w in ctx.recovered:
        ok.setdefault(host_of(w), []).append(w)
    for host, ws in ok.items():
        if len(ws) == 1:
            notes.append(Note(f"✅ {label(ws[0])}: working again", "", ws[0]["url"], 0x2ECC71))
        else:
            notes.append(Note(f"✅ {host}: {len(ws)} checks working again",
                              "\n".join(f"• {label(w)}" for w in ws[:15]), ws[0]["url"], 0x2ECC71))
    ctx.fail_alerts.clear()
    ctx.recovered.clear()
    if not notes:
        return
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    try:
        if not webhook:
            raise RuntimeError("DISCORD_WEBHOOK_URL is not set")
        send_discord(webhook, "Monitor status", "error", notes)
    except Exception as e:
        log(f"  ✗ could not send status messages: {e}")


def load_config() -> tuple[dict, list[dict]]:
    cfg = yaml.safe_load(CONFIG_PATH.read_text("utf-8")) or {}
    watchers = cfg.get("watchers") or []
    seen = set()
    for w in watchers:
        for key in ("name", "type", "url"):
            if not w.get(key):
                sys.exit(f"config error: every watcher needs '{key}' (problem near {w})")
        if w["type"] not in CHECKERS:
            sys.exit(f"config error: unknown type {w['type']!r} for {w['name']} (use {', '.join(CHECKERS)})")
        if slug(w["name"]) in seen:
            sys.exit(f"config error: duplicate watcher name {w['name']!r}")
        seen.add(slug(w["name"]))
        for key in _LIST_KEYS:  # a single value instead of a list is fine: treat it as a list of one
            if isinstance(w.get(key), str):
                w[key] = [w[key]]
    return cfg.get("defaults", {}), watchers


_LIST_KEYS = ("exclude_selectors", "ignore_patterns", "ignore_urls", "include_children", "exclude_children",
              "snapshot_urls", "diff_fields", "keywords")


def cmd_run(args) -> None:
    defaults, watchers = load_config()
    ctx = Ctx(defaults)
    todo = [w for w in watchers if w.get("enabled", True)]
    if args.only:
        todo = [w for w in watchers if w["name"] == args.only]
        if not todo:
            sys.exit(f"no watcher named {args.only!r}")
    log(f"{len(todo)} watcher(s) to check" if args.only else
        f"{len(todo)} active watcher(s), {len(watchers) - len(todo)} disabled")
    for w in todo:
        log(f"→ {w['name']} ({w['type']})")
        try:
            run_watcher(w, ctx, args.dry_run)
        except Exception as e:  # last line of defence: never let one watcher stop the others
            log(f"  ✗ {w['name']}: unexpected error: {e!r}")
    flush_alerts(ctx)
    if not args.dry_run:
        ctx.save_translations()
    if ctx._tr_count:
        log(f"translated {ctx._tr_count} characters this run")
    if not args.dry_run and not args.only:
        y, wk, _ = now().isocalendar()
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        (STATE_DIR / "heartbeat.txt").write_text(f"{y}-W{wk:02d}\n")  # one commit/week keeps the schedule alive


def cmd_list(_args) -> None:
    _, watchers = load_config()
    for w in watchers:
        flag = "on " if w.get("enabled", True) else "OFF"
        extra = "  [keywords]" if w.get("keywords") else ""
        print(f"[{flag}] {w['name']:<32} {w['type']:<10} {w['url']}{extra}")


def cmd_match(args) -> None:
    pats = compile_keywords({"keywords": [args.keyword]})
    ok = kw_ok(pats, args.text)
    print(f"keyword {args.keyword!r}  vs  {args.text!r}  →  {'MATCH' if ok else 'no match'}")


def cmd_test(args) -> None:
    defaults, watchers = load_config()
    w = next((x for x in watchers if x["name"] == args.name), None)
    if not w:
        sys.exit(f"no watcher named {args.name!r}. Try: python monitor.py list")
    ctx = Ctx(defaults)
    data, _ = CHECKERS[w["type"]](w, None, ctx)
    if data.get("missing"):
        print(f"\n'{w['name']}' currently answers 404/410. With allow_missing it counts as 'not online yet': "
              "you'll get a message when it appears.")
        return
    kw = compile_keywords(w)
    print(f"\n=== What '{w['name']}' currently sees ===")
    if kw:
        print(f"(keywords: {w['keywords']}; ✓ = would be reported when new)")
    print()
    if w["type"] == "sitemap":
        for loc, c in data["children"].items():
            print(f"{loc}\n    lastmod: {c['lastmod']}   urls: {len(c['urls'])}")
            for u, lm in list(c["urls"].items())[:3]:
                print(f"      {'✓ ' if kw_ok(kw, u) else '  '}{u}  ({lm})")
        if w.get("snapshots"):
            print(f"\nsnapshots: {len(data.get('snaps', {}))} page(s) saved as the 'before' version")
    elif w["type"] == "wordpress":
        print(f"X-WP-Total header: {data.get('total')}   items returned: {len(data['items'])}\n")
        for i, v in list(data["items"].items())[:10]:
            has = f"  [text: {len(v['text'])} chars]" if "text" in v else ""
            print(f"{'✓ ' if kw_ok(kw, v['title'], v['link'], v.get('text')) else '  '}#{i}  "
                  f"{short_ts(v['modified'])}  {v['title']}  {v['link']}{has}")
    elif w["type"] == "rss":
        feed = feedparser.parse(ctx.get(w["url"], w).content)
        print(f"{len(feed.entries)} entries in the feed:")
        for e in feed.entries[:25]:
            print(f"  {'✓' if kw_ok(kw, rss_text(e)) else '·'} {clean(e.get('title'), 90)}")
    elif w["type"] == "pdf":
        text = data.get("text", "")
        print(f"{data.get('pages')} page(s), {size_str(data.get('size'))}, "
              f"{len(text)} characters of readable text, {len(text.splitlines())} sentences/lines\n")
        if not text:
            print("No readable text found: this looks like a scanned image. Only file changes can be reported.")
        else:
            print(text[:1500] + ("\n…" if len(text) > 1500 else ""))
    elif w.get("mode") == "matches":
        ids = data["ids"]
        print(f"{len(ids)} item(s) found by the pattern" + (" (opening the first 10 without a title):" if w.get("item_url") else ":"))
        opened = 0
        for i, title in list(ids.items())[:40]:
            text = ""
            if not title and w.get("item_url") and opened < 10:
                opened += 1
                try:
                    title, _, text = _item_info(ctx.get(w["item_url"].replace("{id}", i), w).content, w["url"])
                except requests.RequestException as e:
                    title = f"(could not open: {e})"
            print(f"  {'✓' if kw_ok(kw, text, title, i) else '·'} {title or '(no title)'}   [{i[:90]}]")
    elif w.get("mode") == "links":
        print(f"{len(data['links'])} links:")
        for u, t in list(data["links"].items())[:40]:
            print(f"  {'✓' if kw_ok(kw, t, u) else '·'} {t[:60]:<60} {u}")
    else:
        text = data.get("text", "")
        print(text[:3000] or "(empty)")
        print(f"\n[{len(text)} characters]")


def cmd_translate(args) -> None:
    """python monitor.py translate "some text": checks that the translation setup works."""
    defaults, _ = load_config()
    ctx = Ctx(defaults)
    ctx._tr_cache = {}  # don't read or write the real cache
    w = {"translate": args.to}
    out = ctx.tr(w, args.text)
    if out:
        print(f"{args.text}\n  → {out}")
    elif ctx._tr_key and not ctx._tr_off:
        print("(no translation needed: the text already seems to be in that language)")
    else:
        sys.exit("Translation did not work: see the message above.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="check all enabled watchers")
    r.add_argument("--only", metavar="NAME")
    r.add_argument("--dry-run", action="store_true", help="print notifications, send nothing, save nothing")
    t = sub.add_parser("test", help="show what a watcher currently sees")
    t.add_argument("name")
    sub.add_parser("list", help="list watchers")
    m = sub.add_parser("match", help="check how a keyword matches a text")
    m.add_argument("keyword")
    m.add_argument("text")
    tt = sub.add_parser("translate", help="test the translation setup with a text")
    tt.add_argument("text")
    tt.add_argument("--to", default="en")
    args = ap.parse_args()
    cmd = args.cmd or "run"
    if cmd == "run":
        args.only = getattr(args, "only", None)
        args.dry_run = getattr(args, "dry_run", False)
    {"run": cmd_run, "list": cmd_list, "test": cmd_test, "match": cmd_match, "translate": cmd_translate}[cmd](args)


if __name__ == "__main__":
    main()
