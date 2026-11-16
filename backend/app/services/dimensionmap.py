"""维度映射业务规则：状态流转、字段校验、优先级落库与映射重建。

映射规则不再只是“登记三个必填字段”：值转换逻辑、默认取值、映射优先级、
匹配值和最近更新时间都会被完整保存。每次规则变更都会在同一个事务里生成
新的映射版本、重建标准维度数据集，并回算受影响的下载与聚合结果。
"""
from __future__ import annotations

from typing import Any

from app.services import dimensionmap_chain as chain
from app.store import store

MODULE = "dimensionmap"
REQUIRED_FIELDS = ["规则编号", "原始维度名", "标准维度名"]
STATUS_ORDER = ["草稿", "规则评审中", "已生效", "已废弃"]
ACTION_RULES = {"提交规则评审": "规则评审中", "生效规则": "已生效", "废弃规则": "已废弃"}
ACTION_MESSAGES = {"提交规则评审": "已提交规则评审", "生效规则": "已生效", "废弃规则": "已废弃"}
NEGATIVE_ACTIONS = []

# 规则变更时必须整体落库的字段：少了任何一项，优先级/默认值就会被“解析链”
# 当成空值处理，正是历史上默认值覆盖更具体映射的根因。
PERSISTED_FIELDS = [
    "规则编号",
    "原始维度名",
    "标准维度名",
    "映射类型",
    "匹配值",
    "值转换逻辑",
    "默认取值",
    "映射优先级",
    "最近更新时间",
    "规则说明",
]


def _normalize_payload(values: dict[str, Any]) -> dict[str, Any]:
    """把外部字段别名折叠成中文业务字段名，并丢弃内部字段。"""
    normalized: dict[str, Any] = {}
    for key, value in values.items():
        field = chain.FIELD_ALIASES.get(key, key)
        if field in chain.RULE_FIELDS:
            normalized[field] = value
    return normalized


def _validate(values: dict[str, Any], *, partial: bool = False) -> list[str]:
    errors: list[str] = []
    if not partial:
        missing = [
            field for field in REQUIRED_FIELDS
            if not chain.normalize_text(values.get(field))
        ]
        if missing:
            return missing
    priority_text = chain.normalize_text(values.get("映射优先级"))
    if priority_text:
        priority, defaulted, _ = chain.normalize_priority(priority_text)
        if priority == 0 and not defaulted and priority_text not in {"中", "p2", "P2"}:
            # 非法优先级会被按 0 处理，必须显式拦下，避免“静默变最低优先级”。
            try:
                float(priority_text)
            except ValueError:
                errors.append(
                    f"映射优先级「{priority_text}」无法解析为数值，请填写数字或 高/中/低"
                )
    match_type = chain.normalize_text(values.get("映射类型"))
    if match_type:
        folded = match_type.casefold()
        if folded not in chain.MATCH_TYPE_ALIASES and match_type not in chain.MATCH_TYPE_ALIASES.values():
            errors.append(f"映射类型「{match_type}」不受支持，可选：精确匹配、前缀匹配、正则匹配、值转换、默认映射")
    if chain.normalize_text(values.get("映射类型")).casefold() in {"正则匹配", "regex", "regexp"}:
        pattern = chain.normalize_text(values.get("匹配值"))
        if pattern:
            try:
                import re

                re.compile(pattern)
            except re.error as error:
                errors.append(f"匹配表达式不是合法正则：{error}")
    return errors


def _apply_values(entry: dict[str, Any], values: dict[str, Any], *, increment_version: bool) -> None:
    for field in PERSISTED_FIELDS:
        if field in values:
            if field == "映射优先级":
                priority, _, raw = chain.normalize_priority(values[field])
                entry["映射优先级"] = priority
                entry["优先级原始值"] = raw
            elif field == "最近更新时间":
                text = chain.normalize_text(values[field])
                entry["最近更新时间"] = text or chain.now_iso()
            else:
                entry[field] = chain.normalize_text(values[field])
    entry["最近更新时间"] = entry.get("最近更新时间") or chain.now_iso()
    if increment_version:
        # 已有规则默认视为第 1 版；每次修改/状态流转都 +1，便于按版本追溯。
        entry["规则版本"] = int(entry.get("规则版本", 1) or 1) + 1


class DimensionmapService:
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
            rows = [
                row for row in rows
                if keyword in str(row.get("规则编号", ""))
                or keyword in str(row.get("原始维度名", ""))
                or keyword in str(row.get("标准维度名", ""))
            ]
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
        values = _normalize_payload(values)
        missing = _validate(values)
        if missing:
            return None, missing
        errors = _validate(values, partial=True)
        if errors:
            return None, errors
        rule_code = chain.normalize_text(values.get("规则编号"))
        for row in store.rows(MODULE):
            if chain.normalize_text(row.get("规则编号")) == rule_code:
                return None, [f"规则编号「{rule_code}」已存在，请换一个编号"]

        def mutate() -> dict[str, Any]:
            # 规则编号是业务主键，必须和转换逻辑、优先级一起落库；此前只把三个
            # 必填字段拷进新记录，导致后续按规则编号追溯时命中规则为空。
            entry = {"id": store.next_id(MODULE), "规则编号": rule_code}
            _apply_values(entry, values, increment_version=False)
            entry["规则版本"] = 1
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            store.rows(MODULE).append(entry)
            return entry

        entry, summary, error = chain.save_rule_with_rebuild(
            mutate=mutate,
            trigger_type="规则登记",
            trigger_action="登记映射规则",
            trigger_rule=rule_code,
            affected_dimensions={chain.normalize_text(values.get("原始维度名"))},
        )
        if error:
            return None, [error]
        if entry is not None:
            entry["映射重建"] = summary
        return entry, []

    def update_entry(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        """修改规则并完整保存转换逻辑、默认值、优先级与更新时间。"""
        chain.ensure_initialized()
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"映射规则 {entry_id} 不存在或已归档"
        if entry.get("status") == STATUS_ORDER[-1]:
            return None, "已废弃的规则不能修改，请新建规则"
        values = _normalize_payload(values)
        if not values:
            return None, "没有需要修改的字段"
        errors = _validate(values, partial=True)
        if errors:
            return None, "；".join(errors)
        rule_code = chain.normalize_text(entry.get("规则编号"))
        old_dimension = chain.normalize_text(entry.get("原始维度名"))
        new_dimension = chain.normalize_text(values.get("原始维度名") or old_dimension)

        def mutate() -> dict[str, Any]:
            _apply_values(entry, values, increment_version=True)
            return entry

        updated, summary, error = chain.save_rule_with_rebuild(
            mutate=mutate,
            trigger_type="规则修改",
            trigger_action="修改映射规则",
            trigger_rule=rule_code,
            affected_dimensions={old_dimension, new_dimension},
        )
        if error:
            return None, error
        if updated is not None:
            updated["映射重建"] = summary
        return updated, None

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        chain.ensure_initialized()
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"映射规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于维度映射可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if entry.get("status") == target:
            return None, f"映射规则已处于「{target}」状态，无需重复执行"

        before = chain.normalize_text(entry.get("status"))
        rule_code = chain.normalize_text(entry.get("规则编号"))
        dimension = chain.normalize_text(entry.get("原始维度名"))
        # 只有“已生效”的规则参与解析；生效/废弃都会改变映射结果，必须回算。
        recalc_needed = target == chain.ENABLED or before == chain.ENABLED

        def mutate() -> dict[str, Any]:
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            entry["最近更新时间"] = chain.now_iso()
            entry["规则版本"] = int(entry.get("规则版本", 1) or 1) + 1
            return entry

        if recalc_needed:
            updated, summary, error = chain.save_rule_with_rebuild(
                mutate=mutate,
                trigger_type="规则状态流转",
                trigger_action=action,
                trigger_rule=rule_code,
                affected_dimensions={dimension},
            )
            if error:
                return None, error
            if updated is not None:
                updated["映射重建"] = summary
            message = f"映射规则{ACTION_MESSAGES.get(action, action)}"
            if summary:
                message += (
                    f"，已生成映射版本 {summary['版本标识']}，"
                    f"回算下钻 {summary['下钻回算行数']} 行、聚合 {summary['聚合回算行数']} 行"
                )
            return updated, message

        # 提交评审不改变解析结果，只推进状态；仍写入版本快照便于追溯。
        def mutate_only() -> dict[str, Any]:
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            entry["最近更新时间"] = chain.now_iso()
            entry["规则版本"] = int(entry.get("规则版本", 1) or 1) + 1
            return entry

        updated, summary, error = chain.save_rule_with_rebuild(
            mutate=mutate_only,
            trigger_type="规则状态流转",
            trigger_action=action,
            trigger_rule=rule_code,
            affected_dimensions=set(),
        )
        if error:
            return None, error
        if updated is not None:
            updated["映射重建"] = summary
        return updated, f"映射规则{ACTION_MESSAGES.get(action, action)}"

    # ---- 数据集 / 版本 / 追踪 -----------------------------------------
    def list_dataset(
        self,
        *,
        keyword: str | None = None,
        raw_dimension: str | None = None,
        standard_dimension: str | None = None,
        version_number: int | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int, dict[str, Any] | None]:
        return chain.list_dataset(
            keyword=keyword,
            raw_dimension=raw_dimension,
            standard_dimension=standard_dimension,
            version_number=version_number,
            page=page,
            size=size,
        )

    def list_versions(self, limit: int = 50) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        return chain.list_versions(limit=limit)

    def get_version(self, version_number: int) -> dict[str, Any] | None:
        return chain.get_version(version_number)

    def resolve_preview(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        # 预览入参保持中文业务字段名；这里不能用 _normalize_payload，因为
        # “原始维度值”不是规则字段，会被白名单过滤掉。
        raw_dimension = chain.normalize_text(
            values.get("原始维度名") or values.get("raw_dimension")
        )
        raw_value = chain.normalize_text(
            values.get("原始维度值") or values.get("raw_value") or values.get("取值")
        )
        if not raw_dimension:
            return None, "请提供原始维度名"
        if not raw_value:
            return None, "请提供原始维度值"
        version = values.get("映射版本")
        version_number = int(version) if version not in (None, "") else None
        return chain.resolve_preview(
            raw_dimension=raw_dimension,
            raw_value=raw_value,
            version_number=version_number,
        ), None

    def rebuild(self) -> tuple[dict[str, Any] | None, str | None]:
        return chain.rebuild_all(), None

    def mapping_summary(self) -> dict[str, Any]:
        """映射列表页顶部核对：规则数、当前版本、数据集质量。"""
        chain.ensure_initialized()
        rows = store.rows(MODULE)
        version = chain.current_version()
        dataset = store.read_rows(chain.DATASET)
        return {
            "规则总数": len(rows),
            "已生效规则": sum(1 for row in rows if row.get("status") == chain.ENABLED),
            "评审中规则": sum(1 for row in rows if row.get("status") == "规则评审中"),
            "当前版本": version.get("版本标识") if version else None,
            "当前版本映射版本": version.get("映射版本") if version else None,
            "数据集行数": len(dataset),
            "默认命中行数": sum(1 for row in dataset if row.get("是否默认命中")),
            "同级冲突值数": sum(1 for row in dataset if row.get("同级冲突")),
            "未命中规则值数": sum(1 for row in dataset if row.get("映射状态") != "已映射"),
            "冲突处理策略": chain.CONFLICT_POLICY,
        }
