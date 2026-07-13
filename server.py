import html
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import date

import anyio.to_thread
import httpx
import trafilatura
from trafilatura.settings import use_config as _trafilatura_use_config
from dotenv import load_dotenv
from ddgs import DDGS
from mcp.server.fastmcp import FastMCP

from bm25_search import bm25_ranker, tokenizer

load_dotenv()

# Windows defaults stdout/stderr to the system codepage (e.g. cp949 on Korean
# Windows), which cannot represent arbitrary Unicode from search results
# (accented Latin, em/en dashes, other scripts, ...) and would crash the MCP
# stdio protocol mid-response. Force UTF-8 regardless of locale.
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# MCP clients may launch this script from an arbitrary working directory, so
# a relative LOG_FILE (the common case, e.g. "search.log") is always resolved
# against this file's own directory rather than the process cwd.
_log_file_setting = os.environ.get("LOG_FILE", "search.log")
LOG_FILE = (
    _log_file_setting if os.path.isabs(_log_file_setting) else os.path.join(_BASE_DIR, _log_file_setting)
)
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

# stdout is reserved for the MCP stdio protocol, so logs go to a file and stderr only.
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ],
)
logger = logging.getLogger("web-search")


# ---------------------------------------------------------------------------
# Exceptions used to signal *why* an engine couldn't produce results, so the
# router can decide how to react (fall back silently vs. log as a real bug).
# ---------------------------------------------------------------------------


class EngineUnavailable(Exception):
    """Raised when an engine cannot be used at all (e.g. missing credentials).
    This is an expected, non-error condition -- the router treats it as a
    signal to move on to the next engine rather than a bug."""


class QuotaExceeded(Exception):
    """Raised when an engine's call quota is exhausted for the day (e.g. all
    registered Naver keys are past their daily-limit threshold). Also treated
    as expected by the router, not logged as an error."""


class PolicyRestricted(Exception):
    """Raised when every raw result an engine got back matched an excluded
    keyword (see EXCLUDE_KEYWORDS) -- a deliberate content policy exclusion,
    not a technical failure. Still worth falling back to another engine for:
    a different engine may return different, non-excluded results for the
    same query."""


class CallStats:
    """Tracks how many times each "key" (an engine name, or an individual
    Naver credential like "naver_1") has been called *today*, and persists
    that to a JSON file on every write so the count survives server restarts
    (important for daily quota tracking across multiple process lifetimes)."""

    def __init__(self, path: str):
        self._path = path
        # Under sse/streamable-http transport, concurrent requests run their
        # engine calls in separate worker threads (see anyio.to_thread.run_sync
        # in web_search below), so reads/writes to the shared JSON-backed
        # counters need a real lock -- without it, two concurrent record()
        # calls could race and silently lose an increment, or interleave
        # writes to the file.
        self._lock = threading.Lock()
        self._data = self._load()
        # Handles the "server was off across midnight" case: reset anything
        # stale as soon as we read the file back in, rather than waiting for
        # each key to happen to be used again.
        self.reset_stale_entries()

    def _load(self) -> dict:
        # Missing file (first run) or corrupt JSON both just mean "no history yet".
        # Anything else (permissions, disk I/O) shouldn't take the whole server
        # down at startup either -- quota tracking degrading to "start from 0"
        # is far better than the process failing to come up at all.
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        except OSError:
            logger.exception("호출 통계 파일(%s) 읽기 실패 -- 빈 통계로 시작", self._path)
            return {}

    def _save(self) -> None:
        # Best-effort: a disk hiccup here must not bubble up through record()
        # into engine.search() and fail an otherwise-successful search result
        # just because the quota counter couldn't be persisted this once.
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except OSError:
            logger.exception("호출 통계 파일(%s) 저장 실패 -- 이번 호출 수는 기록되지 않음", self._path)

    @staticmethod
    def _today() -> str:
        return date.today().isoformat()

    def today_count(self, key: str) -> int:
        with self._lock:
            entry = self._data.get(key)
            # If the stored date isn't today, the counter has implicitly reset
            # from the caller's point of view (record() below also resets it
            # on disk lazily the next time this key is used) -- but see
            # reset_stale_entries() for actively resetting everything, used
            # so a key doesn't have to be called again before it reflects 0.
            if not entry or entry.get("date") != self._today():
                return 0
            return entry.get("count", 0)

    def record(self, key: str) -> None:
        with self._lock:
            entry = self._data.get(key)
            if not entry or entry.get("date") != self._today():
                entry = {"date": self._today(), "count": 0}
            entry["count"] += 1
            self._data[key] = entry
            self._save()  # written synchronously on every call -- simplicity over throughput

    def reset_stale_entries(self) -> None:
        """Zero out (and persist) every counter whose stored date is no
        longer today. Called once at startup (covers the server having been
        off across a midnight rollover) and once per tick by the hit-count
        background thread (covers midnight passing while the server keeps
        running), so counts reset once a day even for keys nobody happens to
        call right at 00:00."""
        with self._lock:
            today = self._today()
            changed = False
            for key, entry in self._data.items():
                if entry.get("date") != today:
                    entry["date"] = today
                    entry["count"] = 0
                    changed = True
            if changed:
                self._save()


# ---------------------------------------------------------------------------
# Content-policy keyword filtering: results whose title+snippet mention any
# of these are dropped before being returned (e.g. sports coverage crowding
# out an unrelated finance/news query on days with big sports headlines).
# ---------------------------------------------------------------------------

EXCLUDE_KEYWORDS = [
    w.strip()
    for w in os.environ.get(
        "EXCLUDE_KEYWORDS", "야구,축구,농구,배구,골프,올림픽,월드컵,프로야구,K리그,e스포츠"
    ).split(",")
    if w.strip()
]
# When filtering is active, engines ask their underlying API/library for
# this many raw results instead of just `max_results`, so that dropping
# excluded ones still leaves `max_results` to return.
EXCLUDE_OVERFETCH_COUNT = int(os.environ.get("EXCLUDE_OVERFETCH_COUNT", "10"))


def _fetch_count_for(max_results: int) -> int:
    if not EXCLUDE_KEYWORDS:
        return max_results
    return max(max_results, EXCLUDE_OVERFETCH_COUNT)


def _matched_excluded_keyword(text: str) -> str | None:
    for keyword in EXCLUDE_KEYWORDS:
        if keyword in text:
            return keyword
    return None


def _filter_excluded(
    results: list[dict], max_results: int, engine_name: str, request_id: str = ""
) -> list[dict]:
    """Drops any result matching EXCLUDE_KEYWORDS (checked against title+
    snippet) and returns at most `max_results` of what's left, preserving
    the engine's original ranking order. If there WERE raw results but every
    single one got excluded, raises PolicyRestricted rather than silently
    returning an empty list -- that's a distinct, worth-surfacing outcome
    from "the API genuinely found nothing"."""
    if not EXCLUDE_KEYWORDS:
        return results[:max_results]

    kept = []
    for r in results:
        hit = _matched_excluded_keyword(f"{r['title']} {r['snippet']}")
        if hit:
            logger.info(
                "[%s][%s] 불용어 '%s' 포함으로 결과 제외: %s",
                request_id, engine_name, hit, r["title"],
            )
            continue
        kept.append(r)
        if len(kept) >= max_results:
            break

    if results and not kept:
        logger.warning(
            "[%s][%s] 원본 결과 %d건이 모두 불용어에 걸려 제외됨", request_id, engine_name, len(results),
        )
        raise PolicyRestricted("정책상 관련 키워드로 검색이 불가능합니다.")
    return kept


# ---------------------------------------------------------------------------
# Engines. Each one implements search() -> list[{"title","url","snippet"}]
# (a common schema so SearchRouter can format/merge results the same way
# regardless of which engine produced them) and usage_report() for the
# search_stats tool. `request_id` is threaded through purely for log
# correlation -- it ties every log line for one web_search() call together.
# ---------------------------------------------------------------------------


class SearchEngine:
    name = "base"
    requires_key = False

    def search(self, query: str, max_results: int, request_id: str = "") -> list[dict]:
        raise NotImplementedError

    def usage_report(self) -> str:
        return ""


class DuckDuckGoEngine(SearchEngine):
    """Free, keyless search via the `ddgs` library. Always available, so it
    anchors the default engine set."""

    name = "duckduckgo"
    requires_key = False

    def __init__(self, stats: CallStats):
        self._stats = stats

    def search(self, query: str, max_results: int, request_id: str = "") -> list[dict]:
        fetch_count = _fetch_count_for(max_results)
        logger.info(
            "[%s][duckduckgo] 검색 시작 (키 불필요, 호출 한도 없음) query=%r max_results=%d fetch_count=%d",
            request_id, query, max_results, fetch_count,
        )
        started = time.perf_counter()
        self._stats.record(self.name)
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=fetch_count))
        elapsed = time.perf_counter() - started
        logger.info(
            "[%s][duckduckgo] 검색 성공: %d건 (소요 %.3fs, 오늘 누적 %d회)",
            request_id, len(results), elapsed, self._stats.today_count(self.name),
        )
        shaped = [
            {"title": r["title"], "url": r["href"], "snippet": r["body"]}
            for r in results
        ]
        return _filter_excluded(shaped, max_results, self.name, request_id)

    def usage_report(self) -> str:
        return f"duckduckgo (키 불필요): {self._stats.today_count(self.name)}회 호출 (오늘)"


class NaverEngine(SearchEngine):
    """Searches via Naver's Open API, rotating across multiple registered
    Client ID/Secret pairs (each has its own daily quota) and skipping to the
    next one once a key nears its limit."""

    name = "naver"
    requires_key = True
    _TAG_RE = re.compile(r"<.*?>")

    def __init__(
        self,
        stats: CallStats,
        credentials: list[tuple[str, str]],
        daily_limit: int,
        threshold: float,
        endpoint: str = "news",
        name: str | None = None,
    ):
        self._stats = stats
        self._credentials = credentials
        self._daily_limit = daily_limit
        self._threshold = threshold
        # Naver's Open API has several search verticals (news, webkr, blog,
        # cafearticle, ...) sharing the same app/quota. Two NaverEngine
        # instances can be registered with the same credentials but
        # different endpoints (e.g. "naver" for news, "naver_web" for
        # general web documents) -- `name` distinguishes them as separate
        # SearchRouter entries while `_key_name()` below still keys off
        # credential *index*, not endpoint, so both instances correctly
        # share the same underlying per-key quota (it's the same Naver app).
        self._endpoint = endpoint
        if name:
            self.name = name

    @staticmethod
    def _key_name(index: int) -> str:
        return f"naver_{index + 1}"

    @classmethod
    def _strip_html(cls, text: str) -> str:
        return html.unescape(cls._TAG_RE.sub("", text))

    def search(self, query: str, max_results: int, request_id: str = "") -> list[dict]:
        if not self._credentials:
            logger.warning("[%s][%s] 등록된 키가 없어 사용할 수 없음", request_id, self.name)
            raise EngineUnavailable(
                "NAVER_CLIENT_IDS/NAVER_CLIENT_SECRETS(.env) 환경변수가 설정되지 않았습니다."
            )

        # Each key gets its own quota bucket ("naver_1", "naver_2", ...) in
        # CallStats, so keys are entirely independent -- key 2 can still be
        # used even if key 1 is exhausted. We walk them in registration order
        # and use the first one that's still under its threshold line.
        limit_line = self._daily_limit * self._threshold
        fetch_count = _fetch_count_for(max_results)
        logger.info(
            "[%s][%s] 검색 시작 (endpoint=%s, 키 %d개 등록됨, 키당 한도 %d, 임계값 %.0f%%) "
            "query=%r max_results=%d fetch_count=%d",
            request_id, self.name, self._endpoint, len(self._credentials), self._daily_limit,
            self._threshold * 100, query, max_results, fetch_count,
        )
        for index, (client_id, client_secret) in enumerate(self._credentials):
            key_name = self._key_name(index)
            count = self._stats.today_count(key_name)
            if count >= limit_line:
                logger.info(
                    "[%s][%s] %s 건너뜀 (오늘 %d/%d회 사용 >= 임계선 %.0f회, 한도의 %.0f%%)",
                    request_id, self.name, key_name, count, self._daily_limit, limit_line, self._threshold * 100,
                )
                continue

            logger.info(
                "[%s][%s] %s 사용 (오늘 %d/%d회, 사용 후 %d회 예정) client_id=%s***",
                request_id, self.name, key_name, count, self._daily_limit, count + 1, client_id[:4],
            )
            started = time.perf_counter()
            self._stats.record(key_name)
            try:
                response = httpx.get(
                    f"https://openapi.naver.com/v1/search/{self._endpoint}.json",
                    headers={
                        "X-Naver-Client-Id": client_id,
                        "X-Naver-Client-Secret": client_secret,
                    },
                    params={"query": query, "display": fetch_count},
                    timeout=10.0,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                elapsed = time.perf_counter() - started
                logger.error(
                    "[%s][%s] %s HTTP 오류 (%.3fs): status=%d body=%.300s",
                    request_id, self.name, key_name, elapsed, e.response.status_code, e.response.text,
                )
                raise
            elapsed = time.perf_counter() - started
            items = response.json().get("items", [])
            logger.info(
                "[%s][%s] %s 검색 성공: %d건 (소요 %.3fs, status=%d, endpoint=%s)",
                request_id, self.name, key_name, len(items), elapsed, response.status_code, self._endpoint,
            )
            shaped = [
                {
                    "title": self._strip_html(item["title"]),
                    # "originallink" (news endpoint only) is the real
                    # publisher URL; Naver's own "link" can be a redirect/
                    # mirror page that's less reliable to fetch full text
                    # from later, so prefer originallink when present.
                    "url": item.get("originallink") or item["link"],
                    "snippet": self._strip_html(item["description"]),
                }
                for item in items
            ]
            return _filter_excluded(shaped, max_results, self.name, request_id)

        logger.warning("[%s][%s] 등록된 키 %d개 모두 한도 근접, 사용 불가", request_id, self.name, len(self._credentials))
        raise QuotaExceeded(
            f"등록된 네이버 키 {len(self._credentials)}개가 모두 일일 호출 한도의 "
            f"{self._threshold:.0%}에 도달했습니다."
        )

    def usage_report(self) -> str:
        if not self._credentials:
            return f"{self.name} (키 필요): 등록된 키 없음"
        lines = []
        for index in range(len(self._credentials)):
            key_name = self._key_name(index)
            count = self._stats.today_count(key_name)
            remaining = max(self._daily_limit - count, 0)
            ratio = count / self._daily_limit if self._daily_limit else 0
            lines.append(
                # `key_name` (quota bucket) is shared across every NaverEngine
                # instance using the same credentials regardless of endpoint,
                # so this line's numbers are identical whether it's printed
                # from the "naver" (news) or "naver_web" (webkr) instance --
                # self.name/self._endpoint are just here to make that explicit.
                f"{self.name}:{key_name} [{self._endpoint}] (키 필요): {count}/{self._daily_limit}회 호출 "
                f"({ratio:.1%}, 남은 호출 {remaining}회)"
            )
        return "\n".join(lines)


def _format_results(results: list[dict]) -> str:
    lines = []
    for i, r in enumerate(results, 1):
        body = r.get("content") or r["snippet"]
        lines.append(f"{i}. {r['title']}\n   {r['url']}\n   {body}")
    return "\n\n".join(lines)


# Boilerplate that survives trafilatura's extraction on Korean news sites --
# copyright footers, redistribution/AI-training notices, comment-section
# labels -- none of it is actual article content, so it's stripped before
# the text is even counted toward min_useful_chars/max_chars.
_BOILERPLATE_PATTERNS = [
    re.compile(r"Copyright\s*[ⓒ©Cc].{0,100}", re.IGNORECASE),
    re.compile(r"무단\s*전재\s*(및|,)?\s*재배포\s*(금지)?"),
    re.compile(r"무단\s*전재\s*(금지)?"),
    re.compile(r"AI\s*학습\s*(및|,)?\s*이용\s*금지"),
    re.compile(r"^\s*댓글\s*$", re.MULTILINE),
]


def _clean_content(text: str) -> str:
    """Strips known non-article boilerplate and collapses the excessive
    blank lines/whitespace some sites' markup produces (e.g. transcript-
    style articles with one sentence per line and blank lines between),
    so the LLM gets dense readable text instead of noisy formatting."""
    for pattern in _BOILERPLATE_PATTERNS:
        text = pattern.sub("", text)
    lines = [line.strip() for line in text.splitlines()]
    return " ".join(line for line in lines if line)


def _fetch_page_text(url: str, max_chars: int) -> str:
    """Fetches a result URL and extracts its main article/body text (not
    nav/ads/boilerplate) via trafilatura -- readability-style extraction,
    much better than naively stripping HTML tags (e.g. it correctly returns
    ~0 chars for JS-rendered pages with no real static content, instead of
    returning nav-menu text as if it were the article). Best-effort: any
    failure (timeout, non-HTML, 404, blocked, no content found, ...) just
    returns "" so one bad link doesn't affect anything else.

    Uses _TRAFILATURA_CONFIG (FETCH_CONTENT_TIMEOUT_SECONDS) rather than
    trafilatura's own default -- its default DOWNLOAD_TIMEOUT is 30s with no
    cap on how large a file it'll try to pull down (MAX_FILE_SIZE defaults
    to 20MB), so one candidate that happens to be a large PDF/binary can
    single-handedly stall this whole call for 30+ seconds. _enrich_with_
    content submits every candidate to the same thread pool and blocks
    until they've all resolved, so this per-fetch cap is what actually
    bounds the enrichment step's wall-clock time -- found via a review of
    URL-query handling that turned up exactly this case live (a search
    result pointing at a large OWASP PDF took 37.9s to fetch)."""
    try:
        downloaded = trafilatura.fetch_url(url, config=_TRAFILATURA_CONFIG)
        if not downloaded:
            return ""
        # include_comments=False: trafilatura defaults to True, which is
        # exactly why comment-section text ("댓글" etc.) was leaking into
        # the extracted article text. favor_precision=True biases its own
        # boilerplate-vs-content heuristics toward dropping more borderline
        # (likely non-article) text rather than keeping it -- both are
        # trafilatura's own documented options, preferred over hand-rolled
        # regexes wherever they cover the same problem.
        extracted = trafilatura.extract(downloaded, include_comments=False, favor_precision=True) or ""
        return _clean_content(extracted)[:max_chars]
    except Exception:
        return ""


_PDF_URL_RE = re.compile(r"\.pdf(?:[?#]|$)", re.IGNORECASE)


def _looks_like_pdf(url: str) -> bool:
    """PDFs are binary documents trafilatura's HTML-oriented extractor
    doesn't handle well -- verified live: extracting one produced a
    fragmented, low-signal mess ("org 2 The Open Web Application Security
    Project (OWASP) is a worldwide free and open com... cross domain policy
    (OTG-CONFIG-008)...") rather than real usable article text, on top of
    tending to be large/slow downloads (see FETCH_CONTENT_TIMEOUT_SECONDS).
    Detected by URL extension -- cheap, no network round trip -- so these
    candidates are skipped before ever being submitted to the fetch pool,
    freeing that slot for the next (non-PDF) candidate instead."""
    return bool(_PDF_URL_RE.search(url))


def _filter_out_pdfs(results_by_engine: dict[str, list[dict]], request_id: str = "") -> None:
    """Mutates results_by_engine in place, dropping any result whose URL is
    a PDF entirely -- not just skipping our own content-fetch for it (see
    _looks_like_pdf/​_enrich_with_content), but excluding it from the result
    set altogether. Naver's own search snippet for a PDF result is often
    just a raw, out-of-context fragment of the document's text (e.g.
    "org 2 The Open Web Application Security Project (OWASP) is a
    worldwide free and open com...") -- unreadable on its own regardless of
    whether Lens or Naver produced it, so there's nothing useful left to
    show once PDFs are ruled out as a content-enrichment source."""
    for name, results in results_by_engine.items():
        kept = [r for r in results if not (r.get("url") and _looks_like_pdf(r["url"]))]
        dropped = len(results) - len(kept)
        if dropped:
            logger.info("[%s][%s] PDF 결과 %d건 제외", request_id, name, dropped)
            results_by_engine[name] = kept


def _enrich_with_content(
    results: list[dict], top_n: int, max_chars: int, min_useful_chars: int, request_id: str = ""
) -> None:
    """Mutates `results` in place, adding a "content" key (full fetched
    article text, replacing the ~150-char snippet in the formatted output)
    for up to `top_n` entries. Rather than blindly taking the first `top_n`
    URLs, this fetches every candidate concurrently and keeps walking down
    the list until `top_n` of them have yielded *useful* content (>=
    min_useful_chars) -- so JS-rendered pages that return only nav
    boilerplate don't crowd out real article content, they're just skipped
    in favor of the next candidate. PDF URLs are excluded from candidates
    entirely (see _looks_like_pdf)."""
    all_candidates = [r for r in results if r.get("url")]
    candidates = [r for r in all_candidates if not _looks_like_pdf(r["url"])]
    skipped_pdfs = len(all_candidates) - len(candidates)
    if skipped_pdfs:
        logger.info("[%s] PDF로 추정되는 후보 %d개는 본문 보강 대상에서 제외", request_id, skipped_pdfs)
    if not candidates or top_n <= 0:
        return
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=min(len(candidates), 8)) as pool:
        texts = list(pool.map(lambda r: _fetch_page_text(r["url"], max_chars), candidates))
    good = 0
    for r, text in zip(candidates, texts):
        if good >= top_n:
            break
        if len(text) >= min_useful_chars:
            r["content"] = text
            good += 1
        else:
            logger.info(
                "[%s] 본문 추출 실패/부실 (%d자 미만): %s", request_id, min_useful_chars, r["url"]
            )
    logger.info(
        "[%s] 본문 보강 완료 (%.3fs): 후보 %d개 중 %d개 채택",
        request_id, time.perf_counter() - started, len(candidates), good,
    )


# ---------------------------------------------------------------------------
# BM25 relevance filtering: engines run independently and each contributes
# its own top results, so a vertical tuned for a different kind of query
# (e.g. naver_encyc's dictionary/encyclopedia entries) can occasionally
# surface something with almost no real connection to the query -- just an
# incidental shared word rather than genuine relevance (e.g. a "주가"
# (stock price) query pulling in a "영구 제모 효과" (permanent hair-removal)
# dictionary entry). This reuses the same BM25 model as the standalone
# bm25_search pipeline (bm25_search.tokenizer / bm25_ranker), scored against
# each result's title+description -- the description Naver/DuckDuckGo
# already returned, no extra fetch needed -- so it runs before, and gates,
# the expensive full-content fetch in _enrich_with_content below: only
# results that survive this filter are worth fetching full text for.
# ---------------------------------------------------------------------------

RELEVANCE_FILTER = os.environ.get("RELEVANCE_FILTER", "true").lower() == "true"
RELEVANCE_MIN_SCORE_RATIO = float(os.environ.get("RELEVANCE_MIN_SCORE_RATIO", "0.15"))


def _filter_by_relevance(
    query: str, results_by_engine: dict[str, list[dict]], min_score_ratio: float, request_id: str = ""
) -> None:
    """Mutates results_by_engine in place, dropping any result whose
    title+description has low BM25 overlap with the query relative to the
    best-matching result in this batch (score >= best_score * min_score_ratio
    survives). Relative rather than an absolute cutoff, since raw BM25
    scores aren't comparable across different queries/corpus sizes. If every
    candidate scores 0 (e.g. the query tokenizes to nothing but particles/
    stopwords), nothing is dropped -- there's no signal to distinguish
    "relevant" from "not" in that case."""
    flat = [(name, r) for name, results in results_by_engine.items() for r in results]
    if not flat:
        return

    tokenized_query = tokenizer.tokenize(query)
    if not tokenized_query:
        return

    texts = [f"{r['title']} {r['snippet']}" for _, r in flat]
    tokenized_documents = tokenizer.tokenize_batch(texts)
    index = bm25_ranker.build_index(tokenized_documents)
    scored = bm25_ranker.search(index, tokenized_query, top_k=len(flat))
    if not scored or scored[0][1] <= 0:
        return

    threshold = scored[0][1] * min_score_ratio
    dropped = 0
    for i, score in scored:
        if score < threshold:
            name, r = flat[i]
            results_by_engine[name].remove(r)
            dropped += 1
            logger.info(
                "[%s][%s] BM25 유사도 낮음으로 제외 (score=%.3f < 임계값=%.3f): %s",
                request_id, name, score, threshold, r["title"],
            )
    if dropped:
        logger.info("[%s] BM25 관련성 필터: 결과 %d건 중 %d건 제외", request_id, len(flat), dropped)


class SearchRouter:
    """Runs the requested engines and automatically falls back to any other
    registered engine if one fails or runs out of quota.

    `default_engines` controls what "all" expands to (kept separate from
    the full registry so a heavy fallback-only engine, if one is registered
    later, wouldn't run on every call -- only when something else fails)."""

    def __init__(self, engines: list[SearchEngine], default_engines: list[str]):
        self._engines = {engine.name: engine for engine in engines}
        self._default_engines = default_engines

    def _run_one(self, name: str, query: str, max_results: int, request_id: str):
        """Runs a single engine and normalizes the outcome to
        (name, "unknown"|"skip"|"error"|"ok", payload) so the caller (which
        submits many of these to a thread pool at once) can collect results
        without any engine-specific branching at the call site."""
        engine = self._engines.get(name)
        if engine is None:
            logger.warning("[%s][%s] 알 수 없는 검색엔진 요청", request_id, name)
            return name, "unknown", None

        attempt_started = time.perf_counter()
        try:
            results = engine.search(query, max_results, request_id=request_id)
        except (EngineUnavailable, QuotaExceeded, PolicyRestricted) as e:
            elapsed = time.perf_counter() - attempt_started
            logger.info("[%s][%s] 사용 불가 (%.3fs): %s", request_id, name, elapsed, e)
            return name, "skip", e
        except Exception as e:
            elapsed = time.perf_counter() - attempt_started
            logger.exception("[%s][%s] 검색 중 오류 발생 (%.3fs)", request_id, name, elapsed)
            return name, "error", e

        elapsed = time.perf_counter() - attempt_started
        logger.info("[%s][%s] 완료 (%.3fs): %d건", request_id, name, elapsed, len(results))
        return name, "ok", results

    def _run_batch(
        self,
        names: list[str],
        query: str,
        max_results: int,
        request_id: str,
        done: set[str],
        section_items: list[tuple[str, str]],
        results_by_engine: dict[str, list[dict]],
        outcomes: list[str],
    ) -> None:
        """Runs every not-yet-tried engine in `names` concurrently (each
        engine is an independent service/API, so there's no reason to wait
        for one before starting the next) and folds each result back into
        the shared accumulators. Only the main thread touches those
        accumulators -- worker threads just run engine.search() and hand
        their outcome back via the Future, so there's no need for a lock
        around this bookkeeping (CallStats has its own lock for the state
        engines actually mutate concurrently)."""
        pending = [n for n in names if n not in done]
        if not pending:
            return
        # Not a `with` block on purpose: ThreadPoolExecutor.__exit__ calls
        # shutdown(wait=True), which would block here until every submitted
        # thread finishes -- including ones we've already given up on via
        # the per-future timeout below. shutdown(wait=False) lets a slow
        # straggler keep running in the background (it'll finish on its own
        # once its underlying HTTP call times out) without holding up this
        # request's response.
        pool = ThreadPoolExecutor(max_workers=len(pending))
        try:
            futures = {n: pool.submit(self._run_one, n, query, max_results, request_id) for n in pending}
            for name in pending:
                done.add(name)
                try:
                    _, status, payload = futures[name].result(timeout=SEARCH_ENGINE_TIMEOUT_SECONDS)
                except FutureTimeoutError:
                    logger.warning(
                        "[%s][%s] 시간 초과 (%.0fs) -- 건너뜀", request_id, name, SEARCH_ENGINE_TIMEOUT_SECONDS,
                    )
                    section_items.append(("text", f"[{name}]\n{SEARCH_ENGINE_TIMEOUT_SECONDS:.0f}초 내에 응답이 없어 건너뛰었습니다."))
                    outcomes.append(f"{name}=timeout")
                    continue

                if status == "unknown":
                    section_items.append(("text", f"[{name}]\n알 수 없는 검색엔진입니다."))
                    outcomes.append(f"{name}=unknown")
                elif status == "skip":
                    section_items.append(("text", f"[{name}]\n{payload}"))
                    outcomes.append(f"{name}=skip")
                elif status == "error":
                    section_items.append(("text", f"[{name}]\n검색 중 오류가 발생했습니다: {payload}"))
                    outcomes.append(f"{name}=error")
                else:
                    results = payload
                    outcomes.append(f"{name}=ok({len(results)}건)")
                    results_by_engine[name] = results
                    section_items.append(("results", name))
        finally:
            pool.shutdown(wait=False)

    def search(self, query: str, max_results: int, engines: str = "all", output_format: str = "text") -> str:
        # A short random id purely for tying together every log line that
        # belongs to this one call (multiple web_search() calls can be
        # in flight/interleaved in the log file otherwise).
        request_id = uuid.uuid4().hex[:8]
        request_started = time.perf_counter()
        requested = (
            list(self._default_engines)
            if engines == "all"
            else [e.strip() for e in engines.split(",") if e.strip()]
        )
        logger.info(
            "===== [%s] web_search 요청 시작 ===== query=%r max_results=%d engines_param=%r resolved=%s",
            request_id, query, max_results, engines, requested,
        )

        # Each item is either ("text", already-formatted string) for
        # errors/unknown engines, or ("results", engine_name) as a
        # placeholder -- the actual result dicts live in results_by_engine
        # and get content-enriched (see below) *before* being rendered to
        # text, so this two-pass structure is what lets enrichment work
        # across every engine's combined results at once.
        section_items: list[tuple[str, str]] = []
        results_by_engine: dict[str, list[dict]] = {}
        outcomes: list[str] = []  # per-engine one-line summary for the closing log line
        done: set[str] = set()

        # First round: every requested engine runs concurrently (they're
        # independent services -- running them one after another would just
        # add their latencies together for no benefit).
        self._run_batch(requested, query, max_results, request_id, done, section_items, results_by_engine, outcomes)

        # Only fall back to whatever's left over while we don't yet have any
        # real results -- e.g. if duckduckgo already returned results, a
        # missing Naver key shouldn't also trigger a second round; fallback
        # exists so a call still gets an answer, not so every engine always
        # runs. This is a single extra concurrent round (covering engines
        # registered but outside `requested`), not an iterative retry queue.
        has_results = any(results_by_engine.values())
        if not has_results:
            fallback_candidates = [name for name in self._engines if name not in done]
            if fallback_candidates:
                logger.info(
                    "[%s] 결과 없음 -> 나머지 등록된 엔진으로 폴백: %s", request_id, fallback_candidates,
                )
                self._run_batch(
                    fallback_candidates, query, max_results, request_id, done, section_items,
                    results_by_engine, outcomes,
                )

        try:
            _filter_out_pdfs(results_by_engine, request_id)
        except Exception:
            logger.exception("[%s] PDF 결과 제외 중 오류 (필터링 없이 계속 진행)", request_id)

        if RELEVANCE_FILTER:
            # BM25 filtering only makes sense with results from more than
            # one query -- a single-result batch has nothing to be more/less
            # relevant than, and it's a no-op there anyway (its own score
            # always equals the "best" score).
            try:
                _filter_by_relevance(query, results_by_engine, RELEVANCE_MIN_SCORE_RATIO, request_id)
            except Exception:
                # Same rationale as content enrichment below: this is a
                # quality improvement on top of already-successful engine
                # results, so a failure here must fall through to those
                # results unfiltered rather than fail the whole request.
                logger.exception("[%s] BM25 관련성 필터 중 오류 (필터링 없이 계속 진행)", request_id)

        if FETCH_CONTENT:
            flat_results = [r for results in results_by_engine.values() for r in results]
            if flat_results:
                # Enrichment is a nice-to-have on top of already-successful
                # search results -- if it breaks for some unforeseen reason
                # (thread pool exhaustion, trafilatura internals, ...), the
                # request should still return the perfectly good snippet-only
                # results rather than fail outright.
                try:
                    _enrich_with_content(
                        flat_results, FETCH_CONTENT_TOP_N, FETCH_CONTENT_MAX_CHARS,
                        FETCH_CONTENT_MIN_USEFUL_CHARS, request_id,
                    )
                except Exception:
                    logger.exception("[%s] 본문 보강 중 오류 (스니펫만으로 계속 진행)", request_id)

        sections = []
        for kind, payload in section_items:
            if kind == "text":
                sections.append(payload)
                continue
            name = payload
            results = results_by_engine[name]
            body = _format_results(results) if results else "검색 결과가 없습니다."
            sections.append(f"[{name}]\n{body}")
            logger.info("[%s][%s] 최종 반환 결과:\n%s", request_id, name, body)

        total_elapsed = time.perf_counter() - request_started
        logger.info(
            "===== [%s] web_search 요청 종료 ===== 총소요시간=%.3fs 결과=%s",
            request_id, total_elapsed, ", ".join(outcomes) if outcomes else "없음",
        )
        result_text = "\n\n".join(sections) if sections else "검색 결과가 없습니다."

        if output_format != "json":
            return result_text

        try:
            results_payload = []
            for kind, payload in section_items:
                if kind != "results":
                    continue
                name = payload
                for r in results_by_engine[name]:
                    results_payload.append(
                        {
                            "engine": name,
                            "title": r["title"],
                            "url": r["url"],
                            "content": r.get("content") or r["snippet"],
                        }
                    )
            return json.dumps(
                {"query": query, "engines": requested, "results": results_payload},
                ensure_ascii=False, indent=2,
            )
        except Exception:
            # JSON shaping is a presentation detail on top of already-computed
            # results -- if it somehow fails, fall back to the plain-text
            # form rather than losing the results entirely.
            logger.exception("[%s] JSON 변환 실패, 텍스트로 대체", request_id)
            return result_text

    def stats_report(self) -> str:
        return "\n".join(engine.usage_report() for engine in self._engines.values())


def _load_naver_credentials() -> list[tuple[str, str]]:
    """Load NAVER_CLIENT_IDS / NAVER_CLIENT_SECRETS as comma-separated lists
    (paired up by position -- each pair is a separate Naver app with its own
    daily quota, so registering several lets us rotate once one gets close
    to its limit). Falls back to a single unsuffixed NAVER_CLIENT_ID/
    NAVER_CLIENT_SECRET pair if the list-style variables aren't set."""
    ids = [v.strip() for v in os.environ.get("NAVER_CLIENT_IDS", "").split(",") if v.strip()]
    secrets = [v.strip() for v in os.environ.get("NAVER_CLIENT_SECRETS", "").split(",") if v.strip()]

    if not ids or not secrets:
        cid = os.environ.get("NAVER_CLIENT_ID")
        secret = os.environ.get("NAVER_CLIENT_SECRET")
        return [(cid, secret)] if cid and secret else []

    if len(ids) != len(secrets):
        logger.warning(
            "NAVER_CLIENT_IDS(%d개)와 NAVER_CLIENT_SECRETS(%d개) 개수가 달라 "
            "짝이 맞는 %d개만 사용합니다.",
            len(ids), len(secrets), min(len(ids), len(secrets)),
        )
    return list(zip(ids, secrets))


# ---------------------------------------------------------------------------
# Wiring: every tunable here comes from .env (with a sane default), nothing
# is hardcoded, so behavior can be changed without touching this file --
# add/remove Naver keys, change the quota limit/threshold, or change which
# engines "all" covers, purely via environment variables.
# ---------------------------------------------------------------------------

_STATS_FILE = os.path.join(_BASE_DIR, "search_stats.json")

# Per-engine timeout for a single search() call, enforced while collecting
# results from the concurrent thread-pool batch in SearchRouter._run_batch.
# A slow/hanging engine (network issue, upstream API degraded) is logged and
# skipped rather than blocking the whole request indefinitely.
SEARCH_ENGINE_TIMEOUT_SECONDS = float(os.environ.get("SEARCH_ENGINE_TIMEOUT_SECONDS", "8"))

# 네이버 오픈API는 키(앱)마다 1일 호출 한도가 있으므로, 각 키의 한도 중 일정 비율
# (threshold) 이상 쓰면 다음 키로 순환하고, 등록된 모든 키가 한도에 가까워지면 다른
# 엔진으로 자동 대체한다. 아래 값들은 전부 .env에서 조정 가능하며 코드에는 기본값만 둔다.
NAVER_DAILY_LIMIT = int(os.environ.get("NAVER_DAILY_LIMIT", "25000"))
NAVER_LIMIT_THRESHOLD = float(os.environ.get("NAVER_LIMIT_THRESHOLD", "0.8"))
# Naver Open API search vertical for the primary "naver" engine entry --
# "news" fits finance/current-events queries and returns "originallink".
NAVER_SEARCH_ENDPOINT = os.environ.get("NAVER_SEARCH_ENDPOINT", "news")
# Optionally register a second Naver engine ("naver_web") against a
# different vertical (default "webkr", general web documents) using the
# same credentials -- news alone can miss things like a live stock-price
# page that a general web search would find. Shares its quota with the
# "naver" engine above since it's the same underlying app/keys. Set to an
# empty string to disable and only search the one Naver vertical.
NAVER_SEARCH_ENDPOINT_2 = os.environ.get("NAVER_SEARCH_ENDPOINT_2", "webkr")
# Optionally register a third Naver engine ("naver_encyc") against the
# encyclopedia/dictionary vertical (default "encyc") -- fits definitional/
# "what is X" queries (e.g. a word with many unrelated meanings) far better
# than news or general web documents. Same sharing/disable rules as above.
NAVER_SEARCH_ENDPOINT_3 = os.environ.get("NAVER_SEARCH_ENDPOINT_3", "encyc")

# Fetches full article text (via trafilatura) for the top N results across
# all engines combined, so web_search's own output already looks like
# Tavily's {title, url, content} shape instead of just a short snippet --
# any MCP client using this tool benefits, not only the test webapp.
FETCH_CONTENT = os.environ.get("FETCH_CONTENT", "true").lower() == "true"
FETCH_CONTENT_TOP_N = int(os.environ.get("FETCH_CONTENT_TOP_N", "3"))
FETCH_CONTENT_MAX_CHARS = int(os.environ.get("FETCH_CONTENT_MAX_CHARS", "3000"))
FETCH_CONTENT_MIN_USEFUL_CHARS = int(os.environ.get("FETCH_CONTENT_MIN_USEFUL_CHARS", "200"))

# trafilatura.fetch_url()'s own default DOWNLOAD_TIMEOUT is 30s with up to
# 20MB allowed per download (MAX_FILE_SIZE) -- generous enough that one
# candidate happening to be a large PDF/binary can single-handedly stall
# the whole enrichment step (found live: an OWASP security-guide PDF took
# 37.9s to fetch). This overrides just the timeout to something in line
# with the rest of this file's per-call bounds (SEARCH_ENGINE_TIMEOUT_SECONDS).
FETCH_CONTENT_TIMEOUT_SECONDS = float(os.environ.get("FETCH_CONTENT_TIMEOUT_SECONDS", "5"))
_TRAFILATURA_CONFIG = _trafilatura_use_config()
_TRAFILATURA_CONFIG.set("DEFAULT", "DOWNLOAD_TIMEOUT", str(FETCH_CONTENT_TIMEOUT_SECONDS))

# engines used when web_search(engines="all"). SEARCH_ENGINE_MODE is a
# convenience preset ("naver" = every registered Naver vertical only,
# "duckduckgo" = duckduckgo only, "hybrid" = everything registered) so
# switching the whole default set doesn't require spelling out engine names
# by hand; DEFAULT_ENGINES, if set explicitly, always wins over the preset.
_naver_engine_names = ["naver"]
if NAVER_SEARCH_ENDPOINT_2:
    _naver_engine_names.append("naver_web")
if NAVER_SEARCH_ENDPOINT_3:
    _naver_engine_names.append("naver_encyc")

SEARCH_ENGINE_MODE = os.environ.get("SEARCH_ENGINE_MODE", "hybrid").strip().lower()
if SEARCH_ENGINE_MODE == "naver":
    _default_engine_names = list(_naver_engine_names)
elif SEARCH_ENGINE_MODE == "duckduckgo":
    _default_engine_names = ["duckduckgo"]
else:  # "hybrid" (default) or any unrecognized value
    _default_engine_names = ["duckduckgo"] + _naver_engine_names

DEFAULT_ENGINES = [
    e.strip()
    for e in os.environ.get("DEFAULT_ENGINES", ",".join(_default_engine_names)).split(",")
    if e.strip()
]

stats = CallStats(_STATS_FILE)
_naver_credentials = _load_naver_credentials()
_engines = [
    DuckDuckGoEngine(stats),
    NaverEngine(
        stats, _naver_credentials, NAVER_DAILY_LIMIT, NAVER_LIMIT_THRESHOLD,
        endpoint=NAVER_SEARCH_ENDPOINT,
    ),
]
if NAVER_SEARCH_ENDPOINT_2:
    _engines.append(
        NaverEngine(
            stats, _naver_credentials, NAVER_DAILY_LIMIT, NAVER_LIMIT_THRESHOLD,
            endpoint=NAVER_SEARCH_ENDPOINT_2, name="naver_web",
        )
    )
if NAVER_SEARCH_ENDPOINT_3:
    _engines.append(
        NaverEngine(
            stats, _naver_credentials, NAVER_DAILY_LIMIT, NAVER_LIMIT_THRESHOLD,
            endpoint=NAVER_SEARCH_ENDPOINT_3, name="naver_encyc",
        )
    )
router = SearchRouter(_engines, default_engines=DEFAULT_ENGINES)

# One-line startup summary of the active config, so a search.log reader can
# tell what a request *should* have done (which engines "all" resolves to,
# whether relevance filtering/content enrichment were even on) without
# cross-referencing .env separately -- especially useful after the fact,
# reading a log from a server instance whose .env you don't have open.
logger.info(
    "Lens 서버 설정: 등록된 엔진=%s, 기본 엔진(engines=\"all\")=%s, "
    "RELEVANCE_FILTER=%s(ratio=%.2f), FETCH_CONTENT=%s(top_n=%d), "
    "EXCLUDE_KEYWORDS=%d개, 네이버 키=%d개",
    list(router._engines), DEFAULT_ENGINES,
    RELEVANCE_FILTER, RELEVANCE_MIN_SCORE_RATIO, FETCH_CONTENT, FETCH_CONTENT_TOP_N,
    len(EXCLUDE_KEYWORDS), len(_naver_credentials),
)

# ---------------------------------------------------------------------------
# Periodic hit-count logging: a dedicated logger writing to its own file
# (hit_count.log, separate from search.log) so quota/usage can be monitored
# independently of the per-request search logs. A standalone daemon thread
# ticks this on a fixed interval regardless of MCP transport (stdio has no
# event loop to hook into; sse/streamable-http do, but a plain thread works
# for both without transport-specific wiring). Each tick also actively
# resets any counters whose date has rolled past midnight, so quotas free up
# once a day even if nobody happens to call that engine right at 00:00.
# ---------------------------------------------------------------------------

_hit_log_file_setting = os.environ.get("HIT_COUNT_LOG_FILE", "hit_count.log")
HIT_COUNT_LOG_FILE = (
    _hit_log_file_setting
    if os.path.isabs(_hit_log_file_setting)
    else os.path.join(_BASE_DIR, _hit_log_file_setting)
)
HIT_COUNT_INTERVAL_SECONDS = float(os.environ.get("HIT_COUNT_INTERVAL_SECONDS", "60"))

hit_logger = logging.getLogger("web-search.hit_count")
hit_logger.setLevel(logging.INFO)
hit_logger.propagate = False  # this logger only writes to hit_count.log directly (see below)
_hit_handler = logging.FileHandler(HIT_COUNT_LOG_FILE, encoding="utf-8")
_hit_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
hit_logger.addHandler(_hit_handler)


def _hit_count_loop() -> None:
    while True:
        # This is a daemon background thread with no caller to report a
        # failure to -- an uncaught exception here would silently kill hit-
        # count logging for the rest of the process's life (the thread just
        # dies; nothing restarts it). One bad tick logging a failure and
        # trying again next interval is far better than losing all future
        # quota visibility over one transient error.
        try:
            stats.reset_stale_entries()
            report = router.stats_report()
            # Written to both files: hit_count.log as the dedicated, easy-to-tail
            # quota feed, and search.log (via the main `logger`) so anyone
            # already watching that file for request activity also sees usage
            # ticks without needing to open a second file.
            hit_logger.info("호출 현황:\n" + report)
            logger.info("[hit-count] 호출 현황:\n%s", report)
        except Exception:
            logger.exception("[hit-count] 주기 실행 중 오류 (다음 주기에 재시도)")
        time.sleep(HIT_COUNT_INTERVAL_SECONDS)


threading.Thread(target=_hit_count_loop, name="hit-count-logger", daemon=True).start()

# Transport: "stdio" (default, launched by an MCP client via command+args, no
# network port) or "sse"/"streamable-http" (listens on MCP_HOST:MCP_PORT and
# is registered by URL instead, so multiple clients can share one running
# server). host/port only take effect for the network transports.
MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio")
MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MCP_PORT", "8000"))

mcp = FastMCP("Lens", host=MCP_HOST, port=MCP_PORT)


@mcp.tool()
async def web_search(query: str, max_results: int = 3, engines: str = "all", output_format: str = "text") -> str:
    """Search the web across multiple engines and return titles, URLs, and
    content (full article text via trafilatura for the top results, short
    snippet otherwise) grouped by engine. If one engine fails, runs out of
    quota, or every result matches an excluded keyword (EXCLUDE_KEYWORDS in
    .env), another registered engine is automatically tried instead.

    Before content is fetched, all engines' results are merged and scored
    with a BM25 model (RELEVANCE_FILTER in .env) against the query using
    each result's own title+description -- anything with little real
    overlap with the query relative to the best match (e.g. a dictionary
    entry that shares one incidental word with a stock-price query) is
    dropped before it ever reaches full-content fetching or the caller.

    engines: comma-separated list of engines to use ("duckduckgo", "naver"
    for Naver news search, "naver_web" for Naver general web-document
    search, "naver_encyc" for Naver encyclopedia/dictionary search -- all
    three Naver variants share the same Naver key/quota), or "all"
    (default) to search the configured default engines (see
    DEFAULT_ENGINES / SEARCH_ENGINE_MODE in .env).
    output_format: "text" (default, human-readable) or "json" (structured
    {"query", "engines", "results": [{"engine","title","url","content"}]}).
    """
    # FastMCP calls sync tool functions directly on its event loop thread
    # (it does NOT offload them to a worker thread the way it does for
    # resources). router.search() does blocking network I/O -- under stdio
    # transport there's only ever one request in flight so that's harmless,
    # but under sse/streamable-http it would stall every other concurrent
    # client for the full duration of the slowest engine. Running it via
    # anyio.to_thread.run_sync moves the blocking work to a worker thread
    # instead, so concurrent requests don't block each other.
    try:
        return await anyio.to_thread.run_sync(router.search, query, max_results, engines, output_format)
    except Exception as e:
        # Top-level safety net: whatever goes wrong (a bug in a code path
        # not already covered by the per-engine/per-enrichment try/excepts,
        # a thread-pool failure, anything unforeseen), the tool call itself
        # must never raise -- always hand the caller back a usable string
        # rather than surfacing as an MCP tool error.
        logger.exception("web_search 처리 중 예기치 못한 오류 (query=%r)", query)
        return f"검색 처리 중 예기치 못한 오류가 발생했습니다: {e}"


@mcp.tool()
def search_stats() -> str:
    """Show how many times each search engine (and each Naver key) has been
    called today, persisted across restarts, including usage against Naver's
    configured daily quota."""
    try:
        return router.stats_report()
    except Exception as e:
        logger.exception("search_stats 처리 중 예기치 못한 오류")
        return f"통계 조회 중 오류가 발생했습니다: {e}"


if __name__ == "__main__":
    if MCP_TRANSPORT != "stdio":
        logger.info(
            "MCP 서버를 %s 트랜스포트로 시작 (host=%s port=%d)", MCP_TRANSPORT, MCP_HOST, MCP_PORT,
        )
    mcp.run(transport=MCP_TRANSPORT)
