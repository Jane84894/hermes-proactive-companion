"""Small, tool-free JSON client for the configured Xiaomi MiMo endpoint."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class ModelUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    request_id: str | None
    model: str


class MimoModel:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.xiaomimimo.com/v1",
        model: str = "mimo-v2.6-flash",
        timeout_seconds: float = 45.0,
        client: Any | None = None,
    ):
        if not api_key:
            raise ValueError("MiMo API key is required")
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("MiMo base_url must be an HTTPS URL")
        if not model.strip():
            raise ValueError("model must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._client = client or self._create_client(api_key, base_url, timeout_seconds)
        self.last_usage: ModelUsage | None = None

    @staticmethod
    def _create_client(api_key: str, base_url: str, timeout_seconds: float):
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError(
                "The OpenAI Python SDK is required for Xiaomi MiMo requests"
            ) from exc
        return OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,
        )

    def generate_json(
        self, system_prompt: str, user_prompt: str, *, max_tokens: int = 1200
    ) -> dict[str, Any]:
        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        self.last_usage = None
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            max_completion_tokens=max_tokens,
            timeout=self.timeout_seconds,
        )
        usage = getattr(response, "usage", None)
        if usage is None:
            self.last_usage = None
        else:
            prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            total_tokens = int(
                getattr(usage, "total_tokens", prompt_tokens + completion_tokens)
                or prompt_tokens + completion_tokens
            )
            self.last_usage = ModelUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                request_id=getattr(response, "id", None),
                model=str(getattr(response, "model", self.model)),
            )
        try:
            content = response.choices[0].message.content
            if not isinstance(content, str):
                raise TypeError
            parsed = json.loads(self._remove_json_fence(content))
            if not isinstance(parsed, dict):
                raise TypeError
            return parsed
        except (AttributeError, IndexError, TypeError, json.JSONDecodeError):
            raise ValueError("MiMo returned output that was not a valid JSON object") from None

    @staticmethod
    def _remove_json_fence(content: str) -> str:
        content = content.strip()
        if content.startswith("```") and content.endswith("```"):
            content = content[3:-3].strip()
            if content.startswith("json"):
                content = content[4:].lstrip()
        return content
