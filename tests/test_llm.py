"""ClaudeClient.parse offline: the SDK's messages.parse is stubbed."""

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from jobradar.llm import ClaudeClient, LLMError


class Answer(BaseModel):
    value: int


@pytest.fixture
def client():
    client = ClaudeClient(api_key="test-key")
    yield client
    client.close()


USAGE = SimpleNamespace(input_tokens=100, cache_read_input_tokens=900, cache_creation_input_tokens=0,
                        output_tokens=50)


def stub(client, monkeypatch, response):
    response.usage = USAGE
    calls = []

    def fake_parse(**kwargs):
        calls.append(kwargs)
        return response

    monkeypatch.setattr(client._client.messages, "parse", fake_parse)
    return calls


def test_parse_returns_the_validated_object_and_caches_the_system_prompt(client, monkeypatch):
    calls = stub(client, monkeypatch, SimpleNamespace(stop_reason="end_turn", parsed_output=Answer(value=7)))

    result = client.parse(model="claude-haiku-5-5", system="rules", user="job", schema=Answer)

    assert result.value == 7
    request = calls[0]
    assert request["model"] == "claude-haiku-5-5"
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert request["messages"] == [{"role": "user", "content": "job"}]
    assert request["output_format"] is Answer
    assert "temperature" not in request  # rejected by Haiku 5.5


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_parse_raises_when_the_model_did_not_finish(client, monkeypatch, stop_reason):
    stub(client, monkeypatch, SimpleNamespace(stop_reason=stop_reason, parsed_output=None))

    with pytest.raises(LLMError, match=stop_reason):
        client.parse(model="m", system="s", user="u", schema=Answer)


def test_parse_raises_when_output_does_not_match_the_schema(client, monkeypatch):
    stub(client, monkeypatch, SimpleNamespace(stop_reason="end_turn", parsed_output=None))

    with pytest.raises(LLMError):
        client.parse(model="m", system="s", user="u", schema=Answer)


def test_from_env_requires_the_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        ClaudeClient.from_env()


def test_parse_logs_token_usage_including_the_cached_share(client, monkeypatch, caplog):
    stub(client, monkeypatch, SimpleNamespace(stop_reason="end_turn", parsed_output=Answer(value=1)))

    with caplog.at_level("INFO", logger="jobradar.llm"):
        client.parse(model="claude-haiku-5-5", system="s", user="u", schema=Answer)

    assert "1000 input tokens (900 read from cache, 0 written to cache), 50 output" in caplog.text
