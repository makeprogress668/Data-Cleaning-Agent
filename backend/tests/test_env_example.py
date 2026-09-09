"""`.env.example` 被复制成 `.env` 之后，不能带来任何后果。

这个文件是新用户的第一步：DEPLOYMENT.md 写的就是 `Copy-Item .env.example .env`。
它里面每一个未注释的赋值，都会同时落到两个地方 —— 部署出去的容器，以及开发者本机的
`uvicorn` 和 `data-agent`（仓库根的 `.env` 会被 `agent/llm_client._load_dotenv_once`
读走）。所以示例文件里放一个「示例值」，不是示范，是默认配置。

这条规则修过两个真实问题：

1. `DATA_AGENT_API_KEY` 曾经带值且未注释。复制之后本地后端开启鉴权，而 Vite 开发
   服务器不注入 X-API-Key、下载按钮又是 `<a download href>`（浏览器锚点带不了请求
   头），于是控制台整页 401，界面上只有「缺少或无效的访问凭据」，没有任何线索指向
   这个文件。容器部署不受影响，nginx 在服务端注入这个头。
2. `DATA_AGENT_DATABASE_PASSWORD` 和 `DATA_AGENT_ARTIFACT_SECRET` 曾经带占位值且未
   注释。忘了改的人把 Postgres 和 MinIO 跑在一个**公开在本仓库里**的密码上 —— 看起来
   有鉴权，实际等于没有。

注释掉不会让漏配变得危险，只会让它变响：compose 用 ${VAR:?} 守卫这两个密码，缺一个就
停在「Set <name> in .env」；API Key 那侧由 DATA_AGENT_ENV=production 在启动阶段拦下。
两头都是失败要响，而不是静默降级。
"""

from __future__ import annotations

from pathlib import Path

import pytest

# 这些变量里任何一个拿到非空值，本地后端就会开始要求访问凭据。
AUTH_VARIABLES = (
    "DATA_AGENT_API_KEY",
    "DATA_AGENT_TENANT_API_KEYS_JSON",
    "DATA_AGENT_OIDC_ISSUER",
    "DATA_AGENT_OIDC_AUDIENCE",
    "DATA_AGENT_OIDC_JWKS_URL",
    "DATA_AGENT_OIDC_PUBLIC_KEY",
)

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"


def _uncommented_assignments() -> dict[str, str]:
    """Read the file the way python-dotenv does: a commented line is not a setting."""

    values: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        values[name.strip()] = value.strip()
    return values


def test_the_example_file_is_where_this_test_thinks_it_is() -> None:
    """路径写错会让下面每条断言都恒真，那等于没有这些测试。"""

    assert ENV_EXAMPLE.is_file(), f"没找到 {ENV_EXAMPLE}"


def test_nothing_in_the_example_carries_a_value() -> None:
    """示例文件只提供空位，不提供值。带值就是默认配置，不是示范。"""

    carrying = {
        name: value for name, value in _uncommented_assignments().items() if value
    }

    assert not carrying, (
        f"这些变量在 .env.example 里未注释且带值：{sorted(carrying)}。"
        "复制成 .env 之后它们就是生效的配置：本机会被它改变行为，"
        "部署出去则等于用一个公开在本仓库里的凭据。请把它们注释掉，"
        "让使用者在自己的 .env 里取消注释并填真值。"
    )


@pytest.mark.parametrize("variable", AUTH_VARIABLES)
def test_copying_the_example_does_not_switch_authentication_on(variable: str) -> None:
    """上一条的特例，单独钉住：它的症状是最难查的那一种。"""

    value = _uncommented_assignments().get(variable, "")

    assert not value, (
        f"{variable} 在 .env.example 里未注释且带值。复制成 .env 之后本地后端会开启"
        "鉴权，控制台每个请求都是 401，而界面上看不出原因。生产环境请在自己的 .env "
        "里取消注释，不要改这个示例文件。"
    )


def test_the_model_key_stays_present_and_empty() -> None:
    """模型配置是用户唯一必须自己填的东西，位置要留着，值必须是空的。"""

    values = _uncommented_assignments()

    assert "DATA_AGENT_LLM_API_KEY" in values, "模型 Key 的位置被注释掉了，用户会找不到"
    assert values["DATA_AGENT_LLM_API_KEY"] == "", "示例文件里不该出现真实的模型 Key"


def test_the_example_stays_lf_like_the_rest_of_the_repo() -> None:
    """.gitattributes 声明全仓 LF。混进 CRLF 会让整份文件在 diff 里改头换面。"""

    assert b"\r\n" not in ENV_EXAMPLE.read_bytes(), ".env.example 混入了 CRLF 换行"
