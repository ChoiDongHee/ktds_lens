"""Thin wrapper around rank_bm25.BM25Okapi exposing the two operations this
pipeline needs: build an index from tokenized documents, and score a
tokenized query against it.
"""

from dataclasses import dataclass

from rank_bm25 import BM25Okapi


@dataclass
class BM25Index:
    """Bundles the fitted BM25Okapi model with the corpus size it was built
    from, so search() doesn't need the document count passed separately
    (and can't accidentally be called with a mismatched one)."""

    model: BM25Okapi
    size: int


def build_index(documents: list[list[str]]) -> BM25Index:
    """Builds a BM25Okapi index from already-tokenized documents (see
    tokenizer.tokenize_batch). Each element of `documents` is one
    document's token list; a document with an empty token list is a valid,
    indexable "empty" document (BM25Okapi handles zero-length documents
    fine -- it will just always score 0 against any query).

    Raises ValueError on an empty corpus: BM25Okapi's own IDF computation
    divides by document count internally and breaks on zero documents, so
    this is checked explicitly here instead of surfacing as an obscure
    ZeroDivisionError from inside the library."""
    if not documents:
        raise ValueError("build_index() requires at least one document")
    return BM25Index(model=BM25Okapi(documents), size=len(documents))


def search(index: BM25Index, tokenized_query: list[str], top_k: int) -> list[tuple[int, float]]:
    """Scores every document in `index` against `tokenized_query` and
    returns the top_k (doc_index, score) pairs sorted by score descending.
    `doc_index` is the position in the `documents` list originally passed
    to build_index(), so the caller can map a result back to its source
    document. An empty `tokenized_query` scores every document 0 (BM25's
    score is a sum over query terms, so no terms means no score) --
    top_k results are still returned in that case, just all zero."""
    scores = index.model.get_scores(tokenized_query)
    ranked_indices = sorted(range(index.size), key=lambda i: scores[i], reverse=True)
    return [(i, float(scores[i])) for i in ranked_indices[:top_k]]
