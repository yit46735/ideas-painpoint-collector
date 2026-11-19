"""Daily Reddit pain-point collector using public RSS feeds.

Collects recent posts and keyword-matched pain-point discussions from target
subreddits, deduplicates against previously seen post IDs, and writes daily
candidates to `data/candidates_YYYY-MM-DD.json`.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
SEEN_FILE = DATA_DIR / "seen_ids.json"

USER_AGENT = "windows:ideas-painpoint-collector:v1.0 (by /u/ideas_researcher)"
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

SUBREDDIT_GROUPS = {
    "b2b": "sales+marketing+recruiting+freelance+Entrepreneur+SaaS",
    "platform": "Shopify+Notion+Salesforce+AmazonSeller+youtube",
}

PAIN_KEYWORDS = [
    "i wish there was",
    "wish there was",
    "manual process",
    "manually",
    "wasting hours",
    "waste hours",
    "hours every",
    "spreadsheet",
    "excel",
    "csv",
    "export",
    "alternative to",
    "too expensive",
    "copy paste",
    "copy-paste",
    "chrome extension",
    "browser extension",
    "is there a tool",
    "anyone know a tool",
    "automate",
    "automation",
    "tedious",
    "annoying",
    "workaround",
    "stopped working",
]

SEARCH_QUERIES = [
    '"I wish there was" OR "manual process" OR "wasting hours"',
    '"spreadsheet" OR "alternative to" OR "chrome extension"',
]

SELF_PROMO_PATTERNS = [
    "i built ",
    "i've built",
    "we built",
    "launched my",
    "just launched",
    "i made a",
    "i created a",
    "my saas",
    "my chrome extension",
    "feedback from admins",
    "honest feedback",
    "check out my",
    "join the waitlist",
    "beta testers",
]


def strip_html(raw: str) -> str:
    """Convert HTML snippet to clean plain text."""
    text = html.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def fetch_rss(url: str, timeout: int = 15, retries: int = 2) -> list[dict]:
    """Fetch and parse a Reddit Atom RSS feed with 429 backoff."""
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                xml_bytes = resp.read()
            break
        except urllib.error.HTTPError as exc:
            last_err = exc
            if exc.code == 429 and attempt < retries:
                time.sleep(8 * (attempt + 1))
                continue
            raise
    else:
        if last_err:
            raise last_err

    root = ET.fromstring(xml_bytes)
    entries = []
    for entry in root.findall("atom:entry", ATOM_NS):
        post_id = (entry.findtext("atom:id", "", ATOM_NS) or "").strip()
        title = (entry.findtext("atom:title", "", ATOM_NS) or "").strip()
        updated = (entry.findtext("atom:updated", "", ATOM_NS) or "").strip()
        category_el = entry.find("atom:category", ATOM_NS)
        subreddit = category_el.attrib.get("term", "") if category_el is not None else ""
        link_el = entry.find("atom:link", ATOM_NS)
        link = link_el.attrib.get("href", "") if link_el is not None else ""
        content_raw = entry.findtext("atom:content", "", ATOM_NS) or ""
        content = strip_html(content_raw)
        content = re.sub(r"\s*submitted by\s+/u/\S+.*$", "", content).strip()
        entries.append(
            {
                "id": post_id or link,
                "subreddit": f"r/{subreddit}" if subreddit else "",
                "title": title,
                "url": link,
                "updated": updated,
                "body": content[:1200],
            }
        )
    return entries


def score_keywords(title: str, body: str) -> tuple[list[str], bool]:
    """Return matched pain-point keywords and whether post looks like self-promo."""
    haystack = f"{title}\n{body}".lower()
    matched = [kw for kw in PAIN_KEYWORDS if kw in haystack]
    is_promo = any(pat in haystack for pat in SELF_PROMO_PATTERNS)
    return matched, is_promo


def fetch_json(url: str, headers: dict[str, str] | None = None, timeout: int = 15) -> dict:
    """Fetch and decode a JSON endpoint."""
    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_hacker_news() -> list[dict]:
    """Fetch recent Ask HN and pain-point discussions from Algolia HN Search API."""
    queries = [
        "https://hn.algolia.com/api/v1/search_by_date?tags=ask_hn&hitsPerPage=50",
        "https://hn.algolia.com/api/v1/search_by_date?tags=story&query="
        + urllib.parse.quote('"I wish there was" OR "manual process" OR "chrome extension" OR "alternative to"'),
    ]
    items: list[dict] = []
    for url in queries:
        data = fetch_json(url)
        for hit in data.get("hits", []):
            oid = hit.get("objectID")
            if not oid:
                continue
            title = (hit.get("title") or "").strip()
            body = strip_html(hit.get("story_text") or "")
            story_url = hit.get("url") or f"https://news.ycombinator.com/item?id={oid}"
            items.append(
                {
                    "id": f"hn_{oid}",
                    "category": "hacker_news",
                    "subreddit": "HackerNews/AskHN" if "ask_hn" in (hit.get("_tags") or []) else "HackerNews",
                    "source": "hn_algolia",
                    "title": title,
                    "url": story_url,
                    "hn_url": f"https://news.ycombinator.com/item?id={oid}",
                    "updated": hit.get("created_at") or "",
                    "body": body[:1200],
                }
            )
    return items


def fetch_github_issues() -> list[dict]:
    """Fetch recent open GitHub issues expressing workflow or tooling pain points."""
    queries = [
        'is:issue is:open "chrome extension" OR "browser extension" "wish" OR "feature"',
        'is:issue is:open "manual process" OR "wasting hours" OR "copy paste" OR "export csv"',
    ]
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    items: list[dict] = []
    for q in queries:
        url = (
            "https://api.github.com/search/issues?q="
            + urllib.parse.quote(q)
            + "&sort=updated&order=desc&per_page=25"
        )
        data = fetch_json(url, headers=headers)
        for issue in data.get("items", []):
            iid = issue.get("id")
            if not iid:
                continue
            repo_url = issue.get("repository_url") or ""
            repo_name = "/".join(repo_url.split("/")[-2:]) if repo_url else "github"
            items.append(
                {
                    "id": f"gh_{iid}",
                    "category": "github",
                    "subreddit": f"GitHub/{repo_name}",
                    "source": "github_issues",
                    "title": (issue.get("title") or "").strip(),
                    "url": issue.get("html_url") or "",
                    "updated": issue.get("updated_at") or "",
                    "body": strip_html(issue.get("body") or "")[:1200],
                }
            )
    return items


def load_seen_ids() -> set[str]:
    """Load previously seen post IDs."""
    if not SEEN_FILE.exists():
        return set()
    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        return set(data) if isinstance(data, list) else set()
    except (json.JSONDecodeError, OSError):
        return set()


def save_seen_ids(seen: set[str], max_keep: int = 5000) -> None:
    """Persist seen post IDs, keeping the most recent entries bounded."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    items = list(seen)[-max_keep:]
    SEEN_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _ingest_entries(
    entries: list[dict],
    seen: set[str],
    collected: dict[str, dict],
    default_category: str,
    default_source: str,
) -> None:
    """Filter, score, and add unseen entries into collected."""
    for item in entries:
        pid = item["id"]
        if pid in seen or pid in collected:
            continue
        matched, is_promo = score_keywords(item["title"], item["body"])
        if matched or ("?" in item["title"] and len(item["body"]) >= 120):
            collected[pid] = {
                "id": pid,
                "category": item.get("category") or default_category,
                "subreddit": item.get("subreddit") or f"r/{default_category}",
                "source": item.get("source") or default_source,
                "title": item["title"],
                "url": item["url"],
                "updated": item["updated"],
                "matched_keywords": matched,
                "keyword_hits": len(matched),
                "is_self_promo": is_promo,
                "body": item["body"],
            }


def collect_today() -> Path:
    """Collect fresh pain-point posts from Reddit RSS, Hacker News, and GitHub."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    today_str = dt.datetime.now().strftime("%Y-%m-%d")
    out_file = DATA_DIR / f"candidates_{today_str}.json"

    seen = load_seen_ids()
    collected: dict[str, dict] = {}
    errors: list[str] = []

    rss_sources: list[tuple[str, str, str]] = [
        (category, "reddit_rss", f"https://www.reddit.com/r/{multi_sub}/new/.rss?limit=100")
        for category, multi_sub in SUBREDDIT_GROUPS.items()
    ]
    extra_rss = [u.strip() for u in os.environ.get("EXTRA_RSS_URLS", "").split(",") if u.strip()]
    for idx, url in enumerate(extra_rss, start=1):
        rss_sources.append((f"extra_rss_{idx}", "extra_rss", url))

    for category, source_type, url in rss_sources:
        try:
            entries = fetch_rss(url)
            _ingest_entries(entries, seen, collected, category, source_type)
        except (urllib.error.URLError, urllib.error.HTTPError, ET.ParseError, TimeoutError) as exc:
            errors.append(f"{category} ({source_type}): {exc}")
        time.sleep(2.0)

    for label, fetcher in [("hacker_news", fetch_hacker_news), ("github", fetch_github_issues)]:
        try:
            entries = fetcher()
            _ingest_entries(entries, seen, collected, label, label)
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError) as exc:
            errors.append(f"{label}: {exc}")

    ranked = sorted(
        collected.values(),
        key=lambda x: (
            0 if x["is_self_promo"] else 1,
            x["keyword_hits"],
            1 if "?" in x["title"] else 0,
            x["updated"],
        ),
        reverse=True,
    )

    payload = {
        "date": today_str,
        "total_candidates": len(ranked),
        "errors": errors,
        "candidates": ranked[:60],
    }
    out_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # Mark returned candidates as seen so tomorrow's report stays fresh
    for item in ranked[:60]:
        seen.add(item["id"])
    save_seen_ids(seen)

    print(
        json.dumps(
            {
                "output_file": str(out_file),
                "total_candidates": len(ranked),
                "saved_top": len(payload["candidates"]),
                "error_count": len(errors),
            },
            ensure_ascii=False,
        )
    )
    return out_file


if __name__ == "__main__":
    collect_today()
