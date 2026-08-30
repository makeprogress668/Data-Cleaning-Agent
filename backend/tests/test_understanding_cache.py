"""同一份文件、同一句话，每次都该得到同一个理解。

这是整条流水线里唯一不可复现的一步：模型超时（30 秒、重试两次、放弃），运行悄悄退回
关键词理解，用户拿到一份和昨天明显不同的工作簿 —— 而输出里没有任何东西解释这件事。
对每天早上跑同一张报表的业务人员来说，这就是「昨天还好好的」。

所以成功的理解会按「目标 + 表结构 + 提示词版本 + 模型」落盘，四项都相同的下一次直接
复用，不再问一遍模型。任何一项变了，键就变了，不会有陈旧结果被悄悄沿用。
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

import data_agent.agent.understanding_cache as understanding_cache
from data_agent.agent.goal_understanding import understand_goal
from data_agent.agent.understanding_cache import (
    MAX_ENTRIES,
    cache_key,
    cache_stats,
    clear_cache,
    load_entry,
    store_entry,
)
from data_agent.api.metadata_repository import InMemoryMetadataRepository


def _tables() -> dict[str, pd.DataFrame]:
    return {
        "订单明细": pd.DataFrame(
            {"订单号": ["O1", "O2"], "客户名称": ["张三", "李四"], "金额": [100, 200]}
        )
    }


def _key(goal: str = "把这份表整理一下", **kwargs) -> str:
    return cache_key(
        goal,
        kwargs.pop("tables", None) or _tables(),
        prompt_version=kwargs.pop("prompt_version", "v1"),
        model=kwargs.pop("model", "m1"),
        data_access_mode=kwargs.pop("data_access_mode", "masked_samples"),
        tenant_id=kwargs.pop("tenant_id", None),
    )


# --- 键：什么该让理解失效，什么不该 -------------------------------------------


def test_the_same_request_produces_the_same_key() -> None:
    assert _key() == _key()


def test_surrounding_whitespace_does_not_split_the_key() -> None:
    """首尾空白和换行不算改动。词间空格不归一化 —— 中文里那是真的不同的串。"""

    assert _key("把这份表整理一下") == _key("  把这份表整理一下\n")


def test_a_different_goal_gets_a_different_key() -> None:
    assert _key("把这份表整理一下") != _key("统计每个客户的金额")


def test_a_changed_schema_gets_a_different_key() -> None:
    other = {"订单明细": pd.DataFrame({"订单号": ["O1"], "金额": [100]})}
    assert _key() != _key(tables=other)


def test_more_rows_with_the_same_columns_reuse_the_key() -> None:
    """下个月的数据、同样的列 —— 目标的含义没变，不该重新理解一次。"""

    bigger = {
        "订单明细": pd.DataFrame(
            {
                "订单号": [f"O{i}" for i in range(50)],
                "客户名称": ["张三"] * 50,
                "金额": list(range(50)),
            }
        )
    }
    assert _key() == _key(tables=bigger)


def test_a_changed_semantic_profile_invalidates_the_key() -> None:
    text_amount = {
        "订单明细": pd.DataFrame(
            {"订单号": ["O1"], "客户名称": ["张三"], "金额": ["(100)"]}
        )
    }

    assert _key() != _key(tables=text_amount)


def test_tenants_never_share_an_understanding() -> None:
    assert _key(tenant_id="tenant-a") != _key(tenant_id="tenant-b")


def test_a_new_prompt_version_or_model_invalidates_the_key() -> None:
    """提示词改了、模型换了，旧理解必须失效，否则升级永远不生效。"""

    assert _key() != _key(prompt_version="v2")
    assert _key() != _key(model="m2")


def test_widening_the_data_access_policy_invalidates_the_key() -> None:
    """把策略从 metadata_only 放宽到 trusted_samples，就是为了让模型看得更多。

    这一项不进键的话，放宽之后仍然吃旧缓存 —— 一个调了等于没调的设置。
    """

    keys = {
        _key(data_access_mode=mode)
        for mode in ("metadata_only", "masked_samples", "trusted_samples")
    }
    assert len(keys) == 3


# --- 落盘与复用 ---------------------------------------------------------------


def test_a_stored_understanding_comes_back(tmp_path: Path) -> None:
    path = tmp_path / "cache.json"
    store_entry("k1", {"suggested_base_table": "订单明细"}, path)

    assert load_entry("k1", path) == {"suggested_base_table": "订单明细"}


def test_a_missing_key_is_simply_absent(tmp_path: Path) -> None:
    assert load_entry("nope", tmp_path / "cache.json") is None


def test_an_expired_understanding_is_not_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "cache.json"
    path.write_text(
        json.dumps(
            {
                "version": understanding_cache.CACHE_VERSION,
                "entries": {
                    "k1": {
                        "stored_at": "2020-01-01T00:00:00+00:00",
                        "plan": {"suggested_base_table": "订单明细"},
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DATA_AGENT_UNDERSTANDING_CACHE_TTL_SECONDS", "60")

    assert load_entry("k1", path) is None


def test_a_corrupt_cache_file_never_breaks_a_run(tmp_path: Path) -> None:
    path = tmp_path / "cache.json"
    path.write_text("{ this is not json", encoding="utf-8")

    assert load_entry("k1", path) is None
    store_entry("k1", {"ok": True}, path)
    assert load_entry("k1", path) == {"ok": True}


def test_the_cache_is_bounded(tmp_path: Path) -> None:
    path = tmp_path / "cache.json"
    for index in range(MAX_ENTRIES + 10):
        store_entry(f"k{index:04d}", {"n": index}, path)

    entries = json.loads(path.read_text(encoding="utf-8"))["entries"]
    assert len(entries) == MAX_ENTRIES


def test_disabling_the_cache_stops_both_halves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATA_AGENT_UNDERSTANDING_CACHE", "0")
    path = tmp_path / "cache.json"

    store_entry("k1", {"ok": True}, path)
    assert not path.exists()
    assert load_entry("k1", path) is None


def test_postgres_backend_uses_the_shared_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = InMemoryMetadataRepository()
    monkeypatch.setenv("DATA_AGENT_UNDERSTANDING_CACHE_BACKEND", "postgres")
    monkeypatch.setattr(
        understanding_cache,
        "_shared_repository",
        lambda: repository,
    )
    key = "a" * 64

    store_entry(key, {"suggested_base_table": "订单明细"})

    assert load_entry(key) == {"suggested_base_table": "订单明细"}
    assert cache_stats()["entry_count"] == 1
    assert clear_cache() == 1
    assert load_entry(key) is None


# --- 端到端：模型只被问一次，宕机后仍然复现 ------------------------------------


def test_an_identical_rerun_does_not_ask_the_model_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    goal = "把这份表整理一下"
    calls = {"n": 0}

    def _planner(_messages):
        calls["n"] += 1
        return json.dumps(
            {
                "suggested_base_table": "订单明细",
                "capabilities": {"wants_inplace": True},
                "capability_evidence": {"wants_inplace": goal},
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(
        "data_agent.agent.goal_understanding.llm_model_name", lambda: "test-model"
    )

    first = understand_goal(goal, _tables(), llm_planner=_planner)
    second = understand_goal(goal, _tables(), llm_planner=_planner)

    assert calls["n"] == 1, "同样的请求又问了一遍模型"
    assert first["understanding_source"] == "llm"
    assert second["understanding_source"] == "llm_cached"
    assert second["capabilities"]["wants_inplace"] is True
    assert second["task_spec"] == first["task_spec"], "重跑得到了不同的理解"


def test_a_model_outage_after_a_good_run_still_reproduces_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """昨天跑通了，今天模型连不上 —— 结果必须和昨天一致，而不是退回关键词理解。"""

    goal = "把这份表整理一下"
    monkeypatch.setattr(
        "data_agent.agent.goal_understanding.llm_model_name", lambda: "test-model"
    )

    good = understand_goal(
        goal,
        _tables(),
        llm_planner=lambda _m: json.dumps(
            {
                "suggested_base_table": "订单明细",
                "capabilities": {"wants_inplace": True},
                "capability_evidence": {"wants_inplace": goal},
            },
            ensure_ascii=False,
        ),
    )

    def _down(_messages):
        raise TimeoutError("The read operation timed out")

    replay = understand_goal(goal, _tables(), llm_planner=_down)

    assert replay["understanding_source"] == "llm_cached"
    assert replay["capabilities"]["wants_inplace"] is True
    assert replay["task_spec"] == good["task_spec"]


def test_a_bad_understanding_can_be_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """复用让好的理解可复现，也让坏的理解永久黏住。

    没有清除入口的话，某个目标一旦被理解错，每次重跑都会重复同一个错，只有知道
    data/understanding_cache.json 存在的人才逃得掉 —— 业务用户不可能知道这件事。
    """

    goal = "把这份表整理一下"
    monkeypatch.setattr(
        "data_agent.agent.goal_understanding.llm_model_name", lambda: "test-model"
    )
    calls = {"n": 0}

    def _planner(_messages):
        calls["n"] += 1
        return json.dumps({"suggested_base_table": "订单明细"}, ensure_ascii=False)

    understand_goal(goal, _tables(), llm_planner=_planner)
    assert understand_goal(goal, _tables(), llm_planner=_planner)[
        "understanding_source"
    ] == "llm_cached"

    assert clear_cache() >= 1
    assert understand_goal(goal, _tables(), llm_planner=_planner)[
        "understanding_source"
    ] == "llm"
    assert calls["n"] == 2, "清空后没有重新问模型"


def test_cache_stats_reports_what_is_stored(tmp_path: Path) -> None:
    path = tmp_path / "cache.json"
    store_entry("k1", {"ok": True}, path)

    stats = cache_stats(path)
    assert stats["entry_count"] == 1
    assert stats["newest"]
    assert clear_cache(path) == 1
    assert cache_stats(path)["entry_count"] == 0


def test_a_cold_cache_outage_is_reported_rather_than_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有可复用的理解、模型又连不上 —— 那就明说，不要假装这是规则模式。"""

    monkeypatch.setattr(
        "data_agent.agent.goal_understanding.llm_model_name", lambda: "test-model"
    )

    def _down(_messages):
        raise TimeoutError("The read operation timed out")

    result = understand_goal("把这份表整理一下", _tables(), llm_planner=_down)

    assert result["understanding_source"] == "deterministic_fallback"
    assert "timed out" in result["understanding_fallback_reason"]
