"""Shared pytest fixtures / test-environment setup.

The API carries an in-process, per-client sliding-window rate limiter whose limit is
read once when the FastAPI ``app`` is constructed. Running several API-heavy test
modules in one process shares that limiter and trips 429s in a way that depends on
test order, not on the code under test. Disable it for the whole test session by
setting the env var before ``data_agent.api.app`` is ever imported.

We also disable ``.env`` auto-loading: otherwise a developer's real repo-root
``.env`` (with a live LLM key) leaks into the test process, breaking config
assertions and triggering real network calls that hang the suite.

The understanding cache is redirected per test for the same reason, plus one of its
own: it is keyed on goal + schema, and the suite reuses both across tests. Left
pointing at the shared file, one test's stored understanding answers the next test's
call — and the run would also litter the developer's repo.
"""

import os
from pathlib import Path

import pytest

os.environ["DATA_AGENT_RATE_LIMIT_PER_MINUTE"] = "0"
os.environ["DATA_AGENT_DISABLE_DOTENV"] = "1"
for variable in (
    "DATA_AGENT_API_KEY",
    "DATA_AGENT_TENANT_API_KEYS_JSON",
    "DATA_AGENT_OIDC_ISSUER",
    "DATA_AGENT_OIDC_AUDIENCE",
    "DATA_AGENT_OIDC_JWKS_URL",
    "DATA_AGENT_OIDC_PUBLIC_KEY",
    "DATA_AGENT_ENV",
    "DATA_AGENT_METADATA_BACKEND",
    "DATA_AGENT_DATABASE_URL",
    "DATA_AGENT_JOB_QUEUE_BACKEND",
    "DATA_AGENT_REDIS_URL",
    "DATA_AGENT_ARTIFACT_BACKEND",
    "DATA_AGENT_LLM_API_KEY",
    "OPENAI_API_KEY",
    "DATA_AGENT_LLM_ENABLED",
):
    os.environ.pop(variable, None)


@pytest.fixture(autouse=True)
def isolated_understanding_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DATA_AGENT_UNDERSTANDING_CACHE_PATH",
        str(tmp_path / "understanding_cache.json"),
    )
