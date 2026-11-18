"""The one place that talks to the Claude API.

Everything the app asks of a model is "read this text, fill in this
schema", so the whole surface is a single method: ClaudeClient.parse().
Scoring (scoring.py) and CV ingestion (cv_ingest.py) both go through it,
which keeps the SDK details -- structured outputs, prompt caching, refusal
handling -- out of the modules that hold the actual prompts.

Design notes:

  * Structured outputs. `messages.parse(output_format=Schema)` constrains
    the response to the Pydantic schema and returns a validated instance,
    so there is no free-text JSON to parse or repair.
  * Prompt caching. The system prompt (instructions + the candidate's
    profile and CV) is identical for every job, so it is marked cacheable;
    only the job posting in the user message changes between calls. Cached
    input is billed at a tenth of the normal rate.
  * No sampling parameters. Claude Haiku 5.5 rejects non-default
    `temperature`/`top_p`; consistency comes from the schema and the prompt.
  * Thinking is adaptive and on by default on Haiku 5.5. `effort` is the
    cost/quality dial, and thinking counts toward `max_tokens`, so the cap
    is deliberately generous.
  * Errors are NOT swallowed here. parse() raises on any failure and each
    caller decides what failing means for it (the scorer fails open; the
    CV tool just stops).
"""

from __future__ import annotations

import logging
import os
from typing import TypeVar

import anthropic
from pydantic import BaseModel

DEFAULT_MODEL = "claude-haiku-5-5"
API_KEY_ENV = "ANTHROPIC_API_KEY"

# Room for adaptive thinking plus the largest structured answer we ask for
# (the deep pass: requirements + skills + market fields).
DEFAULT_MAX_TOKENS = 8000

SchemaT = TypeVar("SchemaT", bound=BaseModel)

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """The model answered, but not with something usable (refusal, cut off
    at max_tokens, or output that did not validate against the schema)."""


class ClaudeClient:
    def __init__(self, api_key: str, timeout: float = 60.0, max_retries: int = 2) -> None:
        # The SDK retries 429/5xx/connection errors itself (max_retries),
        # with backoff -- no hand-rolled retry loop needed.
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=max_retries)

    @classmethod
    def from_env(cls, **kwargs) -> ClaudeClient:
        api_key = os.environ.get(API_KEY_ENV)
        if not api_key:
            raise RuntimeError(f"{API_KEY_ENV} must be set in .env (see .env.example).")
        return cls(api_key=api_key, **kwargs)

    def close(self) -> None:
        self._client.close()

    def parse(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: type[SchemaT],
        effort: str = "low",
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> SchemaT:
        """Send one system + user prompt and return a validated `schema`
        instance. Raises anthropic.APIError on transport/API failures and
        LLMError when the response is unusable."""
        response = self._client.messages.parse(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_format=schema,
            output_config={"effort": effort},
        )
        _log_usage(model, response.usage)
        if response.stop_reason in ("refusal", "max_tokens"):
            raise LLMError(f"no usable output (stop_reason={response.stop_reason})")
        parsed = response.parsed_output
        if parsed is None:
            raise LLMError(f"response did not match the schema (stop_reason={response.stop_reason})")
        return parsed


def _log_usage(model: str, usage) -> None:
    """One line per call: the only record of what the model costs, and of
    whether the cached prefix is being reused. The API reports input in
    three parts -- uncached, read from cache, written to cache -- which add
    up to the prompt's real size."""
    cache_read = usage.cache_read_input_tokens or 0
    cache_write = usage.cache_creation_input_tokens or 0
    log.info(
        "%s: %d input tokens (%d read from cache, %d written to cache), %d output",
        model, usage.input_tokens + cache_read + cache_write, cache_read, cache_write, usage.output_tokens,
    )


def describe_error(exc: Exception) -> str:
    """A short, log-safe description of an LLM failure. API errors carry
    the request (and so headers) in their repr; this keeps to the status
    code and the API's own message."""
    if isinstance(exc, anthropic.APIStatusError):
        return f"HTTP {exc.status_code}: {exc.message}"[:300]
    if isinstance(exc, anthropic.APIConnectionError):
        return "connection error"
    return f"{type(exc).__name__}: {exc}"[:300]
