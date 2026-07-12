"""Naver News search API list/content collection.

Fetches a candidate list of news articles for a query via Naver's Open API
`news.json` endpoint, then (optionally) fetches each article's full page
text concurrently -- the list call is a single fast request, but visiting
every article's original URL is N independent, slow, blocking HTTP calls,
so that step is parallelized with a thread pool rather than done one at a
time in a loop.
"""

import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.utils import parsedate_to_datetime
from html import unescape

import httpx
import trafilatura
from dotenv import load_dotenv

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_BASE_DIR, ".env"))

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return unescape(_TAG_RE.sub("", text))


def _naver_credentials() -> tuple[str, str]:
    """Reuses the same NAVER_CLIENT_IDS/NAVER_CLIENT_SECRETS (or single
    NAVER_CLIENT_ID/NAVER_CLIENT_SECRET) convention as server.py, but only
    needs one key pair here -- this module doesn't do the multi-key daily-
    quota rotation server.py's NaverEngine does, since it's meant to be
    a lighter-weight, standalone fetch step."""
    ids = [v.strip() for v in os.environ.get("NAVER_CLIENT_IDS", "").split(",") if v.strip()]
    secrets = [v.strip() for v in os.environ.get("NAVER_CLIENT_SECRETS", "").split(",") if v.strip()]
    if ids and secrets:
        return ids[0], secrets[0]

    client_id = os.environ.get("NAVER_CLIENT_ID")
    client_secret = os.environ.get("NAVER_CLIENT_SECRET")
    if client_id and client_secret:
        return client_id, client_secret

    raise RuntimeError(
        "NAVER_CLIENT_IDS/NAVER_CLIENT_SECRETS (또는 NAVER_CLIENT_ID/NAVER_CLIENT_SECRET) "
        ".env 환경변수가 설정되어 있지 않습니다."
    )


def fetch_news_list(query: str, display: int = 30, timeout: float = 10.0) -> list[dict]:
    """Calls Naver's `news.json` search endpoint and returns up to `display`
    candidate documents as {"title", "url", "description", "pub_date"}
    dicts. HTML tags/entities in title/description are stripped;
    "pub_date" is a timezone-aware datetime parsed from Naver's RFC-2822
    "pubDate" field (or None if missing/unparseable). Does not fetch each
    article's own page -- see fetch_contents() for that."""
    client_id, client_secret = _naver_credentials()
    response = httpx.get(
        "https://openapi.naver.com/v1/search/news.json",
        headers={"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret},
        params={"query": query, "display": display},
        timeout=timeout,
    )
    response.raise_for_status()
    items = response.json().get("items", [])

    documents: list[dict] = []
    for item in items:
        try:
            pub_date: datetime | None = parsedate_to_datetime(item["pubDate"])
        except (KeyError, ValueError, TypeError):
            pub_date = None
        documents.append(
            {
                "title": _strip_html(item["title"]),
                # Naver's own "link" can be a redirect/mirror page; prefer
                # the real publisher URL when present.
                "url": item.get("originallink") or item["link"],
                "description": _strip_html(item["description"]),
                "pub_date": pub_date,
            }
        )
    return documents


def _fetch_one_content(url: str, max_chars: int) -> str:
    """Best-effort full-article-text fetch for one URL. Any failure
    (timeout, blocked, non-HTML, no extractable content) just returns "" so
    one bad link doesn't break the whole batch -- the caller falls back to
    the article's `description` when `content` is empty."""
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return ""
        extracted = trafilatura.extract(downloaded, include_comments=False, favor_precision=True) or ""
        return extracted[:max_chars]
    except Exception:
        return ""


def fetch_contents(documents: list[dict], max_chars: int = 3000, max_workers: int = 8) -> list[dict]:
    """Adds a "content" key (full article text via trafilatura) to each
    document in place, fetched concurrently via a thread pool -- every
    fetch is an independent blocking HTTP request, so running them one
    after another in a loop would just add all their latencies together.
    Returns the same list for convenient chaining."""
    if not documents:
        return documents

    candidates = [doc for doc in documents if doc.get("url")]
    if candidates:
        with ThreadPoolExecutor(max_workers=min(len(candidates), max_workers)) as pool:
            texts = list(pool.map(lambda doc: _fetch_one_content(doc["url"], max_chars), candidates))
        for doc, text in zip(candidates, texts):
            doc["content"] = text

    for doc in documents:
        doc.setdefault("content", "")
    return documents


def fetch(query: str, display: int = 30, fetch_full_content: bool = True) -> list[dict]:
    """One-shot convenience: fetch the candidate news list, then (unless
    disabled) fetch each article's full content concurrently. This is the
    function pipeline.py calls."""
    documents = fetch_news_list(query, display=display)
    if fetch_full_content:
        documents = fetch_contents(documents)
    return documents
