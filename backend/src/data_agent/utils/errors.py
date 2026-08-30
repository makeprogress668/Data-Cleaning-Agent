from __future__ import annotations

import logging

logger = logging.getLogger("data_agent.api")


class UserFacingError(Exception):
    """An error whose message is safe and useful to show the end user.

    Raise this (or a subclass) for problems the user can act on — unsupported
    file, empty table, missing required column. The API surfaces ``message``
    verbatim. Everything else is treated as an internal error: logged with a
    trace id, and reported to the user only as a generic, non-leaky message.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# Maps common low-level exception signatures to friendly Chinese guidance, so a
# raw pandas/openpyxl/engine error never reaches the user as English stack noise.
_FRIENDLY_HINTS: tuple[tuple[str, str], ...] = (
    ("No supported", "上传的文件里没有可识别的表格，请确认是 Excel 或 CSV 且内容非空。"),
    ("Condition field not found", "处理规则引用了数据中不存在的字段，请检查规则或列名是否一致。"),
    ("Formula source column not found", "计算公式引用了不存在的列，请检查列名。"),
    ("Unsupported formula op", "存在不支持的计算方式，请调整处理目标或规则。"),
    ("OutputSpec validation failed", "结果未通过交付契约校验，请调整处理目标或联系管理员。"),
    ("lookup source_table not found", "跨表匹配引用了不存在的表，请检查上传文件是否齐全。"),
    ("base_table", "未能定位到主数据表，请在目标中说明要处理哪张表。"),
    ("Unsupported file type", "存在不支持的文件类型，请上传 Excel、CSV、JSON、Markdown 或 TXT。"),
    ("codec can't decode", "文件编码无法识别，请另存为 UTF-8 或标准 CSV 后重试。"),
    ("No columns to parse", "文件内容为空或格式不正确，请检查后重新上传。"),
)


def to_user_message(exc: Exception) -> str:
    """Return a safe, actionable Chinese message for an exception.

    - :class:`UserFacingError` and :class:`ValueError` are considered user-caused
      and their message is returned directly.
    - Otherwise we try to match a known low-level signature; failing that we
      return a generic message and rely on logs (with the exception) for detail.
    """
    if isinstance(exc, UserFacingError):
        return exc.message
    text = str(exc)
    for needle, hint in _FRIENDLY_HINTS:
        if needle in text:
            return hint
    if isinstance(exc, ValueError):
        # ValueErrors in this codebase are raised deliberately with a readable
        # message (validation, config, unsupported input), so they are safe.
        return text
    return "处理过程中出现了内部错误，请稍后重试或联系管理员。"
