from __future__ import annotations

from nutrition_clerk.tools.rank import MIN_MARGIN, MIN_TOP_SCORE, build_rank_matches_tool


def _rank(query, candidates):
    tool = build_rank_matches_tool()
    return tool.func(query=query, candidates=candidates)


def test_empty_candidates():
    result = _rank("chia", [])
    assert result == {"scores": [], "should_clarify": False}


def test_single_candidate_never_clarifies():
    result = _rank("apple", ["apple"])
    assert not result["should_clarify"]
    assert result["scores"][0]["name"] == "apple"
    assert result["scores"][0]["score"] == 100


def test_two_similar_candidates_trigger_clarify():
    result = _rank("chia", ["chia pudding", "chia seeds"])
    # Both are equal partial matches to "chia" -> tight margin -> clarify.
    assert result["should_clarify"], result
    assert {s["name"] for s in result["scores"]} == {"chia pudding", "chia seeds"}


def test_clear_winner_does_not_clarify():
    result = _rank("apple", ["apple", "banana"])
    top = result["scores"][0]
    assert top["name"] == "apple"
    # apple<->apple = 100, apple<->banana ~18 -> huge margin -> no clarify.
    assert not result["should_clarify"]


def test_substring_relationship_correctly_clarifies():
    """apple vs pineapple: rapidfuzz's WRatio caps partial matches at 90, so
    substring hits look almost as good as exact hits. Real usage benefits
    from clarify here — 'apple' plausibly meant apple, but the fuzzy scorer
    can't distinguish intent."""
    result = _rank("apple", ["apple", "pineapple"])
    assert result["should_clarify"], result


def test_top_score_below_threshold_clarifies_even_with_gap():
    """A wide margin doesn't save us if the top match is itself weak."""
    # "xyz123" vs anything below MIN_TOP_SCORE (90); rank still returns
    # something but should_clarify triggers.
    result = _rank("xyz123", ["completely_unrelated_food", "another_thing"])
    assert result["scores"][0]["score"] < MIN_TOP_SCORE
    assert result["should_clarify"]


def test_scores_sorted_descending():
    result = _rank("apple", ["pineapple", "apple", "apricot"])
    scores = [s["score"] for s in result["scores"]]
    assert scores == sorted(scores, reverse=True)


def test_thresholds_are_module_constants():
    """Guard that policy stays in one place (rank.py), not smeared across code."""
    assert MIN_TOP_SCORE == 90
    assert MIN_MARGIN == 15
