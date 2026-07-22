from __future__ import annotations

import re
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,32}$")
_PLACEHOLDER_RE = re.compile(r"^\{([a-z][a-z0-9_]*)\}$")


def render_http_template(template: str, args: dict[str, object] | None = None) -> str:
    """只替换查询参数，绝不允许参数改变 scheme/host/path。"""
    args = args or {}
    parts = urlsplit(template)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    rendered: list[tuple[str, str]] = []
    for key, value in pairs:
        match = _PLACEHOLDER_RE.fullmatch(value)
        if match:
            value = str(args.get(match.group(1), ""))
        rendered.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(rendered), parts.fragment))


class SkillParameter(BaseModel):
    type: Literal["string", "integer", "number", "boolean"] = "string"
    description: str = ""
    required: bool = False


class SkillExecution(BaseModel):
    """Skill 的受限执行声明。

    ``http_get`` 刻意只接受固定地址，不提供请求头、方法、重定向、
    cookie 或参数插值。这样一个经审批的网站探查 Skill 不会变成通用
    HTTP 代理或 SSRF 绕过入口。
    """

    type: Literal["prompt", "http_get"]
    template: str = Field(min_length=1, max_length=8000)
    content_summary: bool = False
    max_content_chars: int = Field(default=4000, ge=500, le=8000)


class SkillManifest(BaseModel):
    id: str
    name: str = Field(min_length=1, max_length=80)
    version: str = Field(default="0.1.0", max_length=32)
    description: str = Field(min_length=1, max_length=500)
    min_level: Literal["readonly", "user", "admin", "owner"] = "user"
    parameters: dict[str, SkillParameter] = Field(default_factory=dict)
    execution: SkillExecution

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _ID_RE.fullmatch(value):
            raise ValueError("id 必须为 2-33 位小写字母、数字或下划线，且以字母开头")
        return value

    @field_validator("parameters")
    @classmethod
    def validate_parameter_names(cls, value: dict[str, SkillParameter]) -> dict[str, SkillParameter]:
        for name in value:
            if not _ID_RE.fullmatch(name):
                raise ValueError(f"参数名不合法: {name}")
        return value
