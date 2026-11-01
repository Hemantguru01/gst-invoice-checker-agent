"""LLM client abstraction. The application works without any LLM configured."""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from app.core.config import Settings
from app.core.exceptions import LLMError

logger = logging.getLogger(__name__)


class LLMClient(Protocol):
    model: str

    def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        """Return a JSON object matching ``schema`` or raise LLMError."""


class AnthropicLLMClient:
    """Claude via the official Anthropic SDK, using structured JSON output."""

    def __init__(self, api_key: str, model: str, timeout: float) -> None:
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=1)
        self.model = model

    def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        a = self._anthropic
        try:
            # Server-side fallback: if the primary model declines, the API retries on a
            # fallback model inside the same call.
            response = self._client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except a.AuthenticationError as exc:
            raise LLMError("LLM authentication failed (check ANTHROPIC_API_KEY).") from exc
        except a.RateLimitError as exc:
            raise LLMError("LLM rate limit reached.") from exc
        except a.APIStatusError as exc:
            raise LLMError(f"LLM API error (HTTP {exc.status_code}).") from exc
        except a.APIConnectionError as exc:
            raise LLMError("Could not reach the LLM API.") from exc

        if response.stop_reason == "refusal":
            raise LLMError("The LLM declined the request.")
        if response.stop_reason == "max_tokens":
            raise LLMError("The LLM response was truncated.")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            raise LLMError("The LLM returned no text.")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError("The LLM returned invalid JSON.") from exc
        if not isinstance(data, dict):
            raise LLMError("The LLM returned an unexpected JSON shape.")
        return data


def build_llm_client(settings: Settings) -> LLMClient | None:
    if not settings.llm_configured:
        return None
    try:
        return AnthropicLLMClient(
            settings.anthropic_api_key.get_secret_value(),  # type: ignore[union-attr]
            settings.llm_model,
            settings.llm_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM client could not be created: %s", type(exc).__name__)
        return None
