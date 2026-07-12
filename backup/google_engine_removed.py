# 2026-07-10 제거된 GoogleEngine 백업
#
# 제거 이유: DuckDuckGo+Naver가 둘 다 실패하는 경우는 드물어서 실제로 거의 안 쓰이는
# 폴백인데, 대가로 noapi-google-search-mcp 패키지(playwright, onnxruntime, opencv,
# faster-whisper 등 500MB+)를 계속 끌고 다녀야 했고, 구글 화면 스크래핑이라 캡차/봇차단에
# 취약하며, 비동기(SSE/HTTP transport) 환경에서 asyncio.run() 충돌 버그도 있었음
# (anyio.to_thread.run_sync로 우회 가능하지만 근본적으로 불안정).
#
# 나중에 다시 붙이려면:
#   1. requirements.txt에 noapi-google-search-mcp 추가하고 pip install
#   2. .venv/Scripts/python.exe -m playwright install chromium 실행
#   3. 아래 클래스를 server.py에 다시 넣고, router 생성부에 GoogleEngine(stats) 추가
#   4. SearchRouter의 default_engines에는 넣지 말고(무거우니 폴백 전용 유지),
#      engines="google"로 명시하거나 다른 엔진이 모두 실패했을 때만 자동 폴백되게 유지

import re


def _parse_google_text(text: str) -> list[dict]:
    """noapi-google-search-mcp's `_do_google_search` returns one big
    preformatted string (not structured data) shaped like:

        1. Title
           URL: https://...
           snippet text

        2. Title
           URL: https://...
           snippet text

    This walks it line by line and reconstructs the {title, url, snippet}
    schema the other engines use natively, so SearchRouter can render every
    engine's output the same way. A new "N. " line starts a new result; a
    "URL:" line fills in the current result's url; anything else is appended
    to the running snippet (Google sometimes wraps snippets across lines)."""
    results: list[dict] = []
    current: dict | None = None
    for line in text.splitlines():
        m = re.match(r"^\d+\.\s+(.*)", line)
        if m:
            if current:
                results.append(current)
            current = {"title": m.group(1).strip(), "url": "", "snippet": ""}
        elif current is not None and line.strip().startswith("URL:"):
            current["url"] = line.split("URL:", 1)[1].strip()
        elif current is not None and line.strip():
            current["snippet"] = (current["snippet"] + " " + line.strip()).strip()
    if current:
        results.append(current)
    return results


class GoogleEngine:  # (was: class GoogleEngine(SearchEngine))
    """Last-resort fallback: scrapes Google via a headless browser (no API
    key required) using the noapi-google-search-mcp package. Heavier and
    slower than the other engines (launches Chromium via Playwright), so it
    is deliberately kept out of the default 'all' set and only used when the
    other engines fail -- see DEFAULT_ENGINES / SearchRouter fallback."""

    name = "google"
    requires_key = False

    def __init__(self, stats):
        self._stats = stats

    def search(self, query: str, max_results: int, request_id: str = "") -> list[dict]:
        logger.info(
            "[%s][google] 검색 시작 (키 불필요, 폴백 전용, 헤드리스 브라우저 사용) query=%r",
            request_id, query,
        )
        try:
            # Imported lazily (not at module load) because this pulls in the
            # whole noapi-google-search-mcp package (playwright, onnxruntime,
            # opencv, faster-whisper, ...) -- heavy to import, so we only pay
            # that cost the first time this fallback actually fires, not on
            # every server start.
            import asyncio

            from google_search_mcp.server import _do_google_search
        except ImportError as e:
            logger.error("[%s][google] noapi-google-search-mcp 로드 실패: %s", request_id, e)
            raise EngineUnavailable(f"google 엔진을 사용할 수 없습니다: {e}") from e

        started = time.perf_counter()
        self._stats.record(self.name)
        text = asyncio.run(_do_google_search(query, num_results=max_results))
        elapsed = time.perf_counter() - started
        parsed = _parse_google_text(text)
        logger.info(
            "[%s][google] 검색 완료: %d건 (소요 %.3fs, 오늘 누적 %d회)",
            request_id, len(parsed), elapsed, self._stats.today_count(self.name),
        )
        return parsed or [{"title": "google", "url": "", "snippet": text}]

    def usage_report(self) -> str:
        return f"google (키 불필요, 폴백 전용): {self._stats.today_count(self.name)}회 호출 (오늘)"
