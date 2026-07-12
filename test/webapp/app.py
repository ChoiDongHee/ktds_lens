"""FastAPI demo chat app for manually testing the web-search MCP tool.

Not part of the MCP server itself -- lives under test/ so it never mixes
into server.py's source. Lets you toggle whether web_search (DuckDuckGo +
Naver) is used to ground the chat model's answer, and shows both the
model's answer and the raw links it was given.

Run: .venv/Scripts/python.exe test/webapp/app.py
Then open http://127.0.0.1:8800
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from pydantic import BaseModel

_TEST_DIR = os.path.dirname(os.path.abspath(__file__))
_BASE_DIR = os.path.dirname(os.path.dirname(_TEST_DIR))

# stdout defaults to the system codepage on Windows (cp949 on Korean
# Windows) and can't represent arbitrary search-result/LLM-answer Unicode --
# same fix as server.py and test_mcp_client.py.
sys.stdout.reconfigure(encoding="utf-8")

load_dotenv(os.path.join(_BASE_DIR, ".env"))

# Own log file (separate from server.py's search.log) so each mode's actual
# search results/prompt are visible for debugging/comparison, not just the
# per-request-id engine logs server.py already writes for the "mcp" mode.
# Deliberately NOT using logging.basicConfig here: that configures the root
# logger process-wide, and since `import server` below calls its own
# basicConfig (which is a silent no-op if the root logger is already
# configured), calling it here first would break server.py's own
# search.log/hit_count.log setup. A separate named logger with its own
# handlers avoids touching the root logger at all.
_webapp_log_setting = os.environ.get("WEBAPP_LOG_FILE", "webapp.log")
WEBAPP_LOG_FILE = (
    _webapp_log_setting if os.path.isabs(_webapp_log_setting) else os.path.join(_TEST_DIR, _webapp_log_setting)
)

logger = logging.getLogger("webapp")
logger.setLevel(logging.INFO)
logger.propagate = False
_log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
for _handler in (
    logging.FileHandler(WEBAPP_LOG_FILE, encoding="utf-8"),
    logging.StreamHandler(sys.stderr),
):
    _handler.setFormatter(_log_formatter)
    logger.addHandler(_handler)

# Import the actual MCP server module directly so this test app calls the
# exact same engine/router code the real MCP tool uses -- no duplicated
# search logic, no protocol round-trip needed for a local test harness.
sys.path.insert(0, _BASE_DIR)
import server  # noqa: E402
from bm25_search import bm25_ranker, tokenizer  # noqa: E402

VLLM_URL = os.environ["QWEN27B_VLLM_SERVER_URL"].rstrip("/") + "/chat/completions"
VLLM_MODEL = os.environ["QWEN27B_VLLM_MODEL_NAME"]
VLLM_API_KEY = os.environ["QWEN27B_API_KEY"]

WEBAPP_PORT = int(os.environ.get("WEBAPP_PORT", "8800"))
WEBAPP_MAX_RESULTS = int(os.environ.get("WEBAPP_MAX_RESULTS", "8"))

# Tavily's hosted remote MCP server -- a second, independent search backend
# (separate from our own DuckDuckGo/Naver MCP tool) purely so the two can be
# compared side by side in this test app. content field already returns
# cleaned, fairly long extracted text, unlike our ~150-char snippets.
TAVILY_MCP_URL = f"https://mcp.tavily.com/mcp/?tavilyApiKey={os.environ['TAVILY_API_KEY']}"

app = FastAPI(title="web-search MCP test chat")


class ChatRequest(BaseModel):
    message: str
    # "mcp" = our own DuckDuckGo+Naver MCP tool, "tavily" = Tavily's remote
    # MCP server (a second, independent search backend), "compare" = run
    # both concurrently and return two answers side by side, "none" = no
    # search, model answers from its own knowledge only.
    search_mode: str = "mcp"


def parse_links(search_text: str) -> list[dict]:
    """Reconstructs {engine, title, url, snippet} entries from the plain-text
    block server.router.search() returns (see server._format_results) --
    kept here rather than in server.py since it's test-app-only rendering
    logic, not something the MCP tool itself needs."""
    links: list[dict] = []
    current: dict | None = None
    engine = None
    for raw_line in search_text.splitlines():
        line = raw_line.strip()
        header = re.match(r"^\[(\w+)\]$", line)
        if header:
            engine = header.group(1)
            continue
        m = re.match(r"^\d+\.\s+(.*)$", line)
        if m:
            if current:
                links.append(current)
            current = {"engine": engine, "title": m.group(1), "url": "", "snippet": ""}
            continue
        if current is not None and line.startswith(("http://", "https://")):
            current["url"] = line
        elif current is not None and line:
            current["snippet"] = (current["snippet"] + " " + line).strip()
    if current:
        links.append(current)
    return links


# Below this fraction of the top BM25 score, a result is considered
# unrelated noise rather than just "less relevant" -- e.g. naver_encyc
# (dictionary search) occasionally returns something wildly off-topic
# (a "삼성전자 주가" query pulling up "영구 제모 효과" because some encyclopedia
# entry happened to share a rare token) when merging results from several
# engines that don't all rank against the same query the same way.
_RELEVANCE_MIN_SCORE_RATIO = 0.15


def _filter_relevant_links(query: str, links: list[dict], top_k: int = 6) -> list[dict]:
    """Reranks `links` by BM25 similarity to `query` (reusing bm25_search's
    tokenizer/bm25_ranker -- the same relevance model the standalone BM25
    pipeline uses, just applied here to whatever server.router.search()
    already returned rather than a fresh fetch) and drops anything scoring
    below _RELEVANCE_MIN_SCORE_RATIO of the top score, then keeps at most
    top_k. This is what keeps an irrelevant naver_encyc hit out of both the
    LLM's context and the displayed link list -- previously every raw
    result from every engine was shown/used unfiltered."""
    if not links:
        return links

    texts = [f"{link['title']} {link['snippet']}" for link in links]
    tokenized_documents = tokenizer.tokenize_batch(texts)
    tokenized_query = tokenizer.tokenize(query)
    if not tokenized_query:
        return links[:top_k]

    index = bm25_ranker.build_index(tokenized_documents)
    scored = bm25_ranker.search(index, tokenized_query, top_k=len(links))
    if not scored or scored[0][1] <= 0:
        # No candidate shares any keyword with the query at all -- BM25 has
        # nothing to rank on, so fall back to the engines' own order rather
        # than returning an empty result set.
        return links[:top_k]

    threshold = scored[0][1] * _RELEVANCE_MIN_SCORE_RATIO
    kept = [links[i] for i, score in scored if score >= threshold]
    return kept[:top_k]


def search_mcp(query: str, max_results: int) -> tuple[str, list[dict]]:
    """Calls our own web_search MCP tool, reranks/filters the combined
    results for actual relevance to `query` (see _filter_relevant_links),
    and reshapes what's left into the same {engine, title, url, snippet} +
    "제목/URL/내용" block format search_tavily() uses, purely for consistent
    display/prompting in this test app.

    No page fetching happens here: server.py's web_search itself now
    fetches and cleans full article text for its own top results
    (FETCH_CONTENT in .env, via trafilatura) and already returns it in place
    of the snippet -- so parse_links()'s "snippet" field is already the
    enriched content where available. Duplicating that fetch here would
    just hit the same URLs a second time for no benefit.

    Must never raise: this is the search step of a chat request, and an
    unhandled exception here would surface as a hard failure to the user
    (a 500 from /api/chat, seen client-side as "Failed to fetch"). If
    anything in the normal path (engines="all", BM25 relevance filtering)
    throws, fall back to DuckDuckGo alone -- keyless, no quota, and the
    simplest path least likely to share whatever broke -- and skip the
    relevance filter on that fallback too, for the same reason. If even
    that fails, return an empty result rather than propagate."""
    try:
        search_text = server.router.search(query, max_results, "all")
        links = _filter_relevant_links(query, parse_links(search_text))
    except Exception:
        logger.exception(
            "search_mcp 검색 중 오류 발생 (query=%r) -- duckduckgo 단독으로 폴백", query
        )
        try:
            search_text = server.router.search(query, max_results, "duckduckgo")
            links = parse_links(search_text)
        except Exception:
            logger.exception("duckduckgo 폴백도 실패 (query=%r) -- 빈 결과로 진행", query)
            links = []

    blocks = [
        f"[{link['engine']}] 제목: {link['title']}\nURL: {link['url']}\n내용: {link['snippet'] or '(내용 없음)'}"
        for link in links
    ]
    return "\n\n---\n\n".join(blocks), links


async def _tavily_search_async(query: str, max_results: int) -> dict:
    async with streamablehttp_client(TAVILY_MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "tavily_search", {"query": query, "max_results": max_results}
            )
            text = "".join(block.text for block in result.content if hasattr(block, "text"))
            return json.loads(text)


def search_tavily(query: str, max_results: int) -> tuple[str, list[dict]]:
    """Calls Tavily's remote MCP server (a separate service from our own
    MCP tool) and returns (search_text_for_prompt, links_for_display).
    Opens a fresh MCP session per call -- simple and correct for a test app,
    though a real integration would keep a session alive across requests."""
    data = asyncio.run(_tavily_search_async(query, max_results))
    links = []
    blocks = []
    for r in data.get("results", []):
        content = r.get("content", "")
        links.append({"engine": "tavily", "title": r.get("title", ""), "url": r.get("url", ""), "snippet": content[:200]})
        blocks.append(f"제목: {r.get('title', '')}\nURL: {r.get('url', '')}\n내용: {content}")
    return "\n\n---\n\n".join(blocks), links


def call_vllm(prompt: str) -> str:
    response = httpx.post(
        VLLM_URL,
        headers={"Authorization": f"Bearer {VLLM_API_KEY}"},
        json={
            "model": VLLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 800,
        },
        timeout=60.0,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


_GROUNDED_INSTRUCTIONS = (
    "위 자료만 근거로 삼아 답변하세요. 자료에 구체적인 수치(가격, 통계 등)가 실제로 "
    "적혀 있지 않다면 그 수치를 지어내지 말고, 정확한 실시간 수치는 없다고 솔직히 말한 "
    "뒤 관련 맥락(동향, 기사 요약 등)만 정리하세요. 관련 출처가 있으면 자연스럽게 "
    "언급하세요."
)


def _search_and_prompt(backend: str, message: str, request_id: str) -> tuple[str, list[dict]]:
    """Runs the given backend's search (if any) and builds the grounded
    prompt for it. Split out from _answer_with_backend so "compare" mode's
    two backends can be prepared independently and run in parallel."""
    if backend == "mcp":
        search_started = time.perf_counter()
        context_text, links = search_mcp(message, WEBAPP_MAX_RESULTS)
        logger.info(
            "[%s][mcp] 검색 완료 (%.3fs): %d개 결과\n%s",
            request_id, time.perf_counter() - search_started, len(links), context_text,
        )
        prompt = (
            "다음은 우리 web_search MCP 툴(DuckDuckGo+Naver)의 검색 결과입니다 "
            "(제목/URL과, 상위 결과는 실제 페이지 본문까지 포함):\n\n"
            f"{context_text}\n\n{_GROUNDED_INSTRUCTIONS}\n\n질문: {message}"
        )
        return prompt, links

    if backend == "tavily":
        search_started = time.perf_counter()
        context_text, links = search_tavily(message, WEBAPP_MAX_RESULTS)
        logger.info(
            "[%s][tavily] 검색 완료 (%.3fs): %d개 결과\n%s",
            request_id, time.perf_counter() - search_started, len(links), context_text,
        )
        prompt = (
            "다음은 Tavily 검색 MCP 서버의 검색 결과입니다 (제목/URL과 이미 정제된 "
            f"본문 내용 포함):\n\n{context_text}\n\n{_GROUNDED_INSTRUCTIONS}\n\n질문: {message}"
        )
        return prompt, links

    logger.info("[%s][%s] 검색 미사용", request_id, backend)
    return message, []


def _answer_with_backend(backend: str, message: str, request_id: str) -> dict:
    """Runs one backend end-to-end (search + LLM) and returns
    {"answer", "links"}. Used directly for mcp/tavily/none modes, and via a
    thread pool for "compare" mode -- the two backends are entirely
    independent services, so running them one after another would just add
    their latencies together for no reason."""
    prompt, links = _search_and_prompt(backend, message, request_id)
    logger.info("[%s][%s] vLLM 호출 시작 (prompt %d자)", request_id, backend, len(prompt))
    llm_started = time.perf_counter()
    answer = call_vllm(prompt)
    logger.info(
        "[%s][%s] vLLM 응답 수신 (%.3fs, %d자): %s",
        request_id, backend, time.perf_counter() - llm_started, len(answer), answer,
    )
    return {"answer": answer, "links": links}


@app.post("/api/chat")
def chat(req: ChatRequest) -> dict:
    # Plain `def` (not `async def`): FastAPI runs sync path functions in a
    # worker thread pool automatically, so the blocking search + LLM calls
    # below don't need the manual anyio offload server.py's async tool does.
    request_id = uuid.uuid4().hex[:8]
    started = time.perf_counter()
    logger.info(
        "[%s] 요청 시작: search_mode=%r message=%r", request_id, req.search_mode, req.message
    )

    if req.search_mode == "compare":
        with ThreadPoolExecutor(max_workers=2) as pool:
            future_mcp = pool.submit(_answer_with_backend, "mcp", req.message, request_id)
            future_tavily = pool.submit(_answer_with_backend, "tavily", req.message, request_id)
            result_mcp = future_mcp.result()
            result_tavily = future_tavily.result()
        total = time.perf_counter() - started
        logger.info("[%s] 비교 요청 종료 (총 %.3fs)", request_id, total)
        return {"search_mode": "compare", "mcp": result_mcp, "tavily": result_tavily}

    result = _answer_with_backend(req.search_mode, req.message, request_id)
    total = time.perf_counter() - started
    logger.info("[%s] 요청 종료 (총 %.3fs)", request_id, total)
    return {"answer": result["answer"], "links": result["links"], "search_mode": req.search_mode}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _PAGE


_PAGE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>web-search MCP 테스트 챗</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 900px; margin: 24px auto; padding: 0 16px; }
  h1 { font-size: 18px; }
  #toggle-row { margin: 12px 0; display: flex; align-items: center; gap: 8px; }
  #chat { border: 1px solid #ccc; border-radius: 8px; padding: 12px; height: 360px; overflow-y: auto; margin-bottom: 12px; }
  .msg { margin: 8px 0; white-space: pre-wrap; }
  .user { color: #1a4; font-weight: bold; }
  .assistant { color: #14a; }
  #input-row { display: flex; gap: 8px; }
  #message { flex: 1; padding: 8px; }
  button { padding: 8px 16px; }
  #links { margin-top: 16px; }
  #links h2 { font-size: 14px; }
  #links ul { padding-left: 20px; }
  #links a { word-break: break-all; }
  .engine-tag { font-size: 11px; color: #888; }
  #status { font-size: 12px; color: #888; }
  #compare-view { display: none; gap: 16px; margin-top: 12px; }
  #compare-view.active { display: flex; }
  .compare-col { flex: 1; min-width: 0; border: 1px solid #ccc; border-radius: 8px; padding: 12px; }
  .compare-col h3 { margin: 0 0 8px 0; font-size: 13px; color: #444; }
  .compare-col .answer { white-space: pre-wrap; margin-bottom: 10px; }
  .compare-col ul { padding-left: 18px; margin: 4px 0; }
</style>
</head>
<body>
<h1>web-search MCP 테스트 챗 (vLLM Qwen)</h1>
<div id="toggle-row">
  <label><input type="radio" name="mode" value="mcp" checked> MCP 웹검색 (DuckDuckGo+Naver)</label>
  <label><input type="radio" name="mode" value="tavily"> Tavily MCP</label>
  <label><input type="radio" name="mode" value="compare"> 비교 (MCP vs Tavily)</label>
  <label><input type="radio" name="mode" value="none"> 검색 안 함</label>
  <span id="status"></span>
</div>
<div id="chat"></div>
<div id="input-row">
  <input type="text" id="message" placeholder="질문을 입력하세요">
  <button id="send">전송</button>
</div>
<div id="links"><h2>검색 결과 링크</h2><ul id="links-list"></ul></div>
<div id="compare-view">
  <div class="compare-col">
    <h3>MCP 웹검색 (DuckDuckGo+Naver)</h3>
    <div class="answer" id="compare-mcp-answer"></div>
    <ul id="compare-mcp-links"></ul>
  </div>
  <div class="compare-col">
    <h3>Tavily MCP</h3>
    <div class="answer" id="compare-tavily-answer"></div>
    <ul id="compare-tavily-links"></ul>
  </div>
</div>

<script>
const chat = document.getElementById('chat');
const messageInput = document.getElementById('message');
const linksPanel = document.getElementById('links');
const linksList = document.getElementById('links-list');
const status = document.getElementById('status');
const compareView = document.getElementById('compare-view');

function addMessage(role, text) {
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  div.textContent = (role === 'user' ? '나: ' : 'AI: ') + text;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
}

function getMode() {
  return document.querySelector('input[name="mode"]:checked').value;
}

function renderLinks(listEl, links) {
  listEl.innerHTML = '';
  for (const link of links) {
    const li = document.createElement('li');
    li.innerHTML = '<span class="engine-tag">[' + link.engine + ']</span> ' +
      '<a href="' + link.url + '" target="_blank">' + link.title + '</a>';
    listEl.appendChild(li);
  }
}

async function send() {
  const message = messageInput.value.trim();
  if (!message) return;
  const searchMode = getMode();
  addMessage('user', message);
  messageInput.value = '';
  status.textContent = '응답 생성 중... (' + searchMode + ')';

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message, search_mode: searchMode}),
    });
    const data = await res.json();
    if (!res.ok) {
      addMessage('assistant', '오류: ' + JSON.stringify(data));
      status.textContent = '';
      return;
    }

    if (data.search_mode === 'compare') {
      linksPanel.style.display = 'none';
      compareView.classList.add('active');
      document.getElementById('compare-mcp-answer').textContent = data.mcp.answer;
      document.getElementById('compare-tavily-answer').textContent = data.tavily.answer;
      renderLinks(document.getElementById('compare-mcp-links'), data.mcp.links);
      renderLinks(document.getElementById('compare-tavily-links'), data.tavily.links);
      addMessage('assistant', '(아래 비교 결과 참고: MCP ' + data.mcp.links.length + '개, Tavily ' + data.tavily.links.length + '개 결과)');
      status.textContent = '비교 완료';
    } else {
      compareView.classList.remove('active');
      linksPanel.style.display = '';
      addMessage('assistant', data.answer);
      renderLinks(linksList, data.links);
      status.textContent = data.search_mode !== 'none' ? '검색: ' + data.search_mode + ' (' + data.links.length + '개 결과)' : '검색 미사용';
    }
  } catch (e) {
    addMessage('assistant', '요청 실패: ' + e);
    status.textContent = '';
  }
}

document.getElementById('send').addEventListener('click', send);
messageInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') send(); });
</script>
</body>
</html>
"""


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=WEBAPP_PORT)
