"""养护材料业务规则：状态流转、字段校验与筛选口径都收在这里。

库存判断（冻结、解冻、耗尽）全系统只有这一份：单条动作、批量动作、
启动回填与库存看板都走同一套规则函数，界面上的临时判断一律作废，
以这里算出的库存台账为准重算。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "material"
REQUIRED_FIELDS = ["材料编号", "材料名称", "规格型号"]

# ── 共用库存规则：状态序列、动作映射、结存口径只在这里定义一次 ──
STATUS_ORDER = ["正常可用", "临近不足", "已冻结", "已耗尽"]
ACTION_RULES = {"冻结材料": "已冻结", "解冻材料": "正常可用", "登记耗尽": "已耗尽"}
NEGATIVE_ACTIONS: list[str] = []
# 历史材料（已耗尽）的结存口径按原记录保留，不参与回填
HISTORICAL_STATUSES = {"已耗尽"}
# 库存看板的分桶口径，与台账共用同一份状态定义
DASHBOARD_BUCKETS = [
    {"label": "可用材料", "statuses": ["正常可用"]},
    {"label": "储备不足材料", "statuses": ["临近不足"]},
    {"label": "已冻结材料", "statuses": ["已冻结"]},
]


def _balance(value: Any) -> int:
    """把台账上的数量字段归一成整数，缺字段或脏数据按 0 计。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _apply_freeze(entry: dict[str, Any]) -> None:
    """冻结：可用结存转入冻结暂存，台账结存归零。"""
    if entry.get("status") == "已冻结":
        return  # 已在冻结态：保留原冻结暂存，重复冻结不冲减
    entry["冻结数量"] = _balance(entry.get("结存数量"))
    entry["结存数量"] = 0


def _apply_unfreeze(entry: dict[str, Any]) -> None:
    """解冻：冻结暂存退回结存；没有暂存时保持账面结存不动。"""
    stashed = _balance(entry.get("冻结数量"))
    if stashed:
        entry["结存数量"] = stashed
    entry["冻结数量"] = 0


def _apply_deplete(entry: dict[str, Any]) -> None:
    """耗尽：结存与冻结暂存一并清零。"""
    entry["结存数量"] = 0
    entry["冻结数量"] = 0


STOCK_EFFECTS = {
    "冻结材料": _apply_freeze,
    "解冻材料": _apply_unfreeze,
    "登记耗尽": _apply_deplete,
}


def apply_stock_action(entry: dict[str, Any], action: str) -> str | None:
    """冻结/解冻/耗尽的唯一入口：成功返回 None，被拦下时返回可读原因。"""
    target = ACTION_RULES.get(action)
    if target is None:
        return f"动作「{action}」不属于养护材料可执行范围"
    if target not in STATUS_ORDER:
        return f"目标状态「{target}」不在允许的状态序列里"
    STOCK_EFFECTS[action](entry)
    entry["status"] = target
    entry["pending"] = target != STATUS_ORDER[-1]
    entry["abnormal"] = action in NEGATIVE_ACTIONS
    return None


def recalculate_balance(entry: dict[str, Any]) -> None:
    """按新口径回填单条记录的结存数；历史材料保持原记录不动。幂等，可重复执行。"""
    status = entry.get("status")
    if status in HISTORICAL_STATUSES:
        return
    if status == "已冻结":
        if "冻结数量" not in entry:
            entry["冻结数量"] = _balance(entry.get("结存数量"))
        entry["结存数量"] = 0
    elif _balance(entry.get("冻结数量")):
        # 旧口径下解冻没把结存还回来的记录，这里一并补齐
        entry["结存数量"] = _balance(entry.get("结存数量")) + _balance(entry.get("冻结数量"))
        entry["冻结数量"] = 0


def summarize_stock(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """库存看板分桶：与台账同源，结存合计直接按台账行累加，保证两边一致。"""
    buckets = []
    for bucket in DASHBOARD_BUCKETS:
        matched = [row for row in rows if row.get("status") in bucket["statuses"]]
        buckets.append({
            "label": bucket["label"],
            "count": len(matched),
            "balance": sum(_balance(row.get("结存数量")) for row in matched),
            "frozen": sum(_balance(row.get("冻结数量")) for row in matched),
        })
    return buckets


class MaterialService:
    def __init__(self) -> None:
        self.backfill_balances()

    def backfill_balances(self) -> None:
        """启动时按新口径回填既有库存记录的结存数；历史材料保持原记录。"""
        for row in store.rows(MODULE):
            recalculate_balance(row)

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("材料编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["结存数量"] = _balance(values.get("结存数量"))
        entry["冻结数量"] = 0
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"养护材料 {entry_id} 不存在或已归档"
        error = apply_stock_action(entry, action)
        if error:
            return None, error
        return entry, f"养护材料已{action}"

    def run_batch_action(self, entry_ids: list[int], action: str) -> tuple[list[dict[str, Any]], list[str]]:
        """批量动作（如批量冻结）：与单条动作走同一份共用规则，逐条结算。"""
        updated: list[dict[str, Any]] = []
        failed: list[str] = []
        for entry_id in entry_ids:
            entry, message = self.run_action(entry_id, action)
            if entry is None:
                failed.append(message)
            else:
                updated.append(entry)
        return updated, failed

    def stock_summary(self) -> dict[str, Any]:
        """库存看板：状态、动作与结存合计都由共用规则算出，与台账同源。"""
        rows = store.rows(MODULE)
        return {
            "module": MODULE,
            "statuses": list(STATUS_ORDER),
            "actions": list(ACTION_RULES),
            "stats": summarize_stock(rows),
            "total_balance": sum(_balance(row.get("结存数量")) for row in rows),
        }
