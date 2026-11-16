"""维度映射解析链：规则归一化、优先级解析、标准维度数据集与版本追溯。

这条链同时被三处消费，任何一处都不得再各写一套“取第一条规则”的逻辑：

* 维度映射：登记 / 修改 / 启停规则时，写规则、生成映射版本、重建标准维度数据集；
* 多维下钻：刷新分层结果时读取标准维度数据集，按标准维度聚合；
* 聚合配置：回算目标粒度时读取同一份标准维度数据集，按标准维度去重聚合。

几个关键约束（对应曾经的事故）：

1. 映射优先级必须原样落库并归一化成可比较的数值，不能因为只保存三个必填字段而丢失；
2. 默认映射永远不能覆盖更具体的映射，即使默认规则的数字优先级更大；
3. 同一原始维度 + 原始维度值只产出一个标准维度映射结果，落选规则留在 trace 里；
4. 每次生成数据集都同时写入映射版本，版本、数据集、来源记录在同一事务里提交；
5. 同一时刻只有一个“当前”版本；并发启停规则由仓库锁串行化，不会出现两个当前版本。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timezone
from typing import Any, Iterable

from app.store import StoreWriteError, store

DIMENSIONMAP = "dimensionmap"
RAW_RECORDS = "raw_dimension_records"
DATASET = "standard_dimension_dataset"
VERSIONS = "dimensionmap_versions"
RECALC_LOG = "mapping_recalc_log"
DRILLDOWN_RESULTS = "drilldown_results"
AGGREGATE_RESULTS = "aggregate_results"

CURRENT = "当前"
HISTORICAL = "历史"
ENABLED = "已生效"

CONFLICT_POLICY = (
    "具体映射优先于默认映射；同为具体映射时按映射优先级从高到低，"
    "优先级相同再按规则匹配精度、最近更新时间、规则编号依次决定；"
    "默认映射只在没有任何具体映射命中时生效。"
)

# 规则里允许被完整保存的字段。只存白名单，避免把 status / id 之类的内部字段
# 从请求体里带进来覆盖状态机。
RULE_FIELDS = [
    "规则编号",
    "原始维度名",
    "标准维度名",
    "映射类型",
    "值转换逻辑",
    "默认取值",
    "映射优先级",
    "最近更新时间",
    "匹配值",
    "匹配表达式",
    "规则说明",
]

FIELD_ALIASES = {
    "rule_code": "规则编号",
    "raw_dimension": "原始维度名",
    "standard_dimension": "标准维度名",
    "mapping_type": "映射类型",
    "conversion": "值转换逻辑",
    "value_transform": "值转换逻辑",
    "default_value": "默认取值",
    "priority": "映射优先级",
    "updated_at": "最近更新时间",
    "match_value": "匹配值",
    "match_pattern": "匹配值",
    "pattern": "匹配值",
    "description": "规则说明",
}

MATCH_TYPE_ALIASES = {
    "exact": "精确匹配",
    "equals": "精确匹配",
    "精确": "精确匹配",
    "精确匹配": "精确匹配",
    "prefix": "前缀匹配",
    "startswith": "前缀匹配",
    "前缀": "前缀匹配",
    "前缀匹配": "前缀匹配",
    "regex": "正则匹配",
    "regexp": "正则匹配",
    "正则": "正则匹配",
    "正则匹配": "正则匹配",
    "default": "默认映射",
    "fallback": "默认映射",
    "默认": "默认映射",
    "默认映射": "默认映射",
    "transform": "值转换",
    "conversion": "值转换",
    "值转换": "值转换",
}

SPECIFICITY = {
    "精确匹配": 100,
    "正则匹配": 80,
    "前缀匹配": 60,
    "值转换": 40,
    "默认映射": 0,
}

PRIORITY_ALIASES = {
    "最高": 100,
    "高": 80,
    "中": 50,
    "低": 20,
    "最低": 5,
    "p0": 100,
    "p1": 80,
    "p2": 50,
    "p3": 20,
}

_DIM_SPLIT = re.compile(r"[,，、;；|/>\s]+")

# 初始版本只允许由一个线程生成，避免并发首访产生两个“当前”版本。
_INIT_LOCK = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_priority(raw: Any) -> tuple[float | int, bool, str]:
    """把优先级归一化成可排序数值。

    返回 (数值, 是否由默认值补齐, 原始值)。数值越大优先级越高；
    空值按 0 处理并标记，保证“没填优先级”不会意外压过显式优先级。
    """
    if raw is None or normalize_text(raw) == "":
        return 0, True, normalize_text(raw)
    text = normalize_text(raw)
    lowered = text.casefold()
    if lowered in PRIORITY_ALIASES:
        return PRIORITY_ALIASES[lowered], False, text
    try:
        number = float(text)
    except ValueError:
        # 非法优先级不能静默变成高优先级，同样按 0 处理并保留原文。
        return 0, False, text
    if number.is_integer():
        number = int(number)
    if number < -1_000_000:
        number = -1_000_000
    if number > 1_000_000:
        number = 1_000_000
    return number, False, text


def normalize_match_type(rule: dict[str, Any]) -> str:
    raw_type = normalize_text(rule.get("映射类型"))
    if raw_type.casefold() in MATCH_TYPE_ALIASES:
        return MATCH_TYPE_ALIASES[raw_type.casefold()]
    if raw_type in MATCH_TYPE_ALIASES.values():
        return raw_type
    pattern = normalize_text(rule.get("匹配值") or rule.get("匹配表达式"))
    logic = normalize_text(rule.get("值转换逻辑"))
    default_value = normalize_text(rule.get("默认取值"))
    if pattern:
        return "精确匹配"
    if logic:
        return "值转换"
    # 只声明了原始维度名 -> 标准维度名的规则是维度级兜底，归入默认映射，
    # 避免它和真正的精确映射打成平手后随机覆盖后者。
    return "默认映射" if default_value or normalize_text(rule.get("标准维度名")) else "值转换"


def normalize_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """返回规则的可比较视图，不改动仓库里的原始记录。"""
    priority, priority_defaulted, priority_raw = normalize_priority(rule.get("映射优先级"))
    match_type = normalize_match_type(rule)
    is_default = match_type == "默认映射"
    return {
        "id": int(rule.get("id", 0) or 0),
        "规则编号": normalize_text(rule.get("规则编号")),
        "原始维度名": normalize_text(rule.get("原始维度名")),
        "标准维度名": normalize_text(rule.get("标准维度名")),
        "映射类型": normalize_text(rule.get("映射类型")) or match_type,
        "匹配类型": match_type,
        "匹配值": normalize_text(rule.get("匹配值") or rule.get("匹配表达式")),
        "值转换逻辑": normalize_text(rule.get("值转换逻辑")),
        "默认取值": normalize_text(rule.get("默认取值")),
        "映射优先级": priority,
        "优先级数值": priority,
        "优先级原始值": priority_raw,
        "优先级是否补齐": priority_defaulted,
        "匹配精度": SPECIFICITY.get(match_type, 0),
        "是否默认映射": is_default,
        "status": normalize_text(rule.get("status")),
        "规则版本": int(rule.get("规则版本", 1) or 1),
        "最近更新时间": normalize_text(rule.get("最近更新时间")),
    }


def _split_pipeline(logic: str) -> list[str]:
    """值转换逻辑用 `|` 串联；正则/替换里的 `|` 需要写成 `\\|`。"""
    return [part.strip() for part in logic.split("|") if part.strip()]


def _parse_pair(text: str, separators: tuple[str, ...]) -> tuple[str, str] | None:
    for separator in separators:
        if separator in text:
            left, right = text.split(separator, 1)
            return left.strip(), right.strip()
    return None


def _parse_mapping_pairs(text: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for chunk in re.split(r"[,，;；]", text):
        chunk = chunk.strip()
        if not chunk:
            continue
        pair = _parse_pair(chunk, ("=>", "->", "=", ":"))
        if pair:
            key, value = pair
            if key:
                pairs[key.casefold()] = value
    return pairs


def apply_transform(raw_value: str, logic: str) -> tuple[str, list[str]]:
    """执行值转换 DSL，返回 (转换结果, 警告列表)。

    支持的写法（可用 `|` 串联）：
      trim / lower / upper
      map:华北=华北区,North=华北区
      replace:旧值->新值
      strip_prefix:CN- / strip_suffix:-OLD
      regex:^A(\\d+)$->A$1
    """
    value = normalize_text(raw_value)
    warnings: list[str] = []
    logic = normalize_text(logic)
    if not logic:
        return value, warnings

    if logic.startswith("{"):
        try:
            spec = json.loads(logic)
        except json.JSONDecodeError:
            warnings.append("值转换逻辑不是合法 JSON，已按原值输出")
            return value, warnings
        if isinstance(spec, dict):
            if "trim" in spec and spec["trim"]:
                value = value.strip()
            if spec.get("lower"):
                value = value.lower()
            if spec.get("upper"):
                value = value.upper()
            mapping = spec.get("map") or spec.get("replace")
            if isinstance(mapping, dict):
                value = mapping.get(value, mapping.get(value.casefold(), value))
            if spec.get("strip_prefix"):
                prefix = str(spec["strip_prefix"])
                if value.startswith(prefix):
                    value = value[len(prefix):]
            if spec.get("strip_suffix"):
                suffix = str(spec["strip_suffix"])
                if value.endswith(suffix):
                    value = value[: -len(suffix)]
            return value, warnings
        warnings.append("值转换逻辑 JSON 不是对象，已按原值输出")
        return value, warnings

    for operation in _split_pipeline(logic):
        name, _, argument = operation.partition(":")
        name = name.strip().casefold()
        argument = argument.strip()
        if name == "trim":
            value = value.strip()
        elif name == "lower":
            value = value.lower()
        elif name == "upper":
            value = value.upper()
        elif name in {"map", "alias"}:
            pairs = _parse_mapping_pairs(argument)
            value = pairs.get(value.casefold(), value)
        elif name == "replace":
            pair = _parse_pair(argument, ("=>", "->"))
            if pair:
                value = value.replace(pair[0], pair[1])
            else:
                warnings.append("replace 缺少 '旧值->新值' 参数")
        elif name == "strip_prefix":
            if value.startswith(argument):
                value = value[len(argument):]
        elif name == "strip_suffix":
            if value.endswith(argument):
                value = value[: -len(argument)]
        elif name == "regex":
            pair = _parse_pair(argument, ("=>", "->"))
            if pair:
                try:
                    value = re.sub(pair[0], pair[1], value)
                except re.error:
                    warnings.append("regex 表达式非法，已跳过")
            else:
                warnings.append("regex 缺少 '表达式->替换值' 参数")
        else:
            warnings.append(f"未知转换操作「{name}」，已跳过")
    return value, warnings


def _rule_matches(rule: dict[str, Any], raw_value: str) -> bool:
    match_type = rule["匹配类型"]
    pattern = rule["匹配值"]
    value = normalize_text(raw_value)
    if match_type == "默认映射":
        return True
    if not pattern:
        # 值转换规则没有匹配值时为该维度的通用转换；精确/前缀/正则缺少
        # 匹配值则无法判定，不能命中。
        return match_type == "值转换" and value != ""
    if match_type == "精确匹配":
        return value.casefold() == pattern.casefold()
    if match_type == "前缀匹配":
        return value.casefold().startswith(pattern.casefold())
    if match_type == "正则匹配":
        try:
            return re.fullmatch(pattern, value) is not None
        except re.error:
            return False
    return value.casefold() == pattern.casefold()


def _rule_result(rule: dict[str, Any], raw_value: str) -> tuple[str, str, list[str]]:
    """返回 (标准维度名, 标准维度值, 转换警告)。"""
    standard_dim = rule["标准维度名"]
    warnings: list[str] = []
    if rule["匹配类型"] == "默认映射":
        value = rule["默认取值"] or normalize_text(raw_value)
        if not value and rule["标准维度名"]:
            value = rule["标准维度名"]
        warnings.append("默认映射兜底")
        return standard_dim, value, warnings
    value, transform_warnings = apply_transform(raw_value, rule["值转换逻辑"])
    warnings.extend(transform_warnings)
    if not value and rule["默认取值"]:
        value = rule["默认取值"]
        warnings.append("转换结果为空，使用默认取值")
    if not value:
        value = normalize_text(raw_value)
    return standard_dim, value, warnings


def _sort_key(rule: dict[str, Any], sequence: int) -> tuple:
    """越大越优先：具体映射 > 默认映射；再比数字优先级、匹配精度、更新时间。

    更新时间按字符串比较即可（ISO 秒级时间）；最后用仓库内序号保证完全确定，
    避免同一秒内并发写入导致同一原始值出现两个结果。
    """
    return (
        0 if rule["是否默认映射"] else 1,
        rule["优先级数值"],
        rule["匹配精度"],
        rule["最近更新时间"],
        sequence,
    )


def resolve_value(
    raw_dimension: str,
    raw_value: str,
    rules: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """为单个原始维度值选出唯一规则，并返回完整 trace。"""
    normalized_dim = normalize_text(raw_dimension)
    normalized_value = normalize_text(raw_value)
    candidates: list[tuple[tuple, dict[str, Any], str, str, list[str]]] = []
    for sequence, rule in enumerate(rules):
        if rule["原始维度名"] != normalized_dim:
            continue
        if rule["status"] != ENABLED:
            continue
        if not _rule_matches(rule, normalized_value):
            continue
        standard_dim, standard_value, warnings = _rule_result(rule, normalized_value)
        candidates.append((_sort_key(rule, sequence), rule, standard_dim, standard_value, warnings))

    trace_candidates: list[dict[str, Any]] = []
    if not candidates:
        return {
            "原始维度名": normalized_dim,
            "原始维度值": normalized_value,
            "标准维度名": normalized_dim,
            "标准维度值": normalized_value,
            "命中规则编号": None,
            "命中规则版本": None,
            "命中规则优先级": None,
            "命中规则更新时间": None,
            "映射类型": None,
            "值转换逻辑": "",
            "默认取值": "",
            "是否默认命中": False,
            "匹配类型": "未命中",
            "映射状态": "未命中规则",
            "候选规则": [],
            "同级冲突": False,
            "转换警告": [],
        }

    candidates.sort(key=lambda item: item[0], reverse=True)
    _, winner, standard_dim, standard_value, warnings = candidates[0]
    same_level = [
        item
        for item in candidates[1:]
        if item[0][:3] == candidates[0][0][:3]
        and (item[2], item[3]) != (standard_dim, standard_value)
    ]
    for _, rule, cand_dim, cand_value, _ in candidates:
        is_winner = rule is winner
        if is_winner:
            conclusion = "命中"
            reason = "具体映射优先" if not rule["是否默认映射"] else "无具体映射，默认映射兜底"
        elif rule["是否默认映射"] and not winner["是否默认映射"]:
            conclusion = "落选"
            reason = "默认映射不得覆盖具体映射"
        else:
            conclusion = "落选"
            reason = "优先级较低或同级排序规则靠后"
        trace_candidates.append({
            "规则编号": rule["规则编号"],
            "规则版本": rule["规则版本"],
            "优先级数值": rule["优先级数值"],
            "匹配类型": rule["匹配类型"],
            "匹配精度": rule["匹配精度"],
            "是否默认映射": rule["是否默认映射"],
            "最近更新时间": rule["最近更新时间"],
            "映射结果": {"标准维度名": cand_dim, "标准维度值": cand_value},
            "结论": conclusion,
            "结论说明": reason,
        })

    return {
        "原始维度名": normalized_dim,
        "原始维度值": normalized_value,
        "标准维度名": standard_dim,
        "标准维度值": standard_value,
        "命中规则编号": winner["规则编号"],
        "命中规则版本": winner["规则版本"],
        "命中规则优先级": winner["优先级数值"],
        "命中规则更新时间": winner["最近更新时间"],
        "映射类型": winner["映射类型"],
        "值转换逻辑": winner["值转换逻辑"],
        "默认取值": winner["默认取值"],
        "是否默认命中": winner["是否默认映射"],
        "匹配类型": winner["匹配类型"],
        "映射状态": "已映射",
        "候选规则": trace_candidates,
        "同级冲突": bool(same_level),
        "转换警告": warnings,
    }


def parse_dimension_list(value: Any) -> list[str]:
    """解析“region,渠道 / region > 渠道”这类粒度、层级配置。"""
    text = normalize_text(value)
    if not text:
        return []
    for arrow in ("->", "→", ">", "/"):
        text = text.replace(arrow, ",")
    result: list[str] = []
    for part in _DIM_SPLIT.split(text):
        part = part.strip()
        if part and part not in result:
            result.append(part)
    return result


# ---------------------------------------------------------------------------
# 版本与数据集
# ---------------------------------------------------------------------------
def _current_version_rows() -> list[dict[str, Any]]:
    return [row for row in store.read_rows(VERSIONS) if row.get("状态") == CURRENT]


def current_version() -> dict[str, Any] | None:
    rows = _current_version_rows()
    if not rows:
        return None
    return sorted(rows, key=lambda row: int(row.get("映射版本", 0)))[-1]


def _rule_snapshot(rules: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    snapshot: list[dict[str, Any]] = []
    for rule in rules:
        normalized = normalize_rule(rule)
        snapshot.append({
            "规则编号": normalized["规则编号"],
            "原始维度名": normalized["原始维度名"],
            "标准维度名": normalized["标准维度名"],
            "映射类型": normalized["映射类型"],
            "匹配值": normalized["匹配值"],
            "值转换逻辑": normalized["值转换逻辑"],
            "默认取值": normalized["默认取值"],
            "映射优先级": normalized["优先级数值"],
            "规则版本": normalized["规则版本"],
            "status": normalized["status"],
            "最近更新时间": normalized["最近更新时间"],
        })
    snapshot.sort(key=lambda item: (item["规则编号"], item["规则版本"]))
    fingerprint = hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return snapshot, fingerprint


def _create_version(
    *,
    trigger_type: str,
    trigger_action: str,
    trigger_rule: str | None,
    rules: list[dict[str, Any]],
    recalc_batch: str,
) -> dict[str, Any]:
    versions = store.rows(VERSIONS)
    replaced_at = now_iso()
    for row in versions:
        if row.get("状态") == CURRENT:
            row["状态"] = HISTORICAL
            row["被替代时间"] = replaced_at
    next_number = max((int(row.get("映射版本", 0)) for row in versions), default=0) + 1
    snapshot, fingerprint = _rule_snapshot(rules)
    version_row = {
        "id": store.next_id(VERSIONS),
        "映射版本": next_number,
        "版本标识": f"V{next_number}",
        "状态": CURRENT,
        "触发类型": trigger_type,
        "触发动作": trigger_action,
        "触发规则编号": trigger_rule,
        "规则数": len(snapshot),
        "生效规则数": sum(1 for item in snapshot if item["status"] == ENABLED),
        "规则指纹": fingerprint,
        "规则快照": snapshot,
        "数据集行数": 0,
        "下钻回算行数": 0,
        "聚合回算行数": 0,
        "回算批次": recalc_batch,
        "创建时间": now_iso(),
        "被替代时间": None,
    }
    versions.append(version_row)
    return version_row


def _build_dataset(version_number: int, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 (原始维度名, 原始维度值) 唯一生成标准维度映射结果。"""
    records = store.read_rows(RAW_RECORDS)
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        raw_dim = normalize_text(record.get("维度字段") or record.get("原始维度名"))
        if not raw_dim:
            continue
        raw_value = normalize_text(record.get("原始维度值"))
        key = (raw_dim, raw_value)
        group = groups.setdefault(key, {
            "数据集": set(),
            "来源记录编号": [],
            "来源记录数": 0,
        })
        dataset_name = normalize_text(record.get("数据集"))
        if dataset_name:
            group["数据集"].add(dataset_name)
        record_code = normalize_text(record.get("记录编号"))
        if record_code:
            marker = f"{dataset_name}:{record_code}" if dataset_name else record_code
            if marker not in group["来源记录编号"]:
                group["来源记录编号"].append(marker)
                group["来源记录数"] += 1

    mapping_time = now_iso()
    dataset_rows: list[dict[str, Any]] = []
    for (raw_dim, raw_value), group in sorted(groups.items(), key=lambda item: item[0]):
        resolved = resolve_value(raw_dim, raw_value, rules)
        dataset_rows.append({
            "id": len(dataset_rows) + 1,
            "数据集": "、".join(sorted(group["数据集"])) or "全部数据集",
            "原始维度名": resolved["原始维度名"],
            "原始维度值": resolved["原始维度值"],
            "标准维度名": resolved["标准维度名"],
            "标准维度值": resolved["标准维度值"],
            "命中规则编号": resolved["命中规则编号"],
            "命中规则版本": resolved["命中规则版本"],
            "命中规则优先级": resolved["命中规则优先级"],
            "命中规则更新时间": resolved["命中规则更新时间"],
            "映射类型": resolved["映射类型"],
            "值转换逻辑": resolved["值转换逻辑"],
            "默认取值": resolved["默认取值"],
            "是否默认命中": resolved["是否默认命中"],
            "匹配类型": resolved["匹配类型"],
            "映射状态": resolved["映射状态"],
            "映射版本": version_number,
            "映射时间": mapping_time,
            "候选规则": resolved["候选规则"],
            "同级冲突": resolved["同级冲突"],
            "转换警告": resolved["转换警告"],
            "来源记录数": group["来源记录数"],
            "来源记录编号": group["来源记录编号"][:200],
            "冲突处理策略": CONFLICT_POLICY,
        })
    return dataset_rows


# ---------------------------------------------------------------------------
# 下游结果回算：下钻 / 聚合都只读标准维度数据集
# ---------------------------------------------------------------------------
def _dataset_index(dataset_rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (row["原始维度名"], row["原始维度值"]): row
        for row in dataset_rows
    }


def _records_by_key() -> dict[tuple[str, str], dict[str, Any]]:
    """把长表形式的原始维度记录还原成“一条记录一组维度”。"""
    records: dict[tuple[str, str], dict[str, Any]] = {}
    for row in store.read_rows(RAW_RECORDS):
        dataset = normalize_text(row.get("数据集"))
        code = normalize_text(row.get("记录编号"))
        key = (dataset, code)
        record = records.setdefault(key, {
            "数据集": dataset,
            "记录编号": code,
            "事件时间": normalize_text(row.get("事件时间")),
            "维度": {},
            "度量值": 0.0,
            "维度冲突": 0,
        })
        raw_dim = normalize_text(row.get("维度字段") or row.get("原始维度名"))
        raw_value = normalize_text(row.get("原始维度值"))
        if raw_dim:
            if raw_dim in record["维度"] and record["维度"][raw_dim] != raw_value:
                record["维度冲突"] += 1
            record["维度"].setdefault(raw_dim, raw_value)
        try:
            measure = float(row.get("度量值") or 0)
        except (TypeError, ValueError):
            measure = 0.0
        if measure:
            record["度量值"] = measure
    return records


def _resolve_record_dimension(
    record: dict[str, Any],
    raw_dim: str,
    index: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any] | None:
    if raw_dim not in record["维度"]:
        return None
    raw_value = normalize_text(record["维度"][raw_dim])
    mapped = index.get((raw_dim, raw_value))
    if mapped is not None:
        return mapped
    # 数据集里没有该键（例如规则重建期间的新增记录），现场解析保证不丢数据。
    rules = [normalize_rule(row) for row in store.read_rows(DIMENSIONMAP)]
    return resolve_value(raw_dim, raw_value, rules)


def _enabled_normalized_rules() -> list[dict[str, Any]]:
    return [
        normalize_rule(row) for row in store.read_rows(DIMENSIONMAP)
        if normalize_text(row.get("status")) == ENABLED
    ]


def _recalc_drilldown(
    *,
    version_number: int,
    recalc_batch: str,
    dataset_rows: list[dict[str, Any]],
    affected: set[str] | None,
    merge: bool = False,
) -> tuple[int, list[str], list[str]]:
    """重算已启用路径的分层结果。

    ``merge=True`` 时只替换本次重算到的路径，其它路径的结果原样保留；
    全量重建（版本变更）时 merge=False，结果表被完整替换。
    """
    index = _dataset_index(dataset_rows)
    records = _records_by_key()
    paths = store.read_rows("drilldown")
    previous = (
        [dict(row) for row in store.read_rows(DRILLDOWN_RESULTS)] if merge else []
    )
    results: list[dict[str, Any]] = []
    recalculated: list[str] = []
    touched: list[str] = []
    for path in paths:
        if normalize_text(path.get("status")) != "已启用":
            continue
        levels = parse_dimension_list(path.get("下钻层级序列"))
        start_dim = normalize_text(path.get("起始维度"))
        if start_dim and start_dim not in levels:
            levels.insert(0, start_dim)
        if not levels:
            continue
        path_code = normalize_text(path.get("路径编号"))
        recalculated.append(path_code)
        if affected is None or any(level in affected for level in levels):
            touched.append(path_code)
        for level_index, raw_dim in enumerate(levels, start=1):
            groups: dict[tuple[str, str], dict[str, Any]] = {}
            covered_records = 0
            for record_key, record in records.items():
                mapped = _resolve_record_dimension(record, raw_dim, index)
                if mapped is None:
                    continue
                covered_records += 1
                standard_dim = mapped["标准维度名"]
                standard_value = mapped["标准维度值"]
                group = groups.setdefault((standard_dim, standard_value), {
                    "标准维度名": standard_dim,
                    "标准维度值": standard_value,
                    "度量合计": 0.0,
                    "记录数": 0,
                    "记录键": set(),
                    "原始维度值": {},
                    "命中规则": {},
                    "默认命中": 0,
                })
                if record_key in group["记录键"]:
                    continue
                group["记录键"].add(record_key)
                group["度量合计"] += float(record["度量值"])
                group["记录数"] += 1
                group["原始维度值"][mapped["原始维度值"]] = (
                    group["原始维度值"].get(mapped["原始维度值"], 0) + 1
                )
                rule_code = mapped["命中规则编号"] or "未命中规则"
                group["命中规则"][rule_code] = group["命中规则"].get(rule_code, 0) + 1
                if mapped["是否默认命中"]:
                    group["默认命中"] += 1
            if not groups:
                # 该层没有任何记录也要留下显式结果，避免“层级丢失”被当成没问题。
                results.append({
                    "id": len(results) + 1,
                    "路径编号": path_code,
                    "层级序号": level_index,
                    "原始维度名": raw_dim,
                    "标准维度名": raw_dim,
                    "标准维度值": "",
                    "度量合计": 0.0,
                    "记录数": 0,
                    "覆盖记录数": 0,
                    "原始维度值分布": {},
                    "命中规则编号集合": [],
                    "是否存在默认命中": False,
                    "映射版本": version_number,
                    "回算批次": recalc_batch,
                    "回算时间": now_iso(),
                })
                continue
            for (standard_dim, standard_value), group in sorted(groups.items()):
                results.append({
                    "id": len(results) + 1,
                    "路径编号": path_code,
                    "层级序号": level_index,
                    "原始维度名": raw_dim,
                    "标准维度名": standard_dim,
                    "标准维度值": standard_value,
                    "度量合计": round(group["度量合计"], 4),
                    "记录数": group["记录数"],
                    "覆盖记录数": covered_records,
                    "原始维度值分布": group["原始维度值"],
                    "命中规则编号集合": sorted(group["命中规则"]),
                    "是否存在默认命中": group["默认命中"] > 0,
                    "映射版本": version_number,
                    "回算批次": recalc_batch,
                    "回算时间": now_iso(),
                })
    if merge:
        recalculated_codes = set(recalculated)
        kept = [
            row for row in previous
            if normalize_text(row.get("路径编号")) not in recalculated_codes
        ]
        for index_, row in enumerate(kept, start=1):
            row["id"] = index_
        offset = len(kept)
        for index_, row in enumerate(results, start=1):
            row["id"] = offset + index_
        results = kept + results
    else:
        for index_, row in enumerate(results, start=1):
            row["id"] = index_
    store.replace_rows(DRILLDOWN_RESULTS, results)
    return len(results), recalculated, touched


def _recalc_aggregate(
    *,
    version_number: int,
    recalc_batch: str,
    dataset_rows: list[dict[str, Any]],
    affected: set[str] | None,
    merge: bool = False,
) -> tuple[int, list[str], list[str], dict[str, dict[str, Any]]]:
    index = _dataset_index(dataset_rows)
    records = _records_by_key()
    rules = store.read_rows("aggregate")
    previous = (
        [dict(row) for row in store.read_rows(AGGREGATE_RESULTS)] if merge else []
    )
    results: list[dict[str, Any]] = []
    recalculated: list[str] = []
    touched: list[str] = []
    checks: dict[str, dict[str, Any]] = {}
    for rule in rules:
        if normalize_text(rule.get("status")) != ENABLED:
            continue
        rule_code = normalize_text(rule.get("规则编号"))
        source_dims = parse_dimension_list(rule.get("源数据粒度"))
        target_dims = parse_dimension_list(rule.get("目标粒度"))
        if not source_dims:
            continue
        recalculated.append(rule_code)
        if affected is None or any(dim in affected for dim in source_dims):
            touched.append(rule_code)
        groups: dict[tuple[tuple[str, str], ...], dict[str, Any]] = {}
        input_records = 0
        covered_records = 0
        uncovered_records = 0
        duplicate_records = 0
        unmatched_targets: set[str] = set()
        seen_record_keys: set[tuple[str, str]] = set()
        for record_key, record in records.items():
            if not any(dim in record["维度"] for dim in source_dims):
                continue
            input_records += 1
            if record_key in seen_record_keys:
                duplicate_records += 1
            seen_record_keys.add(record_key)
            resolved: list[dict[str, Any]] = []
            for raw_dim in source_dims:
                mapped = _resolve_record_dimension(record, raw_dim, index)
                if mapped is not None:
                    resolved.append(mapped)
            if not resolved:
                uncovered_records += 1
                continue
            if target_dims:
                # 目标粒度按标准维度名匹配；也接受原始维度名，避免配置里写的是
                # 原始名时整批记录被丢掉。
                matched: list[dict[str, Any]] = []
                matched_targets: list[str] = []
                for target in target_dims:
                    hit = [
                        mapped for mapped in resolved
                        if mapped["标准维度名"] == target or mapped["原始维度名"] == target
                    ]
                    if hit:
                        matched_targets.append(target)
                        matched.extend(hit)
                    else:
                        unmatched_targets.add(target)
                if len(matched_targets) < len(target_dims):
                    # 有目标维度在当前映射里找不到对应标准维度（例如标准维度改名），
                    # 用该记录全部已解析维度聚合，宁可多带维度也不丢记录。
                    selected = resolved
                else:
                    selected = matched
            else:
                selected = resolved
            covered_records += 1
            key = tuple(sorted(
                (mapped["标准维度名"], mapped["标准维度值"]) for mapped in selected
            ))
            group = groups.setdefault(key, {
                "标准维度组合": {name: value for name, value in key},
                "度量值": 0.0,
                "记录数": 0,
                "来源记录": set(),
                "命中规则编号集合": set(),
                "默认命中数": 0,
            })
            if record_key in group["来源记录"]:
                continue
            group["来源记录"].add(record_key)
            group["度量值"] += float(record["度量值"])
            group["记录数"] += 1
            for mapped in selected:
                if mapped["命中规则编号"]:
                    group["命中规则编号集合"].add(mapped["命中规则编号"])
                if mapped["是否默认命中"]:
                    group["默认命中数"] += 1
        for key, group in sorted(groups.items()):
            results.append({
                "id": len(results) + 1,
                "规则编号": rule_code,
                "源数据粒度": normalize_text(rule.get("源数据粒度")),
                "目标粒度": normalize_text(rule.get("目标粒度")),
                "目标维度键": "|".join(f"{name}={value}" for name, value in key),
                "标准维度组合": group["标准维度组合"],
                "度量值": round(group["度量值"], 4),
                "记录数": group["记录数"],
                "输入记录数": input_records,
                "覆盖记录数": covered_records,
                "未覆盖记录数": uncovered_records,
                "去重丢弃记录数": duplicate_records,
                "命中规则编号集合": sorted(group["命中规则编号集合"]),
                "是否存在默认命中": group["默认命中数"] > 0,
                "未匹配目标维度": sorted(unmatched_targets),
                "映射版本": version_number,
                "回算批次": recalc_batch,
                "回算时间": now_iso(),
            })
        checks[rule_code] = {
            "输入记录数": input_records,
            "覆盖记录数": covered_records,
            "未覆盖记录数": uncovered_records,
            "去重丢弃记录数": duplicate_records,
            "结果分组数": len(groups),
            "未匹配目标维度": sorted(unmatched_targets),
        }
    if merge:
        recalculated_codes = set(recalculated)
        kept = [
            row for row in previous
            if normalize_text(row.get("规则编号")) not in recalculated_codes
        ]
        for index_, row in enumerate(kept, start=1):
            row["id"] = index_
        offset = len(kept)
        for index_, row in enumerate(results, start=1):
            row["id"] = offset + index_
        results = kept + results
    else:
        for index_, row in enumerate(results, start=1):
            row["id"] = index_
    store.replace_rows(AGGREGATE_RESULTS, results)
    return len(results), recalculated, touched, checks


# ---------------------------------------------------------------------------
# 统一重建入口
# ---------------------------------------------------------------------------
def rebuild_mapping(
    *,
    trigger_type: str,
    trigger_action: str,
    trigger_rule: str | None = None,
    affected_dimensions: set[str] | None = None,
    recalc_all: bool = False,
) -> dict[str, Any]:
    """在事务内生成新映射版本、标准维度数据集，并回算受影响的输出。

    调用方负责开启事务；这里再套一层嵌套事务没有副作用，单独调用也安全。
    """
    with store.transaction():
        rule_rows = store.read_rows(DIMENSIONMAP)
        normalized_rules = [normalize_rule(row) for row in rule_rows]
        recalc_batch = f"RC-{trigger_type}-{now_iso()}"
        version_row = _create_version(
            trigger_type=trigger_type,
            trigger_action=trigger_action,
            trigger_rule=trigger_rule,
            rules=rule_rows,
            recalc_batch=recalc_batch,
        )
        version_number = int(version_row["映射版本"])
        # 只有“已生效”的规则参与解析；草稿/评审中/已废弃保留在版本快照里审计。
        enabled_rules = [rule for rule in normalized_rules if rule["status"] == ENABLED]
        dataset_rows = _build_dataset(version_number, enabled_rules)
        store.replace_rows(DATASET, dataset_rows)
        store.check_write("dataset")

        affected = None if recalc_all else affected_dimensions
        # 全量重算：结果表每行都打上新版本号，避免“只回算一部分”把其它路径/
        # 规则的结果冲掉，也避免同一结果表混着两个映射版本。
        drilldown_count, drilldown_paths, touched_paths = _recalc_drilldown(
            version_number=version_number,
            recalc_batch=recalc_batch,
            dataset_rows=dataset_rows,
            affected=affected,
        )
        aggregate_count, aggregate_rules, touched_rules, checks = _recalc_aggregate(
            version_number=version_number,
            recalc_batch=recalc_batch,
            dataset_rows=dataset_rows,
            affected=affected,
        )
        affected_drilldown = touched_paths
        affected_aggregate = touched_rules
        store.check_write("recalc")

        version_row["数据集行数"] = len(dataset_rows)
        version_row["下钻回算行数"] = drilldown_count
        version_row["聚合回算行数"] = aggregate_count
        log = {
            "id": store.next_id(RECALC_LOG),
            "映射版本": version_number,
            "触发类型": trigger_type,
            "触发动作": trigger_action,
            "触发规则编号": trigger_rule,
            "受影响原始维度": sorted(affected) if affected is not None else ["全部"],
            "受影响下钻路径": affected_drilldown,
            "受影响聚合规则": affected_aggregate,
            "下钻结果行数": drilldown_count,
            "聚合结果行数": aggregate_count,
            "聚合核对": checks,
            "回算批次": recalc_batch,
            "创建时间": now_iso(),
        }
        store.rows(RECALC_LOG).append(log)
        return {
            "映射版本": version_number,
            "版本标识": version_row["版本标识"],
            "数据集行数": len(dataset_rows),
            "下钻回算行数": drilldown_count,
            "聚合回算行数": aggregate_count,
            "受影响下钻路径": affected_drilldown,
            "受影响聚合规则": affected_aggregate,
            "回算批次": recalc_batch,
        }


def ensure_initialized() -> None:
    """首次访问时生成初始版本与数据集；重置仓库后同样适用。

    只读检查放在事务外，避免“什么都不用做”的读请求也被计入一次提交；
    需要初始化时再进事务，双检锁保证并发首访只生成一个初始版本。
    """
    if _current_version_rows() and store.read_rows(DATASET):
        return
    with _INIT_LOCK:
        if _current_version_rows() and store.read_rows(DATASET):
            return
        rebuild_mapping(
            trigger_type="系统初始化",
            trigger_action="初始化映射数据集",
            recalc_all=True,
        )


def rebuild_all(trigger_action: str = "手动重建映射数据集") -> dict[str, Any]:
    ensure_initialized()
    return rebuild_mapping(
        trigger_type="手动重建",
        trigger_action=trigger_action,
        recalc_all=True,
    )


def list_dataset(
    *,
    keyword: str | None = None,
    raw_dimension: str | None = None,
    standard_dimension: str | None = None,
    version_number: int | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[list[dict[str, Any]], int, dict[str, Any] | None]:
    ensure_initialized()
    rows = store.read_rows(DATASET)
    version = current_version()
    if version_number is not None:
        rows = [row for row in rows if int(row.get("映射版本", 0)) == version_number]
    elif version is not None:
        rows = [row for row in rows if int(row.get("映射版本", 0)) == int(version["映射版本"])]
    if raw_dimension:
        rows = [row for row in rows if raw_dimension in row["原始维度名"]]
    if standard_dimension:
        rows = [row for row in rows if standard_dimension in row["标准维度名"]]
    if keyword:
        rows = [
            row for row in rows
            if keyword in row["原始维度值"]
            or keyword in row["标准维度值"]
            or keyword in (row.get("命中规则编号") or "")
            or keyword in row["原始维度名"]
            or keyword in row["标准维度名"]
        ]
    total = len(rows)
    start = max(page - 1, 0) * size
    return rows[start:start + size], total, version


def list_versions(limit: int = 50) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    ensure_initialized()
    rows = sorted(
        store.read_rows(VERSIONS),
        key=lambda row: int(row.get("映射版本", 0)),
        reverse=True,
    )
    return rows[:limit], current_version()


def get_version(version_number: int) -> dict[str, Any] | None:
    ensure_initialized()
    for row in store.read_rows(VERSIONS):
        if int(row.get("映射版本", 0)) == version_number:
            return row
    return None


def resolve_preview(
    *,
    raw_dimension: str,
    raw_value: str,
    version_number: int | None = None,
) -> dict[str, Any]:
    """只读解析预览：优先命中当前数据集，数据集未收录时现场按规则解析。"""
    ensure_initialized()
    dataset_rows = store.read_rows(DATASET)
    version = current_version()
    if version_number is not None:
        dataset_rows = [
            row for row in dataset_rows
            if int(row.get("映射版本", 0)) == version_number
        ]
        version = get_version(version_number)
    for row in dataset_rows:
        if row["原始维度名"] == normalize_text(raw_dimension) and row["原始维度值"] == normalize_text(raw_value):
            return {
                "命中来源": "标准维度数据集",
                "映射版本": row["映射版本"],
                "结果": row,
            }
    rules = [normalize_rule(row) for row in store.read_rows(DIMENSIONMAP)]
    resolved = resolve_value(raw_dimension, raw_value, rules)
    resolved["映射版本"] = int(version["映射版本"]) if version else None
    return {"命中来源": "规则实时解析（数据集未收录该值）", "映射版本": resolved.get("映射版本"), "结果": resolved}


def replay_version(version_number: int) -> dict[str, Any] | None:
    """按历史版本的规则快照重放解析，验证历史映射结果可追溯。"""
    version = get_version(version_number)
    if version is None:
        return None
    live_rules = {
        normalize_text(row.get("规则编号")): row for row in store.read_rows(DIMENSIONMAP)
    }
    replay_rules: list[dict[str, Any]] = []
    for snapshot_rule in version.get("规则快照", []):
        live = live_rules.get(snapshot_rule["规则编号"], {})
        merged = {
            **live,
            **snapshot_rule,
            "映射优先级": snapshot_rule.get("映射优先级", 0),
            # 快照里的 status 是当时的生效状态，重放必须用它而不是当前值。
            "status": snapshot_rule.get("status", live.get("status")),
        }
        replay_rules.append(normalize_rule(merged))
    records = store.read_rows(RAW_RECORDS)
    keys: set[tuple[str, str]] = set()
    for record in records:
        keys.add((
            normalize_text(record.get("维度字段") or record.get("原始维度名")),
            normalize_text(record.get("原始维度值")),
        ))
    items = [resolve_value(raw_dim, raw_value, replay_rules) for raw_dim, raw_value in sorted(keys)]
    return {"映射版本": version_number, "规则指纹": version.get("规则指纹"), "items": items}


# ---------------------------------------------------------------------------
# 供维度映射服务调用的写入封装
# ---------------------------------------------------------------------------
def save_rule_with_rebuild(
    *,
    mutate,
    trigger_type: str,
    trigger_action: str,
    trigger_rule: str | None,
    affected_dimensions: set[str] | None,
) -> tuple[Any, dict[str, Any] | None, str | None]:
    """在一个事务里完成“改规则 + 新版本 + 数据集 + 回算”。

    返回 (mutate 的返回值, 重建摘要, 错误信息)。写入失败时仓库整体回滚，
    调用方不会看到残缺版本或半张数据集。
    """
    try:
        with store.transaction():
            result = mutate()
            summary = rebuild_mapping(
                trigger_type=trigger_type,
                trigger_action=trigger_action,
                trigger_rule=trigger_rule,
                affected_dimensions=affected_dimensions,
            )
            return result, summary, None
    except StoreWriteError as error:
        return None, None, str(error)


def _purge_path_results(path_code: str) -> None:
    store.replace_rows(DRILLDOWN_RESULTS, [
        row for row in store.read_rows(DRILLDOWN_RESULTS)
        if row.get("路径编号") != path_code
    ])


def _purge_rule_results(rule_code: str) -> None:
    store.replace_rows(AGGREGATE_RESULTS, [
        row for row in store.read_rows(AGGREGATE_RESULTS)
        if row.get("规则编号") != rule_code
    ])


def refresh_drilldown(path_id: int) -> tuple[dict[str, Any] | None, str | None]:
    """刷新单条下钻路径的分层结果，严格读取当前标准维度数据集。

    路径已归档/未启用时清除其残留结果并返回可读说明，而不是让旧分层继续挂在
    结果表里，也不是把“归档导致刷新失败”当成错误抛给调用方。
    """
    ensure_initialized()
    try:
        with store.transaction():
            path = store.find("drilldown", path_id)
            if path is None:
                return None, f"下钻路径 {path_id} 不存在或已归档"
            path_code = normalize_text(path.get("路径编号"))
            if normalize_text(path.get("status")) != "已启用":
                _purge_path_results(path_code)
                return {
                    "路径编号": path_code,
                    "映射版本": (current_version() or {}).get("映射版本"),
                    "结果行数": 0,
                    "回算批次": None,
                    "已清理残留结果": True,
                }, None
            version = current_version()
            if version is None:
                return None, "当前没有可用的映射版本"
            dataset_rows = [
                row for row in store.read_rows(DATASET)
                if int(row.get("映射版本", 0)) == int(version["映射版本"])
            ]
            levels = parse_dimension_list(path.get("下钻层级序列"))
            start_dim = normalize_text(path.get("起始维度"))
            if start_dim and start_dim not in levels:
                levels.insert(0, start_dim)
            affected = set(levels)
            batch = f"RC-刷新-{now_iso()}"
            # 只重算该路径：merge=True 保留其它路径的结果，不整表覆盖。
            _recalc_drilldown(
                version_number=int(version["映射版本"]),
                recalc_batch=batch,
                dataset_rows=dataset_rows,
                affected=affected,
                merge=True,
            )
            count = sum(
                1 for row in store.read_rows(DRILLDOWN_RESULTS)
                if row.get("路径编号") == path_code
            )
            return {
                "路径编号": path_code,
                "映射版本": int(version["映射版本"]),
                "结果行数": count,
                "回算批次": batch,
            }, None
    except StoreWriteError as error:
        return None, str(error)


def recalc_aggregate(rule_id: int) -> tuple[dict[str, Any] | None, str | None]:
    """回算单条聚合规则，返回核对信息。"""
    ensure_initialized()
    try:
        with store.transaction():
            rule = store.find("aggregate", rule_id)
            if rule is None:
                return None, f"聚合规则 {rule_id} 不存在或已归档"
            rule_code = normalize_text(rule.get("规则编号"))
            if normalize_text(rule.get("status")) != ENABLED:
                _purge_rule_results(rule_code)
                return {
                    "规则编号": rule_code,
                    "映射版本": (current_version() or {}).get("映射版本"),
                    "结果行数": 0,
                    "回算批次": None,
                    "核对": aggregate_check(rule_code),
                    "已清理残留结果": True,
                }, None
            version = current_version()
            if version is None:
                return None, "当前没有可用的映射版本"
            dataset_rows = [
                row for row in store.read_rows(DATASET)
                if int(row.get("映射版本", 0)) == int(version["映射版本"])
            ]
            batch = f"RC-聚合回算-{now_iso()}"
            affected = set(parse_dimension_list(rule.get("源数据粒度")))
            _recalc_aggregate(
                version_number=int(version["映射版本"]),
                recalc_batch=batch,
                dataset_rows=dataset_rows,
                affected=affected,
                merge=True,
            )
            count = sum(
                1 for row in store.read_rows(AGGREGATE_RESULTS)
                if row.get("规则编号") == rule_code
            )
            checks = aggregate_check(rule_code)
            return {
                "规则编号": rule_code,
                "映射版本": int(version["映射版本"]),
                "结果行数": count,
                "回算批次": batch,
                "核对": checks,
            }, None
    except StoreWriteError as error:
        return None, str(error)


# ---------------------------------------------------------------------------
# 下游查询
# ---------------------------------------------------------------------------
def list_drilldown_results(
    *,
    path_code: str | None = None,
    version_number: int | None = None,
    page: int = 1,
    size: int = 50,
) -> tuple[list[dict[str, Any]], int, dict[str, Any] | None]:
    ensure_initialized()
    rows = store.read_rows(DRILLDOWN_RESULTS)
    version = current_version()
    if version_number is not None:
        rows = [row for row in rows if int(row.get("映射版本", 0)) == version_number]
    if path_code:
        rows = [row for row in rows if row.get("路径编号") == path_code]
    rows = sorted(rows, key=lambda row: (row.get("路径编号", ""), int(row.get("层级序号", 0)), row.get("标准维度值", "")))
    total = len(rows)
    start = max(page - 1, 0) * size
    return rows[start:start + size], total, version


def list_aggregate_results(
    *,
    rule_code: str | None = None,
    version_number: int | None = None,
    page: int = 1,
    size: int = 50,
) -> tuple[list[dict[str, Any]], int, dict[str, Any] | None]:
    ensure_initialized()
    rows = store.read_rows(AGGREGATE_RESULTS)
    version = current_version()
    if version_number is not None:
        rows = [row for row in rows if int(row.get("映射版本", 0)) == version_number]
    if rule_code:
        rows = [row for row in rows if row.get("规则编号") == rule_code]
    rows = sorted(rows, key=lambda row: (row.get("规则编号", ""), row.get("目标维度键", "")))
    total = len(rows)
    start = max(page - 1, 0) * size
    return rows[start:start + size], total, version


def aggregate_check(rule_code: str) -> dict[str, Any]:
    """聚合详情核对：统计记录数是否重复/丢失、目标键是否唯一。"""
    rows = [
        row for row in store.read_rows(AGGREGATE_RESULTS)
        if row.get("规则编号") == rule_code
    ]
    if not rows:
        return {
            "结果分组数": 0,
            "输入记录数": 0,
            "覆盖记录数": 0,
            "未覆盖记录数": 0,
            "统计记录数合计": 0,
            "目标键唯一": True,
            "是否重复": False,
            "是否丢失": False,
            "映射版本": None,
        }
    first = rows[0]
    summed = sum(int(row.get("记录数", 0)) for row in rows)
    covered = int(first.get("覆盖记录数", 0))
    input_count = int(first.get("输入记录数", 0))
    uncovered = int(first.get("未覆盖记录数", 0))
    keys = [row.get("目标维度键") for row in rows]
    return {
        "结果分组数": len(rows),
        "输入记录数": input_count,
        "覆盖记录数": covered,
        "未覆盖记录数": uncovered,
        "统计记录数合计": summed,
        "目标键唯一": len(set(keys)) == len(keys),
        "是否重复": summed != covered or len(set(keys)) != len(keys),
        "是否丢失": covered + uncovered != input_count,
        "映射版本": first.get("映射版本"),
        "回算批次": first.get("回算批次"),
        "未匹配目标维度": first.get("未匹配目标维度", []),
    }
