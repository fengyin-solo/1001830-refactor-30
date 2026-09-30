"""养护材料库存共用规则：冻结、解冻、耗尽三条路径的判断都只认这一份。

口径说明（与业务约定对应）：

- 结存数量是物理台账数，冻结/解冻只改「是否冻结」，不增不减数量；
  因此「解冻以后结存数量没有回来」在结构上不再可能发生。
- 冻结、解冻、耗尽三处状态互相打架时，一律以服务端库存台账
  （``inventory_ledger``）为准，界面上的临时判断一律重算覆盖。
- 状态只允许由台账数据按本模块的优先级推导，不在各条路径里各写一份：
  耗尽（数量 <= 0） > 冻结 > 临近不足 > 正常可用。
- 历史材料（``历史记录=True``）的结存口径按原记录保留，不参与回填重算。
"""
from __future__ import annotations

from typing import Any

# 状态集合与展示顺序
STATUS_NORMAL = "正常可用"
STATUS_LOW = "临近不足"
STATUS_FROZEN = "已冻结"
STATUS_DEPLETED = "已耗尽"
STATUS_ORDER = [STATUS_NORMAL, STATUS_LOW, STATUS_FROZEN, STATUS_DEPLETED]

# 动作与台账操作的唯一映射
ACTION_FREEZE = "冻结材料"
ACTION_UNFREEZE = "解冻材料"
ACTION_DEPLETE = "登记耗尽"
ACTION_RULES = {
    ACTION_FREEZE: "freeze",
    ACTION_UNFREEZE: "unfreeze",
    ACTION_DEPLETE: "deplete",
}

# 临近不足阈值：结存数量低于该值（且未冻结、未耗尽）时判为临近不足
LOW_BALANCE_THRESHOLD = 10

# 材料记录字段
REQUIRED_FIELDS = ["材料编号", "材料名称", "规格型号"]
BALANCE_FIELD = "结存数量"
MATERIAL_STATUS_FIELD = "材料状态"
HISTORICAL_FIELD = "历史记录"

# 台账（服务端事实）字段
LEDGER_BALANCE = "ledger_balance"
LEDGER_FROZEN = "ledger_frozen"
LEDGER_HISTORICAL = "ledger_historical"


def parse_balance(value: Any) -> int:
    """把结存数量统一转成非负整数；无法识别时按 0 处理。"""
    try:
        balance = int(value)
    except (TypeError, ValueError):
        return 0
    return max(balance, 0)


def derive_status(balance: int, *, frozen: bool) -> str:
    """台账数据 -> 库存状态，唯一的状态推导口径。

    耗尽优先级最高：哪怕还挂着冻结标记，数量为 0 也算已耗尽。
    """
    if balance <= 0:
        return STATUS_DEPLETED
    if frozen:
        return STATUS_FROZEN
    if balance < LOW_BALANCE_THRESHOLD:
        return STATUS_LOW
    return STATUS_NORMAL


def reconcile_entry(entry: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
    """按台账账号重算单条材料记录，界面上/原记录里的临时判断在此被覆盖。

    台账同时是结存数量与状态的唯一事实来源：返回的记录只负责展示。
    历史材料不回填，结存数量与状态沿用原记录。
    """
    if account.get(LEDGER_HISTORICAL):
        return entry
    balance = parse_balance(account.get(LEDGER_BALANCE))
    frozen = bool(account.get(LEDGER_FROZEN))
    status = derive_status(balance, frozen=frozen)
    entry[BALANCE_FIELD] = balance
    entry["status"] = status
    entry[MATERIAL_STATUS_FIELD] = status
    entry["pending"] = status != STATUS_DEPLETED
    entry["abnormal"] = status == STATUS_LOW
    return entry


class InventoryLedger:
    """服务端库存台账：冻结/解冻/耗尽期间的唯一事实来源。"""

    def __init__(self) -> None:
        self._accounts: dict[int, dict[str, Any]] = {}

    @staticmethod
    def _entry_id(entry: dict[str, Any]) -> int:
        return int(entry.get("id", 0))

    def is_historical(self, entry: dict[str, Any]) -> bool:
        return bool(entry.get(HISTORICAL_FIELD))

    def account(self, entry_id: int) -> dict[str, Any] | None:
        return self._accounts.get(entry_id)

    def open_from(self, entry: dict[str, Any]) -> dict[str, Any]:
        """材料记录首次纳入台账时按原记录开户（历史回填的基准动作）。"""
        entry_id = self._entry_id(entry)
        account = self._accounts.get(entry_id)
        if account is None:
            account = {
                LEDGER_BALANCE: parse_balance(entry.get(BALANCE_FIELD)),
                # 原记录已是冻结状态的，冻结事实一并继承，结存数量不动
                LEDGER_FROZEN: entry.get("status") == STATUS_FROZEN,
                LEDGER_HISTORICAL: self.is_historical(entry),
            }
            self._accounts[entry_id] = account
        return account

    def sync_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        """确保台账里有这条材料的账号，并按台账把记录重算一遍。"""
        account = self.open_from(entry)
        return reconcile_entry(entry, account)

    def backfill(self, entries: list[dict[str, Any]]) -> None:
        """已有库存记录按新口径回填结存数；历史材料按原记录保留。"""
        for entry in entries:
            self.sync_entry(entry)

    def all_accounts(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """台账全量视图：每条材料对应一个账号，状态由台账当场推导。

        看板与台账列表都从这里取数，保证两处结存数一致；历史材料的
        状态/结存沿用原记录，只打历史标记，不混进在库汇总。
        """
        snapshot: list[dict[str, Any]] = []
        for entry in entries:
            account = self.open_from(entry)
            if account.get(LEDGER_HISTORICAL):
                status = str(entry.get("status") or "")
                balance = parse_balance(entry.get(BALANCE_FIELD))
                frozen = status == STATUS_FROZEN
            else:
                balance = parse_balance(account.get(LEDGER_BALANCE))
                frozen = bool(account.get(LEDGER_FROZEN))
                status = derive_status(balance, frozen=frozen)
            snapshot.append({
                "id": self._entry_id(entry),
                LEDGER_BALANCE: balance,
                LEDGER_FROZEN: frozen,
                LEDGER_HISTORICAL: bool(account.get(LEDGER_HISTORICAL)),
                "status": status,
            })
        return snapshot

    def apply(self, entry: dict[str, Any], operation: str) -> dict[str, Any]:
        """对台账执行冻结/解冻/耗尽，再以台账为准重算记录。

        - freeze：只置冻结标记，结存数量不变；
        - unfreeze：只清冻结标记，结存数量保持台账值（修复「解冻丢结存」）；
        - deplete：结存数量登记为 0，冻结标记保留与否不再影响结果。
        """
        account = self.open_from(entry)
        if account[LEDGER_HISTORICAL]:
            return entry
        if operation == "freeze":
            account[LEDGER_FROZEN] = True
        elif operation == "unfreeze":
            account[LEDGER_FROZEN] = False
        elif operation == "deplete":
            account[LEDGER_BALANCE] = 0
        return reconcile_entry(entry, account)


# 进程内唯一台账实例：列表、单条动作、批量动作、看板都从这里取数
inventory_ledger = InventoryLedger()
