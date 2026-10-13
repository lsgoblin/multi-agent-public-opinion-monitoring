"""Coverage tests for event-level public HN collection."""

from __future__ import annotations

import pytest

from riskshield.evaluation_hn_collection import HNCoverageError, collect_hits, title_matches


def _story(item_id, second, title="State Farm cuts 72,000 home policies"):
    return {"objectID": item_id, "created_at_i": second, "title": title,
            "url": "https://example.org/report", "author": "discard-me"}


def _response(hits, *, exhaustive=True, count=None, pages=1):
    return {"hits": hits, "nbHits": len(hits) if count is None else count,
            "nbPages": pages, "exhaustiveNbHits": exhaustive}


def test_complete_story_search_keeps_only_public_title_and_identity():
    result = collect_hits(kind="story", start=100, end=200, query="State Farm",
                          fetch=lambda params: _response([_story("1", 110)]))
    assert result["coverage"] == "exhaustive"
    assert result["count"] == 1
    assert result["hits"][0] == {
        "objectID": "1", "created_at_i": 110,
        "title": "State Farm cuts 72,000 home policies",
        "url": "https://example.org/report",
    }
    assert result["requests"][0]["params"]["typoTolerance"] == "false"


def test_nonexhaustive_count_must_not_be_taken_as_complete_zero():
    def fetch(params):
        left = int(params["numericFilters"].split(",")[0].split(">=")[1])
        right = int(params["numericFilters"].split(",")[1].split("<")[1])
        if (left, right) == (100, 104):
            return _response([], exhaustive=False, count=0, pages=0)
        return _response([_story("1", 101)] if (left, right) == (100, 102) else [],
                         pages=1 if (left, right) == (100, 102) else 0)
    result = collect_hits(kind="story", start=100, end=104, query="State Farm", fetch=fetch)
    assert result["count"] == 1
    assert len(result["requests"]) == 3


def test_truncated_result_splits_and_reconciles():
    def fetch(params):
        left = int(params["numericFilters"].split(",")[0].split(">=")[1])
        right = int(params["numericFilters"].split(",")[1].split("<")[1])
        if (left, right) == (100, 104):
            return _response([_story("1", 101)], count=2, pages=2)
        return _response([_story("1", 101)] if left == 100 else [_story("2", 103)])
    result = collect_hits(kind="story", start=100, end=104, query="State Farm", fetch=fetch)
    assert [row["objectID"] for row in result["hits"]] == ["1", "2"]


def test_indivisible_nonexhaustive_interval_is_refused():
    with pytest.raises(HNCoverageError, match="one-second"):
        collect_hits(kind="story", start=100, end=101, query="insurance",
                     fetch=lambda params: _response([], exhaustive=False, pages=0))


def test_comment_must_belong_to_declared_story_and_no_body_is_retained():
    good = {"objectID": "9", "created_at_i": 101, "story_id": 7,
            "comment_text": "discard-me", "author": "discard-me"}
    result = collect_hits(kind="comment", start=100, end=102, story_id="7",
                          fetch=lambda params: _response([good]))
    assert result["hits"] == [{"objectID": "9", "created_at_i": 101, "story_id": "7"}]
    bad = good | {"story_id": 8}
    with pytest.raises(HNCoverageError, match="another story"):
        collect_hits(kind="comment", start=100, end=102, story_id="7",
                     fetch=lambda params: _response([bad]))


def test_late_item_is_refused_even_if_search_claims_completeness():
    with pytest.raises(HNCoverageError, match="publication time"):
        collect_hits(kind="story", start=100, end=102, query="State Farm",
                     fetch=lambda params: _response([_story("1", 102)]))


def test_declared_title_rule_is_reused():
    story = _story("1", 101)
    assert title_matches(story, all_phrases=["State Farm"],
                         any_phrases=["72,000", "home policies"])
    assert not title_matches(story, all_phrases=["Liberty Mutual"],
                             any_phrases=["home policies"])
    assert not title_matches(story, all_phrases=[""], any_phrases=["home policies"])
