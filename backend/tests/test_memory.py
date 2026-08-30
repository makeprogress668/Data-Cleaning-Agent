import json
from pathlib import Path

import pandas as pd

from data_agent.agent.memory import (
    load_memory,
    load_memory_summary,
    memory_enabled,
    record_task,
    save_memory,
    summarize_for_prompt,
    update_from_job_config,
)
from data_agent.agent.planner import build_llm_planner_messages


def _job_config(base_table="orders", critical=("amount",), match_mode="fuzzy"):
    return {
        "base_table": base_table,
        "quality_score": {"critical_fields": list(critical)},
        "lookups": [{"name": "l1", "match_mode": match_mode}],
    }


# --- enable flag / defaults ---------------------------------------------------


def test_memory_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("DATA_AGENT_MEMORY_ENABLED", raising=False)
    assert memory_enabled() is False


def test_memory_enabled_when_set(monkeypatch) -> None:
    monkeypatch.setenv("DATA_AGENT_MEMORY_ENABLED", "1")
    assert memory_enabled() is True
    monkeypatch.setenv("DATA_AGENT_MEMORY_ENABLED", "off")
    assert memory_enabled() is False


# --- load / update / save (pure functions) ------------------------------------


def test_load_returns_empty_when_absent(tmp_path: Path) -> None:
    memory = load_memory(tmp_path / "nope.json")
    assert memory["task_count"] == 0
    assert memory["preferences"]["base_table"] == {}


def test_update_from_job_config_counts_preferences() -> None:
    memory = load_memory(Path("/nonexistent/never.json"))
    memory = update_from_job_config(memory, _job_config(critical=("amount", "order_id")))
    memory = update_from_job_config(memory, _job_config(critical=("amount",)))
    prefs = memory["preferences"]
    assert prefs["base_table"]["orders"] == 2
    assert prefs["critical_fields"]["amount"] == 2
    assert prefs["critical_fields"]["order_id"] == 1
    assert prefs["match_modes"]["fuzzy"] == 2
    assert memory["task_count"] == 2


def test_save_and_reload_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "mem.json"
    memory = update_from_job_config(load_memory(path), _job_config())
    written = save_memory(memory, path)
    assert written == path
    reloaded = load_memory(path)
    assert reloaded["preferences"]["base_table"]["orders"] == 1
    assert reloaded["updated_at"] is not None


def test_save_debounces_when_payload_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "mem.json"
    memory = update_from_job_config(load_memory(path), _job_config())
    assert save_memory(memory, path) == path
    # Saving the exact same payload again should be skipped (returns None).
    assert save_memory(load_memory(path), path) is None


def test_load_tolerates_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "mem.json"
    path.write_text("{ not valid json", encoding="utf-8")
    memory = load_memory(path)
    assert memory["task_count"] == 0  # falls back to empty, does not raise


def test_normalize_drops_non_integer_counts(tmp_path: Path) -> None:
    path = tmp_path / "mem.json"
    path.write_text(
        json.dumps({"preferences": {"base_table": {"orders": "lots", "sales": 3}}}),
        encoding="utf-8",
    )
    memory = load_memory(path)
    assert memory["preferences"]["base_table"] == {"sales": 3}


# --- summary hint -------------------------------------------------------------


def test_summarize_returns_none_when_empty() -> None:
    assert summarize_for_prompt(load_memory(Path("/nope.json"))) is None


def test_summarize_ranks_by_frequency() -> None:
    memory = load_memory(Path("/nope.json"))
    for _ in range(3):
        memory = update_from_job_config(memory, _job_config(base_table="orders"))
    memory = update_from_job_config(memory, _job_config(base_table="sales"))
    hint = summarize_for_prompt(memory)
    assert hint["frequent_base_tables"][0] == "orders"
    assert "preferred_match_modes" in hint


# --- record_task honors the enable flag --------------------------------------


def test_record_task_noop_when_disabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("DATA_AGENT_MEMORY_ENABLED", raising=False)
    path = tmp_path / "mem.json"
    assert record_task(_job_config(), path=path) is None
    assert not path.exists()


def test_record_task_writes_when_enabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DATA_AGENT_MEMORY_ENABLED", "1")
    path = tmp_path / "mem.json"
    assert record_task(_job_config(), path=path) == path
    assert load_memory(path)["preferences"]["base_table"]["orders"] == 1


# --- planner integration ------------------------------------------------------


def test_planner_context_omits_memory_when_disabled(monkeypatch) -> None:
    monkeypatch.delenv("DATA_AGENT_MEMORY_ENABLED", raising=False)
    messages = build_llm_planner_messages(
        goal="清洗订单",
        deterministic_config={"name": "job"},
        profile_sheets={"table_profile": pd.DataFrame([{"table": "orders", "row_count": 2}])},
        recommendation_sheets={},
    )
    context = json.loads(messages[1]["content"].split("\n", 1)[1])
    assert "user_memory" not in context


def test_planner_context_includes_memory_when_enabled(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "mem.json"
    save_memory(update_from_job_config(load_memory(path), _job_config()), path)
    monkeypatch.setenv("DATA_AGENT_MEMORY_ENABLED", "1")
    monkeypatch.setenv("DATA_AGENT_MEMORY_PATH", str(path))
    messages = build_llm_planner_messages(
        goal="清洗订单",
        deterministic_config={"name": "job"},
        profile_sheets={"table_profile": pd.DataFrame([{"table": "orders", "row_count": 2}])},
        recommendation_sheets={},
    )
    context = json.loads(messages[1]["content"].split("\n", 1)[1])
    assert "user_memory" in context
    assert context["user_memory"]["frequent_base_tables"] == ["orders"]


def test_load_memory_summary_noop_when_disabled(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "mem.json"
    save_memory(update_from_job_config(load_memory(path), _job_config()), path)
    monkeypatch.delenv("DATA_AGENT_MEMORY_ENABLED", raising=False)
    monkeypatch.setenv("DATA_AGENT_MEMORY_PATH", str(path))
    assert load_memory_summary() is None
