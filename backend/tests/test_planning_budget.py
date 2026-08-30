"""修复轮是锦上添花，不该让用户为它无限等待。

规划最多跑两轮：第一轮草案没通过校验，就带着修复提示再问一次。这条逻辑是对的 ——
让模型改一版比直接退回确定性方案要好。问题在代价：每次调用最坏是 超时×(重试+1)，
按出厂配置是 96 秒。一次网络抖动就能把 15 秒的规划变成 90 秒的等待，而结果还是退回
确定性方案。

所以超出预算之后不再**开始**新的一轮；已经在飞的那一轮不打断。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from data_agent.agent.planner import PlannerResult, plan_from_goal
from data_agent.observability import infer_llm_operation

_GOAL = "把客户名称关联过来"


def _inputs(tmp_path: Path) -> Path:
    src = tmp_path / "in"
    src.mkdir(parents=True, exist_ok=True)
    (src / "订单明细.csv").write_text(
        "订单号,客户编号,金额\nO1,C1,100\nO2,C2,200\n", encoding="utf-8-sig"
    )
    (src / "客户档案.csv").write_text(
        "客户编号,客户名称\nC1,张三\nC2,李四\n", encoding="utf-8-sig"
    )
    return src


class _CountingPlanner:
    """同一个回调既服务目标理解又服务规划，只数规划那部分。"""

    def __init__(self, delay: float = 0.0) -> None:
        self.planning_calls = 0
        self._delay = delay

    def __call__(self, messages) -> str:
        if infer_llm_operation(messages) == "planning":
            self.planning_calls += 1
        if self._delay:
            time.sleep(self._delay)
        return _rejected_draft(messages)


def _rejected_draft(_messages) -> str:
    """能解析、但引用了不存在的表 —— 必定被校验拒绝，从而触发修复轮。"""

    return json.dumps(
        {"base_table": "订单明细", "lookups": [{"source_table": "根本没有这张表"}]},
        ensure_ascii=False,
    )


def _plan(tmp_path: Path, planner) -> PlannerResult:
    return plan_from_goal(
        input_paths=[_inputs(tmp_path)],
        goal=_GOAL,
        output_file=tmp_path / "out.xlsx",
        output_job_path=tmp_path / "job.json",
        llm_planner=planner,
    )


def test_a_rejected_draft_earns_one_repair_round(tmp_path: Path) -> None:
    """预算之内，修复轮照常进行 —— 这条能力不能被优化掉。"""

    planner = _CountingPlanner()
    _plan(tmp_path, planner)

    assert planner.planning_calls == 2, "草案被拒后没有再问一次"


def test_a_slow_first_round_cancels_the_repair_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第一轮已经花光了用户的耐心，就别再开第二轮。"""

    monkeypatch.setenv("DATA_AGENT_PLANNING_BUDGET_SECONDS", "0.05")
    planner = _CountingPlanner(delay=0.1)
    result = _plan(tmp_path, planner)

    assert planner.planning_calls == 1, "超预算之后仍然开了修复轮"
    # 放弃修复轮不等于任务失败：确定性方案本来就是一份有效的计划。
    assert result.job_config.get("base_table")


def test_giving_up_on_the_repair_round_is_recorded_not_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """跳过要留痕。否则「今天为什么规划得差一点」无从查起。"""

    monkeypatch.setenv("DATA_AGENT_PLANNING_BUDGET_SECONDS", "0.05")

    result = _plan(tmp_path, _CountingPlanner(delay=0.1))

    statuses = [] if result.llm_attempts.empty else list(result.llm_attempts["status"])
    assert "skipped" in statuses, f"跳过没有留下记录：{statuses}"


def test_an_unparseable_budget_falls_back_to_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配错了环境变量不该让规划悄悄降级。"""

    monkeypatch.setenv("DATA_AGENT_PLANNING_BUDGET_SECONDS", "不是数字")
    planner = _CountingPlanner()
    _plan(tmp_path, planner)

    assert planner.planning_calls == 2, "配置写错就不修复了，等于悄悄降级"
