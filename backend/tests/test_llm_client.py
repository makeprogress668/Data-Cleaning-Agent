import json
import urllib.error
from io import BytesIO
from pathlib import Path

import pandas as pd
import pytest

from data_agent.agent import plan_from_goal
from data_agent.agent.llm_client import (
    LLMClient,
    LLMConfig,
    LLMError,
    build_default_llm_planner,
    load_llm_config,
)


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self._text = text.encode("utf-8")

    def read(self) -> bytes:
        return self._text

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


def test_load_llm_config_returns_none_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "DATA_AGENT_LLM_API_KEY",
        "OPENAI_API_KEY",
        "DATA_AGENT_LLM_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    assert load_llm_config() is None
    assert build_default_llm_planner() is None


def test_load_llm_config_resolves_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_AGENT_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("DATA_AGENT_LLM_BASE_URL", "https://gw.internal/v1")
    monkeypatch.setenv("DATA_AGENT_LLM_MODEL", "qwen-max")
    monkeypatch.delenv("DATA_AGENT_LLM_THINKING", raising=False)
    config = load_llm_config()
    assert config is not None
    assert config.model == "qwen-max"
    assert config.chat_completions_url == "https://gw.internal/v1/chat/completions"
    # Unset by default so the payload stays OpenAI-compatible.
    assert config.thinking is None


def test_load_llm_config_reads_thinking_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_AGENT_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("DATA_AGENT_LLM_THINKING", "disabled")
    config = load_llm_config()
    assert config is not None
    assert config.thinking == "disabled"


def test_llm_enabled_switch_forces_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_AGENT_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("DATA_AGENT_LLM_ENABLED", "0")
    assert load_llm_config() is None


def test_llm_client_sends_openai_compatible_request() -> None:
    captured: dict[str, object] = {}

    def fake_opener(request, timeout=None):  # noqa: ANN001
        captured["url"] = request.full_url
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.data)
        envelope = {"choices": [{"message": {"content": '{"job_config": {}}'}}]}
        return _FakeResponse(json.dumps(envelope))

    config = LLMConfig(base_url="https://gw.internal/v1", api_key="sk-test", model="qwen-max")
    client = LLMClient(config, opener=fake_opener)
    content = client.complete([{"role": "user", "content": "hi"}])

    assert content == '{"job_config": {}}'
    assert captured["url"] == "https://gw.internal/v1/chat/completions"
    assert captured["auth"] == "Bearer sk-test"
    assert captured["body"]["model"] == "qwen-max"
    assert captured["body"]["response_format"] == {"type": "json_object"}
    # No thinking switch configured -> field omitted (OpenAI-compatible payload).
    assert "thinking" not in captured["body"]


def test_llm_client_includes_thinking_switch_when_configured() -> None:
    captured: dict[str, object] = {}

    def fake_opener(request, timeout=None):  # noqa: ANN001
        captured["body"] = json.loads(request.data)
        envelope = {"choices": [{"message": {"content": "{}"}}]}
        return _FakeResponse(json.dumps(envelope))

    config = LLMConfig(
        base_url="https://gw.internal/v1",
        api_key="sk-test",
        model="doubao-seed-evolving",
        thinking="disabled",
    )
    LLMClient(config, opener=fake_opener).complete([{"role": "user", "content": "hi"}])

    assert captured["body"]["thinking"] == {"type": "disabled"}


def test_llm_client_raises_on_invalid_envelope() -> None:
    def bad_opener(request, timeout=None):  # noqa: ANN001
        return _FakeResponse("not-json")

    config = LLMConfig(base_url="https://api.test/v1", api_key="y", model="m", max_retries=0)
    with pytest.raises(LLMError):
        LLMClient(config, opener=bad_opener).complete([{"role": "user", "content": "x"}])


def test_llm_client_retries_then_raises() -> None:
    calls = {"n": 0}

    def flaky(request, timeout=None):  # noqa: ANN001
        calls["n"] += 1
        raise OSError("connection refused")

    config = LLMConfig(base_url="https://api.test/v1", api_key="y", model="m", max_retries=1)
    with pytest.raises(LLMError):
        LLMClient(config, opener=flaky).complete([{"role": "user", "content": "x"}])
    assert calls["n"] == 2


def test_llm_client_does_not_retry_permission_error_and_surfaces_detail() -> None:
    calls = {"n": 0}

    def forbidden(request, timeout=None):  # noqa: ANN001
        calls["n"] += 1
        raise urllib.error.HTTPError(
            request.full_url,
            403,
            "Forbidden",
            hdrs=None,
            fp=BytesIO(b'{"error":{"message":"model access denied"}}'),
        )

    config = LLMConfig(
        base_url="https://api.test/v1",
        api_key="secret-not-for-logs",
        model="m",
        max_retries=2,
    )
    with pytest.raises(LLMError, match="model access denied"):
        LLMClient(config, opener=forbidden).complete(
            [{"role": "user", "content": "x"}]
        )
    assert calls["n"] == 1


def _write_demo_inputs(tmp_path: Path) -> Path:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    pd.DataFrame(
        {
            "order_id": ["O-1", "O-2", "O-3"],
            "customer_id": ["C001", "C002", "C404"],
            "amount": [100, 200, -5],
            "status": ["paid", "unpaid", "paid"],
        }
    ).to_excel(input_dir / "orders.xlsx", index=False)
    return input_dir


def test_plan_accepts_valid_llm_draft(tmp_path: Path) -> None:
    input_dir = _write_demo_inputs(tmp_path)

    def fake_planner(_messages: list[dict[str, str]]) -> str:
        return json.dumps(
            {
                "job_config": {"name": "llm-refined-job"},
                "assumptions": ["order_id 是主键"],
                "clarification_questions": [],
            }
        )

    result = plan_from_goal(
        [input_dir],
        goal="清洗订单数据并标记异常",
        output_file=tmp_path / "output" / "r.xlsx",
        output_job_path=tmp_path / "configs" / "j.json",
        llm_planner=fake_planner,
    )
    # LLM draft accepted -> name overridden; data-safety keys stay deterministic.
    assert result.job_config["name"] == "llm-refined-job"
    assert result.job_config["output_mode"] == "business_answer"


def test_plan_falls_back_when_llm_unavailable(tmp_path: Path) -> None:
    input_dir = _write_demo_inputs(tmp_path)

    def broken_planner(_messages: list[dict[str, str]]) -> str:
        raise LLMError("endpoint down")

    result = plan_from_goal(
        [input_dir],
        goal="清洗订单数据并标记异常",
        output_file=tmp_path / "output" / "r.xlsx",
        output_job_path=tmp_path / "configs" / "j.json",
        llm_planner=broken_planner,
    )
    # Deterministic plan is returned unchanged; the failure is recorded, not fatal.
    assert result.job_config["output_mode"] == "business_answer"
    assert not result.llm_attempts.empty
    assert (result.llm_attempts["status"] == "unavailable").any()
