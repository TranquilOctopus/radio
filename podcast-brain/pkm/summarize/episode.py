from __future__ import annotations

import json

import anthropic

from pkm.config import BudgetConfig
from pkm.summarize.base import EpisodeContext, EpisodeSummary
from pkm.summarize.prompts import load_summary_prompt


class SummarizationError(Exception):
    """Raised when the Anthropic API returns an unrecoverable error."""


class EpisodeSummarizer:
    def __init__(
        self,
        config: BudgetConfig,
        client: anthropic.Anthropic | None = None,
        max_tokens: int = 4000,
        effort: str = "medium",
    ) -> None:
        self._config = config
        self._client = client or anthropic.Anthropic()
        self._max_tokens = max_tokens
        self._effort = effort

    def summarize(self, ctx: EpisodeContext) -> EpisodeSummary:
        system_prompt = load_summary_prompt(ctx.style)

        # Stable system content cached at the block level so the prefix is
        # reused across episodes of the same style.
        system = [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        # Per-episode user content is always volatile — goes after the cached prefix.
        user_payload: dict = {
            "podcast": ctx.podcast,
            "title": ctx.title,
            "published": ctx.published,
            "duration_s": ctx.duration_s,
            "style": ctx.style,
            "language": ctx.language,
            "chunk_extractions": ctx.chunk_extractions,
        }
        # Omit transcript_text from the payload when absent to keep messages lean.
        if ctx.transcript_text is not None:
            user_payload["transcript_text"] = ctx.transcript_text

        # Deterministic serialization: sorted keys to avoid cache invalidation
        # from varying dict insertion order.
        user_message = json.dumps(user_payload, ensure_ascii=False, sort_keys=True, indent=2)

        try:
            response = self._client.messages.create(
                model=self._config.summarize_model,
                max_tokens=self._max_tokens,
                output_config={"effort": self._effort},
                system=system,
                messages=[{"role": "user", "content": user_message}],
            )
        except anthropic.RateLimitError as exc:
            raise SummarizationError(f"Rate limited: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise SummarizationError(f"API error {exc.status_code}: {exc}") from exc

        usage = response.usage
        return EpisodeSummary(
            markdown=response.content[0].text,
            model_used=response.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        )
