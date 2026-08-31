"""Native crawl engine (replaces the Crawl4AI REST path; see docs/native-crawler-design.md)."""
from __future__ import annotations

from .engine import crawl
from .policy import Policy
from .results import CrawlResult, Escalate

__all__ = ["CrawlResult", "Escalate", "Policy", "crawl"]
