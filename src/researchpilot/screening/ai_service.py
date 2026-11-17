"""Single-call screening AI with one bounded JSON correction retry."""

from __future__ import annotations

import time
from typing import Any

from pydantic import ValidationError

from ..agent import providers as pv
from ..agent.client import LLMClient
from ..agent.secrets import redact
from .contracts import ScreeningAIResponse, ScreeningAIResult, ScreeningCandidateInput
from .errors import AIError
from .prompts import (
    CORRECTION_PROMPT,
    PROMPT_VERSION,
    SCREENING_JSON_SCHEMA,
    SYSTEM_PROMPT,
    build_user_prompt,
)

SCREENING_TIMEOUT_SECONDS = 60.0
SCREENING_MODEL = pv.DEEPSEEK_DEFAULT_MODEL


class CandidateScreeningAI:
    def __init__(self, client: LLMClient | None = None) -> None:
        self.client = client or LLMClient(name="candidate-screening")

    def screen(
        self,
        candidate_input: ScreeningCandidateInput,
        *,
        actor_id: str = "",
    ) -> ScreeningAIResponse:
        del actor_id
        started = time.monotonic()
        try:
            credentials = self.client.store.require()
        except pv.LLMUnavailable as exc:
            raise AIError(str(exc), code="AI_NOT_CONNECTED") from exc

        base_user = build_user_prompt(candidate_input)
        validation_error = ""
        parsed: ScreeningAIResult | None = None
        response = None
        raw: dict[str, Any] = {}
        json_error = ""
        for attempt in range(2):
            user = base_user
            if attempt:
                user = (
                    base_user
                    + "\n\n"
                    + CORRECTION_PROMPT.format(
                        validation_error=(validation_error or json_error or "unknown")[:1000]
                    )
                )
            try:
                raw, response = self.client.complete_json(
                    SYSTEM_PROMPT,
                    user,
                    json_schema=SCREENING_JSON_SCHEMA,
                    max_tokens=2200,
                    temperature=0.0,
                    timeout=SCREENING_TIMEOUT_SECONDS,
                    thinking="disabled",
                    model=SCREENING_MODEL,
                    retries=0,
                )
            except pv.LLMBadResponseError as exc:
                json_error = redact(str(exc), (credentials.api_key,))
                if attempt == 0:
                    continue
                raise self._map_error(exc, credentials.api_key, json_error) from exc
            except pv.LLMError as exc:
                message = redact(str(exc), (credentials.api_key,))
                raise self._map_error(exc, credentials.api_key, message) from exc

            try:
                parsed = ScreeningAIResult.model_validate(self._normalize_raw(raw))
                break
            except ValidationError as exc:
                validation_error = redact(str(exc), (credentials.api_key,))
                if attempt == 0:
                    continue
                raise AIError(
                    "模型两次输出均不符合 screening.v1 Schema",
                    code="AI_SCHEMA_INVALID",
                ) from exc
        if parsed is None or response is None:
            raise AIError("模型没有返回可用评估", code="AI_EMPTY_RESPONSE")
        elapsed_ms = round((time.monotonic() - started) * 1000)
        return ScreeningAIResponse(
            result=parsed,
            model=response.model or SCREENING_MODEL,
            provider=credentials.provider,
            prompt_version=PROMPT_VERSION,
            elapsed_ms=elapsed_ms,
            usage=dict(response.usage or {}),
            raw_response=raw,
        )

    @staticmethod
    def _normalize_raw(raw: dict[str, Any]) -> dict[str, Any]:
        """Accept the compact JSON shape DeepSeek emits without response_format."""

        data = dict(raw)
        data.setdefault("schema_version", "screening.v1")
        scores = data.get("scores")
        if not isinstance(scores, dict):
            scores = {}
            for dimension, key in (
                ("topic_match", "topic_match"),
                ("academic_value", "academic_value"),
                ("citation_potential", "citation_potential"),
                ("reading_priority", "reading_priority"),
            ):
                value = data.get(key)
                if isinstance(value, dict):
                    scores[dimension] = value
                elif value is not None:
                    scores[dimension] = {
                        "score": int(value),
                        "rationale": "",
                    }
        data["scores"] = scores
        reasons = data.get("reasons")
        if isinstance(reasons, list) and reasons and isinstance(reasons[0], str):
            dimensions = (
                "topic_match",
                "academic_value",
                "citation_potential",
                "reading_priority",
            )
            data["reasons"] = [
                {
                    "dimension": dimensions[index % len(dimensions)],
                    "polarity": "neutral",
                    "text": text,
                    "source_kind": "fulltext"
                    if "fulltext" in text.lower() or "pageIndex" in text
                    else "metadata",
                    "page_index": None,
                    "quote": "",
                }
                for index, text in enumerate(reasons[:6])
            ]
        data.setdefault(
            "conclusion_summary",
            "基于当前可用元数据与正文形成保守评估。",
        )
        if "model_confidence" not in data:
            data["model_confidence"] = data.get("confidence", 0.5)
        data.setdefault(
            "confidence_rationale",
            "输入覆盖范围与证据完整性共同决定置信度。",
        )
        return data

    @staticmethod
    def _map_error(exc: pv.LLMError, api_key: str, message: str) -> AIError:
        if isinstance(exc, pv.LLMTimeoutError):
            code = "AI_TIMEOUT"
        elif isinstance(exc, pv.LLMRateLimitError):
            code = "AI_RATE_LIMIT"
        elif isinstance(exc, pv.LLMAuthError):
            code = "AI_AUTH_FAILED"
        elif isinstance(exc, pv.LLMUnavailable):
            code = "AI_NOT_CONNECTED"
        elif isinstance(exc, pv.LLMBadResponseError):
            lower = message.lower()
            code = (
                "AI_EMPTY_RESPONSE"
                if "empty" in lower or "空内容" in message
                else "AI_BAD_JSON"
            )
        else:
            code = "INTERNAL_ERROR"
        return AIError(redact(message, (api_key,))[:2000], code=code)
