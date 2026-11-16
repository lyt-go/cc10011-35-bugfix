"""聚合配置接口：维护聚合规则，并查询按标准维度映射回算的目标粒度结果。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.aggregate import AggregateService

router = APIRouter(prefix="/api/aggregate", tags=["聚合配置"])

service = AggregateService()

LIST_FIELDS = ["规则编号", "源数据粒度", "目标粒度", "聚合函数集", "数据偏移分钟", "补全策略", "空值处理", "输出数据集"]
STATUSES = ["草稿", "规则评审中", "已生效", "已废弃"]


@router.get("/results", response_model=PageResult[dict])
def list_results(
    rule_code: str | None = Query(default=None, description="按规则编号过滤"),
    version: int | None = Query(default=None, description="查看指定映射版本的目标粒度结果"),
    page: int = 1,
    size: int = 50,
) -> PageResult[dict]:
    """目标粒度结果：每行标准维度组合唯一，附带命中规则集合与映射版本。"""
    if size > 500:
        raise HTTPException(status_code=400, detail="每页最多 500 条，请缩小分页范围")
    items, total, current = service.list_results(
        rule_code=rule_code,
        version_number=version,
        page=page,
        size=size,
    )
    return PageResult(
        items=items,
        total=total,
        page=page,
        size=size,
        extra={"current_version": current.get("映射版本") if current else None},
    )


@router.get("/mapping-link")
def mapping_link() -> dict[str, Any]:
    """聚合页与映射版本的联动核对：分组数、覆盖记录数与重复/丢失判定。"""
    return service.mapping_link()


@router.post("/{entry_id}/recalc")
def recalc(entry_id: int) -> dict[str, Any]:
    """按当前映射版本回算单条聚合规则的目标粒度结果。"""
    result, error = service.recalc(entry_id)
    if error:
        return {"ok": False, "message": error}
    return {"ok": True, "message": "目标粒度结果已按当前映射版本回算", **result}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出聚合配置清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "aggregate", "total": total, "items": items}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、规则评审中、已生效、已废弃"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤聚合配置列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条聚合规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"聚合规则 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/detail")
def get_detail(entry_id: int) -> dict[str, Any]:
    """聚合详情：规则、目标粒度结果与记录数核对一起返回，便于和映射列表同步核对。"""
    detail, error = service.detail(entry_id)
    if error:
        raise HTTPException(status_code=404, detail=error)
    return detail


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条聚合规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="聚合规则已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条聚合规则执行提交规则评审、生效规则、废弃规则；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
