"""Pluggable LLM client for the goal-understanding layer.

This module turns the project from a "fake agent" (pure keyword matching) into a
real two-layer agent: an LLM reasons about the business goal and drafts a plan,
while the deterministic engine still performs every data mutation and validates
the LLM draft against a strict whitelist.

Design principles:
- Provider-agnostic, OpenAI-compatible Chat Completions protocol (works with
  OpenAI, Azure OpenAI, and most self-hosted gateways / internal proxies).
- Fully configured through environment variables; no secrets in code.
- The LLM is optional. When it is not configured or unreachable, callers fall
  back to the deterministic engine, so behaviour degrades gracefully.
- No hard dependency on any vendor SDK: requests are sent with the standard
  library so the client works even in minimal environments.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from data_agent.observability.llm_usage import (
    infer_llm_operation,
    record_llm_usage,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_TEMPERATURE = 0.0


class LLMError(RuntimeError):
    """Raised when the LLM endpoint cannot be reached or returns an invalid reply."""


@dataclass(frozen=True)
class LLMConfig:
    """Runtime configuration for the pluggable LLM client.

    All fields are resolved from environment variables so operators can point the
    agent at any OpenAI-compatible endpoint (public cloud or internal gateway)
    without touching code.
    """

    base_url: str
    api_key: str
    model: str
    timeout: float = DEFAULT_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    temperature: float = DEFAULT_TEMPERATURE
    organization: str | None = None
    # Deep-thinking toggle for reasoning models (e.g. Volcengine Ark Doubao Seed).
    # ``None`` keeps the payload OpenAI-compatible (field omitted); "disabled" turns
    # off the chain-of-thought so structured-extraction calls return in seconds
    # instead of running a multi-thousand-token reasoning pass first.
    thinking: str | None = None

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"


def _env(*names: str) -> str | None:
    """Return the first non-empty environment variable among ``names``."""
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def _load_dotenv_once() -> None:
    """Best-effort load of a local ``.env`` file so config works out of the box.

    Uses python-dotenv when available; silently skips if it is not installed or
    no ``.env`` is present. Never overrides variables already set in the process.

    The file is looked up relative to this package as well as the current working
    directory. python-dotenv's default search starts at the CWD, so running the CLI
    or uvicorn from outside the repository used to drop the whole LLM configuration —
    and because a missing key silently falls back to the deterministic engine, the
    only symptom was worse goal understanding with no error anywhere.

    Set ``DATA_AGENT_DISABLE_DOTENV=1`` to skip this entirely — used by the test
    suite so a developer's real repo-root ``.env`` never leaks a live key into
    tests (which would break config assertions and trigger real network calls).
    """
    if os.environ.get("DATA_AGENT_DISABLE_DOTENV") in {"1", "true", "yes", "on"}:
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(override=False)
    for candidate in _package_dotenv_candidates():
        if candidate.is_file():
            load_dotenv(candidate, override=False)
            return


def _package_dotenv_candidates() -> tuple[Path, ...]:
    """``.env`` locations anchored to the installed package, not the CWD."""

    here = Path(__file__).resolve()
    # .../<repo>/backend/src/data_agent/agent/llm_client.py -> backend/ and <repo>/
    return tuple(parent / ".env" for parent in here.parents[3:5])


def load_llm_config() -> LLMConfig | None:
    """Build an :class:`LLMConfig` from environment variables.

    Returns ``None`` when the agent is not configured for LLM use, which signals
    callers to stay on the deterministic path. Recognised variables (with common
    aliases so existing deployments keep working):

    - ``DATA_AGENT_LLM_API_KEY`` / ``OPENAI_API_KEY``
    - ``DATA_AGENT_LLM_BASE_URL`` / ``OPENAI_BASE_URL`` (default OpenAI public API)
    - ``DATA_AGENT_LLM_MODEL`` / ``OPENAI_MODEL`` (default ``gpt-4o-mini``)
    - ``DATA_AGENT_LLM_TIMEOUT`` / ``DATA_AGENT_LLM_MAX_RETRIES``
    - ``DATA_AGENT_LLM_TEMPERATURE`` / ``DATA_AGENT_LLM_ORG``
    - ``DATA_AGENT_LLM_THINKING`` (e.g. ``disabled``): for reasoning models that
      run a chain-of-thought by default; set to ``disabled`` for fast, second-scale
      structured extraction. Left unset for OpenAI-compatible endpoints.

    Set ``DATA_AGENT_LLM_ENABLED=0`` to force the deterministic path even when a
    key is present.
    """

    _load_dotenv_once()

    if _env("DATA_AGENT_LLM_ENABLED") in {"0", "false", "no", "off"}:
        _log_resolution("disabled", "DATA_AGENT_LLM_ENABLED=0，本次使用确定性规则引擎")
        return None

    api_key = _env("DATA_AGENT_LLM_API_KEY", "OPENAI_API_KEY")
    if not api_key:
        _log_resolution(
            "unconfigured",
            "未读到 DATA_AGENT_LLM_API_KEY，本次使用确定性规则引擎"
            "（如已写入 .env，请确认该文件在仓库根目录）",
        )
        return None

    base_url = _env("DATA_AGENT_LLM_BASE_URL", "OPENAI_BASE_URL") or "https://api.openai.com/v1"
    model = _env("DATA_AGENT_LLM_MODEL", "OPENAI_MODEL") or "gpt-4o-mini"

    _log_resolution("configured", f"理解层已启用：{base_url} / {model}")
    return LLMConfig(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout=_env_float("DATA_AGENT_LLM_TIMEOUT", DEFAULT_TIMEOUT),
        max_retries=_env_int("DATA_AGENT_LLM_MAX_RETRIES", DEFAULT_MAX_RETRIES),
        temperature=_env_float("DATA_AGENT_LLM_TEMPERATURE", DEFAULT_TEMPERATURE),
        organization=_env("DATA_AGENT_LLM_ORG", "OPENAI_ORG_ID"),
        thinking=_env("DATA_AGENT_LLM_THINKING"),
    )


# Config resolution is looked up on every planning call; log each distinct outcome
# once so "is my key actually live?" is answerable from the logs without turning
# every run into noise.
_logged_resolutions: set[str] = set()


def _log_resolution(kind: str, message: str) -> None:
    if kind in _logged_resolutions:
        return
    _logged_resolutions.add(kind)
    logger.info("%s", message)


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid float for %s=%r, using default %s", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid int for %s=%r, using default %s", name, raw, default)
        return default


class LLMClient:
    """Thin OpenAI-compatible Chat Completions client using the standard library."""

    def __init__(self, config: LLMConfig, *, opener=None) -> None:
        self._config = config
        # ``opener`` is injectable so tests can exercise the client without network.
        self._opener = opener or urllib.request.urlopen

    @property
    def model(self) -> str:
        return self._config.model

    def complete(self, messages: list[dict[str, str]]) -> str:
        """Send chat messages and return the assistant's text content.

        Retries transient failures with linear backoff. Raises :class:`LLMError`
        after exhausting retries so the caller can fall back deterministically.
        """

        payload = {
            "model": self._config.model,
            "messages": messages,
            "temperature": self._config.temperature,
            "response_format": {"type": "json_object"},
        }
        # Reasoning models (e.g. Doubao Seed) run a chain-of-thought pass by default,
        # which makes a structured-extraction call take tens of seconds. When the
        # operator opts in, pass the vendor ``thinking`` switch so the call returns
        # in seconds. Omitted entirely otherwise to stay OpenAI-compatible.
        if self._config.thinking:
            payload["thinking"] = {"type": self._config.thinking}
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._config.api_key}",
        }
        if self._config.organization:
            headers["OpenAI-Organization"] = self._config.organization

        started = time.perf_counter()
        operation = infer_llm_operation(messages)
        last_error: Exception | None = None
        attempts = 0
        for attempt in range(1, self._config.max_retries + 2):
            attempts = attempt
            try:
                request = urllib.request.Request(
                    self._config.chat_completions_url,
                    data=body,
                    headers=headers,
                    method="POST",
                )
                with self._opener(request, timeout=self._config.timeout) as response:
                    raw = response.read().decode("utf-8")
                content, usage = _extract_response(raw)
                record_llm_usage(
                    operation=operation,
                    model=self._config.model,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    attempts=attempt,
                    status="succeeded",
                    usage=usage,
                )
                return content
            except urllib.error.HTTPError as exc:
                detail = _http_error_detail(exc, api_key=self._config.api_key)
                last_error = LLMError(f"HTTP {exc.code}: {detail}")
                logger.warning(
                    "LLM request failed (attempt %s/%s): %s",
                    attempt,
                    self._config.max_retries + 1,
                    last_error,
                )
                # Authentication, permission, and request-shape errors do not heal
                # with an immediate retry. Keep retries for rate limits, timeouts,
                # conflicts, and provider failures only.
                retryable = exc.code in {408, 409, 425, 429} or exc.code >= 500
                if not retryable:
                    break
                if attempt <= self._config.max_retries:
                    time.sleep(min(2.0 * attempt, 5.0))
            except (urllib.error.URLError, TimeoutError, OSError, LLMError) as exc:
                last_error = exc
                logger.warning(
                    "LLM request failed (attempt %s/%s): %s",
                    attempt,
                    self._config.max_retries + 1,
                    exc,
                )
                if attempt <= self._config.max_retries:
                    time.sleep(min(2.0 * attempt, 5.0))
        record_llm_usage(
            operation=operation,
            model=self._config.model,
            latency_ms=(time.perf_counter() - started) * 1000,
            attempts=attempts,
            status="failed",
            error_type=type(last_error).__name__ if last_error else "LLMError",
        )
        raise LLMError(f"LLM request failed after retries: {last_error}")


def _http_error_detail(exc: urllib.error.HTTPError, *, api_key: str) -> str:
    """Return a short provider error without ever echoing the configured secret."""

    try:
        detail = exc.read(2048).decode("utf-8", errors="replace")
    except (AttributeError, OSError):
        detail = str(exc.reason or "request rejected")
    detail = " ".join(detail.split()) or str(exc.reason or "request rejected")
    if api_key:
        detail = detail.replace(api_key, "[REDACTED]")
    return detail[:500]


def _extract_response(raw: str) -> tuple[str, dict]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMError(f"LLM returned non-JSON envelope: {exc}") from exc
    try:
        content = str(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"LLM response missing choices/message content: {exc}") from exc
    usage = data.get("usage")
    return content, usage if isinstance(usage, dict) else {}


def llm_model_name() -> str:
    """The configured model id, or "" when no LLM is set up.

    Part of the understanding cache key: a model swap has to invalidate understandings
    the previous model produced, or an upgrade would silently never take effect.
    """

    config = load_llm_config()
    return config.model if config else ""


def build_default_llm_planner():
    """Return an ``LLMPlannerCallable`` bound to env config, or ``None``.

    The returned callable takes the planner's prompt messages and returns the raw
    JSON string draft. Returning ``None`` means "no LLM configured" so the planner
    stays fully deterministic.
    """

    config = load_llm_config()
    if config is None:
        return None
    client = LLMClient(config)

    def _planner(messages: list[dict[str, str]]) -> str:
        return client.complete(messages)

    return _planner
