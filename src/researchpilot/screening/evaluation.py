"""Server-side recommendation thresholds and evidence calibration."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .contracts import ScreeningAIResult, ScreeningCandidateInput


@dataclass
class ComputedEvaluation:
    topic_match: int
    academic_value: int
    citation_potential: int
    reading_priority: int
    read_recommendation: str
    citation_recommendation: str
    confidence_raw: float
    confidence_adjusted: float
    confidence_label: str
    input_coverage: str
    evidence_status: str
    reasons: list[dict[str, Any]]
    result: dict[str, Any]


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().casefold()


def _evidence_match(
    quote: str,
    page_index: int | None,
    pages: dict[int, str],
) -> str:
    if not quote.strip():
        return "none"
    needle = _normalize_text(quote)
    if page_index is None:
        haystacks = list(pages.values())
    else:
        haystacks = [pages.get(page_index, "")]
    for haystack in haystacks:
        normalized = _normalize_text(haystack)
        if needle and needle in normalized:
            return "exact"
    # A short but recognisable excerpt may still be a partial match after PDF
    # line-break/hyphenation artifacts. Keep it clearly marked.
    head = needle[: min(80, len(needle))]
    if head and any(head in _normalize_text(item) for item in haystacks):
        return "partial"
    return "none"


def _confidence_label(value: float) -> str:
    return "high" if value >= 0.75 else "medium" if value >= 0.50 else "low"


def compute_evaluation(
    *,
    candidate_input: ScreeningCandidateInput,
    result: ScreeningAIResult,
    pages: dict[int, str] | None = None,
) -> ComputedEvaluation:
    pages = pages or {}
    reasons: list[dict[str, Any]] = []
    exact = 0
    partial = 0
    for reason in result.reasons:
        match_status = _evidence_match(reason.quote, reason.page_index, pages)
        if match_status == "exact":
            exact += 1
        elif match_status == "partial":
            partial += 1
        reasons.append(
            {
                "dimension": reason.dimension,
                "polarity": reason.polarity,
                "text": reason.text,
                "source_kind": reason.source_kind,
                "page_index": reason.page_index,
                "quote": reason.quote,
                "match_status": match_status,
            }
        )

    coverage = candidate_input.input_coverage
    coverage_cap = {"fulltext": 0.90, "abstract": 0.65, "metadata_only": 0.45}[coverage]
    evidence_reasons = [
        item
        for item in reasons
        if item["source_kind"] in {"fulltext", "abstract"} and item["quote"]
    ]
    if evidence_reasons and all(item["match_status"] == "exact" for item in evidence_reasons):
        evidence_cap = 0.90
        evidence_status = "verified"
    elif exact or partial:
        evidence_cap = 0.70
        evidence_status = "partial"
    else:
        evidence_cap = 0.45
        evidence_status = "none"
    adjusted = min(result.model_confidence, coverage_cap, evidence_cap)

    scores = result.scores
    topic = scores.topic_match.score
    academic = scores.academic_value.score
    citation = scores.citation_potential.score
    priority = scores.reading_priority.score
    if coverage == "metadata_only" and adjusted < 0.45:
        read_recommendation = "insufficient_evidence"
        citation_recommendation = "insufficient_evidence"
    else:
        if topic >= 75 and academic >= 60 and priority >= 75:
            read_recommendation = "must_read"
        elif topic >= 60 and priority >= 60:
            read_recommendation = "recommended"
        elif topic >= 45 or priority >= 45:
            read_recommendation = "optional"
        else:
            read_recommendation = "not_recommended"

        if citation >= 75 and academic >= 55:
            citation_recommendation = "strong_candidate"
        elif citation >= 55:
            citation_recommendation = "candidate"
        elif citation >= 40:
            citation_recommendation = "optional"
        else:
            citation_recommendation = "not_candidate"

    result_dict = result.model_dump(by_alias=True)
    return ComputedEvaluation(
        topic_match=topic,
        academic_value=academic,
        citation_potential=citation,
        reading_priority=priority,
        read_recommendation=read_recommendation,
        citation_recommendation=citation_recommendation,
        confidence_raw=result.model_confidence,
        confidence_adjusted=adjusted,
        confidence_label=_confidence_label(adjusted),
        input_coverage=coverage,
        evidence_status=evidence_status,
        reasons=reasons,
        result=result_dict,
    )
