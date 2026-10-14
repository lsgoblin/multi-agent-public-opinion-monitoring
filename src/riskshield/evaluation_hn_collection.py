"""Exhaustive, minimal HN item collection for event-level evaluation evidence.

Only IDs, publication times and public story titles/URLs are retained. Search
hit counts are never accepted as coverage unless Algolia explicitly marks them
exhaustive and the returned hits reconcile with the count. Large or approximate
windows are split by time; an indivisible incomplete window fails closed.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

import httpx

ENDPOINT = "https://hn.algolia.com/api/v1/search_by_date"
HITS_PER_PAGE = 1000
MAX_REQUESTS = 128


class HNCoverageError(ValueError):
    """A requested interval cannot be proven complete."""


def http_fetch(params: dict[str, Any]) -> dict[str, Any]:
    """Fetch public search metadata and hits; the caller keeps only projections."""
    response = httpx.get(
        ENDPOINT, params=params, timeout=45,
        headers={"User-Agent": "RiskShieldDataEvaluation/0.2 event-evidence"},
        follow_redirects=False,
    )
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise HNCoverageError("search response is not an object")
    return value


def _project(hit: Any, kind: str, start: int, end: int) -> dict[str, Any]:
    if not isinstance(hit, dict) or not isinstance(hit.get("objectID"), str) or \
            type(hit.get("created_at_i")) is not int or \
            not start <= hit["created_at_i"] < end:
        raise HNCoverageError("hit identity or publication time is invalid")
    row = {"objectID": hit["objectID"], "created_at_i": hit["created_at_i"]}
    if kind == "story":
        if not isinstance(hit.get("title"), str):
            raise HNCoverageError("story has no public title")
        row.update(title=hit["title"], url=hit.get("url"))
    elif kind == "comment":
        if type(hit.get("story_id")) not in (int, str):
            raise HNCoverageError("comment has no story identity")
        row["story_id"] = str(hit["story_id"])
    else:
        raise ValueError(f"unsupported HN item kind {kind!r}")
    return row


def collect_hits(*, kind: str, start: int, end: int,
                 query: str = "", story_id: str | None = None,
                 fetch: Callable[[dict[str, Any]], dict[str, Any]] = http_fetch,
                 ) -> dict[str, Any]:
    """Collect all hits in a half-open UTC interval or refuse incomplete coverage.

    A nonempty query discovers story candidates. A story ID collects only that
    story's comments. Both fields are frozen by the caller's event plan; this
    function never expands a query or invents a new event identity.
    """
    if type(start) is not int or type(end) is not int or start >= end:
        raise ValueError("start/end must be increasing epoch seconds")
    if kind == "story" and (not query or story_id is not None):
        raise ValueError("story discovery requires a query and no story_id")
    if kind == "comment" and (query or not isinstance(story_id, str) or not story_id.isdigit()):
        raise ValueError("comment collection requires one numeric story_id")
    tags = "story" if kind == "story" else f"comment,story_{story_id}"
    requests: list[dict[str, Any]] = []

    def interval(left: int, right: int) -> list[dict[str, Any]]:
        if len(requests) >= MAX_REQUESTS:
            raise HNCoverageError("too many search partitions without complete coverage")
        params = {
            "query": query, "tags": tags,
            "numericFilters": f"created_at_i>={left},created_at_i<{right}",
            "hitsPerPage": HITS_PER_PAGE, "typoTolerance": "false", "page": 0,
        }
        response = fetch(params)
        if not isinstance(response, dict):
            raise HNCoverageError("search response is not an object")
        hits = response.get("hits")
        count = response.get("nbHits")
        pages = response.get("nbPages")
        exhaustive = response.get("exhaustiveNbHits") is True
        requests.append({
            "params": params, "nbHits": count, "nbPages": pages,
            "hits_returned": len(hits) if isinstance(hits, list) else None,
            "exhaustiveNbHits": exhaustive,
            "response_digest": hashlib.sha256(json.dumps(
                response, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8")).hexdigest(),
        })
        if type(count) is not int or count < 0 or type(pages) is not int or \
                pages < 0 or not isinstance(hits, list):
            raise HNCoverageError("search response lacks count, pages or hits")
        complete = exhaustive and count == len(hits) and pages <= 1
        if complete:
            projected = [_project(hit, kind, left, right) for hit in hits]
            if kind == "comment" and any(row["story_id"] != story_id for row in projected):
                raise HNCoverageError("comment belongs to another story")
            return projected
        if right - left <= 1:
            raise HNCoverageError("one-second interval remains non-exhaustive or truncated")
        middle = left + (right - left) // 2
        return interval(left, middle) + interval(middle, right)

    hits = interval(start, end)
    ids = [row["objectID"] for row in hits]
    if len(ids) != len(set(ids)):
        raise HNCoverageError("duplicate item ID across time partitions")
    hits.sort(key=lambda row: (row["created_at_i"], row["objectID"]))
    return {"schema": "riskshield.data_evaluation.hn_complete_hits.v1",
            "endpoint": ENDPOINT, "kind": kind, "query": query,
            "story_id": story_id, "start_epoch": start, "end_epoch": end,
            "coverage": "exhaustive", "count": len(hits),
            "hits": hits, "requests": requests}


def title_matches(story: dict[str, Any], *, all_phrases: list[str],
                  any_phrases: list[str]) -> bool:
    """Apply the same declared title rule before and after the cutoff."""
    if not all_phrases or not any_phrases or any(
            not isinstance(phrase, str) or not phrase.strip()
            for phrase in all_phrases + any_phrases):
        return False
    title = story["title"].casefold()
    return (all(phrase.casefold() in title for phrase in all_phrases)
            and any(phrase.casefold() in title for phrase in any_phrases))
