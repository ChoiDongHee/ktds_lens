"""Orchestrates fetcher -> tokenizer -> bm25_ranker -> ranker into a single
search(query) call that returns the top-K most relevant *and* recent Naver
News articles for a query.

Performance note: the ~100ms design target for this pipeline covers
tokenizing + indexing + scoring a candidate set of 30-100 already-fetched
documents (the tokenizer/bm25_ranker/ranker stages) -- not the network
fetch itself, which is inherently slower (real HTTP calls) and has its own
concurrency (see fetcher.fetch_contents' thread pool).

Measured (30 candidates, warmed-up Kiwi instance -- the first batch
tokenize call in a process pays a one-time ~1.5s cost spinning up Kiwi's
internal worker pool, then stays fast for the life of the process, which is
why this matters for a long-running server but not a one-shot CLI run):
  - fetch_full_content=False (rank against title+description only): ~12ms
    for tokenize+bm25+rerank combined -- comfortably inside the 100ms goal.
  - fetch_full_content=True (rank against title+full article text):
    ~170ms for the same stage, since there's simply much more text per
    document to tokenize. Still fast in absolute terms, and network fetch
    (2-4s for 30 URLs even with the thread pool) dominates total latency
    in this mode regardless -- use fetch_full_content=False when the
    100ms figure specifically needs to hold end to end.
"""

import logging
import time
from datetime import datetime

from . import bm25_ranker, fetcher, ranker, tokenizer

logger = logging.getLogger("bm25_search")


def search(
    query: str,
    candidate_count: int = 30,
    top_k: int = 4,
    decay_lambda: float = 0.01,
    fetch_full_content: bool = True,
    now: datetime | None = None,
) -> list[dict]:
    """Fetches `candidate_count` Naver News candidates for `query` (each
    article's full text is fetched concurrently unless fetch_full_content
    is False), tokenizes title+content (falling back to title+description
    when a page's content couldn't be fetched), builds a BM25 index, scores
    the query against every candidate, and reranks by BM25 score combined
    with recency decay -- returning the `top_k` best documents.

    Each returned dict is the original fetcher document (title, url,
    description, pub_date, content) plus bm25_score/recency_decay/
    final_score from ranker.rerank().
    """
    request_started = time.perf_counter()
    documents = fetcher.fetch(query, display=candidate_count, fetch_full_content=fetch_full_content)
    fetch_elapsed = time.perf_counter() - request_started

    if not documents:
        logger.info("[bm25_search] '%s' 후보 없음 (fetch %.3fs)", query, fetch_elapsed)
        return []

    rank_started = time.perf_counter()
    texts = [f"{doc['title']} {doc.get('content') or doc['description']}" for doc in documents]
    tokenized_documents = tokenizer.tokenize_batch(texts)
    tokenized_query = tokenizer.tokenize(query)

    index = bm25_ranker.build_index(tokenized_documents)
    bm25_results = bm25_ranker.search(index, tokenized_query, top_k=len(documents))
    reranked = ranker.rerank(bm25_results, documents, top_k=top_k, decay_lambda=decay_lambda, now=now)
    rank_elapsed = time.perf_counter() - rank_started

    logger.info(
        "[bm25_search] '%s' 후보 %d건 (fetch %.3fs) -> tokenize+bm25+rerank %.3fs -> 상위 %d건 반환",
        query, len(documents), fetch_elapsed, rank_elapsed, len(reranked),
    )
    return reranked


if __name__ == "__main__":
    # Run via: .venv/Scripts/python.exe -m bm25_search.pipeline "<query>"
    # (relative imports above require running as a package, not as a bare script)
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    sys.stdout.reconfigure(encoding="utf-8")

    query_arg = sys.argv[1] if len(sys.argv) > 1 else "삼성전자 주가"
    for result in search(query_arg):
        print(f"{result['final_score']:.4f} (bm25={result['bm25_score']:.4f}, decay={result['recency_decay']:.4f}) {result['title']}")
        print(f"  {result['url']}")
