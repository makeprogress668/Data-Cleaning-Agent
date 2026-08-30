from __future__ import annotations

import pandas as pd

from data_agent.tools.lookup import vlookup


def test_large_exact_lookup_uses_duckdb_and_preserves_contract(monkeypatch) -> None:
    monkeypatch.setenv("DATA_AGENT_DUCKDB_MIN_ROWS", "1")
    left = pd.DataFrame({"客户号": [" A ", "B", "", "C"], "名称": ["旧", "乙", "空", "丙"]})
    right = pd.DataFrame(
        {
            "编号": ["A", "B", "B"],
            "名称": ["甲", "乙一", "乙二"],
            "区域": ["北", "南", "南"],
        }
    )

    result = vlookup(
        left,
        right,
        left_key="客户号",
        right_key="编号",
        fields=["名称", "区域"],
        suffix="_关联",
        match_field="匹配成功",
        ambiguity_field="需复核",
        match_mode="trim",
        duplicate_strategy="review",
    )

    assert result.attrs["data_agent_engine"] == "duckdb"
    assert result["名称"].tolist() == ["旧", "乙", "空", "丙"]
    assert result["名称_关联"].fillna("").tolist() == ["甲", "乙一", "", ""]
    assert result["区域"].fillna("").tolist() == ["北", "南", "", ""]
    assert result["匹配成功"].tolist() == [True, True, False, False]
    assert result["需复核"].tolist() == [False, True, False, False]
