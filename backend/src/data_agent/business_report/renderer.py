from __future__ import annotations

from pathlib import Path

from data_agent.business_report.html_renderer import render_business_answer_html
from data_agent.business_report.markdown_renderer import render_business_answer_markdown
from data_agent.schemas import BusinessAnswer


def write_business_report(
    answer: BusinessAnswer,
    output_dir: str | Path,
    formats: list[str] | None = None,
) -> dict[str, Path]:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    requested = formats or ["markdown", "html"]
    paths: dict[str, Path] = {}
    if "markdown" in requested:
        markdown_path = output_path / "business_answer.md"
        markdown_path.write_text(
            render_business_answer_markdown(answer),
            encoding="utf-8",
        )
        paths["markdown"] = markdown_path
    if "html" in requested:
        html_path = output_path / "business_answer.html"
        html_path.write_text(render_business_answer_html(answer), encoding="utf-8")
        paths["html"] = html_path
    return paths
