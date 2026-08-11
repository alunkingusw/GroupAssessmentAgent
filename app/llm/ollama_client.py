"""Thin HTTP wrapper over Ollama's API. Nothing above this module knows or cares that Ollama
specifically is being used - it only sees EmailCommandParser.parse_email()."""
from __future__ import annotations

import httpx


class OllamaUnavailable(Exception):
    """Raised when Ollama can't be reached or returns an error. Not a parsing failure - the
    caller should leave the email unprocessed for a later retry (spec S20), not fail it."""


class OllamaClient:
    def __init__(self, host: str, model: str, timeout: float = 60.0):
        self._model = model
        self._client = httpx.Client(base_url=host.rstrip("/"), timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def is_available(self) -> bool:
        try:
            resp = self._client.get("/api/tags")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    def generate(self, system_prompt: str, user_prompt: str, temperature: float = 0.0) -> str:
        try:
            resp = self._client.post(
                "/api/generate",
                json={
                    "model": self._model,
                    "system": system_prompt,
                    "prompt": user_prompt,
                    "format": "json",
                    "stream": False,
                    "options": {"temperature": temperature},
                },
            )
        except httpx.HTTPError as e:
            raise OllamaUnavailable(f"Could not reach Ollama: {e}") from e

        if resp.status_code != 200:
            raise OllamaUnavailable(f"Ollama returned {resp.status_code}: {resp.text[:200]}")

        return resp.json().get("response", "")
