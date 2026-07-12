"""Naver News fetch -> tokenize -> BM25 index -> recency-aware rerank pipeline.

Standalone from server.py's MCP engines on purpose -- this is a separate
concern (local BM25 similarity ranking over a fetched candidate set) rather
than another web_search backend. See pipeline.search() for the main entry
point.
"""
