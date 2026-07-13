# Lens — 통합 웹 검색 MCP 서버 설계 및 검증 기록

## 배경

`server.py` 하나로 이루어진 MCP 서버를 DuckDuckGo만 지원하던 단일 툴에서, 여러 검색엔진을 조합하고 실제 페이지 본문까지 정리해서 반환하는 통합 검색 툴로 확장했다. 처음에는 Google 헤드리스 브라우저 스크래핑(`noapi-google-search-mcp`)까지 포함했으나, 무겁고(playwright/opencv/onnx 등 500MB+) 느리고(20초/회) 비동기(SSE) 환경에서 `asyncio.run()` 충돌 버그까지 있어 제거했다(`backup/google_engine_removed.py`에 복원 노트와 함께 보관). 이후 네이버 검색을 `webkr`(일반 웹문서)에서 `news`로 바꿨다가, 뉴스만으로는 실시간 주가 페이지 같은 걸 놓친다는 게 확인되어 최종적으로 **DuckDuckGo + 네이버 뉴스 + 네이버 일반 웹문서** 세 엔진을 병행하는 구조로 정착했다.

## 최종 엔진 구성 (기본 `engines="all"` 기준, 각 3개씩)

1. **DuckDuckGo** (`duckduckgo`) — 키 불필요, 호출 한도 없음. `ddgs` 라이브러리가 내부적으로 여러 백엔드(위키피디아, Brave, Yahoo, Startpage, Mojeek, Yandex 등)를 섞어서 결과를 줌.
2. **Naver 뉴스** (`naver`) — 오픈API 키 필요. `news.json` 엔드포인트, `originallink`(원본 기사 URL) 사용. 뉴스/시사성 질의에 강함.
3. **Naver 일반 웹문서** (`naver_web`) — 같은 키로 `webkr.json` 엔드포인트를 추가 등록. 뉴스가 놓치는 실시간 시세 페이지, 공식 IR 페이지 등을 보완. `naver`와 같은 앱/쿼터를 공유(별도 소진 아님).

키(앱) 여러 개를 배열로 등록하면 하나가 일일 한도의 threshold(기본 80%)에 도달했을 때 다음 키로 자동 순환하고, 모두 소진되면 다른 엔진으로 자동 폴백한다.

## 핵심 동작

- **불용어 필터**: 검색어와 무관한 화제(스포츠 등)가 결과에 섞이는 걸 막기 위해, 각 엔진이 요청한 개수보다 넉넉히(기본 10개) 가져온 뒤 제목/스니펫에 `EXCLUDE_KEYWORDS`가 있으면 제외하고 남은 것 중 상위 개수만 반환. 원본이 전부 걸러지면 `PolicyRestricted` 예외로 "정책상 관련 키워드로 검색이 불가능합니다"를 반환하며 다른 엔진으로 자동 폴백.
- **BM25 관련성 필터**: 본문 보강 *이전에*, 전체 엔진 결과를 합쳐서 각 결과의 제목+설명(엔진이 이미 돌려준 텍스트)을 `bm25_search.tokenizer`/`bm25_ranker`(독립 BM25 파이프라인과 동일한 모델)로 검색어와 비교하고, 이번 배치 최고 점수 대비 `RELEVANCE_MIN_SCORE_RATIO`(기본 0.15) 미만인 결과는 제외한다. 처음에는 `test/webapp`(데모 챗)에만 있었으나, 실사용 시 naver_encyc 같은 벡터가 검색어와 한 단어만 우연히 겹치는 무관한 결과(예: 주가 질문에 "영구 제모 효과" 사전 항목)를 섞는 게 실제 MCP 툴 자체에서도 재현되어, `server.py`의 `SearchRouter.search()`에 직접 내장했다 — 데모 페이지뿐 아니라 어떤 MCP 클라이언트로 붙어도 동일하게 적용된다.
- **본문 보강**: 관련성 필터를 통과한 결과 중 상위 N개(기본 3개)는 스니펫이 아니라 실제 페이지 본문을 `trafilatura`로 가져와 채움 (`include_comments=False`, `favor_precision=True` + 정규식으로 저작권 문구/댓글 라벨/빈 줄 정리). 내용이 부실한 링크(보통 JS 렌더링 페이지)는 건너뛰고 다음 후보로 넘어감 — 후보를 넉넉히 동시에 가져온 뒤 실제로 유효한 것만 채택하는 방식.
- **폴백**: 한 엔진이 `EngineUnavailable`(키 없음)/`QuotaExceeded`(한도 초과)/`PolicyRestricted`(전부 불용어)로 실패하면, **아직 다른 엔진에서 결과를 못 받은 경우에만** 나머지 등록된 엔진을 자동으로 시도. 이미 결과가 있으면 불필요한 폴백을 하지 않음(발견된 버그: 이 조건이 없으면 duckduckgo가 이미 성공했는데도 naver 키 미설정만으로 매번 무거운 폴백이 걸림).
- **출력 형식**: 기본 텍스트, 또는 `output_format="json"`으로 Tavily 스타일 `{"query","engines","results":[{"engine","title","url","content"}]}` 구조.
- **동시성/트랜스포트**: `stdio`(기본) 또는 `sse`/`streamable-http`(네트워크 포트, URL로 등록) 선택 가능. FastMCP가 동기 툴 함수를 이벤트 루프 스레드에서 직접 호출하는 문제(블로킹 I/O가 다른 동시 요청을 막음, Google 엔진 시절엔 `asyncio.run()` 충돌까지 발생) 때문에 `web_search`를 `async`로 바꾸고 `anyio.to_thread.run_sync`로 실제 작업을 워커 스레드에 위임.
- **예외 처리**: `web_search`/`search_stats` 둘 다 전체를 `try/except`로 감싸 어떤 경우에도 MCP 툴 오류로 전파되지 않고 문자열 오류 메시지를 반환. 본문 보강/JSON 변환도 각각 자체 예외 처리로 실패 시 스니펫/텍스트로 자동 대체.
- **로깅**: `search.log`(요청별 request_id로 묶인 상세 로그: 엔진 시도, 네이버 키/한도 체크, 불용어 제외, 본문 보강 결과, 폴백 사유, 소요시간, 최종 반환 결과) + 별도 `hit_count.log`(주기적 호출 현황, 자정 리셋).

## 설정 (.env, 하드코딩 없음)

전체 목록과 설명은 `README.md`의 설정 표와 `.env.example`을 참고. 요약:

- 네이버: `NAVER_CLIENT_IDS`/`NAVER_CLIENT_SECRETS`(배열), `NAVER_DAILY_LIMIT`/`NAVER_LIMIT_THRESHOLD`, `NAVER_SEARCH_ENDPOINT`/`NAVER_SEARCH_ENDPOINT_2`
- 불용어: `EXCLUDE_KEYWORDS`, `EXCLUDE_OVERFETCH_COUNT`
- BM25 관련성 필터: `RELEVANCE_FILTER`, `RELEVANCE_MIN_SCORE_RATIO`
- 본문 보강: `FETCH_CONTENT`, `FETCH_CONTENT_TOP_N`, `FETCH_CONTENT_MAX_CHARS`, `FETCH_CONTENT_MIN_USEFUL_CHARS`
- 엔진 구성: `DEFAULT_ENGINES`
- 로깅: `LOG_LEVEL`/`LOG_FILE`, `HIT_COUNT_LOG_FILE`/`HIT_COUNT_INTERVAL_SECONDS`
- 트랜스포트: `MCP_TRANSPORT`/`MCP_HOST`/`MCP_PORT`
- 테스트 웹앱(`test/webapp`) 전용: `QWEN27B_VLLM_*`, `TAVILY_API_KEY`, `WEBAPP_MAX_RESULTS`, `WEBAPP_LOG_FILE`

## 검증 내역

- **UTF-8 인코딩 버그**: Windows 콘솔 기본 인코딩(cp949)에서 검색 결과에 특수문자가 섞이면 MCP 서버가 크래시하는 걸 재현 후 `sys.stdout/stderr.reconfigure(encoding="utf-8")`로 해결하고 재검증.
- **네이버 키 순환**: 가짜 자격증명 + `httpx.get` 목(mock)으로 한도(90%) 넘은 키 2개는 건너뛰고 남은 키로 정상 전환됨을 확인.
- **폴백 과다 실행 버그**: `engines="all"`에서 duckduckgo가 이미 성공했는데도 naver 키 미설정만으로 매번 무거운 폴백(당시 Google, 20초)이 걸리는 걸 재현 → `has_results` 플래그로 수정 → 8.9초 → 1.7초로 개선 확인.
- **동시성 버그**: FastMCP가 동기 툴을 이벤트 루프 스레드에서 직접 호출한다는 것을 소스 확인 → `asyncio.run()`이 이미 실행 중인 이벤트 루프 안에서 크래시하는 걸 재현 → `anyio.to_thread.run_sync`로 수정 → 워커 스레드에서는 충돌 없음을 재확인, 8개 동시 요청이 17.4초(직렬 가정) 대신 4.07초(진짜 병렬)에 끝남을 측정, `CallStats`에 락을 걸어 카운트 손실 없음도 확인.
- **본문 추출 품질**: 초기 BeautifulSoup 기반 추출이 JS 렌더링 페이지(예: visitkorea.or.kr)에서 "본문 바로가기 좋아요 조회수 0..." 같은 네비게이션 텍스트만 가져오는 문제를 발견 → `trafilatura`로 교체, 뉴스 기사 실제 테스트에서 정상 추출 확인 → `include_comments=True`(trafilatura 기본값) 때문에 "댓글" 텍스트가 섞이는 것도 발견 → `include_comments=False`+`favor_precision=True`로 해결.
- **네이버 검색 관련성 한계**: "삼성전자 주가"와 "SK하이닉스 주가" 질의에 네이버 뉴스 API가 동일한 top-3 결과(당일 최대 화제 뉴스)를 반환하는 걸 원시 API 직접 호출로 재현 — 우리 코드 버그가 아니라 네이버 API 자체의 관련성 한계로 확인. `naver_web`(webkr) 엔진 추가 후 DuckDuckGo와 함께 실제 삼성전자 시세 페이지(매일경제 마켓, 실제 종가 285,000원 등)를 정상적으로 찾아옴을 확인해 완화.
- **JSON/텍스트 출력**: 실제 SK하이닉스/삼성전자 질의로 두 출력 형식 모두 정상 동작 확인.
- **불용어 필터**: 구현 후 컴파일 확인(실제 걸러지는 사례는 후속 테스트 필요).
- **FastAPI 데모 챗 (test/webapp)**: vLLM(Qwen) 연동 확인(실제 200 응답), Tavily MCP 서버(`https://mcp.tavily.com`) 연동 확인(`tavily_search` 툴 호출 성공), 우리 MCP 검색 결과를 근거로 한 RAG 답변이 실제 수치를 인용하고 "정확한 실시간 수치는 없다"고 솔직히 밝히는 것까지 확인. 두 백엔드를 나란히 비교할 수 있는 3-way 토글(MCP/Tavily/검색안함) 구현.
- **BM25 관련성 필터를 MCP 서버 자체에 내장**: 처음엔 `test/webapp`에만 있던 필터(naver_encyc가 검색어와 우연히 한 단어만 겹치는 무관한 결과, 예: 주가 질문에 "영구 제모 효과" 사전 항목을 섞는 문제 대응)를 `server.py`의 `SearchRouter.search()`에 직접 옮겨 넣음 — 합성 테스트 케이스("나스닥 상장 후 주가 흐름" 검색어에 관련 결과 2건 + "영구 제모 효과와 부작용" 무관 결과 1건을 섞어서 `_filter_by_relevance()` 직접 호출)로 무관한 결과가 정확히 제외되는 것을 확인. 실제 네이버 API 호출로도 정상 동작(관련 결과는 그대로 유지, 필터가 오탐 없이 통과) 확인.
- **URL/특정 사이트 검색어 처리 검토 → BM25 필터 무력화 버그 발견 및 수정**: "검색어에 URL이 들어오면 어떻게 되는지" 검토 중 `tokenizer.tokenize('https://www.samsung.com/sec/')`가 빈 리스트 `[]`를 반환하는 걸 발견 — Kiwi가 URL 전체를 `_KEEP_TAGS` 밖의 단일 토큰으로 처리하기 때문. 이 경우 `_filter_by_relevance()`의 `if not tokenized_query: return` 가드에 걸려 **관련성 필터가 조용히 완전히 스킵**됨(에러는 없지만 필터 기능 자체가 무력화). `bm25_search/tokenizer.py`에 URL을 도메인/경로 토큰으로 분해하는 `_url_tokens`/`_extract_and_strip_urls`를 추가해 수정(예: `https://www.samsung.com/sec/` → `["samsung", "sec"]`); 수정 과정에서 두 번째 버그도 발견 — Kiwi는 외래어(SL) 토큰의 원본 대소문자를 그대로 보존(`"Samsung"`)하는데 BM25는 정확한 문자열 일치로 매칭하므로, URL에서 뽑은 소문자 토큰(`"samsung"`)이 실제 문서의 `"Samsung"`과 절대 매칭되지 않는 문제 → 모든 토큰을 일괄 소문자화(`.lower()`)해서 해결. 2건짜리 합성 테스트는 BM25 idf 공식이 우연히 정확히 0이 되는 소규모 코퍼스 특성 때문에 거짓 음성이 나왔던 것도 확인(코드 버그 아님) — 실제 사용 규모(엔진 3개 × 결과 3개 = 9건)로 재현해 정상 동작 확인, 실제 네이버 API 호출로도 `naver_encyc`의 무관한 결과가 정확히 전부 제외되는 것을 확인.
- **웹앱에서 URL 검색어 다각도 실측 → 본문 보강 타임아웃 무제한 버그 발견 및 수정**: 위 수정 후 실제 웹앱(`test/webapp/app.py`)의 `/api/chat`으로 여러 URL 검색어를 테스트하던 중, `"https://nonexistent-fake-domain-xyz123.com/"` 검색어에서 전체 요청이 54.7초 걸리는 걸 발견. `search.log`로 추적한 결과 검색 자체(엔진 호출)는 빨랐고 `본문 보강 완료 (37.861s)` 한 줄에서 원인 확인 — `naver_web`이 반환한 후보 중 하나가 큰 OWASP 보안 가이드 PDF였는데, `trafilatura.fetch_url()`의 기본 `DOWNLOAD_TIMEOUT`이 30초(`MAX_FILE_SIZE` 20MB까지 허용)라 이 한 건이 스레드풀 전체를 30초 넘게 붙잡음(`_enrich_with_content`는 모든 후보가 끝나야 반환됨). `trafilatura.settings.use_config()`로 커스텀 Config를 만들어 `DOWNLOAD_TIMEOUT`을 새 `FETCH_CONTENT_TIMEOUT_SECONDS`(처음 8초)로 낮춰 해결 — 재측정 결과 동일 검색어의 검색 단계가 38.475초 → 0.548초로, 전체 요청이 54.659초 → 8.307초로 개선된 것을 확인.
- **타임아웃 값 조정 + PDF 결과 완전 제외로 후속 개선**: 사용자 지시로 `FETCH_CONTENT_TIMEOUT_SECONDS`를 5초, `SEARCH_ENGINE_TIMEOUT_SECONDS`를 15초→8초로 더 조였음. 이어서 실측 결과를 다시 보니, PDF 결과는 우리가 직접 fetch를 건너뛰어도 Naver 자체 스니펫 역시 PDF 본문을 문맥 없이 잘라 보여주는 터라("org 2 The Open Web Application Security Project (OWASP) is a worldwide free and open com...") 어차피 못 읽는다는 지적을 받아, PDF로 판별되는(`.pdf` 확장자) 결과는 `_enrich_with_content` 진입 전에 아예 결과 목록에서 제외(`_filter_out_pdfs`)하도록 변경 — 재측정으로 PDF 2건이 결과에서 완전히 빠지고 정상 결과만 남는 것, 일반 검색어(비-URL, 비-PDF)는 회귀 없이 동일하게 동작하는 것을 확인.
