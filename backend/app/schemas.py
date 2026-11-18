"""通用接口契约；模块字段由领域服务持有，不再为每个模块复制一份模型类。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class ActionPayload(BaseModel):
    action: str
    remark: str | None = None


class LedgerBuildPayload(BaseModel):
    """定位策略执行台账：构建数据集请求。"""
    rule_ids: list[int] | None = None


class LedgerExecutePayload(BaseModel):
    """定位策略执行台账：按版本数据集批量执行请求。"""
    version: str | None = None


class LedgerRetestPayload(BaseModel):
    """定位策略执行台账：异常元素重测请求。"""
    record_ids: list[str] = Field(default_factory=list)
