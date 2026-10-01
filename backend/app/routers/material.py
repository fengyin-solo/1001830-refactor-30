"""养护材料接口：维护养护材料，覆盖冻结材料、解冻材料、登记耗尽等动作。

状态序列与动作口径统一由 app.services.material 的共用库存规则给出，
路由层不再各自维护一份，也不做业务判断。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.material import ACTION_RULES, STATUS_ORDER, MaterialService

router = APIRouter(prefix="/api/material", tags=["养护材料"])

service = MaterialService()


@router.get("/summary")
def stock_summary() -> dict[str, Any]:
    """库存看板：结存口径与库存台账同源，都由服务端的共用库存规则算出。"""
    return service.stock_summary()


@router.post("/actions/batch", response_model=ActionResult)
def run_batch_action(payload: EntryPayload) -> ActionResult:
    """批量执行同一动作（如批量冻结）；与单条动作共用同一份库存规则。"""
    action = str(payload.values.get("action") or "").strip()
    if action not in ACTION_RULES:
        return ActionResult(ok=False, message=f"动作「{action}」不属于养护材料可执行范围")
    raw_ids = payload.values.get("ids") or []
    if not isinstance(raw_ids, list) or not raw_ids:
        return ActionResult(ok=False, message="批量动作需要给出材料 id 列表")
    try:
        entry_ids = [int(item) for item in raw_ids]
    except (TypeError, ValueError):
        return ActionResult(ok=False, message="批量动作的材料 id 必须是整数")
    updated, failed = service.run_batch_action(entry_ids, action)
    message = f"养护材料已{action}：成功 {len(updated)} 条"
    if failed:
        message += f"，失败 {len(failed)} 条（{'；'.join(failed)}）"
    return ActionResult(ok=not failed, message=message)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出养护材料清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "material", "total": total, "items": items}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按材料编号检索"),
    status: str | None = Query(default=None, description="、".join(STATUS_ORDER)),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按材料编号与状态过滤养护材料列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条养护材料明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"养护材料 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条养护材料，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="养护材料已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条养护材料执行冻结材料、解冻材料、登记耗尽；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
