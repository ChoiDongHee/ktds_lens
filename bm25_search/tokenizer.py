"""Korean tokenization for BM25 indexing, backed by kiwipiepy.

Loading the Kiwi morphological analysis model is the expensive part of
using it (not each individual tokenize call), so a single Kiwi instance is
constructed once at module import time and reused for every call in this
process, rather than constructing one per request.
"""

import re
from html import unescape

from kiwipiepy import Kiwi

_TAG_RE = re.compile(r"<[^>]+>")

# Morpheme tags kept for BM25 indexing: nouns/pronouns/numerals and verb-
# or-adjective stems carry the actual topical meaning of a sentence; particles
# (J*), endings (E*), and punctuation (S*) don't distinguish one document's
# topic from another's and would just add noise to the term-overlap model
# BM25 relies on.
_KEEP_TAGS = frozenset(
    {
        "NNG", "NNP", "NNB", "NP", "NR",  # nouns / pronouns / numerals
        "VV", "VA", "VX", "VCP", "VCN",  # verb/adjective stems
        "SL", "SH", "SN",  # foreign word, hanja, number
        "XR",  # bound root (e.g. "깨끗" in "깨끗하다")
    }
)

# One Kiwi instance for the whole process -- see module docstring.
_kiwi = Kiwi()


def _strip_html(text: str) -> str:
    return unescape(_TAG_RE.sub(" ", text))


def tokenize(text: str) -> list[str]:
    """Strips HTML tags/entities from `text`, then returns the surface
    forms of its semantically meaningful morphemes (see _KEEP_TAGS) in
    order -- e.g. "삼성전자의 주가가 급등했다" -> ["삼성전자", "주가", "급등", "하"]."""
    cleaned = _strip_html(text)
    if not cleaned.strip():
        return []
    tokens = _kiwi.tokenize(cleaned)
    return [token.form for token in tokens if token.tag in _KEEP_TAGS]


def tokenize_batch(texts: list[str]) -> list[list[str]]:
    """Tokenizes many texts in one call. Kiwi's own `tokenize()` accepts an
    iterable of strings and analyzes them as a batch (parallelized
    internally across the `num_workers` the Kiwi() instance was
    constructed with), which is faster than calling tokenize() once per
    text in a Python for-loop. Preserves input order and length: an empty/
    whitespace-only input produces an empty token list at that position
    rather than being dropped."""
    if not texts:
        return []

    cleaned = [_strip_html(text) for text in texts]
    non_empty_indices = [i for i, text in enumerate(cleaned) if text.strip()]

    results: list[list[str]] = [[] for _ in texts]
    if not non_empty_indices:
        return results

    batch = _kiwi.tokenize([cleaned[i] for i in non_empty_indices])
    for index, tokens in zip(non_empty_indices, batch):
        results[index] = [token.form for token in tokens if token.tag in _KEEP_TAGS]
    return results
