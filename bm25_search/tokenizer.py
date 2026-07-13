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

# Matches a URL starting at "http(s)://" or "www.", using only characters
# that are actually legal in a URL -- deliberately NOT `\S+`, which would
# also swallow a directly-attached Korean particle (common in Korean text
# with no space before it, e.g. "...unpacked에서 확인" -- `\S+` would eat
# "에서" into the "URL" too). Stopping at the first non-URL character keeps
# the surrounding Korean text intact for Kiwi to analyze normally.
_URL_RE = re.compile(r"(?:https?://|www\.)[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+", re.IGNORECASE)
_URL_SPLIT_RE = re.compile(r"[^0-9a-zA-Z가-힣]+")
# Scheme/domain-suffix noise that's the same across virtually every URL and
# so carries no topical signal for BM25 (unlike "samsung", "sec", "ai-subs").
_URL_NOISE_TOKENS = frozenset({"http", "https", "www", "com", "net", "org", "co", "kr", "html", "htm", "php"})

# One Kiwi instance for the whole process -- see module docstring.
_kiwi = Kiwi()


def _strip_html(text: str) -> str:
    return unescape(_TAG_RE.sub(" ", text))


def _url_tokens(url: str) -> list[str]:
    """Breaks a URL into its meaningful domain/path fragments, e.g.
    "https://www.samsung.com/sec/ai-subs/" -> ["samsung", "sec", "ai",
    "subs"]. Kiwi tags an entire URL as a single non-content token (outside
    _KEEP_TAGS) rather than analyzing it morphemically, so without this a
    query or document that's just a URL tokenizes to [] -- which, for a
    query, silently disables BM25 relevance filtering entirely (see
    server.py's _filter_by_relevance: "if not tokenized_query: return")."""
    pieces = (p.lower() for p in _URL_SPLIT_RE.split(url) if p)
    return [p for p in pieces if len(p) >= 2 and p not in _URL_NOISE_TOKENS and not p.isdigit()]


def _extract_and_strip_urls(text: str) -> tuple[str, list[str]]:
    """Pulls every URL out of `text`, returning the URL-free remainder (so
    Kiwi's morphological analyzer sees clean surrounding text instead of
    choking on/fragmenting a URL embedded in it) plus the combined tokens
    extracted from all URLs found via _url_tokens."""
    urls = _URL_RE.findall(text)
    if not urls:
        return text, []
    remainder = _URL_RE.sub(" ", text)
    url_tokens = [token for url in urls for token in _url_tokens(url)]
    return remainder, url_tokens


def tokenize(text: str) -> list[str]:
    """Strips HTML tags/entities from `text`, pulls out any URLs (see
    _extract_and_strip_urls) and returns the surface forms of the remaining
    text's semantically meaningful morphemes (see _KEEP_TAGS) followed by
    tokens extracted from any URLs -- e.g. "삼성전자의 주가가 급등했다" ->
    ["삼성전자", "주가", "급등", "하"], or a bare "https://www.samsung.com/sec/"
    -> ["samsung", "sec"] rather than [].

    Lowercased uniformly (a no-op on Hangul, harmless on Korean/numeric
    forms) so foreign-word (SL) tokens compare equal regardless of case --
    Kiwi preserves a token's original case (e.g. "Samsung" vs "samsung"),
    and BM25 does exact string matching, so without this a query's URL-
    derived tokens (already lowercased by _url_tokens) would never match
    the same word's differently-cased form in a document."""
    cleaned = _strip_html(text)
    remainder, url_tokens = _extract_and_strip_urls(cleaned)
    if not remainder.strip():
        return url_tokens
    tokens = _kiwi.tokenize(remainder)
    return [token.form.lower() for token in tokens if token.tag in _KEEP_TAGS] + url_tokens


def tokenize_batch(texts: list[str]) -> list[list[str]]:
    """Tokenizes many texts in one call. Kiwi's own `tokenize()` accepts an
    iterable of strings and analyzes them as a batch (parallelized
    internally across the `num_workers` the Kiwi() instance was
    constructed with), which is faster than calling tokenize() once per
    text in a Python for-loop. Preserves input order and length: an empty/
    whitespace-only (after URL extraction) input produces just its URL
    tokens (or [] if it had none) at that position rather than being
    dropped."""
    if not texts:
        return []

    cleaned = [_strip_html(text) for text in texts]
    extracted = [_extract_and_strip_urls(text) for text in cleaned]
    remainders = [remainder for remainder, _ in extracted]
    url_token_lists = [url_tokens for _, url_tokens in extracted]

    non_empty_indices = [i for i, text in enumerate(remainders) if text.strip()]

    results: list[list[str]] = [list(url_tokens) for url_tokens in url_token_lists]
    if not non_empty_indices:
        return results

    batch = _kiwi.tokenize([remainders[i] for i in non_empty_indices])
    for index, tokens in zip(non_empty_indices, batch):
        # Lowercased uniformly -- see tokenize()'s docstring for why (Kiwi
        # preserves original case on foreign-word tokens, BM25 matches
        # tokens by exact string equality).
        results[index] = [token.form.lower() for token in tokens if token.tag in _KEEP_TAGS] + url_token_lists[index]
    return results
