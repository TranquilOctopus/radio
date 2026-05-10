from __future__ import annotations

import json
from unittest.mock import MagicMock, call

import anthropic
import httpx
import pytest

from pkm.config import BudgetConfig
from pkm.summarize.base import EpisodeContext, EpisodeSummary
from pkm.summarize.episode import EpisodeSummarizer, SummarizationError
from pkm.summarize.prompts import load_summary_prompt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_client(
    text: str = "## TL;DR\nGreat episode.",
    cache_read: int = 0,
    cache_create: int = 800,
) -> MagicMock:
    client = MagicMock(spec=anthropic.Anthropic)
    fake_response = MagicMock()
    fake_response.content = [MagicMock(type="text", text=text)]
    fake_response.model = "claude-sonnet-4-6"
    fake_response.usage = MagicMock(
        input_tokens=1000,
        output_tokens=500,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_create,
    )
    client.messages.create.return_value = fake_response
    return client


def _ctx(style: str = "informational") -> EpisodeContext:
    return EpisodeContext(
        podcast="Test Pod",
        title="Episode 42",
        published="2024-01-15",
        duration_s=3600,
        style=style,
        language="en",
        chunk_extractions=[
            {"people": ["Alice"], "claims": ["AI will change everything"]},
        ],
    )


def _config() -> BudgetConfig:
    return BudgetConfig()


# ---------------------------------------------------------------------------
# Smoke test: request shape
# ---------------------------------------------------------------------------

class TestRequestShape:
    def test_model_is_sonnet(self) -> None:
        client = _make_mock_client()
        summarizer = EpisodeSummarizer(config=_config(), client=client)
        summarizer.summarize(_ctx("informational"))

        kwargs = client.messages.create.call_args.kwargs
        assert kwargs["model"] == "claude-sonnet-4-6"

    def test_output_config_effort_medium(self) -> None:
        client = _make_mock_client()
        summarizer = EpisodeSummarizer(config=_config(), client=client)
        summarizer.summarize(_ctx())

        kwargs = client.messages.create.call_args.kwargs
        assert kwargs["output_config"] == {"effort": "medium"}

    def test_system_is_single_cached_text_block(self) -> None:
        client = _make_mock_client()
        summarizer = EpisodeSummarizer(config=_config(), client=client)
        summarizer.summarize(_ctx())

        kwargs = client.messages.create.call_args.kwargs
        system = kwargs["system"]
        assert isinstance(system, list)
        assert len(system) == 1
        block = system[0]
        assert block["type"] == "text"
        assert block["cache_control"] == {"type": "ephemeral"}

    def test_banter_system_prompt_content(self) -> None:
        """System block for banter style must contain style-specific guidance."""
        client = _make_mock_client()
        summarizer = EpisodeSummarizer(config=_config(), client=client)
        summarizer.summarize(_ctx("banter"))

        kwargs = client.messages.create.call_args.kwargs
        system_text = kwargs["system"][0]["text"].lower()
        # banter prompt has "vibe" and "chat" or "comedy"
        assert "vibe" in system_text

    def test_user_message_contains_metadata(self) -> None:
        client = _make_mock_client()
        summarizer = EpisodeSummarizer(config=_config(), client=client)
        ctx = _ctx()
        summarizer.summarize(ctx)

        kwargs = client.messages.create.call_args.kwargs
        messages = kwargs["messages"]
        assert len(messages) == 1
        assert messages[0]["role"] == "user"

        payload = json.loads(messages[0]["content"])
        assert payload["podcast"] == "Test Pod"
        assert payload["title"] == "Episode 42"
        assert payload["published"] == "2024-01-15"
        assert payload["duration_s"] == 3600
        assert payload["language"] == "en"
        assert isinstance(payload["chunk_extractions"], list)
        assert len(payload["chunk_extractions"]) == 1

    def test_max_tokens_default(self) -> None:
        client = _make_mock_client()
        summarizer = EpisodeSummarizer(config=_config(), client=client)
        summarizer.summarize(_ctx())

        kwargs = client.messages.create.call_args.kwargs
        assert kwargs["max_tokens"] == 4000

    def test_transcript_text_omitted_when_none(self) -> None:
        client = _make_mock_client()
        summarizer = EpisodeSummarizer(config=_config(), client=client)
        ctx = _ctx()
        assert ctx.transcript_text is None
        summarizer.summarize(ctx)

        kwargs = client.messages.create.call_args.kwargs
        payload = json.loads(kwargs["messages"][0]["content"])
        assert "transcript_text" not in payload

    def test_transcript_text_included_when_present(self) -> None:
        client = _make_mock_client()
        summarizer = EpisodeSummarizer(config=_config(), client=client)
        ctx = _ctx()
        ctx = ctx.model_copy(update={"transcript_text": "Full transcript here."})
        summarizer.summarize(ctx)

        kwargs = client.messages.create.call_args.kwargs
        payload = json.loads(kwargs["messages"][0]["content"])
        assert payload["transcript_text"] == "Full transcript here."


# ---------------------------------------------------------------------------
# Style routing: each style loads a distinct system prompt
# ---------------------------------------------------------------------------

class TestStyleRouting:
    def test_three_styles_produce_different_prompts(self) -> None:
        prompts = {
            style: load_summary_prompt(style)
            for style in ("informational", "banter", "narrative")
        }
        assert prompts["informational"] != prompts["banter"]
        assert prompts["banter"] != prompts["narrative"]
        assert prompts["informational"] != prompts["narrative"]

    def test_informational_mentions_key_claims(self) -> None:
        prompt = load_summary_prompt("informational")
        assert "key claims" in prompt.lower() or "Key claims" in prompt

    def test_banter_mentions_vibe(self) -> None:
        prompt = load_summary_prompt("banter")
        assert "vibe" in prompt.lower() or "Vibe" in prompt

    def test_narrative_mentions_what_happens(self) -> None:
        prompt = load_summary_prompt("narrative")
        assert "what happens" in prompt.lower() or "What happens" in prompt

    def test_summarizer_uses_correct_style_prompt(self) -> None:
        """Each style's system block text differs at the API call level."""
        system_texts = {}
        for style in ("informational", "banter", "narrative"):
            client = _make_mock_client()
            summarizer = EpisodeSummarizer(config=_config(), client=client)
            summarizer.summarize(_ctx(style))
            kwargs = client.messages.create.call_args.kwargs
            system_texts[style] = kwargs["system"][0]["text"]

        assert system_texts["informational"] != system_texts["banter"]
        assert system_texts["banter"] != system_texts["narrative"]


# ---------------------------------------------------------------------------
# Returned EpisodeSummary fields
# ---------------------------------------------------------------------------

class TestReturnedSummary:
    def test_markdown_body_captured(self) -> None:
        expected = "## TL;DR\nThis is a great episode about AI.\n\n## Key claims\n- AI is transforming the world."
        client = _make_mock_client(text=expected)
        summarizer = EpisodeSummarizer(config=_config(), client=client)
        result = summarizer.summarize(_ctx())

        assert isinstance(result, EpisodeSummary)
        assert result.markdown == expected

    def test_model_used_captured(self) -> None:
        client = _make_mock_client()
        summarizer = EpisodeSummarizer(config=_config(), client=client)
        result = summarizer.summarize(_ctx())

        assert result.model_used == "claude-sonnet-4-6"

    def test_all_four_token_counts_captured(self) -> None:
        client = _make_mock_client(cache_read=5000, cache_create=200)
        summarizer = EpisodeSummarizer(config=_config(), client=client)
        result = summarizer.summarize(_ctx())

        assert result.input_tokens == 1000
        assert result.output_tokens == 500
        assert result.cache_read_tokens == 5000
        assert result.cache_creation_tokens == 200


# ---------------------------------------------------------------------------
# Cache verification: cache_read_input_tokens is surfaced
# ---------------------------------------------------------------------------

class TestCacheVerification:
    def test_cache_hit_surfaced_on_result(self) -> None:
        """A usage with cache_read_input_tokens=5000 must appear on EpisodeSummary."""
        client = _make_mock_client(cache_read=5000)
        summarizer = EpisodeSummarizer(config=_config(), client=client)
        result = summarizer.summarize(_ctx())

        assert result.cache_read_tokens == 5000

    def test_zero_cache_read_when_no_hit(self) -> None:
        client = _make_mock_client(cache_read=0)
        summarizer = EpisodeSummarizer(config=_config(), client=client)
        result = summarizer.summarize(_ctx())

        assert result.cache_read_tokens == 0


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_rate_limit_raises_summarization_error(self) -> None:
        client = MagicMock(spec=anthropic.Anthropic)
        # RateLimitError needs a response; mock it via side_effect
        req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        resp = httpx.Response(429, text="rate limited", request=req)
        client.messages.create.side_effect = anthropic.RateLimitError(
            "Rate limit exceeded", response=resp, body=None
        )

        summarizer = EpisodeSummarizer(config=_config(), client=client)
        with pytest.raises(SummarizationError):
            summarizer.summarize(_ctx())

    def test_api_status_error_raises_summarization_error(self) -> None:
        client = MagicMock(spec=anthropic.Anthropic)
        req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        resp = httpx.Response(500, text="internal server error", request=req)
        client.messages.create.side_effect = anthropic.APIStatusError(
            "Server error", response=resp, body=None
        )

        summarizer = EpisodeSummarizer(config=_config(), client=client)
        with pytest.raises(SummarizationError):
            summarizer.summarize(_ctx())

    def test_summarization_error_wraps_original(self) -> None:
        client = MagicMock(spec=anthropic.Anthropic)
        req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        resp = httpx.Response(503, text="service unavailable", request=req)
        original = anthropic.APIStatusError("Service unavailable", response=resp, body=None)
        client.messages.create.side_effect = original

        summarizer = EpisodeSummarizer(config=_config(), client=client)
        with pytest.raises(SummarizationError) as exc_info:
            summarizer.summarize(_ctx())
        # Original exception preserved as __cause__
        assert exc_info.value.__cause__ is original


# ---------------------------------------------------------------------------
# Prompt loader
# ---------------------------------------------------------------------------

class TestPromptLoader:
    def test_informational_returns_nonempty(self) -> None:
        prompt = load_summary_prompt("informational")
        assert isinstance(prompt, str)
        assert len(prompt) > 100

    def test_banter_returns_nonempty(self) -> None:
        prompt = load_summary_prompt("banter")
        assert isinstance(prompt, str)
        assert len(prompt) > 100

    def test_narrative_returns_nonempty(self) -> None:
        prompt = load_summary_prompt("narrative")
        assert isinstance(prompt, str)
        assert len(prompt) > 100

    def test_skip_raises(self) -> None:
        # "skip" style is not summarized
        with pytest.raises((FileNotFoundError, KeyError, ValueError)):
            load_summary_prompt("skip")

    def test_unknown_style_raises(self) -> None:
        with pytest.raises((FileNotFoundError, KeyError, ValueError)):
            load_summary_prompt("doesnotexist")


# ---------------------------------------------------------------------------
# Custom max_tokens / effort overrides
# ---------------------------------------------------------------------------

class TestOverrides:
    def test_custom_max_tokens(self) -> None:
        client = _make_mock_client()
        summarizer = EpisodeSummarizer(config=_config(), client=client, max_tokens=2000)
        summarizer.summarize(_ctx())

        kwargs = client.messages.create.call_args.kwargs
        assert kwargs["max_tokens"] == 2000

    def test_custom_effort(self) -> None:
        client = _make_mock_client()
        summarizer = EpisodeSummarizer(config=_config(), client=client, effort="high")
        summarizer.summarize(_ctx())

        kwargs = client.messages.create.call_args.kwargs
        assert kwargs["output_config"] == {"effort": "high"}
