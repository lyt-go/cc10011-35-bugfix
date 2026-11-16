"""维度映射解析链的回归测试。

覆盖四类历史问题：
1. 优先级 / 转换逻辑 / 默认值 / 更新时间必须完整落库；
2. 默认映射不得覆盖更具体的映射，同一原始值只产出一个结果；
3. 数据集、下钻、聚合三处消费同一份映射结果，粒度不重复不丢失；
4. 版本唯一、事务失败不暴露残缺版本、旧映射变更触发回算。
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import dimensionmap_chain as chain
from app.services.aggregate import AggregateService
from app.services.dimensionmap import DimensionmapService
from app.services.drilldown import DrilldownService
from app.store import store

client = TestClient(app)
service = DimensionmapService()
drilldown_service = DrilldownService()
aggregate_service = AggregateService()


@pytest.fixture(autouse=True)
def fresh_store():
    store.reset()
    chain.ensure_initialized()
    yield
    store.reset()


def _dataset_row(raw_dim: str, raw_value: str) -> dict:
    for row in store.read_rows(chain.DATASET):
        if row["原始维度名"] == raw_dim and row["原始维度值"] == raw_value:
            return row
    raise AssertionError(f"数据集缺少 {raw_dim}={raw_value}")


def _current_versions() -> list[dict]:
    return [row for row in store.read_rows(chain.VERSIONS) if row["状态"] == "当前"]


def test_default_does_not_override_specific_mapping():
    row = _dataset_row("region", "华北")
    assert row["标准维度值"] == "华北"
    assert row["命中规则编号"] == "DIME-0001"
    assert row["是否默认命中"] is False
    # 默认规则的优先级数值更大，但也不能压过具体映射。
    default_rule = store.find("dimensionmap", 2)
    assert default_rule["映射优先级"] == 100
    assert row["命中规则优先级"] == 10


def test_default_only_applies_when_no_specific_rule_matches():
    row = _dataset_row("region", "华南")
    assert row["是否默认命中"] is True
    assert row["命中规则编号"] == "DIME-0002"
    assert row["标准维度值"] == "未知区域"


def test_one_result_per_raw_value():
    keys = [
        (row["原始维度名"], row["原始维度值"])
        for row in store.read_rows(chain.DATASET)
    ]
    assert len(keys) == len(set(keys))
    trace = chain.resolve_value(
        "region", "华北", [chain.normalize_rule(r) for r in store.read_rows("dimensionmap")]
    )
    assert trace["同级冲突"] is False
    assert [item["结论"] for item in trace["候选规则"]].count("命中") == 1


def test_rule_fields_fully_persisted_and_reloaded():
    updated, error = service.update_entry(
        1,
        {
            "映射优先级": "高",
            "值转换逻辑": "strip_prefix:华",
            "默认取值": "待补",
            "最近更新时间": "2026-11-10T08:00:00",
            "匹配值": "华北",
        },
    )
    assert error is None
    stored = store.find("dimensionmap", 1)
    assert stored["映射优先级"] == 80
    assert stored["优先级原始值"] == "高"
    assert stored["值转换逻辑"] == "strip_prefix:华"
    assert stored["默认取值"] == "待补"
    assert stored["最近更新时间"] == "2026-11-10T08:00:00"
    assert stored["规则版本"] == 2
    # 持久化的转换逻辑必须体现在新的映射版本里。
    assert updated["映射重建"]["映射版本"] == 2
    assert _dataset_row("region", "华北")["标准维度值"] == "北"


def test_invalid_priority_rejected_instead_of_silently_zeroed():
    _, missing = service.create_entry(
        {
            "规则编号": "DIME-9000",
            "原始维度名": "region",
            "标准维度名": "区域",
            "映射类型": "精确匹配",
            "匹配值": "西南",
            "映射优先级": "很高很高",
        }
    )
    assert missing and "映射优先级" in missing[0]
    assert store.find("dimensionmap", 999) is None


def test_too_per_granularity_not_duplicated_or_lost():
    check = chain.aggregate_check("AGGR-0003")
    assert check["目标键唯一"] is True
    assert check["是否重复"] is False
    assert check["是否丢失"] is False
    assert check["统计记录数合计"] == check["覆盖记录数"]


def test_drilldown_keeps_raw_and_standard_names_aligned():
    rows, _, _ = chain.list_drilldown_results(page=1, size=100)
    assert rows
    for row in rows:
        assert row["原始维度名"]
        assert row["标准维度名"]
        # 每个标准维度只来自一个原始维度的映射，不允许串列。
        assert row["原始维度名"] in {"region", "渠道", "city"}
    region_rows = [row for row in rows if row["原始维度名"] == "region"]
    assert {row["标准维度名"] for row in region_rows} == {"区域"}
    assert any(row["标准维度值"] == "华北" for row in region_rows)


def test_transaction_failure_leaves_no_partial_version():
    before = store.snapshot_tables()
    store.fail_next_write("commit")
    entry, missing = service.create_entry(
        {
            "规则编号": "DIME-9100",
            "原始维度名": "region",
            "标准维度名": "区域",
            "映射类型": "精确匹配",
            "匹配值": "西南",
            "映射优先级": 9,
        }
    )
    assert entry is None and missing
    assert store.snapshot_tables() == before
    assert [row["映射版本"] for row in _current_versions()] == [1]

    store.fail_next_write("dataset")
    updated, error = service.update_entry(1, {"映射优先级": 77})
    assert updated is None and "写入失败" in error
    assert store.find("dimensionmap", 1)["映射优先级"] == 10
    assert [row["映射版本"] for row in _current_versions()] == [1]


def test_concurrent_actions_leave_single_current_version():
    barrier = threading.Barrier(8)
    failures: list[str] = []

    def worker(index: int) -> None:
        try:
            barrier.wait()
            service.run_action(1, "生效规则" if index % 2 else "废弃规则")
        except Exception as error:  # pragma: no cover - 失败时给出可读信息
            failures.append(repr(error))

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    current = _current_versions()
    assert len(current) == 1
    current_number = current[0]["映射版本"]
    assert {row["映射版本"] for row in store.read_rows(chain.DATASET)} == {current_number}
    assert {row["映射版本"] for row in store.read_rows(chain.DRILLDOWN_RESULTS)} == {current_number}
    assert {row["映射版本"] for row in store.read_rows(chain.AGGREGATE_RESULTS)} == {current_number}


def test_mapping_change_recalculates_downstream():
    auto_before = _dataset_row("region", "华北")["标准维度值"]
    assert auto_before == "华北"
    _, error = service.update_entry(1, {"标准维度名": "销售大区", "值转换逻辑": "map:华北=华北区"})
    assert error is None
    assert _dataset_row("region", "华北")["标准维度值"] == "华北区"
    drill_rows, _, _ = chain.list_drilldown_results(page=1, size=200)
    assert any(row["标准维度值"] == "华北区" for row in drill_rows)
    agg_rows, _, _ = chain.list_aggregate_results(page=1, size=200)
    assert any("华北区" in row["目标维度键"] for row in agg_rows)


def test_historical_version_can_be_replayed():
    _, error = service.update_entry(1, {"默认取值": "补充值"})
    assert error is None
    replay_v1 = chain.replay_version(1)
    replay_v2 = chain.replay_version(2)
    assert replay_v1 and replay_v2
    v1 = {item["原始维度值"]: item["命中规则编号"] for item in replay_v1["items"] if item["原始维度名"] == "region"}
    v2 = {item["原始维度值"]: item["命中规则编号"] for item in replay_v2["items"] if item["原始维度名"] == "region"}
    assert v1["华北"] == "DIME-0001"
    assert v2["华北"] == "DIME-0001"


def test_api_dataset_rows_carry_rule_and_version():
    response = client.get("/api/dimensionmap/dataset?size=200")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] > 0
    for row in payload["items"]:
        assert "命中规则编号" in row
        assert "命中规则版本" in row
        assert "映射版本" in row


def test_api_resolve_reports_single_winner_and_rejected_candidates():
    response = client.post(
        "/api/dimensionmap/resolve",
        json={"values": {"原始维度名": "region", "原始维度值": "华北"}},
    )
    payload = response.json()
    assert payload["ok"] is True
    result = payload["结果"]
    assert result["命中规则编号"] == "DIME-0001"
    rejected = [item for item in result["候选规则"] if item["结论"] == "落选"]
    assert any(item["是否默认映射"] for item in rejected)


def test_api_aggregate_detail_exposes_check():
    response = client.get("/api/aggregate/3/detail")
    assert response.status_code == 200
    detail = response.json()
    assert detail["核对"]["是否重复"] is False
    assert detail["核对"]["是否丢失"] is False
    assert detail["版本标识"] == f"V{detail['映射版本']}"


def test_equal_priority_resolved_by_specificity_then_rule_version():
    # 再登记一条与 DIME-0001 同优先级的精确规则，只有一条能命中，
    # 且落选方会记录在候选规则里。
    entry, missing = service.create_entry(
        {
            "规则编号": "DIME-9500",
            "原始维度名": "region",
            "标准维度名": "区域",
            "映射类型": "精确匹配",
            "匹配值": "华北",
            "映射优先级": 10,
        }
    )
    assert missing == []
    service.run_action(entry["id"], "生效规则")
    rows = [
        row for row in store.read_rows(chain.DATASET)
        if row["原始维度名"] == "region" and row["原始维度值"] == "华北"
    ]
    assert len(rows) == 1
    trace = chain.resolve_value(
        "region", "华北", [chain.normalize_rule(r) for r in store.read_rows("dimensionmap")]
    )
    winners = [item for item in trace["候选规则"] if item["结论"] == "命中"]
    assert len(winners) == 1
    assert winners[0]["规则编号"] == rows[0]["命中规则编号"]
    assert winners[0]["映射结果"]["标准维度值"] == rows[0]["标准维度值"]
    # 同优先级的另一条精确规则必须作为落选候选保留，而不是悄悄消失；
    # 同优先级按最近更新时间倒序稳定判定，不会出现两个命中。
    pair = {
        item["规则编号"]: item["结论"]
        for item in trace["候选规则"]
        if item["规则编号"] in {"DIME-9500", "DIME-0001"}
    }
    assert sorted(pair.values()) == ["命中", "落选"]
    assert trace["同级冲突"] is False  # 结果一致，不构成需要人工裁决的冲突


def test_every_dataset_row_has_winner_and_rejected_trace():
    for row in store.read_rows(chain.DATASET):
        assert row["命中规则编号"]
        assert row["命中规则版本"] is not None
        conclusions = [item["结论"] for item in row["候选规则"]]
        assert conclusions.count("命中") == 1
        winning = [item for item in row["候选规则"] if item["结论"] == "命中"][0]
        assert winning["规则编号"] == row["命中规则编号"]
        assert winning["映射结果"]["标准维度值"] == row["标准维度值"]


def test_single_path_refresh_preserves_other_results():
    # 先启用第二条路径，再单独刷新一条，不能把另一条的分层结果冲掉。
    _, message = drilldown_service.run_action(2, "启用路径")
    assert "刷新" in message or "分层结果" in message
    codes_before = {row["路径编号"] for row in store.read_rows(chain.DRILLDOWN_RESULTS)}
    assert "DRIL-0002" in codes_before and "DRIL-0003" in codes_before
    result, error = chain.refresh_drilldown(3)
    assert error is None
    codes_after = {row["路径编号"] for row in store.read_rows(chain.DRILLDOWN_RESULTS)}
    assert codes_after == codes_before
    assert all(row["映射版本"] == result["映射版本"] for row in store.read_rows(chain.DRILLDOWN_RESULTS))


def test_single_aggregate_recalc_preserves_other_rules():
    _, message = aggregate_service.run_action(3, "提交规则评审")
    assert message
    aggregate_service.run_action(3, "生效规则")
    codes = {row["规则编号"] for row in store.read_rows(chain.AGGREGATE_RESULTS)}
    assert codes == {"AGGR-0003"}
    result, error = chain.recalc_aggregate(3)
    assert error is None
    assert result["结果行数"] == len([
        row for row in store.read_rows(chain.AGGREGATE_RESULTS) if row["规则编号"] == "AGGR-0003"
    ])


def test_archiving_path_clears_stale_layers():
    assert any(row["路径编号"] == "DRIL-0003" for row in store.read_rows(chain.DRILLDOWN_RESULTS))
    drilldown_service.run_action(3, "归档路径")
    assert not any(row["路径编号"] == "DRIL-0003" for row in store.read_rows(chain.DRILLDOWN_RESULTS))
    _, message = drilldown_service.run_action(3, "启用路径")
    assert "刷新分层结果" in message
    assert any(row["路径编号"] == "DRIL-0003" for row in store.read_rows(chain.DRILLDOWN_RESULTS))


def test_deprecating_aggregate_rule_clears_stale_results():
    assert any(row["规则编号"] == "AGGR-0003" for row in store.read_rows(chain.AGGREGATE_RESULTS))
    aggregate_service.run_action(3, "废弃规则")
    assert not any(row["规则编号"] == "AGGR-0003" for row in store.read_rows(chain.AGGREGATE_RESULTS))
    result, error = chain.recalc_aggregate(3)
    assert error is None
    assert result["结果行数"] == 0
