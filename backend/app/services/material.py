"""养护材料业务规则：库存状态、结存数量的判断统一走 services.inventory。

冻结、解冻、耗尽不再各自维护一份条件，所有路径都先改服务端库存台账，
再按台账重算记录，避免新增状态时漏改、以及解冻后结存数量回不来的问题。
"""
from __future__ import annotations

from typing import Any

from app.services.inventory import (
    ACTION_FREEZE,
    ACTION_RULES,
    BALANCE_FIELD,
    HISTORICAL_FIELD,
    LEDGER_BALANCE,
    LEDGER_FROZEN,
    LEDGER_HISTORICAL,
    REQUIRED_FIELDS,
    STATUS_DEPLETED,
    STATUS_FROZEN,
    STATUS_LOW,
    STATUS_NORMAL,
    inventory_ledger,
)
from app.store import store

MODULE = "material"


class MaterialService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        # 台账是唯一事实来源：展示/筛选前先按台账重算，杜绝界面临时判断
        rows = [inventory_ledger.sync_entry(row) for row in rows]
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("材料编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        return inventory_ledger.sync_entry(entry)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry[BALANCE_FIELD] = values.get(BALANCE_FIELD, 0)
        entry[HISTORICAL_FIELD] = bool(values.get(HISTORICAL_FIELD))
        rows.append(entry)
        return inventory_ledger.sync_entry(entry), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"养护材料 {entry_id} 不存在或已归档"
        operation = ACTION_RULES.get(action)
        if operation is None:
            return None, f"动作「{action}」不属于养护材料可执行范围"
        account = inventory_ledger.account(entry_id) or inventory_ledger.open_from(entry)
        if account.get(LEDGER_HISTORICAL):
            return None, "历史材料的结存口径按原记录保留，不允许库存操作"
        target_entry = inventory_ledger.apply(entry, operation)
        return target_entry, f"养护材料已{action}"

    def batch_freeze(self, entry_ids: list[int]) -> tuple[list[dict[str, Any]], list[int]]:
        """批量冻结：与单条冻结同一份台账规则，返回冻结结果与未命中的编号。"""
        affected: list[dict[str, Any]] = []
        missing_ids: list[int] = []
        for entry_id in entry_ids:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                missing_ids.append(entry_id)
                continue
            account = inventory_ledger.open_from(entry)
            if account.get(LEDGER_HISTORICAL):
                # 历史材料按原记录保留，跳过且不算失败
                continue
            affected.append(inventory_ledger.apply(entry, ACTION_RULES[ACTION_FREEZE]))
        return affected, missing_ids

    def board(self) -> dict[str, Any]:
        """库存看板：汇总数与台账列表都取自同一个台账，口径天然一致。"""
        rows = store.rows(MODULE)
        accounts = inventory_ledger.all_accounts(rows)
        current = [item for item in accounts if not item[LEDGER_HISTORICAL]]

        def count(status: str) -> int:
            return sum(1 for item in current if item["status"] == status)

        frozen_balance = sum(
            int(item[LEDGER_BALANCE]) for item in current if item[LEDGER_FROZEN]
        )
        return {
            "总材料数": len(current),
            "可用材料": sum(
                1
                for item in current
                if item["status"] in (STATUS_NORMAL, STATUS_LOW)
            ),
            "储备不足材料": count(STATUS_LOW),
            "已冻结材料": count(STATUS_FROZEN),
            "已耗尽材料": count(STATUS_DEPLETED),
            "总结存数量": sum(int(item[LEDGER_BALANCE]) for item in current),
            "冻结结存数量": frozen_balance,
        }

    def backfill_from_ledger(self) -> None:
        """启动回填：已有库存记录按新口径重算结存数，历史材料保留原记录。"""
        inventory_ledger.backfill(store.rows(MODULE))
