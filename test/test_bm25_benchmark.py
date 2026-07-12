"""Benchmark/quality report for bm25_search (performance, speed, quality).

Not a pytest suite -- a standalone script that exercises the real pipeline
against the live Naver News API and prints a report, used to produce the
numbers documented in README.md's "bm25_search 성능 리포트" section. Rerun
this whenever the pipeline changes materially, since these are live-API
timings that will drift with network conditions and how much news exists
for a given query on a given day.

Run: .venv/Scripts/python.exe test/test_bm25_benchmark.py
"""

import logging
import os
import statistics
import sys
import time

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE_DIR)
sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.WARNING)  # keep the report output clean; pipeline logs are INFO

from bm25_search import bm25_ranker, fetcher, pipeline, ranker, tokenizer  # noqa: E402

QUERY = "삼성전자 주가"


def _warm_up_kiwi() -> None:
    # Kiwi's batch tokenize spins up an internal worker pool on first use
    # (~1-1.5s one-time cost) -- warm it up before timing anything so the
    # benchmark measures steady-state performance, matching a long-running
    # server process rather than a cold CLI invocation.
    tokenizer.tokenize_batch(["워밍업 문장입니다."] * 5)


def test_1_performance_scaling() -> None:
    """Test 1 -- 성능(Performance): does the ranking stage scale reasonably
    as candidate_count grows across the documented 30-100 design range?"""
    print("\n=== Test 1: 성능 (후보 개수별 랭킹 단계 소요시간) ===")
    for candidate_count in (30, 60, 100):
        documents = fetcher.fetch_news_list(QUERY, display=candidate_count)
        texts = [f"{d['title']} {d['description']}" for d in documents]

        started = time.perf_counter()
        tokenized = tokenizer.tokenize_batch(texts)
        tokenized_query = tokenizer.tokenize(QUERY)
        index = bm25_ranker.build_index(tokenized)
        bm25_results = bm25_ranker.search(index, tokenized_query, top_k=len(documents))
        ranker.rerank(bm25_results, documents, top_k=4)
        elapsed_ms = (time.perf_counter() - started) * 1000

        print(f"  candidate_count={candidate_count:>3} (실제 수집 {len(documents):>3}건): {elapsed_ms:6.1f}ms")


def test_2_speed_breakdown() -> None:
    """Test 2 -- 속도(Speed): where does the time actually go, and how much
    does including full article content (vs. just title+description) cost?
    Reports median of 5 warm runs for each stage/mode."""
    print("\n=== Test 2: 속도 (단계별 분해, title+description vs 전체 본문, 5회 중앙값) ===")

    documents_short = fetcher.fetch_news_list(QUERY, display=30)
    # fetch_contents mutates its documents in place -- pass independent dict
    # copies (not just a shallow list copy, which would still share the
    # same underlying dict objects and silently "leak" content into
    # documents_short too) so the two cases below are actually different.
    documents_full = fetcher.fetch_contents([dict(d) for d in documents_short], max_chars=3000)

    for label, documents in (("title+description", documents_short), ("title+전체본문", documents_full)):
        texts = [f"{d['title']} {d.get('content') or d['description']}" for d in documents]

        tokenize_times, rank_times = [], []
        for _ in range(5):
            t0 = time.perf_counter()
            tokenized = tokenizer.tokenize_batch(texts)
            tokenize_times.append(time.perf_counter() - t0)

            t0 = time.perf_counter()
            tokenized_query = tokenizer.tokenize(QUERY)
            index = bm25_ranker.build_index(tokenized)
            bm25_results = bm25_ranker.search(index, tokenized_query, top_k=len(documents))
            ranker.rerank(bm25_results, documents, top_k=4)
            rank_times.append(time.perf_counter() - t0)

        print(
            f"  [{label}] tokenize_batch 중앙값={statistics.median(tokenize_times) * 1000:6.1f}ms, "
            f"bm25+rerank 중앙값={statistics.median(rank_times) * 1000:5.1f}ms, "
            f"합계={( statistics.median(tokenize_times) + statistics.median(rank_times) ) * 1000:6.1f}ms"
        )


def test_3_quality_vs_naive_order() -> None:
    """Test 3 -- 품질(Quality): does BM25+recency reranking actually surface
    different (and more query-relevant) results than just taking Naver's
    own top-4, and does the recency decay meaningfully reorder pure-BM25
    ranking? Reports both comparisons; "relevance" is approximated by
    counting query tokens present in each result's own title+description
    (a simple, inspectable proxy given there's no labeled ground truth)."""
    print("\n=== Test 3: 품질 (네이버 기본 순서 vs BM25+최신성 재정렬) ===")

    documents = fetcher.fetch_news_list(QUERY, display=30)
    texts = [f"{d['title']} {d['description']}" for d in documents]
    tokenized_documents = tokenizer.tokenize_batch(texts)
    tokenized_query = tokenizer.tokenize(QUERY)
    query_terms = set(tokenized_query)

    def relevance(doc_index: int) -> int:
        return len(query_terms & set(tokenized_documents[doc_index]))

    naive_top4 = list(range(min(4, len(documents))))

    index = bm25_ranker.build_index(tokenized_documents)
    bm25_only = bm25_ranker.search(index, tokenized_query, top_k=len(documents))
    bm25_top4 = [i for i, _ in bm25_only[:4]]

    reranked = ranker.rerank(bm25_only, documents, top_k=4, decay_lambda=0.01)
    reranked_urls = [d["url"] for d in reranked]
    reranked_indices = [next(i for i, d in enumerate(documents) if d["url"] == url) for url in reranked_urls]

    naive_relevance = statistics.mean(relevance(i) for i in naive_top4) if naive_top4 else 0.0
    bm25_relevance = statistics.mean(relevance(i) for i in bm25_top4) if bm25_top4 else 0.0
    reranked_relevance = statistics.mean(relevance(i) for i in reranked_indices) if reranked_indices else 0.0

    print(f"  질의 토큰: {sorted(query_terms)}")
    print(f"  네이버 기본 상위 4건 평균 질의어 일치 수: {naive_relevance:.2f}")
    print(f"  BM25만 적용한 상위 4건 평균 질의어 일치 수: {bm25_relevance:.2f}")
    print(f"  BM25+최신성 재정렬 상위 4건 평균 질의어 일치 수: {reranked_relevance:.2f}")
    print(f"  BM25 전용 순서와 최신성 반영 후 순서가 동일한가: {bm25_top4 == reranked_indices}")
    print("  (동일하지 않다면 recency decay가 실제로 순위를 바꿨다는 뜻)")


if __name__ == "__main__":
    _warm_up_kiwi()
    test_1_performance_scaling()
    test_2_speed_breakdown()
    test_3_quality_vs_naive_order()
