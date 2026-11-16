"""聚合配置业务规则：状态流转 + 目标粒度结果（消费标准维度映射数据集）。

聚合结果严格按标准维度去重分组：同一个原始维度值只对应一个标准维度映射，
因此目标粒度统计不会因为同一原始值命中多条规则而重复，也不会因为默认值
覆盖具体映射而落到错误分组里。聚合详情同时返回记录数核对信息。
"""
from __future__ import annotations

from typing import Any

from app.services import dimensionmap_chain as chain
from app.store import store

MODULE = "aggregate"
REQUIRED_FIELDS = ["规则编号", "源数据粒度", "目标粒度"]
STATUS_ORDER = ["草稿", "规则评审中", "已生效", "已废弃"]
ACTION_RULES = {"提交规则评审": "规则评审中", "生效规则": "已生效", "废弃规则": "已废弃"}
NEGATIVE_ACTIONS = []


class AggregateService:
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
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
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
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in ("聚合函数集", "数据偏移分钟", "补全策略", "空值处理", "输出数据集"):
            if field in values:
                entry[field] = values.get(field)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        chain.ensure_initialized()
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"聚合规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于聚合配置可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if entry.get("status") == target:
            return None, f"聚合规则已处于「{target}」状态，无需重复执行"

        was_enabled = entry.get("status") == "已生效"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        entry["最近更新时间"] = chain.now_iso()
        message = f"聚合规则已{action}"

        if target == "已生效" or (was_enabled and target == "已废弃"):
            result, error = chain.recalc_aggregate(entry_id)
            if error:
                return entry, f"{message}，但目标粒度回算失败：{error}"
            if result:
                if result.get("已清理残留结果"):
                    message += "，已清理该规则的目标粒度结果"
                else:
                    check = result.get("核对", {})
                    message += (
                        f"，已按映射版本 {result['映射版本']} 回算 {result['结果行数']} 个分组"
                        f"（覆盖 {check.get('覆盖记录数', 0)} 条/未覆盖 {check.get('未覆盖记录数', 0)} 条）"
                    )
        return entry, message

    # ---- 目标粒度结果 / 核对 ------------------------------------------
    def list_results(
        self,
        *,
        rule_code: str | None = None,
        version_number: int | None = None,
        page: int = 1,
        size: int = 50,
    ) -> tuple[list[dict[str, Any]], int, dict[str, Any] | None]:
        return chain.list_aggregate_results(
            rule_code=rule_code,
            version_number=version_number,
            page=page,
            size=size,
        )

    def recalc(self, entry_id: int) -> tuple[dict[str, Any] | None, str | None]:
        return chain.recalc_aggregate(entry_id)

    def detail(self, entry_id: int) -> tuple[dict[str, Any] | None, str | None]:
        """聚合详情：规则 + 结果 + 重复/丢失核对，用于与映射列表交叉验证。"""
        chain.ensure_initialized()
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"聚合规则 {entry_id} 不存在或已归档"
        rule_code = chain.normalize_text(entry.get("规则编号"))
        results, total, version = chain.list_aggregate_results(rule_code=rule_code, page=1, size=200)
        check = chain.aggregate_check(rule_code)
        return {
            "规则": entry,
            "映射版本": version.get("映射版本") if version else None,
            "版本标识": version.get("版本标识") if version else None,
            "结果": results,
            "结果行数": total,
            "核对": check,
        }, None

    def mapping_link(self) -> dict[str, Any]:
        """聚合配置与映射版本联动核对：每条规则的分组数、覆盖与重复情况。"""
        chain.ensure_initialized()
        version = chain.current_version()
        rules = []
        for row in store.rows(MODULE):
            code = chain.normalize_text(row.get("规则编号"))
            rules.append({
                "规则编号": code,
                "status": row.get("status"),
                "核对": chain.aggregate_check(code),
            })
        return {
            "当前版本": version.get("版本标识") if version else None,
            "当前版本映射版本": version.get("映射版本") if version else None,
            "rules": rules,
        }
