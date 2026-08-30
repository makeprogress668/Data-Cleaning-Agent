from pathlib import Path

import pandas as pd

from data_agent.tools.analysis import analyze_business_data
from data_agent.tools.charting import generate_business_charts


def test_analysis_engine_builds_business_findings_and_summary() -> None:
    business_result = pd.DataFrame(
        {
            "处理状态": ["通过", "需处理", "需处理", "通过"],
            "问题类型": ["", "未找到楼宇", "金额为空", ""],
            "城市": ["北京", "上海", "上海", "北京"],
            "金额": [100, None, 300, 200],
        }
    )
    final_result = business_result[business_result["处理状态"].eq("通过")]
    needs_review = business_result[business_result["处理状态"].eq("需处理")]
    quality_summary = pd.DataFrame(
        [{"指标": "数据质量评分", "值": 82}, {"指标": "严重问题类型数", "值": 1}]
    )
    exception_impact = pd.DataFrame(
        [
            {"异常类型": "未找到楼宇", "影响行数": 1, "严重级别": "错误", "建议处理": "补充主数据"},
            {"异常类型": "金额为空", "影响行数": 1, "严重级别": "提醒", "建议处理": "补齐金额"},
        ]
    )
    dirty_issue_summary = pd.DataFrame(
        [
            {
                "issue_type": "null_values",
                "field": "金额",
                "affected_rows": 1,
                "recommended_action": "补齐金额",
            }
        ]
    )
    relationships = pd.DataFrame(
        [
            {
                "left_table": "raw",
                "left_field": "sitecode",
                "right_table": "buildings",
                "right_field": "sitecode",
                "left_match_rate": 0.75,
                "unmatched_left_count": 1,
                "right_duplicate_key_count": 0,
                "join_will_expand": False,
                "recommendation": "recommended_lookup",
            }
        ]
    )

    result = analyze_business_data(
        business_result=business_result,
        final_result=final_result,
        needs_review=needs_review,
        data_quality_summary=quality_summary,
        exception_impact=exception_impact,
        dirty_issue_summary=dirty_issue_summary,
        relationship_candidates=relationships,
        goal="输出可用楼宇清单",
    )

    assert "异常率分析" in set(result.summary["分析类型"])
    assert "exception_distribution" in result.details
    assert not result.details["match_rate"].empty
    assert any("可用率" in finding for finding in result.findings)


def test_chart_engine_writes_html_and_svg_charts(tmp_path: Path) -> None:
    details = {
        "record_status_distribution": pd.DataFrame(
            [
                {"处理状态": "可直接使用", "记录数": 3, "占比": 0.75},
                {"处理状态": "需要复核", "记录数": 1, "占比": 0.25},
            ]
        ),
        "exception_distribution": pd.DataFrame(
            [
                {"异常类型": "未找到楼宇", "影响行数": 2},
                {"异常类型": "金额为空", "影响行数": 1},
            ]
        ),
    }

    charts = generate_business_charts(details, charts_dir=tmp_path / "charts", output_dir=tmp_path)

    paths = {chart.path.as_posix() for chart in charts}
    assert "charts/record_status_distribution.html" in paths
    assert "charts/exception_distribution.html" in paths
    assert "charts/exception_distribution.svg" not in paths
    assert (tmp_path / "charts" / "exception_distribution.html").exists()
