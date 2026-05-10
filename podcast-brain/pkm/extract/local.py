from __future__ import annotations

import json

import httpx
from pydantic import BaseModel, ValidationError

from pkm.config import ExtractConfig
from pkm.extract.base import ExtractionError
from pkm.extract.chunker import Chunk
from pkm.extract.prompts import load_system_prompt
from pkm.extract.schemas import SCHEMA_BY_STYLE


class LocalExtractor:
    def __init__(self, config: ExtractConfig, client: httpx.Client | None = None) -> None:
        self._config = config
        # Caller may inject a mock client for tests; otherwise build a real one.
        # timeout=120 because large local models can take 30s+ per chunk.
        self._client = client or httpx.Client(timeout=120.0)

    def name(self) -> str:
        return f"local:{self._config.local_model}"

    def extract_chunk(
        self, chunk: Chunk, *, style: str, language: str | None = None
    ) -> BaseModel:
        if self._config.json_mode == "grammar":
            # TODO: wire llama.cpp GBNF backend here once needed.
            raise NotImplementedError("GBNF grammar backend is pending; use json_schema mode")

        model_cls = SCHEMA_BY_STYLE[style]
        schema = model_cls.model_json_schema()

        system_prompt = load_system_prompt(style)
        if language is not None:
            system_prompt = f"{system_prompt}\n\nThe transcript is in language: {language}."

        user_prompt = (
            f"Transcript chunk (t={chunk.start:.0f}s–{chunk.end:.0f}s):\n\n{chunk.text}"
        )

        payload = {
            "model": self._config.local_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "format": schema,
            "stream": False,
            "options": {"temperature": 0.2, "num_ctx": 8192},
        }

        return self._call_with_retry(payload, model_cls)

    def _call_with_retry(
        self, payload: dict, model_cls: type[BaseModel]
    ) -> BaseModel:
        endpoint = f"{self._config.local_endpoint}/api/chat"
        last_content: str = ""

        for attempt in range(2):
            response = self._client.post(endpoint, json=payload)
            response.raise_for_status()
            data = response.json()
            last_content = data["message"]["content"]
            try:
                return model_cls.model_validate_json(last_content)
            except (ValidationError, json.JSONDecodeError):
                if attempt == 0:
                    # Retry once — ollama occasionally emits a malformed JSON on first try.
                    continue
                raise ExtractionError(
                    f"Extraction failed after retry; model output snippet: {last_content[:200]!r}"
                )

        # unreachable, but satisfies type checkers
        raise ExtractionError("Extraction loop exited unexpectedly")
