"""多维下钻接口：维护下钻路径，并查询按标准维度映射结果生成的分层结果。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.drilldown import DrilldownService

router = APIRouter(prefix="/api/drilldown", tags=["多维下钻"])

service = DrilldownService()

LIST_FIELDS = ["路径编号", "起始维度", "下钻层级序列", "每层聚合粒度", "默认度量", "路径深度", "缓存秒数", "数据新鲜度"]
STATUSES = ["草稿", "路径评审中", "已启用", "已归档"]


@router.get("/results", response_model=PageResult[dict])
def list_results(
    path_code: str | None = Query(default=None, description="按路径编号过滤"),
    version: int | None = Query(default=None, description="查看指定映射版本的分层结果"),
    page: int = 1,
    size: int = 50,
) -> PageResult[dict]:
    """分层下钻结果：标准维度来自维度映射，行内同时保留原始维度名便于核对。"""
    if size > 500:
        raise HTTPException(status_code=400, detail="每页最多 500 条，请缩小分页范围")
    items, total, current = service.list_results(
        path_code=path_code,
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
    """下钻页与映射版本的联动核对：当前版本、各路径结果行数与默认命中情况。"""
    return service.mapping_link()


@router.post("/{entry_id}/refresh")
def refresh(entry_id: int) -> dict[str, Any]:
    """按当前映射版本刷新单条路径的分层结果。"""
    result, error = service.refresh(entry_id)
    if error:
        return {"ok": False, "message": error}
    return {"ok": True, "message": "分层结果已按当前映射版本刷新", **result}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出多维下钻清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "drilldown", "total": total, "items": items}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按路径编号检索"),
    status: str | None = Query(default=None, description="草稿、路径评审中、已启用、已归档"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按路径编号与状态过滤多维下钻列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条下钻路径明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"下钻路径 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条下钻路径，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段或字段不合法：{'、'.join(missing)}")
    return ActionResult(ok=True, message="下钻路径已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条下钻路径执行提交路径评审、启用路径、归档路径；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
