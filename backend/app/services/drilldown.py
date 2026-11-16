"""多维下钻业务规则：路径状态流转 + 分层结果（消费标准维度映射数据集）。

下钻结果不再自己拼接原始维度名，而是严格读取映射解析链生成的
标准维度数据集；每一行都带原始维度名、标准维度名、命中规则与映射版本，
因此刷新前后不会出现标准/原始维度名错位。
"""
from __future__ import annotations

from typing import Any

from app.services import dimensionmap_chain as chain
from app.store import store

MODULE = "drilldown"
REQUIRED_FIELDS = ["路径编号", "起始维度", "下钻层级序列"]
STATUS_ORDER = ["草稿", "路径评审中", "已启用", "已归档"]
ACTION_RULES = {"提交路径评审": "路径评审中", "启用路径": "已启用", "归档路径": "已归档"}
NEGATIVE_ACTIONS = []


class DrilldownService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        chain.ensure_initialized()
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("路径编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        chain.ensure_initialized()
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        chain.ensure_initialized()
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        levels = chain.parse_dimension_list(values.get("下钻层级序列"))
        start_dim = str(values.get("起始维度") or "").strip()
        if start_dim and levels and levels[0] != start_dim:
            return None, ["起始维度必须是下钻层级序列的第一层，避免层级顺序与起始维度错位"]
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in ("每层聚合粒度", "默认度量", "路径深度", "缓存秒数", "数据新鲜度"):
            if field in values:
                entry[field] = values.get(field)
        entry.setdefault("路径深度", len(levels))
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        chain.ensure_initialized()
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"下钻路径 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于多维下钻可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if entry.get("status") == target:
            return None, f"下钻路径已处于「{target}」状态，无需重复执行"

        was_enabled = entry.get("status") == "已启用"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        entry["最近更新时间"] = chain.now_iso()
        message = f"下钻路径已{action}"

        # 启用/归档会改变下钻结果集合；按当前映射版本立即刷新，避免残留旧分层。
        if target == "已启用" or (was_enabled and target == "已归档"):
            result, error = chain.refresh_drilldown(entry_id)
            if error:
                # 状态已经落库，但结果无法按当前映射生成时要明确告知。
                return entry, f"{message}，但分层结果刷新失败：{error}"
            if result:
                if result.get("已清理残留结果"):
                    message += "，已清理该路径的残留分层结果"
                else:
                    message += (
                        f"，已按映射版本 {result['映射版本']} 刷新分层结果 {result['结果行数']} 行"
                    )
        return entry, message

    # ---- 分层结果 / 追踪 ----------------------------------------------
    def list_results(
        self,
        *,
        path_code: str | None = None,
        version_number: int | None = None,
        page: int = 1,
        size: int = 50,
    ) -> tuple[list[dict[str, Any]], int, dict[str, Any] | None]:
        return chain.list_drilldown_results(
            path_code=path_code,
            version_number=version_number,
            page=page,
            size=size,
        )

    def refresh(self, entry_id: int) -> tuple[dict[str, Any] | None, str | None]:
        return chain.refresh_drilldown(entry_id)

    def mapping_link(self) -> dict[str, Any]:
        """下钻页与维度映射版本的联动信息。"""
        chain.ensure_initialized()
        version = chain.current_version()
        paths = {}
        for row in store.read_rows(chain.DRILLDOWN_RESULTS):
            code = row.get("路径编号")
            meta = paths.setdefault(code, {
                "路径编号": code,
                "映射版本": row.get("映射版本"),
                "回算批次": row.get("回算批次"),
                "结果行数": 0,
                "默认命中行数": 0,
            })
            meta["结果行数"] += 1
            if row.get("是否存在默认命中"):
                meta["默认命中行数"] += 1
        return {
            "当前版本": version.get("版本标识") if version else None,
            "当前版本映射版本": version.get("映射版本") if version else None,
            "层级口径": "严格按标准维度数据集聚合；原始维度名与标准维度名分列返回",
            "paths": list(paths.values()),
        }
