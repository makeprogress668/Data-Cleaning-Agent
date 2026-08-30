import json
import logging
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from data_agent.agent import plan_from_goal, write_plan_artifacts
from data_agent.pipelines import profile_job_from_file, run_job_from_file
from data_agent.services import (
    DestructivePlanNotConfirmedError,
    answer_input_paths,
    discover_input_paths,
)
from data_agent.tools import (
    build_recommendation_package,
    export_profile_report,
    profile_dataset,
    read_tables_from_paths,
    write_recommended_job_config,
)

app = typer.Typer(help="Data cleaning agent CLI.")
console = Console()


@app.command("answer")
def answer(
    input_path: Annotated[
        Path,
        typer.Argument(help="Excel/CSV file or directory to clean, analyze, and deliver."),
    ],
    goal: Annotated[str, typer.Option("--goal", help="Business goal for this delivery.")],
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Business delivery output directory."),
    ] = Path("data/output"),
    include_audit: Annotated[
        bool,
        typer.Option("--include-audit", help="Export detailed audit package."),
    ] = False,
    debug: Annotated[
        bool,
        typer.Option("--debug", help="Print debug logs and include audit details."),
    ] = False,
    recursive: Annotated[
        bool,
        typer.Option("--recursive/--no-recursive", help="Scan directories recursively."),
    ] = True,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="预先批准计划记录的风险（高影响删行、影响无法估算或模糊匹配）。",
        ),
    ] = False,
) -> None:
    """Generate business-ready answer, final data, review list, and audit refs."""

    if debug:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(levelname)s %(name)s: %(message)s",
            force=True,
        )

    try:
        result = answer_input_paths(
            [input_path],
            goal=goal,
            output_dir=output,
            include_audit=include_audit,
            debug=debug,
            recursive=recursive,
            confirm_destructive=yes,
        )
    except DestructivePlanNotConfirmedError as exc:
        # Same layered policy as the console: authorization is checked during planning;
        # --yes approves only the risk reasons recorded on that exact plan.
        console.print(f"[yellow]需要确认：[/yellow]{exc}")
        raise typer.Exit(code=2) from exc

    console.print("[green]Business delivery generated:[/green]")
    for name, path in result.output_files.items():
        console.print(f"- {name}: {path}")


@app.command("discover")
def discover(
    input_path: Annotated[
        Path,
        typer.Argument(help="Excel/CSV file or directory to discover business value."),
    ],
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Discovery output directory."),
    ] = Path("data/output"),
    recursive: Annotated[
        bool,
        typer.Option("--recursive/--no-recursive", help="Scan directories recursively."),
    ] = True,
) -> None:
    """Discover business objects, relationships, quality issues, and next goals."""

    files = discover_input_paths([input_path], output_dir=output, recursive=recursive)
    console.print("[green]Business discovery generated:[/green]")
    console.print(f"- business_discovery_report.html: {files['business_discovery_report_html']}")
    console.print(f"- data_inventory.xlsx: {files['data_inventory']}")


@app.command("plan")
def plan(
    inputs: Annotated[
        list[Path],
        typer.Argument(help="Excel/CSV files or directories to scan and plan from."),
    ],
    goal: Annotated[str, typer.Option("--goal", help="Natural-language business goal.")],
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Planner review workbook output path."),
    ] = Path("data/output/planning_review.xlsx"),
    job_output: Annotated[
        Path,
        typer.Option("--job-output", help="Generated executable job config path."),
    ] = Path("configs/planned_cleaning_job.json"),
    result_output: Annotated[
        Path,
        typer.Option("--result-output", help="Output file path inside the generated job config."),
    ] = Path("data/output/planned_cleaning_result.xlsx"),
    recursive: Annotated[
        bool,
        typer.Option("--recursive/--no-recursive", help="Scan directories recursively."),
    ] = True,
    llm_candidate: Annotated[
        Optional[Path],
        typer.Option(
            "--llm-candidate",
            help="Optional JSON file containing an LLM proposed planning response.",
        ),
    ] = None,
) -> None:
    """Plan a validated JobConfig from a natural-language business goal."""

    llm_payload = None
    if llm_candidate:
        llm_payload = json.loads(llm_candidate.read_text(encoding="utf-8"))

    result = plan_from_goal(
        inputs,
        goal=goal,
        output_file=result_output,
        output_job_path=job_output,
        recursive=recursive,
        llm_candidate=llm_payload,
    )
    files = write_plan_artifacts(result, report_output=output, job_output=job_output)

    console.print(_rich_table("Planner Summary", result.plan_summary))
    console.print(_rich_table("Clarification Questions", result.clarification_questions))
    console.print(f"[green]Planner review written:[/green] {files['planning_review']}")
    console.print(f"[green]Job config written:[/green] {files['job_config']}")


@app.command()
def run(
    config: Annotated[Path, typer.Argument(help="Path to a JSON job config.")],
    verbose: Annotated[
        bool,
        typer.Option("--verbose", help="Print detailed execution and audit logs."),
    ] = False,
) -> None:
    """Run a deterministic data cleaning job."""

    if verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(levelname)s %(name)s: %(message)s",
            force=True,
        )

    result = run_job_from_file(config)

    table = Table(title="Cleaning Summary")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")

    for row in result.summary.to_dict(orient="records"):
        table.add_row(str(row["metric"]), str(row["value"]))

    console.print(table)
    console.print(f"[green]Output written:[/green] {result.output_file}")


@app.command("understanding-cache")
def understanding_cache(
    clear: Annotated[
        bool,
        typer.Option("--clear", help="清空已缓存的目标理解结果。"),
    ] = False,
) -> None:
    """查看或清空目标理解缓存。

    相同的文件和相同的一句话会复用上次的理解，所以同一个请求不会因为模型抖动而漂移。
    代价是一次错误的理解也会被固定下来 —— 觉得某个目标一直理解得不对，就在这里清掉，
    下次重新问模型。
    """

    from data_agent.agent.understanding_cache import cache_stats, clear_cache

    stats = cache_stats()
    if clear:
        removed = clear_cache()
        console.print(f"[green]已清空 {removed} 条理解缓存[/green]：{stats['path']}")
        return
    console.print(
        f"缓存文件：{stats['path']}\n"
        f"状态：{'启用' if stats['enabled'] else '已关闭'}\n"
        f"条目数：{stats['entry_count']}\n"
        f"最近写入：{stats['newest'] or '-'}\n\n"
        "如需重新理解，请加 --clear。"
    )


@app.command()
def inspect(config: Annotated[Path, typer.Argument(help="Path to a JSON job config.")]) -> None:
    """Validate and print the resolved job config."""

    from data_agent.pipelines import load_job_config

    job = load_job_config(config)
    console.print_json(job.model_dump_json(indent=2))


@app.command()
def profile(
    config: Annotated[Path, typer.Argument(help="Path to a JSON job config.")],
    output: Annotated[
        Optional[Path],
        typer.Option("--output", "-o", help="Optional Excel profile report output path."),
    ] = None,
) -> None:
    """Profile all input tables before cleaning."""

    profile_sheets = profile_job_from_file(config)
    table_profile = profile_sheets["table_profile"]

    table = Table(title="Input Table Profile")
    for column in table_profile.columns:
        table.add_column(str(column))

    for row in table_profile.to_dict(orient="records"):
        table.add_row(*(str(row[column]) for column in table_profile.columns))

    console.print(table)

    if output:
        output_path = export_profile_report(output, profile_sheets)
        console.print(f"[green]Profile report written:[/green] {output_path}")


@app.command("profile-files")
def profile_files(
    inputs: Annotated[
        list[Path],
        typer.Argument(help="Excel/CSV files or directories to scan."),
    ],
    output: Annotated[
        Optional[Path],
        typer.Option("--output", "-o", help="Optional Excel profile report output path."),
    ] = Path("data/output/data_map_profile.xlsx"),
    recursive: Annotated[
        bool,
        typer.Option("--recursive/--no-recursive", help="Scan directories recursively."),
    ] = True,
) -> None:
    """Profile raw Excel/CSV files directly, including every Excel sheet."""

    tables, source_inventory = read_tables_from_paths(inputs, recursive=recursive)
    profile_sheets = profile_dataset(tables, source_inventory=source_inventory)

    console.print(_rich_table("Dataset Overview", profile_sheets["overview"]))
    console.print(_rich_table("Input Tables", profile_sheets["table_profile"]))

    relationships = profile_sheets["relationship_candidates"]
    if not relationships.empty:
        recommended = relationships[relationships["recommendation"].eq("recommended_lookup")]
        preview = recommended.head(8) if not recommended.empty else relationships.head(8)
        console.print(_rich_table("Relationship Candidates", preview))

    if output:
        output_path = export_profile_report(output, profile_sheets)
        console.print(f"[green]Profile report written:[/green] {output_path}")


@app.command("recommend-files")
def recommend_files(
    inputs: Annotated[
        list[Path],
        typer.Argument(help="Excel/CSV files or directories to scan and recommend cleaning plan."),
    ],
    output: Annotated[
        Optional[Path],
        typer.Option("--output", "-o", help="Excel recommendation report output path."),
    ] = Path("data/output/cleaning_recommendations.xlsx"),
    job_output: Annotated[
        Path,
        typer.Option("--job-output", help="Generated executable job config path."),
    ] = Path("configs/recommended_cleaning_job.json"),
    recursive: Annotated[
        bool,
        typer.Option("--recursive/--no-recursive", help="Scan directories recursively."),
    ] = True,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", help="Print detailed recommendation decision logs."),
    ] = False,
) -> None:
    """Recommend lookup, cleaning, and labeling plan from raw files."""

    if verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(levelname)s %(name)s: %(message)s",
            force=True,
        )

    tables, source_inventory = read_tables_from_paths(inputs, recursive=recursive)
    profile_sheets = profile_dataset(tables, source_inventory=source_inventory)
    recommendation_sheets, job_config = build_recommendation_package(
        tables,
        profile_sheets,
        output_job_path=job_output,
    )
    report_sheets = {**profile_sheets, **recommendation_sheets}

    job_path = write_recommended_job_config(job_output, job_config)
    report_path = export_profile_report(output, report_sheets)

    console.print(
        _rich_table("Recommendation Summary", recommendation_sheets["recommendation_summary"])
    )
    if not recommendation_sheets["recommended_lookups"].empty:
        console.print(
            _rich_table("Recommended Lookups", recommendation_sheets["recommended_lookups"])
        )
    if not recommendation_sheets["label_taxonomy"].empty:
        console.print(_rich_table("Label Taxonomy", recommendation_sheets["label_taxonomy"]))
    console.print(f"[green]Recommendation report written:[/green] {report_path}")
    console.print(f"[green]Job config written:[/green] {job_path}")


def _rich_table(title: str, df) -> Table:
    table = Table(title=title)
    for column in df.columns:
        table.add_column(str(column))
    for row in df.to_dict(orient="records"):
        table.add_row(*(str(row[column]) for column in df.columns))
    return table


if __name__ == "__main__":
    app()
