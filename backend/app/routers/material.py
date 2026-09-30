"""养护材料接口：维护养护材料，覆盖批量冻结、单条冻结/解冻/耗尽与库存台账、看板。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, BatchActionPayload, EntryPayload, PageResult
from app.services.inventory import STATUS_ORDER
from app.services.material import MODULE, MaterialService

router = APIRouter(prefix="/api/material", tags=["养护材料"])

service = MaterialService()

LIST_FIELDS = ["材料编号", "材料名称", "规格型号", "结存数量", "计量单位", "存放场地", "保管人员", "材料状态"]
STATUSES = STATUS_ORDER


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按材料编号检索"),
    status: str | None = Query(default=None, description="正常可用、临近不足、已冻结、已耗尽"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按材料编号与状态过滤养护材料列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条养护材料，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="养护材料已登记", entry=entry)


@router.post("/batch-freeze", response_model=ActionResult)
def batch_freeze(payload: BatchActionPayload) -> ActionResult:
    """批量冻结：与单条冻结共用同一份台账规则，逐条返回冻结后的产出。"""
    entry_ids = list(dict.fromkeys(int(i) for i in payload.ids))
    if not entry_ids:
        return ActionResult(ok=False, message="未选择任何养护材料")
    affected, missing_ids = service.batch_freeze(entry_ids)
    if missing_ids and not affected:
        return ActionResult(ok=False, message=f"养护材料 {'、'.join(map(str, missing_ids))} 不存在或已归档")
    message = f"养护材料已冻结 {len(affected)} 条"
    if missing_ids:
        message += f"，{'、'.join(map(str, missing_ids))} 不存在或已归档"
    return ActionResult(ok=True, message=message, entries=affected)


@router.get("/ledger")
def inventory_ledger_view() -> dict[str, Any]:
    """库存台账：以服务端台账为准的全量结存视图，历史材料单列保留。"""
    items, total = service.list_entries(page=1, size=10000)
    board = service.board()
    return {
        "module": MODULE,
        "total": total,
        "items": items,
        # 台账汇总与看板同源，结存数保持一致
        "summary": board,
    }


@router.get("/board")
def inventory_board() -> dict[str, Any]:
    """库存看板：结存数直接汇总自库存台账，不做任何界面临时判断。"""
    return service.board()


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出养护材料清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": MODULE, "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条养护材料明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"养护材料 {entry_id} 不存在或已归档")
    return entry


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条养护材料执行冻结材料、解冻材料、登记耗尽；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
