"""业务意图验收矩阵 —— 判定「做到了没有」的唯一标准。

每个场景都是业务人员真的说得出口的一句话或一段话。断言的不是内部结构，而是
**拿到手的工作簿能不能直接用**：
  - 他要的东西必须在里面
  - 他没要的东西不能在里面
  - 数据不能悄悄变少

全部在**确定性路径**（关闭 LLM）下运行，因为这是不依赖模型也必须守住的底线。
配置了模型只会让更多同义表述命中，不会放松这里的任何一条。
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import pytest

from data_agent.services import answer_input_paths

DELIVERY_SHEET = "处理结果"
AGGREGATE_SHEET = "汇总结果"


@pytest.fixture(autouse=True)
def deterministic_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_AGENT_LLM_ENABLED", "0")


@pytest.fixture
def business_data(tmp_path: Path) -> Path:
    """一份中文业务数据：订单明细 + 客户档案，跨表键是「客户编号」。"""

    input_dir = tmp_path / "input"
    input_dir.mkdir()
    pd.DataFrame(
        {
            "订单号": ["O1", "O2", "O3", "O4", "O5"],
            "客户编号": ["C1", "C2", "C1", "C3", "C2"],
            "城市": ["北京", "上海", "北京", "广州", "上海"],
            "金额": [1200, 300, 800, 2500, 150],
        }
    ).to_excel(input_dir / "订单明细.xlsx", index=False)
    pd.DataFrame(
        {
            "客户编号": ["C1", "C2", "C3"],
            "客户名称": ["甲公司", "乙公司", "丙公司"],
        }
    ).to_excel(input_dir / "客户档案.xlsx", index=False)
    return input_dir


def _deliver(business_data: Path, tmp_path: Path, goal: str, name: str) -> dict:
    result = answer_input_paths(
        [business_data],
        goal=goal,
        output_dir=tmp_path / name,
        confirm_destructive=True,
    )
    book = tmp_path / name / "final_result.xlsx"
    excel = pd.ExcelFile(book)
    return {
        "answer": result.answer,
        "sheets": excel.sheet_names,
        "frames": {
            sheet: pd.read_excel(book, sheet_name=sheet) for sheet in excel.sheet_names
        },
    }


# --- 1. 打标 ---------------------------------------------------------------


def test_labeling_adds_a_column_and_never_drops_rows(
    business_data: Path, tmp_path: Path
) -> None:
    """「标记为 X」是加一列，不是删行。

    打标条件曾被当成过滤条件执行：5 行只剩 2 行，且没有任何标记列——该做的没做，
    不该做的做了。
    """

    out = _deliver(
        business_data,
        tmp_path,
        "给订单打标：金额大于 1000 的标记为大额订单，其余标记为普通订单",
        "label",
    )
    delivered = out["frames"][DELIVERY_SHEET]

    assert len(delivered) == 5, "打标不能减少记录"
    source_columns = {"订单号", "客户编号", "城市", "金额"}
    label_columns = [c for c in delivered.columns if c not in source_columns]
    assert label_columns, "必须新增一个标记列"

    labels = dict(zip(delivered["订单号"], delivered[label_columns[0]], strict=True))
    assert labels["O1"] == labels["O4"] == "大额订单"      # 1200 / 2500
    assert labels["O2"] == labels["O3"] == labels["O5"] == "普通订单"  # 300 / 800 / 150


# --- 2. 跨表关联 -----------------------------------------------------------


def test_lookup_brings_the_requested_column_across(
    business_data: Path, tmp_path: Path
) -> None:
    """中文列名下的 VLOOKUP 必须真的发生。"""

    out = _deliver(
        business_data, tmp_path, "把客户名称从客户档案关联到订单明细上", "lookup"
    )
    delivered = out["frames"][DELIVERY_SHEET]

    assert "客户名称" in delivered.columns
    assert len(delivered) == 5
    names = dict(zip(delivered["订单号"], delivered["客户名称"], strict=True))
    assert names["O1"] == "甲公司" and names["O2"] == "乙公司" and names["O4"] == "丙公司"


# --- 3. 分析重构 -----------------------------------------------------------


def test_aggregation_delivers_a_summary_table_not_raw_rows(
    business_data: Path, tmp_path: Path
) -> None:
    """「按城市统计」要的是一张按城市的汇总表，不是原始明细。

    透视步骤本来就在执行计划里，但 OutputSpec 只能表达一张行级明细表，聚合结果
    进不了工作簿，用户拿到的还是 5 行原始数据。
    """

    out = _deliver(business_data, tmp_path, "按城市统计订单总金额和订单数量", "aggregate")

    assert AGGREGATE_SHEET in out["sheets"], f"缺少汇总表，实际 sheets={out['sheets']}"
    summary = out["frames"][AGGREGATE_SHEET]
    assert len(summary) == 3, "北京/上海/广州 三组"
    assert "城市" in summary.columns

    by_city = summary.set_index("城市")
    amount_column = next(c for c in summary.columns if c != "城市" and "金额" in str(c))
    assert by_city.loc["北京", amount_column] == 2000   # 1200 + 800
    assert by_city.loc["上海", amount_column] == 450    # 300 + 150
    assert by_city.loc["广州", amount_column] == 2500


# --- 4. 一段话，多个诉求 ---------------------------------------------------


def test_a_paragraph_with_three_asks_delivers_all_three(
    business_data: Path, tmp_path: Path
) -> None:
    """业务人员不会只说一句话。三个诉求要么都做到，要么明确告诉他哪个没做。"""

    out = _deliver(
        business_data,
        tmp_path,
        "我要做经营分析。先把客户名称补齐，然后给金额大于 1000 的订单打上大额订单标记，"
        "最后按城市汇总每个城市的订单总金额和订单数。",
        "paragraph",
    )
    delivered = out["frames"][DELIVERY_SHEET]

    assert len(delivered) == 5, "没有任何一句话要求删除记录"
    assert "客户名称" in delivered.columns, "① 关联未执行"
    assert any("大额订单" == str(v) for v in delivered.values.ravel()), "② 打标未执行"
    assert AGGREGATE_SHEET in out["sheets"], "③ 汇总未交付"


# --- 5. 产出不含过程字段 ---------------------------------------------------


def test_delivered_sheet_contains_no_internal_columns(
    business_data: Path, tmp_path: Path
) -> None:
    """交付表里不能出现用户看不懂的过程字段。"""

    out = _deliver(
        business_data, tmp_path, "把客户名称从客户档案关联到订单明细上", "clean_columns"
    )
    for column in out["frames"][DELIVERY_SHEET].columns:
        name = str(column)
        assert not name.startswith("_"), f"内部列泄漏：{name}"
        assert "lookup" not in name.lower(), f"内部列泄漏：{name}"
        assert not name.endswith("_std"), f"内部列泄漏：{name}"


# --- 6. 没做到的事必须扣分并说明 -------------------------------------------


def test_an_unmet_goal_is_scored_down_and_explained(tmp_path: Path) -> None:
    """做不到的事必须扣分并写进结论——而不是拿着满分交一份没做事的结果。

    质量评分只衡量数据干净度，所以一个三项诉求全部落空的任务也能拿 100 分，
    反思闭环因此永远发现不了「任务没做对」。
    """

    input_dir = tmp_path / "input"
    input_dir.mkdir()
    # 只有一张表，且没有任何可关联的第二张表：关联诉求注定无法满足。
    pd.DataFrame({"订单号": ["O1", "O2"], "金额": [100, 200]}).to_excel(
        input_dir / "订单明细.xlsx", index=False
    )

    result = answer_input_paths(
        [input_dir],
        goal="把客户名称关联到订单明细上",
        output_dir=tmp_path / "unmet",
        confirm_destructive=True,
    )

    assert result.answer.data_quality_score < 100, "目标没达成不该拿满分"
    explanation = " ".join(
        [result.answer.result_summary, *result.answer.key_findings, *result.answer.assumptions]
    )
    assert "关联" in explanation, f"必须说明关联未执行，实际结论：{explanation}"


# --- 7. 阈值必须读成数字 ---------------------------------------------------


@pytest.mark.parametrize(
    "goal",
    [
        "只保留金额超过 1000 的订单",
        "只保留金额超过 1000 元的订单",
        "只保留金额超过 1,000 的大额订单",
        "只保留金额大于 1000 的订单",
    ],
)
def test_a_threshold_is_read_as_a_number_not_as_the_rest_of_the_sentence(
    business_data: Path, tmp_path: Path, goal: str
) -> None:
    """中文把被描述的东西挂在条件后面：「超过 1000 的订单」里「的订单」是名词。

    贪婪匹配曾把整条尾巴当成阈值，于是最普通不过的一句话直接让任务崩溃：
    `could not convert string to float: '1000的订单'`。
    """

    out = _deliver(business_data, tmp_path, goal, "threshold")
    delivered = out["frames"][DELIVERY_SHEET]

    assert list(delivered["金额"]) == [1200, 2500], f"阈值没被当成 1000：{list(delivered['金额'])}"


# --- 8. 打标的同义说法不能变成删行 -----------------------------------------


@pytest.mark.parametrize(
    "verb",
    ["标成", "标记成", "标记为", "标为", "算作", "视为", "当作", "定为", "列为", "归为"],
)
def test_every_way_of_saying_label_adds_a_column_instead_of_deleting_rows(
    business_data: Path, tmp_path: Path, verb: str
) -> None:
    """业务人员说「标成」和说「标记为」是同一件事，结果不能一个加列一个删行。

    词表漏掉「标成」时，这句话被解析成 filter_rows：用户只想打标，5 行却被删到 2 行。
    """

    out = _deliver(
        business_data, tmp_path, f"金额大于 1000 的{verb}大额订单，其余{verb}普通订单", f"v_{verb}"
    )
    delivered = out["frames"][DELIVERY_SHEET]

    assert len(delivered) == 5, f"「{verb}」被当成了删行：只剩 {len(delivered)} 行"
    values = {str(v) for v in delivered.values.ravel()}
    assert "大额订单" in values and "普通订单" in values, f"「{verb}」没有产生标记：{values}"


# --- 9. 关联来的字段也能当汇总维度 -----------------------------------------


@pytest.mark.parametrize("dimension", ["所属大区", "大区"])
def test_a_summary_can_group_by_a_field_the_lookup_brought_across(
    tmp_path: Path, dimension: str
) -> None:
    """「关联出大区，再按大区汇总」是最常见的一句话，必须真的产出汇总表。

    汇总维度原本只在基表字段里找，于是关联过来的 所属大区 永远匹配不上：关联做了、
    打标做了，唯独用户真正要的那张分析表悄悄没了。业务人员还习惯说简称（大区 →
    所属大区），所以列名的后缀也要认。
    """

    input_dir = tmp_path / "input"
    input_dir.mkdir()
    pd.DataFrame(
        {
            "订单号": ["O1", "O2", "O3"],
            "客户编号": ["C1", "C2", "C1"],
            "金额": [1000, 300, 500],
        }
    ).to_excel(input_dir / "订单明细.xlsx", index=False)
    pd.DataFrame(
        {"客户编号": ["C1", "C2"], "所属大区": ["华北", "华东"]}
    ).to_excel(input_dir / "客户档案.xlsx", index=False)

    result = answer_input_paths(
        [input_dir],
        goal=f"把所属大区从客户档案关联过来，再按{dimension}汇总订单金额合计",
        output_dir=tmp_path / f"agg_{dimension}",
        confirm_destructive=True,
    )
    assert result is not None

    book = tmp_path / f"agg_{dimension}" / "final_result.xlsx"
    sheets = pd.ExcelFile(book).sheet_names
    assert AGGREGATE_SHEET in sheets, f"没有交付汇总表，只有 {sheets}"

    summary = pd.read_excel(book, sheet_name=AGGREGATE_SHEET)
    totals = dict(zip(summary["所属大区"], summary["金额合计"], strict=False))
    assert totals == {"华北": 1500, "华东": 300}, f"汇总数字不对：{totals}"


# --- 10. 关联不能把明细行压没 ----------------------------------------------


@pytest.mark.parametrize(
    "goal",
    [
        "把所属大区从客户档案关联过来",
        "把订单明细和客户档案关联起来，补上所属大区",
        "订单明细关联客户档案，带出所属大区",
    ],
)
def test_a_lookup_keeps_every_detail_row(tmp_path: Path, goal: str) -> None:
    """关联是给明细补字段，不是把明细并进档案表。

    主表选错时，3 条订单 join 进 2 个客户，交付回来就是 2 行——每个客户只剩第一单，
    用户既没要求删行，也没有任何地方提示行没了。
    """

    input_dir = tmp_path / "input"
    input_dir.mkdir()
    pd.DataFrame(
        {"订单号": ["O1", "O2", "O3"], "客户编号": ["C1", "C2", "C1"], "金额": [1000, 300, 500]}
    ).to_excel(input_dir / "订单明细.xlsx", index=False)
    pd.DataFrame(
        {"客户编号": ["C1", "C2"], "所属大区": ["华北", "华东"]}
    ).to_excel(input_dir / "客户档案.xlsx", index=False)

    result = answer_input_paths(
        [input_dir], goal=goal, output_dir=tmp_path / "keep", confirm_destructive=True
    )
    assert result is not None

    delivered = pd.read_excel(tmp_path / "keep" / "final_result.xlsx", sheet_name=DELIVERY_SHEET)
    assert sorted(delivered["订单号"]) == ["O1", "O2", "O3"], (
        f"关联把明细行压没了，只剩 {len(delivered)} 行：{delivered.to_dict('records')}"
    )


# --- 11. 全角/空格不该让一条订单掉出交付 -----------------------------------


def test_a_full_width_key_still_matches_its_customer(tmp_path: Path) -> None:
    """中文输入法随手打出的 Ｃ002 和 C002 在人眼里是同一个客户，机器也必须这么看。

    normalized_exact 只做了 strip+lower，全角字母折不下来，于是这一单被判成
    「关联数据未匹配」，直接从交付结果里掉了出去——用户既没要求删它，也看不到它去哪了。
    """

    input_dir = tmp_path / "input"
    input_dir.mkdir()
    pd.DataFrame(
        {"订单号": ["O1", "O2"], "客户编号": [" C001 ", "Ｃ002"], "金额": [100, 200]}
    ).to_excel(input_dir / "订单明细.xlsx", index=False)
    pd.DataFrame(
        {"客户编号": ["C001", "C002"], "客户名称": ["甲公司", "乙公司"]}
    ).to_excel(input_dir / "客户档案.xlsx", index=False)

    result = answer_input_paths(
        [input_dir],
        goal="把客户名称从客户档案关联到订单明细上",
        output_dir=tmp_path / "fullwidth",
        confirm_destructive=True,
    )
    assert result is not None

    delivered = pd.read_excel(
        tmp_path / "fullwidth" / "final_result.xlsx", sheet_name=DELIVERY_SHEET
    )
    names = dict(zip(delivered["订单号"], delivered["客户名称"], strict=False))
    assert names == {"O1": "甲公司", "O2": "乙公司"}, f"全角键没匹配上：{names}"


# --- 7. 意图即权限：没要求就不许动 -------------------------------------------


def test_unmatched_rows_stay_in_the_delivery_when_nobody_asked_to_review_them(
    business_data: Path, tmp_path: Path
) -> None:
    """只要求补一列，就不能少给行。

    ``exception_policy`` 的三个开关曾是系统默认常量（全开），所以关联不上的记录
    一律被扣下、只写进内部文件。用户要的是「把客户名称补上」，拿回来的却比上传的
    行数少——扣行是对用户数据的判断，没人授权过。
    """

    extra = pd.read_excel(business_data / "订单明细.xlsx")
    extra.loc[len(extra)] = ["O6", "C99", "西安", 900]  # 档案里没有的客户
    extra.to_excel(business_data / "订单明细.xlsx", index=False)

    delivered = _deliver(
        business_data, tmp_path, "把客户名称关联过来", "no_withholding"
    )
    result = delivered["frames"][DELIVERY_SHEET]

    assert len(result) == 6, "关联不上的行被悄悄扣下了"
    assert "O6" in set(result["订单号"]), "用户没要求删除的记录必须还在"
    assert result.loc[result["订单号"].eq("O6"), "客户名称"].isna().all(), (
        "匹配不上就留空，不能编造，也不能因此丢行"
    )


def test_asking_to_see_problem_records_does_hold_them_back(
    business_data: Path, tmp_path: Path
) -> None:
    """要求「列出异常供复核」时，扣行才是用户要的东西。"""

    extra = pd.read_excel(business_data / "订单明细.xlsx")
    extra.loc[len(extra)] = ["O6", "C99", "西安", 900]
    extra.to_excel(business_data / "订单明细.xlsx", index=False)

    delivered = _deliver(
        business_data,
        tmp_path,
        "把客户名称关联过来，并列出关联不上的异常记录供我复核",
        "withholding",
    )

    assert "问题说明" in delivered["sheets"], "用户点名要看的问题清单必须出现"
    result = delivered["frames"][DELIVERY_SHEET]
    assert "O6" not in set(result["订单号"]), "被点名复核的记录不应混在可用结果里"


def test_asking_to_deduplicate_actually_removes_the_duplicates(
    business_data: Path, tmp_path: Path
) -> None:
    """要求去重就直接去重，交付的是干净表，不是一份「建议你去重」的说明。"""

    duplicated = pd.read_excel(business_data / "订单明细.xlsx")
    duplicated.loc[len(duplicated)] = duplicated.iloc[0].tolist()
    duplicated.to_excel(business_data / "订单明细.xlsx", index=False)

    delivered = _deliver(
        business_data, tmp_path, "把订单明细里重复的记录去掉，只保留一条", "dedupe"
    )
    result = delivered["frames"][DELIVERY_SHEET]

    assert len(result) == 5, f"重复行没有被真的删掉：{len(result)} 行"
    assert result["订单号"].is_unique


def test_the_problem_sheet_reports_only_the_problem_that_was_asked_about(
    business_data: Path, tmp_path: Path
) -> None:
    """点名问哪一类问题，就只报那一类。

    「列出关联不上的异常记录」曾把十个检测器的全部产出倒给用户：备注 为空、
    duplicate_客户编号（明细表里外键本来就重复）、数字存成文本……每一行都命中了
    其中之一，于是全部被扣下，交付表是空的，而用户真正问的那一行淹没在里面。
    """

    extra = pd.read_excel(business_data / "订单明细.xlsx")
    extra.loc[len(extra)] = ["O6", "C99", "西安", -900]  # 关联不上，且金额为负
    extra.to_excel(business_data / "订单明细.xlsx", index=False)

    delivered = _deliver(
        business_data,
        tmp_path,
        "把客户名称关联过来，并列出关联不上的记录供我复核",
        "scoped_problems",
    )
    problems = delivered["frames"]["问题说明"]

    assert len(problems) == 1, f"报了用户没问的问题：{len(problems)} 行"
    assert "O6" in set(problems["订单号"])
    reported = "；".join(problems["问题类型"].astype(str))
    assert "关联" in reported
    assert "负" not in reported, "金额为负没人问起，不该出现在问题说明里"
    assert len(delivered["frames"][DELIVERY_SHEET]) == 5, "只该扣下被点名的那一行"


def test_a_lookup_goal_does_not_edit_cells_nobody_asked_about(
    business_data: Path, tmp_path: Path
) -> None:
    """没要求清洗，就一个字符都不改。

    capabilities 曾在目标没点名任何动作时兜底成「就地清洗」，于是「把客户名称关联
    过来」顺手把整张表的前后空格、不可见字符、全角字符全改了一遍。用户要的是补一
    列，其余每一处改动都是 agent 自己的主意。
    """

    messy = pd.read_excel(business_data / "订单明细.xlsx")
    messy.loc[0, "客户编号"] = " C1 "  # 前后空格，且仍应匹配得上
    messy.to_excel(business_data / "订单明细.xlsx", index=False)

    delivered = _deliver(business_data, tmp_path, "把客户名称关联过来", "no_edit")
    result = delivered["frames"][DELIVERY_SHEET]

    assert result.loc[0, "客户编号"] == " C1 ", "用户没要求清洗，原值被擅自改了"
    assert result.loc[0, "客户名称"] == "甲公司", "匹配本就该忽略空格，不必改原值"


def test_cleaning_happens_when_it_is_actually_requested(
    business_data: Path, tmp_path: Path
) -> None:
    """点名要清洗，就真的清洗 —— 这是功能，只是必须由用户发起。"""

    messy = pd.read_excel(business_data / "订单明细.xlsx")
    messy.loc[0, "城市"] = "  北京  "
    messy.to_excel(business_data / "订单明细.xlsx", index=False)

    delivered = _deliver(
        business_data, tmp_path, "清洗订单明细，去掉文本前后空格", "asked_to_clean"
    )
    assert delivered["frames"][DELIVERY_SHEET].loc[0, "城市"] == "北京"


def test_no_problems_are_volunteered_when_none_were_asked_about(
    business_data: Path, tmp_path: Path
) -> None:
    """没问起问题，就不要主动上报。"""

    extra = pd.read_excel(business_data / "订单明细.xlsx")
    extra.loc[len(extra)] = ["O6", "C99", "西安", -900]
    extra.to_excel(business_data / "订单明细.xlsx", index=False)

    delivered = _deliver(business_data, tmp_path, "把客户名称关联过来", "no_reports")

    assert "问题说明" not in delivered["sheets"]
    assert len(delivered["frames"][DELIVERY_SHEET]) == 6


# --- 8. 分析类目标：问了就要答得准 -------------------------------------------


def test_an_analysis_goal_summarises_instead_of_withholding_rows(
    business_data: Path, tmp_path: Path
) -> None:
    """「有什么问题」是在问，不是在授权扣行。

    这句话曾被当成「把有问题的记录挑出来」，15 行里 14 行被移出交付、丢进一张没人
    要的复核表；同时「按城市看看销售情况」没有产出任何汇总 —— 问的没答，没问的做了。
    """

    delivered = _deliver(
        business_data,
        tmp_path,
        "帮我分析一下这份订单数据，有什么问题，按城市看看销售情况",
        "analysis",
    )

    assert len(delivered["frames"][DELIVERY_SHEET]) == 5, "分析请求不该扣下任何一行"
    assert AGGREGATE_SHEET in delivered["sheets"], "「按城市看看销售情况」必须给出汇总"
    summary = delivered["frames"][AGGREGATE_SHEET]
    assert "城市" in summary.columns
    totals = dict(zip(summary["城市"], summary["金额合计"], strict=False))
    assert totals == {"北京": 2000, "上海": 450, "广州": 2500}, (
        "「销售情况」问的是金额，不能用记录数搪塞"
    )


def test_findings_are_written_in_business_language_without_repeats(
    business_data: Path, tmp_path: Path
) -> None:
    """结论里不能出现内部标签，也不能同一个问题报两遍。"""

    result = answer_input_paths(
        [business_data],
        goal="帮我分析一下这份数据有什么问题",
        output_dir=tmp_path / "findings",
        confirm_destructive=True,
    )
    findings = list(result.answer.key_findings)

    assert findings == list(dict.fromkeys(findings)), "同一条结论重复出现"
    for finding in findings:
        assert not re.search(r"[a-z]+_[a-z_]+", finding), f"内部标签泄漏到结论：{finding}"


def test_an_action_only_the_model_proposed_is_not_reported_as_the_users(
    business_data: Path, tmp_path: Path
) -> None:
    """模型自作主张的动作失败了，不能说成「你要求的」，更不该为此扣分。"""

    result = answer_input_paths(
        [business_data],
        goal="按城市统计订单金额合计",
        output_dir=tmp_path / "no_false_unmet",
        confirm_destructive=True,
    )

    assert "未能执行" not in result.answer.result_summary
    assert not any("未能执行" in str(item) for item in result.answer.key_findings)


def test_totals_rows_are_not_counted_into_the_totals(
    business_data: Path, tmp_path: Path
) -> None:
    """合计行和重复表头行留在明细里，但不进汇总。

    上传文件里的「合计」行本来会被加进它自己汇总的那个总额，重复表头行则变成一个
    叫「城市」的分类。两者都不是记录，把它们算进去就是算错；而删掉它们没人授权，
    所以明细照旧保留。
    """

    orders = pd.read_excel(business_data / "订单明细.xlsx")
    orders.loc[len(orders)] = ["订单号", "客户编号", "城市", "金额"]  # 混入的重复表头
    orders.loc[len(orders)] = ["合计", "", "", 5250]  # 混入的合计行
    orders.to_excel(business_data / "订单明细.xlsx", index=False)

    delivered = _deliver(business_data, tmp_path, "按城市统计订单金额合计", "totals_row")
    detail = delivered["frames"][DELIVERY_SHEET]
    summary = delivered["frames"][AGGREGATE_SHEET]

    assert len(detail) == 7, "没人要求删掉这两行，明细里必须还在"
    assert "合计" in set(detail["订单号"].astype(str))

    totals = dict(zip(summary["城市"], summary["金额合计"], strict=False))
    assert totals == {"北京": 2000, "上海": 450, "广州": 2500}
    assert "城市" not in totals, "重复表头行变成了一个分类"
    assert summary["金额合计"].sum() == 4950, "合计行被重复计入了总额"


# --- 9. 删行必须是明说的 -----------------------------------------------------


def test_a_bare_condition_is_not_permission_to_delete_rows(
    business_data: Path, tmp_path: Path
) -> None:
    """「金额超过5000的订单」是在指一批数据，不是在下删除令。

    裸条件曾被执行成「其余全删」，连「看看金额超过5000的订单」——明说只是想看——
    也照删不误。删行是交付物里唯一不可逆的操作，必须由动词授权。
    """

    for name, goal in (
        ("bare", "金额大于1000的订单"),
        ("look", "看看金额大于1000的订单"),
    ):
        delivered = _deliver(business_data, tmp_path, goal, name)
        assert len(delivered["frames"][DELIVERY_SHEET]) == 5, f"「{goal}」删了行"


def test_delete_means_delete_those_rows_not_keep_them(
    business_data: Path, tmp_path: Path
) -> None:
    """「把金额低于500的删掉」删的是低于 500 的，不是别的。

    「删掉」不在词表里，于是判定落到默认的 keep 分支：该删的两行被留下，该留的
    三行被删光 —— 结果与要求完全相反。
    """

    delivered = _deliver(
        business_data, tmp_path, "把金额低于500的订单删掉", "delete_means_delete"
    )
    result = delivered["frames"][DELIVERY_SHEET]

    assert set(result["订单号"]) == {"O1", "O3", "O4"}
    assert result["金额"].min() >= 500


def test_delete_leaves_rows_it_could_not_evaluate(
    business_data: Path, tmp_path: Path
) -> None:
    """删除条件判不了的行，不算「符合条件」。

    「删掉金额小于0的」曾被编译成「保留金额 >= 0」，而空值和非数字两边都不满足，
    于是它们也一并消失 —— 删的比要求的多。
    """

    orders = pd.read_excel(business_data / "订单明细.xlsx")
    orders.loc[len(orders)] = ["O6", "C1", "北京", None]  # 金额为空
    orders.loc[len(orders)] = ["O7", "C2", "上海", -50]  # 真的小于 0
    orders.to_excel(business_data / "订单明细.xlsx", index=False)

    delivered = _deliver(
        business_data, tmp_path, "把金额小于0的订单删掉", "delete_unknown"
    )
    kept = set(delivered["frames"][DELIVERY_SHEET]["订单号"])

    assert "O7" not in kept, "该删的没删"
    assert "O6" in kept, "金额为空的行没被要求删除，却被删了"


def test_a_model_proposed_filter_cannot_delete_rows_the_goal_never_authorised(
    business_data: Path, tmp_path: Path
) -> None:
    """模型可以细化「怎么删」，但决定「删不删」的只能是用户。

    确定性层已正确判定「金额超过5000的订单」不是删除指令，模型却补了一条过滤，
    15 行里 12 行照删不误。
    """

    import pandas as pd_

    from data_agent.agent.task_spec import build_task_spec
    from data_agent.planning.goal_interpreter import interpret_goal

    tables = {
        "订单明细": pd_.DataFrame(
            {"订单号": ["O1", "O2"], "金额": [8600.0, 450.0]}
        )
    }
    goal = "金额超过5000的订单"
    goal_plan = interpret_goal(goal, tables)
    goal_plan["filters"] = [{"field": "金额", "op": "gt", "value": 5000, "mode": "keep"}]

    spec = build_task_spec(goal, goal_plan, tables)
    assert spec.filters == [], "模型自作主张的过滤被当成了用户指令"
    assert "filter" not in spec.actions


# --- 10. 变更清单：可核对「确实没多动」 --------------------------------------


def test_a_read_only_goal_produces_no_change_manifest(
    business_data: Path, tmp_path: Path
) -> None:
    """没动过就没有清单 —— 空表本身就是噪音。"""

    delivered = _deliver(business_data, tmp_path, "把客户名称关联过来", "no_manifest")
    assert "本次改动" not in delivered["sheets"]


def test_edits_are_accounted_for_in_business_language(
    business_data: Path, tmp_path: Path
) -> None:
    """改了什么必须能一眼核对，而且写的是人话。

    改动记录本来只进内部审计工作簿：把表格交给 agent 之后最该能验证的一件事 ——
    「别的地方没被动过」—— 用户反而看不到。
    """

    messy = pd.read_excel(business_data / "订单明细.xlsx")
    messy.loc[0, "客户编号"] = " C1 "
    messy.to_excel(business_data / "订单明细.xlsx", index=False)

    delivered = _deliver(
        business_data,
        tmp_path,
        "清洗订单明细，去掉前后空格，并列出本次改了什么",
        "manifest_edit",
    )
    manifest = delivered["frames"]["本次改动"]

    assert list(manifest.columns) == ["改动类型", "字段", "影响行数", "说明"]
    assert (manifest["字段"] == "客户编号").any()
    assert manifest["影响行数"].sum() >= 1
    text = " ".join(manifest.astype(str).to_numpy().ravel())
    assert not re.search(r"[a-z]{3,}_[a-z_]+", text), f"内部标签泄漏到改动清单：{text}"


def test_removals_say_which_rule_removed_them(
    business_data: Path, tmp_path: Path
) -> None:
    """删了几行、按什么规则删的，要写清楚。"""

    delivered = _deliver(
        business_data,
        tmp_path,
        "只保留金额超过1000的订单，并说明改动",
        "manifest_removal",
    )
    manifest = delivered["frames"]["本次改动"]
    removal = manifest[manifest["改动类型"].eq("删除行")]

    assert len(removal) == 1
    assert int(removal.iloc[0]["影响行数"]) == 3
    assert "金额" in str(removal.iloc[0]["说明"])


# --- 11. Excel 做起来费劲的：时间分桶与组内排名 ------------------------------


@pytest.fixture
def dated_orders(tmp_path: Path) -> Path:
    """跨月份的订单，日期格式故意不统一 —— 真实表格就是这样。"""

    input_dir = tmp_path / "dated"
    input_dir.mkdir()
    pd.DataFrame(
        {
            "订单号": ["O1", "O2", "O3", "O4", "O5"],
            "下单日期": ["2024-01-05", "2024/01/20", "2024.02.03", "2024-02-18", "2024-05-09"],
            "城市": ["北京", "上海", "北京", "上海", "北京"],
            "销售员": ["张伟", "李娜", "王强", "李娜", "张伟"],
            "金额": [1000, 2000, 300, 400, 5000],
        }
    ).to_excel(input_dir / "订单明细.xlsx", index=False)
    return input_dir


def test_monthly_summary_groups_by_a_month_that_is_not_a_column(
    dated_orders: Path, tmp_path: Path
) -> None:
    """「按月统计」是业务里最高频的问法，而「月」不是任何一列。

    按原始日期分组会得到每天一行，等于没回答；这个维度是为汇总临时派生的，
    不会往明细表里加一列 月份 让用户去猜。
    """

    delivered = _deliver(dated_orders, tmp_path, "按月统计订单金额合计", "by_month")
    summary = delivered["frames"][AGGREGATE_SHEET]

    assert dict(zip(summary["月份"], summary["金额合计"], strict=False)) == {
        "2024-01": 3000,
        "2024-02": 700,
        "2024-05": 5000,
    }
    assert "月份" not in delivered["frames"][DELIVERY_SHEET].columns


def test_quarterly_summary_reads_as_a_quarter(
    dated_orders: Path, tmp_path: Path
) -> None:
    delivered = _deliver(dated_orders, tmp_path, "按季度统计销售额", "by_quarter")
    summary = delivered["frames"][AGGREGATE_SHEET]

    assert dict(zip(summary["季度"], summary["金额合计"], strict=False)) == {
        "2024-Q1": 3700,
        "2024-Q2": 5000,
    }


def test_top_n_within_each_group_is_ranked_per_group(
    dated_orders: Path, tmp_path: Path
) -> None:
    """「每个城市金额最高的前1个销售员」是每组一名，不是全表一名。

    这正是 Excel 需要数组公式或 Power Query 才做得到的那件事。
    """

    delivered = _deliver(
        dated_orders, tmp_path, "每个城市金额最高的前1个销售员", "top_per_group"
    )
    summary = delivered["frames"][AGGREGATE_SHEET]

    assert len(summary) == 2, "组内排名退化成了全表排名"
    assert dict(zip(summary["城市"], summary["销售员"], strict=False)) == {
        "北京": "张伟",  # 1000 + 5000
        "上海": "李娜",  # 2000 + 400
    }


def test_two_dimensions_named_with_a_conjunction_both_count(
    dated_orders: Path, tmp_path: Path
) -> None:
    """「按城市和销售员统计」是两个维度，只读第一个等于答了一半。"""

    delivered = _deliver(
        dated_orders, tmp_path, "按城市和销售员统计金额合计", "two_dims"
    )
    summary = delivered["frames"][AGGREGATE_SHEET]

    assert list(summary.columns)[:2] == ["城市", "销售员"]
    assert len(summary) == 3


def test_the_model_cannot_authorise_edits_the_goal_never_asked_for(
    business_data: Path, tmp_path: Path
) -> None:
    """改动用户的数据要有用户的原话为凭，模型自己说了不算。

    确定性层对「把客户名称关联过来」正确判定 wants_inplace=False，模型却把它设成
    True，于是整张表的前后空格和全角字符被悄悄改了一遍 —— 而合并逻辑里还留着一份
    早已删除的「默认就地清洗」兜底。
    """

    from data_agent.agent.goal_understanding import _merge_overlay
    from data_agent.planning.goal_interpreter import derive_capabilities

    tables = {"订单明细": pd.read_excel(business_data / "订单明细.xlsx")}
    goal = "把客户名称关联过来"
    baseline = {"capabilities": derive_capabilities(goal, tables)}
    assert baseline["capabilities"]["wants_inplace"] is False

    merged = _merge_overlay(
        baseline,
        {"capabilities": {"wants_inplace": True, "needs_lookup": True}},
        tables,
        goal,
    )

    assert merged["capabilities"]["wants_inplace"] is False, "模型擅自授权了改动"
    assert merged["capabilities"]["needs_lookup"] is True, "非破坏性能力仍应由模型决定"

    # 编造一段目标里没有的话，同样不算授权。
    forged = _merge_overlay(
        baseline,
        {
            "capabilities": {"wants_inplace": True},
            "capability_evidence": {"wants_inplace": "顺便把表清洗一下"},
        },
        tables,
        goal,
    )
    assert forged["capabilities"]["wants_inplace"] is False, "伪造的引用被当成了授权"

    # 反方向同样不许：用户明说要清洗时，模型不能把它关掉。
    clean_goal = "清洗订单明细，去掉前后空格"
    clean_baseline = {"capabilities": derive_capabilities(clean_goal, tables)}
    assert clean_baseline["capabilities"]["wants_inplace"] is True
    still_cleans = _merge_overlay(
        clean_baseline, {"capabilities": {"wants_inplace": False}}, tables, clean_goal
    )
    assert still_cleans["capabilities"]["wants_inplace"] is True, "模型取消了用户要的清洗"


def test_a_cleaning_request_the_token_table_misses_still_gets_done(
    business_data: Path,
) -> None:
    """「把这份表整理一下」—— 词表里没有「整理」，但用户确实要求了。

    以前模型对这两个能力没有投票权，所以词表漏掉的说法就等于没说过：用户要求整理，
    agent 一个字段都不动，还看不出哪里出了问题。现在模型引得出原话就算数。
    """

    from data_agent.agent.goal_understanding import _merge_overlay
    from data_agent.planning.goal_interpreter import derive_capabilities

    tables = {"订单明细": pd.read_excel(business_data / "订单明细.xlsx")}
    goal = "把这份表整理一下"
    baseline = {"capabilities": derive_capabilities(goal, tables)}
    assert baseline["capabilities"]["wants_inplace"] is False, "词表本来就不认识「整理」"

    merged = _merge_overlay(
        baseline,
        {
            "capabilities": {"wants_inplace": True},
            "capability_evidence": {"wants_inplace": "把这份表整理一下"},
        },
        tables,
        goal,
    )

    assert merged["capabilities"]["wants_inplace"] is True, "用户要求整理，却什么都没做"


def test_a_column_the_user_uploaded_is_never_dropped_by_name(
    business_data: Path, tmp_path: Path
) -> None:
    """用户自己的列不能因为「重名」被整列删掉。

    交付视图有一张「过程列」名单用来去掉流水线自己加的批注列，其中包含「备注」。
    用户表里本来就有的 备注 列于是一起被删 —— 连同里面的真实内容。
    删一整列比删几行更严重，而且完全没人要求过。
    """

    orders = pd.read_excel(business_data / "订单明细.xlsx")
    orders["备注"] = ["客户要求加急", "", "退货", "", "新客户待建档"]
    orders.to_excel(business_data / "订单明细.xlsx", index=False)

    delivered = _deliver(business_data, tmp_path, "把客户名称关联过来", "keep_column")
    result = delivered["frames"][DELIVERY_SHEET]

    assert "备注" in result.columns, "用户上传的列被整列删掉了"
    assert "客户要求加急" in set(result["备注"].astype(str))
    assert "新客户待建档" in set(result["备注"].astype(str))


def test_clarification_answers_do_not_rewrite_the_users_goal() -> None:
    """澄清问题是 agent 自己的措辞，不能当成用户的目标读回来。

    问题文本「将客户档案.客户名称补充到订单明细中？」被整句拼进目标后，主表规则读到
    「客户档案」在前，于是选它当主表、关联步骤消失，任务以「处理计划未覆盖用户要求的
    关联字段」失败 —— 而同一个目标走 CLI 完全正常。目标是用户要求的记录，agent 生成
    的任何文字都不该进去。
    """

    from data_agent.api.job_manager import _fold_answers_into_config

    plan_state = {
        "goal": "把客户名称关联过来",
        "mode": "answer",
        "clarification_questions": [
            {
                "id": "slot_1",
                "question": "请确认是否以「客户编号」作为关联键，"
                "将「客户档案.客户名称」补充到「订单明细」中？",
            }
        ],
    }
    enriched = _fold_answers_into_config(
        {}, plan_state, [{"question_id": "slot_1", "answer": "是"}]
    )

    goal = enriched["goal"]
    assert goal.startswith("把客户名称关联过来")
    assert "是" in goal
    assert "客户档案" not in goal, "agent 的提问措辞混进了用户目标"
    assert "订单明细" not in goal
