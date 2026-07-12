# Lens — 통합 검색 MCP 서버

DuckDuckGo와 네이버(뉴스/일반 웹문서/백과사전)를 동시에(스레드풀 병렬 실행) 검색하는 통합 웹 검색 MCP(Model Context Protocol) 서버입니다. 하나가 실패하거나, 시간 초과되거나, 호출 한도를 넘거나, 결과가 전부 불용어에 걸리면 자동으로 다른 엔진으로 넘어갑니다. 상위 결과는 링크만 주는 게 아니라 실제 페이지 본문까지 가져와 정리해서 반환합니다.

기본값은 **네이버 3종(뉴스+웹문서+백과사전) + 본문 보강**으로, DuckDuckGo는 응답 속도가 느리고 편차가 커서(실측 평균 4.9초, 최대 8.9초) 기본에서 제외되어 있습니다 — 자세한 실측 비교는 아래 [검색 속도 최적화](#검색-속도-최적화) 참고.

### Lens의 핵심 특장점

- **여러 엔진 동시 검색 + 자동 폴백** — 네이버 3종 + DuckDuckGo를 스레드풀로 병렬 실행하고, 하나가 실패/시간초과/한도초과되면 자동으로 다른 엔진을 시도합니다.
- **BM25 기반 관련성 필터 (자체 내장)** — 엔진을 여러 개 합치면 성격이 다른 검색 종류(특히 `naver_encyc` 같은 사전/백과사전)가 검색어와 우연히 한 단어만 겹치는 무관한 결과를 섞어 넣는 경우가 있습니다(예: 주가 질문에 "영구 제모 효과" 사전 항목이 낀 사례). Lens는 `bm25_search` 패키지와 동일한 BM25 모델을 `web_search` 툴 안에 직접 내장해, 각 결과의 **네이버 API 응답 자체(description/제목)** 를 검색어와 비교하고 관련성 낮은 결과는 본문을 가져오기도 전에 제외합니다. 이 필터는 데모 웹앱만이 아니라 **`server.py`의 실제 MCP 툴 자체에 내장**되어 있어, 어떤 MCP 클라이언트로 붙어도 동일하게 적용됩니다.
- **본문 보강 (관련성 필터 통과 결과만)** — 위 필터를 통과한 결과 중 상위 N개만 실제 페이지 본문을 가져와 채웁니다. 관련 없는 링크에 본문 요청을 낭비하지 않는 순서로 설계되어 있습니다.
- **불용어 정책 필터 + 절대 실패하지 않는 예외 처리** — 특정 키워드 결과 제외, 그리고 어떤 내부 오류가 나도 MCP 툴 자체는 항상 사용 가능한 문자열을 반환합니다.



> **전체 설명 문서**: [`docs/lens-overview.html`](./docs/lens-overview.html) — 아키텍처, 쓰레드 동시성 구조, 설정 가이드, BM25 파이프라인과 성능 평가를 한 페이지로 정리했습니다. 브라우저로 파일을 직접 열면 됩니다 (`start docs/lens-overview.html` 또는 더블클릭).

## 제공하는 툴

- **`web_search(query, max_results=3, engines="all", output_format="text")`** — 여러 검색엔진을 동시에 조회해 제목/URL/본문을 엔진별로 묶어서 반환합니다.
  - `engines`: `"duckduckgo"`, `"naver"`(뉴스), `"naver_web"`(일반 웹문서), `"naver_encyc"`(백과사전/용어사전) 중 콤마로 구분해 지정하거나, 기본값 `"all"`(=`.env`의 `DEFAULT_ENGINES` 또는 `SEARCH_ENGINE_MODE` 프리셋)을 사용합니다.
  - `output_format`: `"text"`(기본, 사람이 읽기 좋은 형태) 또는 `"json"`(`{"query", "engines", "results": [{"engine","title","url","content"}]}` 구조).
  - **동시 실행 + 시간 초과**: 요청된 엔진들은 스레드풀로 한 번에 실행되고(순차 실행 대비 훨씬 빠름), `SEARCH_ENGINE_TIMEOUT_SECONDS` 안에 응답 없는 엔진은 건너뜁니다.
  - **본문 보강**: 전체 엔진 결과를 합쳐서 상위 몇 개(기본 3개)는 스니펫이 아니라 실제 페이지 본문(trafilatura로 추출, 광고/댓글/저작권 문구 등은 제거)을 가져와 채워줍니다. Tavily 같은 LLM 전용 검색 API의 `content` 필드와 비슷한 형태입니다.
  - **불용어 필터**: 제목/스니펫에 특정 키워드(기본: 야구/축구/올림픽 등 스포츠 용어)가 있으면 결과에서 제외합니다. 검색 결과가 전부 불용어에 걸리면 "정책상 관련 키워드로 검색이 불가능합니다"라고 응답하고 자동으로 다른 엔진을 시도합니다.
  - **BM25 관련성 필터** (`RELEVANCE_FILTER`, 기본 켜짐): 전체 엔진 결과를 합쳐서 각 결과의 제목+설명(엔진이 이미 돌려준 텍스트, 추가 요청 없음)을 `bm25_search`와 동일한 BM25 모델로 검색어와 비교하고, 이번 배치 최고점 대비 `RELEVANCE_MIN_SCORE_RATIO`(기본 0.15) 미만인 결과는 본문 보강 이전에 제외합니다.
- **`search_stats()`** — 오늘 각 엔진(및 등록된 네이버 키별)이 몇 번 호출됐는지, 네이버는 일일 한도 대비 사용률까지 보여줍니다.

## 소스 위치

이 저장소 전체가 Lens입니다 — 핵심 서버 로직은 프로젝트 루트의 **`server.py`** 한 파일입니다. GitHub: [ChoiDongHee/ktds_lens](https://github.com/ChoiDongHee/ktds_lens)

```
git clone https://github.com/ChoiDongHee/ktds_lens.git
```

## 설치

```
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

## 실행 방법

`.env`를 채운 뒤(아래 [설정](#설정-env) 참고), 세 가지 방식 중 하나로 띄울 수 있습니다.

1. **직접 실행 (수동 테스트/확인용)** — `MCP_TRANSPORT=stdio`(기본값)일 때는 클라이언트 연결을 기다리며 대기하고, 자체 출력은 없습니다(stdout은 MCP 프로토콜 전용이라 로그도 `search.log`/stderr로만 나감).
   ```
   .venv/Scripts/python.exe server.py
   ```
2. **MCP 클라이언트가 자동으로 띄우게 등록 (실제 사용 방식)** — Claude Desktop/Claude Code에 등록하면 클라이언트가 필요할 때 위 명령을 자동으로 실행합니다. 등록 명령어는 아래 [MCP 클라이언트에 등록하기](#mcp-클라이언트에-등록하기) 참고.
3. **네트워크(sse/streamable-http)로 띄워 여러 클라이언트가 공유** — `.env`에서 `MCP_TRANSPORT=sse`(또는 `streamable-http`)로 바꾸고 위와 동일하게 실행하면 `MCP_HOST:MCP_PORT`(기본 `127.0.0.1:8000`)에서 대기합니다. 이 경우 클라이언트는 명령이 아니라 URL로 등록합니다.

프로토콜을 거치지 않고 코드만 빠르게 확인하려면:
```
.venv/Scripts/python.exe -c "import server; print(server.router.search('제주도 여행', 3, engines='all'))"
```

## 설정 (`.env`)

`.env.example`을 복사해 `.env`를 만들고 값을 채웁니다.

```
cp .env.example .env
```

| 변수 | 설명 | 기본값 |
|---|---|---|
| `NAVER_CLIENT_IDS` / `NAVER_CLIENT_SECRETS` | 네이버 Client ID/Secret 배열 (콤마 구분, 순서로 짝지어짐) | (없음) |
| `NAVER_DAILY_LIMIT` / `NAVER_LIMIT_THRESHOLD` | 키 1개당 1일 호출 한도 / 이 비율 도달 시 다음 키로 순환 | `25000` / `0.8` |
| `NAVER_SEARCH_ENDPOINT` / `_2` / `_3` | 네이버 검색 종류 (뉴스/웹문서/백과사전), 같은 키로 3개까지 등록 가능 | `news` / `webkr` / `encyc` |
| `EXCLUDE_KEYWORDS` | 결과에서 제외할 키워드 목록 (콤마 구분) | 스포츠 관련 키워드 |
| `EXCLUDE_OVERFETCH_COUNT` | 필터링 활성 시 미리 넉넉히 가져올 개수 | `10` |
| `RELEVANCE_FILTER` / `RELEVANCE_MIN_SCORE_RATIO` | BM25 관련성 필터 켜기/끄기, 최고점 대비 살아남는 최소 점수 비율 | `true` / `0.15` |
| `FETCH_CONTENT` / `FETCH_CONTENT_TOP_N` / `FETCH_CONTENT_MAX_CHARS` / `FETCH_CONTENT_MIN_USEFUL_CHARS` | 본문 보강 켜기/끄기, 보강할 개수/최대 글자수/최소 유효 글자수 | `true` / `3` / `3000` / `200` |
| `SEARCH_ENGINE_TIMEOUT_SECONDS` | 엔진 하나당 최대 대기 시간(초) | `15` |
| `SEARCH_ENGINE_MODE` | 엔진 구성 프리셋: `hybrid`/`naver`/`duckduckgo` (`DEFAULT_ENGINES` 지정 시 무시됨) | `naver` |
| `DEFAULT_ENGINES` | `engines="all"`일 때 사용할 엔진 목록 (지정 시 `SEARCH_ENGINE_MODE`보다 우선) | `naver,naver_web,naver_encyc` |
| `LOG_LEVEL` / `LOG_FILE` | 로그 레벨 / 파일 경로 | `INFO` / `search.log` |
| `HIT_COUNT_LOG_FILE` / `HIT_COUNT_INTERVAL_SECONDS` | 별도 호출 현황 로그 파일 / 기록 주기(초) | `hit_count.log` / `60` |
| `MCP_TRANSPORT` / `MCP_HOST` / `MCP_PORT` | MCP 통신 방식(`stdio`/`sse`/`streamable-http`) / 호스트 / 포트 | `stdio` / `127.0.0.1` / `8000` |

네이버 키는 [네이버 개발자센터](https://developers.naver.com/apps)에서 애플리케이션을 등록하고 "검색" API를 추가하면 앱 상세 페이지에서 확인할 수 있습니다. **ID와 Secret은 서로 다른 값**이니 헷갈려 같은 값을 넣지 않도록 주의하세요(둘 다 같은 값을 넣으면 `401 Unauthorized`가 발생합니다). 여러 개의 앱을 등록해 키를 여러 개 넣어두면, 하나가 한도에 가까워졌을 때 자동으로 다음 키로 순환합니다.

## 검색 속도 최적화

`web_search`의 실제 응답 속도를 여러 구성으로 실측한 결과입니다 (동일 질의 3~5개 반복 평균, 네트워크 상태에 따라 변동 가능 — 재현: 아래와 같이 `server.router.search()`를 직접 호출해 `time.perf_counter()`로 측정).

| 구성 | 평균 | 비고 |
|---|---|---|
| DuckDuckGo만 | 4.9초 (최대 8.9초) | ddgs가 내부적으로 여러 백엔드를 순회해서 느리고 편차가 큼 |
| 전체 4엔진 + 본문보강 (예전 기본값) | 5.2초 | DuckDuckGo가 병목 |
| 네이버 3종(뉴스+웹+사전), 본문보강 OFF | 0.63초 | |
| **네이버 뉴스만, 본문보강 OFF** | **0.25초** | 순수 속도 최우선일 때 |
| 네이버 뉴스만, 본문보강 ON | 0.84초 | |
| **네이버 3종 + 본문보강 ON (현재 기본값)** | **1.48초** | 속도보다 커버리지/답변 품질 우선 |

**결론**: DuckDuckGo(ddgs)는 응답 속도가 근본적으로 느리고 편차가 커서 기본 엔진 목록에서 제외했습니다(`engines="duckduckgo"`로 명시하면 여전히 사용 가능). 본문 보강(`FETCH_CONTENT`)은 상위 결과 페이지를 실제로 방문하는 추가 네트워크 단계라 어떤 조합이든 +0.5~0.8초 정도 더 붙는 근본적인 트레이드오프입니다. 현재 기본값은 답변 품질(실제 기사 본문 기반 요약)을 우선해 네이버 3종 + 본문 보강을 켜둔 상태(평균 1.48초)이며, 0.5초 이내가 필요하면 `.env`에서 `DEFAULT_ENGINES=naver`, `FETCH_CONTENT=false`로 바꾸면 됩니다(주석 처리된 대안 값이 `.env`/`.env.example`에 이미 적혀 있음).

```
.venv/Scripts/python.exe -c "
import time, server
t0 = time.perf_counter()
server.router.search('삼성전자 주가', 3, engines='all')
print(time.perf_counter() - t0)
"
```

## MCP 클라이언트에 등록하기

### Claude Desktop

`stdio` 방식(기본값)은 설정 파일(Windows: `%APPDATA%\Claude\claude_desktop_config.json`)의 `mcpServers`에 아래 항목을 추가합니다.

```json
{
  "mcpServers": {
    "web-search": {
      "command": "D:\\Projects\\WEB_SEARCH\\.venv\\Scripts\\python.exe",
      "args": ["D:\\Projects\\WEB_SEARCH\\server.py"]
    }
  }
}
```

저장 후 Claude Desktop을 재시작하면 `web_search`, `search_stats` 툴을 사용할 수 있습니다. `sse`/`streamable-http`로 띄운 원격 서버를 등록하려면 Claude Desktop이 URL을 직접 지원하지 않으므로 `mcp-remote` 같은 stdio↔HTTP 브리지가 필요합니다.

### Claude Code

**stdio (기본, URL 없음)** — 프로젝트 루트(또는 아무 위치)에서 (`--` 뒤에 실제 실행할 명령을 그대로 적습니다):

```
claude mcp add --transport stdio web-search -- "D:\Projects\WEB_SEARCH\.venv\Scripts\python.exe" "D:\Projects\WEB_SEARCH\server.py"
```

기본 스코프는 현재 프로젝트에만 등록되는 `local`입니다. 여러 프로젝트에서 쓰려면 `--scope user`를, 팀과 공유하려면 `--scope project`(프로젝트 루트에 `.mcp.json`으로 저장됨)를 붙입니다.

**sse/streamable-http (네트워크 포트, URL로 등록)** — `.env`에서 `MCP_TRANSPORT=sse`(또는 `streamable-http`), `MCP_HOST`/`MCP_PORT`를 설정하고 서버를 실행한 뒤:

```
claude mcp add --transport http web-search http://127.0.0.1:8000/mcp
```

(`sse`는 레거시 스펙이라 새로 구성할 때는 `streamable-http` + `--transport http` 조합을 권장합니다.) `.mcp.json`에 직접 추가할 때는 stdio 방식 기준으로 아래 형식을 씁니다.

```json
{
  "mcpServers": {
    "web-search": {
      "command": "D:\\Projects\\WEB_SEARCH\\.venv\\Scripts\\python.exe",
      "args": ["D:\\Projects\\WEB_SEARCH\\server.py"]
    }
  }
}
```

등록 후 `claude mcp list`로 연결 상태를 확인할 수 있습니다.

## 로컬에서 빠르게 테스트하기

MCP 클라이언트 없이도 파이썬으로 직접 호출해볼 수 있습니다 (stdout은 MCP 프로토콜 전용이라 `server.py`를 직접 실행하면 아무것도 출력되지 않는 것이 정상입니다):

```
.venv/Scripts/python.exe -c "import server; print(server.router.search('제주도 여행', 3, engines='all'))"
```

로그는 `search.log`(및 stderr)에 상세히 남습니다 — 요청마다 부여되는 request_id로 묶여서 어떤 엔진/네이버 키가 쓰였는지, 불용어 필터링 결과, BM25 관련성 필터링 결과, 본문 보강 결과, 소요시간, 실패 시 폴백 여부 등을 확인할 수 있습니다. 호출 현황은 `hit_count.log`에 `HIT_COUNT_INTERVAL_SECONDS`마다 별도로 쌓입니다.

### 로그 읽는 법 (중요 인사이트)

`search.log`에서 아래 패턴을 찾으면 대부분의 문제를 빠르게 진단할 수 있습니다.

| 찾을 문자열 | 의미 |
|---|---|
| `Lens 서버 설정:` | 서버 기동 시 1회 남는 요약 로그. 등록된 엔진, `engines="all"`이 실제로 무엇으로 풀리는지, 관련성 필터/본문 보강 on-off, 등록된 네이버 키 개수를 한 줄로 보여줌 — `.env`를 따로 열어보지 않아도 "이 로그를 남긴 서버가 그 시점에 어떤 설정으로 떠 있었는지" 바로 확인 가능. |
| `===== [xxxxxxxx] web_search 요청 시작/종료 =====` | 8자 request_id로 묶인 한 번의 호출 전체. 시작~종료 사이 줄만 보면 그 요청에서 어떤 엔진이 시도됐고 얼마나 걸렸는지 전부 나옴. |
| `건너뜀` / `한도 근접` | 네이버 키가 임계값(`NAVER_LIMIT_THRESHOLD`)에 도달해 다음 키로 순환 중. 모든 키가 이 상태면 `QuotaExceeded`로 폴백. |
| `불용어 '...' 포함으로 결과 제외` | `EXCLUDE_KEYWORDS`에 걸려 제외된 개별 결과. 원본이 전부 이걸로 제외되면 `PolicyRestricted`. |
| `BM25 유사도 낮음으로 제외` | 관련성 필터가 특정 결과를 제외한 이유(어떤 엔진, 어떤 점수/임계값). 검색 결과가 예상보다 적게 나올 때 여기부터 확인. |
| `본문 추출 실패/부실` | 해당 URL이 JS 렌더링 페이지 등이라 유효한 본문을 못 가져와 다음 후보로 넘어감. |
| `결과 없음 -> 나머지 등록된 엔진으로 폴백` | 요청된 엔진이 전부 실패/제외되어 나머지 등록된 엔진(보통 duckduckgo)으로 자동 전환. |
| `[hit-count] 주기 실행 중 오류` | 호출 현황 백그라운드 스레드에서 오류가 났지만 다음 주기에 재시도 중(검색 자체와는 무관, 통계 파일 I/O 문제 등). |
| `호출 통계 파일(...) 저장/읽기 실패` | `search_stats.json` 쓰기/읽기 실패 (디스크 문제 등) — 이번 호출 수만 누락되고 검색 자체는 정상 진행됨. |

이 로그들은 전부 "무언가 잘못돼도 검색 자체는 계속 진행된다"는 원칙 위에서 설계되어 있습니다: `web_search`/`search_stats` 툴은 무슨 일이 있어도 예외를 그대로 던지지 않고(항상 문자열을 반환), 호출 통계 저장 실패나 호출 현황 백그라운드 스레드의 오류도 검색 결과나 서버 프로세스 자체에 영향을 주지 않도록 각각 자체적으로 예외를 잡아 로그만 남기고 계속 동작합니다.

### 실제 MCP 프로토콜 종단 테스트

`test/test_mcp_client.py`는 실제로 서버 프로세스를 SSE 트랜스포트로 띄우고, 진짜 MCP 클라이언트처럼 연결해서 `list_tools`/`call_tool`을 호출해 검증합니다.

```
.venv/Scripts/python.exe test/test_mcp_client.py
```

### 검색 품질 비교 데모 (FastAPI 챗 + vLLM)

`test/webapp/app.py`는 vLLM(Qwen)에 연결된 간단한 챗 UI로, 네 가지 모드(우리 MCP `web_search` / Tavily MCP / **비교(둘 다 동시 실행 후 나란히 표시)** / 검색 안 함)를 토글해서 답변과 실제 참고한 링크를 비교해볼 수 있습니다. "비교" 모드는 두 백엔드의 검색+LLM 호출을 스레드풀로 동시에 실행해 한 번의 질문으로 양쪽 답변을 한 화면에서 볼 수 있습니다. `.env`에 `QWEN27B_VLLM_*`, `TAVILY_API_KEY`를 채운 뒤:

```
.venv/Scripts/python.exe test/webapp/app.py
```

브라우저에서 `http://127.0.0.1:8800`(기본 `WEBAPP_PORT`)을 엽니다.

## bm25_search — BM25 유사도 검색 파이프라인

`bm25_search/`는 네이버 뉴스 후보군을 가져와 **BM25 키워드 유사도 + 최신성 감쇠**로 재정렬하는 독립 파이프라인(`pipeline.py`, CLI로 직접 실행 가능)이자, 그 안의 `tokenizer.py`/`bm25_ranker.py` 두 모듈은 `server.py`의 `web_search` 툴이 [BM25 관련성 필터](#제공하는-툴)로, `test/webapp/app.py`가 데모용 링크 필터로 직접 가져다 쓰는 공용 부품이기도 합니다 — 즉 "독립 파이프라인" + "재사용되는 BM25 코어" 두 가지 역할을 겸합니다.

- `fetcher.py` — 네이버 뉴스 API로 후보 목록 수집 + 각 기사 원문을 스레드풀로 동시에 가져옴
- `tokenizer.py` — Kiwi 인스턴스를 모듈 레벨에서 1회 생성, HTML 제거 후 형태소 분석(명사/용언 어간만 추출), 배치 토큰화 지원 — **`server.py`/`test/webapp/app.py`가 직접 import**
- `bm25_ranker.py` — `rank_bm25.BM25Okapi` 기반 `build_index()`/`search()` — **`server.py`/`test/webapp/app.py`가 직접 import**
- `ranker.py` — BM25 점수 × `exp(-λ*hours)` 최신성 감쇠로 재정렬하는 `rerank()` (파이프라인 전용)
- `pipeline.py` — `fetcher → tokenizer → bm25_ranker → ranker` 순서로 오케스트레이션, 기본 상위 4건만 반환 (독립 CLI/스크립트 전용, `web_search` 툴과는 별개)

```
.venv/Scripts/python.exe -m bm25_search.pipeline "검색어"
```

### 성능 리포트 (`test/test_bm25_benchmark.py`)

네이버 뉴스 실 API로 "삼성전자 주가" 질의를 대상으로 측정 (Kiwi 워커풀 워밍업 후). 재현: `.venv/Scripts/python.exe test/test_bm25_benchmark.py` — 실시간 뉴스/네트워크 상태에 따라 수치는 변동될 수 있음.

**1) 성능 — 후보 개수별 랭킹 단계(tokenize+bm25+rerank) 소요시간**

| 후보 개수 | 소요시간 |
|---|---|
| 30 | 14.2ms |
| 60 | 27.2ms |
| 100 | 45.9ms |

후보 30~100건 전 구간에서 100ms 목표를 여유 있게 만족하며, 대략 후보 개수에 선형으로 비례해 늘어남.

**2) 속도 — title+설명만 vs 전체 본문 포함, 5회 중앙값 (후보 30건)**

| 대상 텍스트 | tokenize_batch | bm25+rerank | 합계 |
|---|---|---|---|
| title + 설명(스니펫)만 | 11.6ms | 1.1ms | **12.7ms** |
| title + 전체 본문 | 149.7ms | 4.7ms | **154.4ms** |

랭킹 단계만 100ms 안에 끝내야 한다면 `fetch_full_content=False`(설명 텍스트만 사용)로 호출. 전체 본문을 포함하면 텍스트량이 늘어 랭킹 자체는 150ms 안팎이 되지만, 이 모드에선 기사 원문을 가져오는 네트워크 fetch(30건 기준 2~4초, 스레드풀 동시 실행)가 전체 소요시간을 지배하므로 실질적인 영향은 적음. (참고: Kiwi의 배치 토큰화는 프로세스 내 최초 1회 호출 시 내부 워커풀을 초기화하며 ~1~1.5초의 워밍업 비용이 들고, 이후 호출부터는 위 수치대로 빠름 — 요청마다 새 프로세스를 띄우는 1회성 스크립트가 아니라 계속 떠있는 서버 프로세스에서 의미 있는 절충.)

**3) 품질 — 네이버 기본 순서 vs BM25/BM25+최신성 재정렬 (상위 4건 평균 질의어 일치 수)**

| 정렬 방식 | 평균 질의어 일치 수 |
|---|---|
| 네이버 기본 순서(그대로) | 1.75 |
| BM25만 적용 | 2.00 |
| BM25 + 최신성 감쇠 | 2.00 |

BM25 재정렬이 네이버의 기본 순서보다 질의어와 실제로 더 많이 겹치는 문서를 상위로 올림을 확인. 이 실행에서는 최신성 감쇠가 BM25 전용 순서를 바꾸지 않았는데(모든 후보가 같은 날짜 뉴스라 감쇠 계수가 서로 비슷했기 때문), 오래된 기사와 최신 기사가 섞인 질의에서는 감쇠가 순위를 실제로 재조정하는 경우가 발생함(`decay_lambda`로 감쇠 속도 조절 가능, `test_3_quality_vs_naive_order()`가 "BM25 전용 순서와 최신성 반영 후 순서가 동일한가"를 매 실행마다 출력해 확인 가능).

## 아키텍처

엔진 구조, 폴백 로직, 본문 보강/불용어 필터링, 로깅, 동시성 처리 등 코드 구조에 대한 자세한 설명은 [`CLAUDE.md`](./CLAUDE.md)를, 설계 배경과 검증 내역은 [`PLAN.md`](./PLAN.md)를 참고하세요.
