from __future__ import annotations

import asyncio
from typing import Any

from ..config import settings


class PromptGuardClassifier:
    """Lazy wrapper around Meta's Prompt Guard 2 sequence classifier."""

    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = model_name or settings.prompt_guard_model
        self._pipeline: Any = None
        self._load_lock = asyncio.Lock()

    async def classify(self, text: str) -> dict:
        pipeline = await self._get_pipeline()
        result = await asyncio.to_thread(pipeline, text, top_k=None)
        return self._normalize(result)

    async def _get_pipeline(self) -> Any:
        if self._pipeline is not None:
            return self._pipeline
        async with self._load_lock:
            if self._pipeline is None:
                from transformers import pipeline

                self._pipeline = await asyncio.to_thread(
                    pipeline,
                    "text-classification",
                    model=self.model_name,
                )
        return self._pipeline

    def _normalize(self, raw_result: Any) -> dict:
        values = raw_result[0] if raw_result and isinstance(raw_result[0], list) else raw_result
        scores = {
            str(item["label"]).lower(): float(item["score"])
            for item in values
            if "label" in item and "score" in item
        }
        label = max(scores, key=scores.get) if scores else "unknown"
        is_safe = label in {"benign", "label_0", "safe"}
        return {
            "model": self.model_name,
            "label": label,
            "is_safe": is_safe,
            "score": round(scores.get(label, 0.0), 6),
            "scores": scores,
        }


prompt_guard = PromptGuardClassifier()