"""Combines a document's BM25 relevance score with a recency-decay factor,
so a highly relevant but old article doesn't always outrank a slightly
less relevant but much fresher one.
"""

import math
from datetime import datetime, timezone

# Treat a missing/unparseable publish date as "very old" (~1 year) rather
# than crashing or defaulting to "brand new" -- under-ranking a dateless
# article is the safer failure mode than over-ranking one.
_UNKNOWN_DATE_HOURS = 24.0 * 365


def _hours_since(pub_date: datetime | None, now: datetime) -> float:
    if pub_date is None:
        return _UNKNOWN_DATE_HOURS
    if pub_date.tzinfo is None:
        pub_date = pub_date.replace(tzinfo=timezone.utc)
    return max((now - pub_date).total_seconds() / 3600.0, 0.0)


def rerank(
    bm25_results: list[tuple[int, float]],
    documents: list[dict],
    top_k: int = 4,
    decay_lambda: float = 0.01,
    now: datetime | None = None,
) -> list[dict]:
    """Combines each (doc_index, bm25_score) candidate with a recency decay
    of exp(-decay_lambda * hours_since_published), re-sorts by
    `final_score = bm25_score * recency_decay`, and returns the top_k
    original document dicts -- each augmented with "bm25_score",
    "recency_decay", and "final_score" for transparency/debugging.

    `decay_lambda` controls how fast old articles lose weight: at the
    default 0.01, a 24h-old article keeps ~79% of its BM25 score, a
    week-old one ~26%, a month-old one ~3% -- tune per how time-sensitive
    the use case is (0 disables recency weighting entirely, i.e. pure BM25
    ranking).

    `documents` must be the same list (by index) that was tokenized and
    passed to bm25_ranker.build_index() -- `bm25_results`' doc_index values
    refer to positions in it.
    """
    now = now or datetime.now(timezone.utc)

    scored = []
    for doc_index, bm25_score in bm25_results:
        doc = documents[doc_index]
        hours = _hours_since(doc.get("pub_date"), now)
        recency_decay = math.exp(-decay_lambda * hours)
        final_score = bm25_score * recency_decay
        scored.append((final_score, bm25_score, recency_decay, doc))

    scored.sort(key=lambda entry: entry[0], reverse=True)

    results = []
    for final_score, bm25_score, recency_decay, doc in scored[:top_k]:
        enriched = dict(doc)
        enriched["bm25_score"] = bm25_score
        enriched["recency_decay"] = recency_decay
        enriched["final_score"] = final_score
        results.append(enriched)
    return results
