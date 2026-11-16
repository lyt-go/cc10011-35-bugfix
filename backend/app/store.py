"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

除了业务表之外，仓库还提供两件映射链路必需的能力：

* ``transaction()``：按表快照实现原子提交/回滚。映射版本与标准维度数据集必须
  同时落库，任何一步写入失败都要整体回退，不能把残缺版本暴露给查询。
* ``check_write()`` / ``fail_next_write()``：可注入的写入失败点，用来验证事务
  失败时数据集、版本与来源记录仍然一致（生产环境由数据库驱动承担这一角色）。
"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS

# 内部表只服务于维度映射解析链，不在运营概览里当成独立业务模块展示。
HIDDEN_MODULES = {
    "raw_dimension_records",
    "standard_dimension_dataset",
    "dimensionmap_versions",
    "mapping_recalc_log",
    "drilldown_results",
    "aggregate_results",
}


class StoreWriteError(RuntimeError):
    """模拟数据库写入失败：事务会整体回滚，调用方得到可读的错误。"""


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tables: dict[str, list[dict[str, Any]]] = {}
        self._txn_depth = 0
        self._txn_snapshot: dict[str, list[dict[str, Any]]] | None = None
        self._injected_failures: dict[str, int] = {}
        self.reset()

    def reset(self) -> None:
        """恢复示例数据；测试和本地重置都走这里。"""
        with self._lock:
            self._tables = {
                name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
            }
            self._txn_depth = 0
            self._txn_snapshot = None
            self._injected_failures = {}

    def module_names(self) -> list[str]:
        with self._lock:
            return sorted(name for name in self._tables if name not in HIDDEN_MODULES)

    def rows(self, module: str) -> list[dict[str, Any]]:
        # RLock 让同一线程在事务里可以继续读写；其它线程在事务提交前会被挡住，
        # 因而看不到“构建中”的中间状态。
        with self._lock:
            return self._tables.setdefault(module, [])

    def read_rows(self, module: str) -> list[dict[str, Any]]:
        """只读访问入口，语义与 rows 一致，便于区分读路径与写路径。"""
        return self.rows(module)

    def replace_rows(self, module: str, rows: list[dict[str, Any]]) -> None:
        with self._lock:
            self._tables[module] = [dict(row) for row in rows]

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        with self._lock:
            for row in self.rows(module):
                if int(row.get("id", 0)) == entry_id:
                    return row
            return None

    def next_id(self, module: str) -> int:
        with self._lock:
            return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    @contextmanager
    def transaction(self) -> Iterator["Store"]:
        """事务边界：最外层进入时做全量快照，异常或提交失败整体回滚。

        支持嵌套调用；只有最外层负责提交校验与回滚，内层异常会向上冒泡。
        """
        with self._lock:
            outermost = self._txn_depth == 0
            if outermost:
                self._txn_snapshot = copy.deepcopy(self._tables)
            self._txn_depth += 1
            try:
                yield self
            except BaseException:
                self._txn_depth -= 1
                if outermost:
                    self._rollback()
                raise
            else:
                self._txn_depth -= 1
                if outermost:
                    try:
                        # 模拟 COMMIT 阶段失败：数据集与版本已写入，但提交失败。
                        self.check_write("commit")
                    except BaseException:
                        self._rollback()
                        raise
                    self._txn_snapshot = None
                # 内层事务不单独提交，由最外层统一决定

    def _rollback(self) -> None:
        if self._txn_snapshot is not None:
            self._tables = self._txn_snapshot
        self._txn_snapshot = None
        self._txn_depth = 0

    # ---- 写入失败注入 -------------------------------------------------
    def fail_next_write(self, operation: str = "commit") -> None:
        """下一次指定写入操作抛出 StoreWriteError（只生效一次）。"""
        with self._lock:
            self._injected_failures[operation] = (
                self._injected_failures.get(operation, 0) + 1
            )

    def clear_injected_failures(self) -> None:
        with self._lock:
            self._injected_failures = {}

    def check_write(self, operation: str) -> None:
        with self._lock:
            remaining = self._injected_failures.get(operation, 0)
            if not remaining and operation != "*":
                remaining = self._injected_failures.get("*", 0)
                if remaining:
                    self._injected_failures["*"] = remaining - 1
                    raise StoreWriteError(f"模拟数据库写入失败：{operation}")
                return
            if remaining:
                if remaining == 1:
                    self._injected_failures.pop(operation, None)
                else:
                    self._injected_failures[operation] = remaining - 1
                raise StoreWriteError(f"模拟数据库写入失败：{operation}")

    def snapshot_tables(self) -> dict[str, list[dict[str, Any]]]:
        with self._lock:
            return copy.deepcopy(self._tables)

    # ---- 运营概览 -----------------------------------------------------
    def overview(self) -> dict[str, object]:
        with self._lock:
            modules: list[dict[str, object]] = []
            for name in self.module_names():
                rows = self.rows(name)
                modules.append({
                    "name": name,
                    "created": len(rows),
                    "pending": sum(1 for row in rows if row.get("pending")),
                    "abnormal": sum(1 for row in rows if row.get("abnormal")),
                })
            cards = [
                {"label": "业务模块", "value": len(modules)},
                {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
                {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
                {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
            ]
            return {"cards": cards, "modules": modules}


store = Store()
