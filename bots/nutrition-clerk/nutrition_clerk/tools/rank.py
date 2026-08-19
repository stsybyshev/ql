"""Deterministic candidate ranking via rapidfuzz.

Given the query the user typed and a list of candidate food names (the LLM
takes these from lookup_food's response), returns fuzzy scores and a boolean
should_clarify flag. Keeps the threshold policy out of the prompt so it's
easy to tune without a prompt rewrite.
"""
from __future__ import annotations

from google.adk.tools import FunctionTool
from rapidfuzz import fuzz

MIN_TOP_SCORE = 90
MIN_MARGIN = 15


def build_rank_matches_tool() -> FunctionTool:
    def rank_matches(query: str, candidates: list[str]) -> dict:
        """Score candidate food names against a user query.

        Args:
            query: The raw food term the user typed (e.g. "chia").
            candidates: Food names from lookup_food's response.

        Returns:
            dict with:
              - scores: [{"name": ..., "score": int 0..100}] sorted best-first
              - should_clarify: True when the top match is not confident enough
                (top score < 90 OR the gap to the runner-up is < 15).
        """
        if not candidates:
            return {"scores": [], "should_clarify": False}
        scored = sorted(
            (
                {"name": c, "score": int(fuzz.WRatio(query, c))}
                for c in candidates
            ),
            key=lambda x: x["score"],
            reverse=True,
        )
        should_clarify = False
        if len(scored) > 1:
            top, second = scored[0], scored[1]
            if top["score"] < MIN_TOP_SCORE or (top["score"] - second["score"]) < MIN_MARGIN:
                should_clarify = True
        return {"scores": scored, "should_clarify": should_clarify}

    return FunctionTool(func=rank_matches)
