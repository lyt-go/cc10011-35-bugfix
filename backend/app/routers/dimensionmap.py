"""维度映射接口：规则维护、标准维度数据集、映射版本与解析追踪。

路由顺序刻意把固定路径（dataset / versions / summary / resolve / rebuild）
放在 `/{entry_id}` 之前，避免被当成 id 解析。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.dimensionmap import DimensionmapService

router = APIRouter(prefix="/api/dimensionmap", tags=["维度映射"])

service = DimensionmapService()

LIST_FIELDS = ["规则编号", "原始维度名", "标准维度名", "映射类型", "值转换逻辑", "默认取值", "映射优先级", "最近更新时间"]
STATUSES = ["草稿", "规则评审中", "已生效", "已废弃"]


@router.get("/summary")
def summary() -> dict[str, Any]:
    """映射列表页核对：规则总数、当前映射版本与数据集质量。"""
    return service.mapping_summary()


@router.get("/dataset", response_model=PageResult[dict])
def list_dataset(
    keyword: str | None = Query(default=None, description="按原始/标准维度名、维度值或命中规则编号检索"),
    raw_dimension: str | None = Query(default=None, description="按原始维度名过滤"),
    standard_dimension: str | None = Query(default=None, description="按标准维度名过滤"),
    version: int | None = Query(default=None, description="查看指定映射版本的数据集，默认当前版本"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """标准维度映射数据集：每条记录带命中规则、规则版本与映射版本，可追溯到来源记录。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total, current = service.list_dataset(
        keyword=keyword,
        raw_dimension=raw_dimension,
        standard_dimension=standard_dimension,
        version_number=version,
        page=page,
        size=size,
    )
    return PageResult(
        items=items,
        total=total,
        page=page,
        size=size,
        extra={
            "current_version": current.get("映射版本") if current else None,
            "version_label": current.get("版本标识") if current else None,
            "conflict_policy": service.mapping_summary()["冲突处理策略"],
        },
    )


@router.get("/versions")
def list_versions(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    """映射版本列表：每个版本带规则快照与回算统计，只有一个是“当前”。"""
    items, current = service.list_versions(limit=limit)
    return {
        "total": len(items),
        "current_version": current.get("映射版本") if current else None,
        "items": items,
    }


@router.get("/versions/{version_number}")
def get_version(version_number: int) -> dict[str, Any]:
    version = service.get_version(version_number)
    if version is None:
        raise HTTPException(status_code=404, detail=f"映射版本 {version_number} 不存在")
    return version


@router.get("/versions/{version_number}/replay")
def replay_version(version_number: int) -> dict[str, Any]:
    """按历史版本规则快照重放解析，核对历史映射结果是否可追溯。"""
    from app.services import dimensionmap_chain as chain

    result = chain.replay_version(version_number)
    if result is None:
        raise HTTPException(status_code=404, detail=f"映射版本 {version_number} 不存在")
    return result


@router.post("/resolve")
def resolve(payload: EntryPayload) -> dict[str, Any]:
    """解析预览：给定原始维度名与值，返回唯一命中的规则、落选规则与映射版本。"""
    result, error = service.resolve_preview(payload.values)
    if error:
        return {"ok": False, "message": error, "result": None}
    return {"ok": True, "message": "已按优先级选出唯一映射规则", **result}


@router.post("/rebuild")
def rebuild() -> dict[str, Any]:
    """手动重建当前映射版本：重新生成数据集并全量回算下钻与聚合。"""
    result, error = service.rebuild()
    if error:
        return {"ok": False, "message": error}
    return {"ok": True, "message": "映射数据集已重建并完成回算", **result}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出维度映射清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "dimensionmap", "total": total, "items": items}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号、原始/标准维度名检索"),
    status: str | None = Query(default=None, description="草稿、规则评审中、已生效、已废弃"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤维度映射列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(
        items=items,
        total=total,
        page=page,
        size=size,
        extra=service.mapping_summary(),
    )


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条映射规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"映射规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条映射规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段或字段不合法：{'、'.join(missing)}")
    return ActionResult(ok=True, message="映射规则已登记，并已生成映射版本", entry=entry)


@router.put("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """修改映射规则：完整保存转换逻辑、默认值、优先级与更新时间，并触发版本重算。"""
    entry, error = service.update_entry(entry_id, payload.values)
    if error:
        return ActionResult(ok=False, message=error)
    return ActionResult(ok=True, message="映射规则已保存并重新生成映射版本", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条映射规则执行提交规则评审、生效规则、废弃规则；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
