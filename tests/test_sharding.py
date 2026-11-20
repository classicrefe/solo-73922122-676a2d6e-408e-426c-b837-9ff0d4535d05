"""分片规划器单元测试（不依赖数据库）。

覆盖：ON 匹配行号、跨片读数复用、WHERE 不影响清单、外键额外站点、
重复读数逐条保留、快照/归属校验、分片 SELECT 渲染。
"""
import pytest

from app.sharding import (
    ShardPlanError,
    build_manifest,
    plan_shards,
    render_shard_query,
    validate_assignments,
    validate_snapshot,
)
from app.sql_parser import parse_query


STATIONS = [(1, 20), (2, 30), (3, 40)]
# 行1/2 属站点1（含一条会被 WHERE 滤掉的低读数），行3 属站点2，行4 属站点3
READINGS = [(1, 11), (1, 5), (2, 22), (3, 9)]


def _plan(sql, stations, readings, assignments):
    ir = parse_query(sql)
    snap = validate_snapshot(stations, readings)
    asg = validate_assignments(snap, assignments)
    return ir, snap, plan_shards(ir, snap, asg)


def test_matched_reading_rows_by_on_ignoring_where():
    """清单只由 ON 决定：会被 WHERE 滤掉的匹配读数（行2，value=5）仍随片装载。"""
    _, _, manifests = _plan(
        "SELECT s.id, r.value FROM stations s "
        "LEFT JOIN readings r ON s.id = r.station_id WHERE r.value > 10",
        STATIONS,
        READINGS,
        [("north", [1, 2]), ("south", [3])],
    )
    by_name = {m.shard: m for m in manifests}
    assert by_name["north"].matched_reading_rows == (1, 2, 3)
    assert by_name["south"].matched_reading_rows == (4,)
    # 外键都指向本片归属站点时没有额外站点
    assert by_name["north"].extra_station_ids == ()
    assert by_name["south"].extra_station_ids == ()


def test_reading_reused_across_shards_when_on_matches_multiple_stations():
    """读数可跨片使用：一条读数匹配多个分片的站点时，逐片列出、各装一份。"""
    # ON 非外键列：threshold=value。行 (station_id=3, value=20) 匹配站点1(th=20)
    readings = [(3, 20), (1, 30), (2, None)]
    _, _, manifests = _plan(
        "SELECT s.id, r.station_id FROM stations s "
        "LEFT JOIN readings r ON s.threshold = r.value",
        STATIONS,
        readings,
        [("north", [1, 2]), ("south", [3])],
    )
    by_name = {m.shard: m for m in manifests}
    # north 归属站点 1(th=20)/2(th=30)：行1 匹配站点1，行2 匹配站点2
    assert by_name["north"].matched_reading_rows == (1, 2)
    # 行1 外键指向站点3（不属 north）-> 额外站点 3；行2 外键指向本站点1
    assert by_name["north"].extra_station_ids == (3,)
    # south 归属站点3(th=40)：无读数 value=40，且 NULL 不匹配
    assert by_name["south"].matched_reading_rows == ()
    assert by_name["south"].extra_station_ids == ()


def test_null_on_side_never_matches():
    """SQL 等值语义：ON 任一侧为 NULL 都不匹配。"""
    # 站点 threshold=NULL；读数 value=NULL 或非 NULL，均无法等值匹配
    readings = [(1, None), (1, 20)]
    _, _, manifests = _plan(
        "SELECT s.id FROM stations s LEFT JOIN readings r "
        "ON s.threshold = r.value",
        [(1, None)],
        readings,
        [("north", [1])],
    )
    assert manifests[0].matched_reading_rows == ()


def test_duplicate_readings_kept_individually():
    """源表中的重复读数逐条保留，行号分别列出。"""
    readings = [(1, 7), (1, 7), (1, 7)]
    _, _, manifests = _plan(
        "SELECT s.id, r.value FROM stations s "
        "LEFT JOIN readings r ON s.id = r.station_id",
        [(1, 1)],
        readings,
        [("north", [1])],
    )
    assert manifests[0].matched_reading_rows == (1, 2, 3)


def test_shard_query_limits_driver_to_owned_stations():
    ir = parse_query(
        "SELECT s.id, r.value FROM stations s "
        "LEFT JOIN readings r ON s.id = r.station_id "
        "WHERE r.value > 10 OR s.threshold IS NULL"
    )
    sql = render_shard_query(ir, (2, 3))
    assert sql.startswith(
        "SELECT s.id, r.value FROM stations s "
        "LEFT JOIN readings r ON s.id = r.station_id "
        "WHERE s.id IN (2, 3) AND ("
    )
    # 原 WHERE 被完整保留在驱动谓词之后
    assert "r.value > 10 OR s.threshold IS NULL" in sql


def test_shard_query_empty_ownership_is_always_false():
    ir = parse_query("SELECT id FROM stations")
    sql = render_shard_query(ir, ())
    assert "s.id IS NULL" in sql
    assert "IN" not in sql


def test_single_table_query_has_no_reading_manifest():
    _, _, manifests = _plan(
        "SELECT id, threshold FROM stations WHERE threshold >= 30",
        STATIONS,
        [],
        [("north", [1]), ("south", [2, 3])],
    )
    for m in manifests:
        assert m.matched_reading_rows == ()
        assert m.extra_station_ids == ()
    assert "s.id IN (1)" in manifests[0].shard_query


def test_snapshot_pk_and_fk_validation():
    # 主键重复
    with pytest.raises(ShardPlanError, match="主键"):
        validate_snapshot([(1, 1), (1, 2)], [])
    # 外键悬空
    with pytest.raises(ShardPlanError, match="外键"):
        validate_snapshot([(1, 1)], [(9, 1)])
    # 超 int4 值域
    with pytest.raises(ShardPlanError, match="int4"):
        validate_snapshot([(1, 2**31)], [])
    # 外键为 NULL 合法
    snap = validate_snapshot([(1, 1)], [(None, None)])
    assert snap.readings == [(None, None)]


def test_assignment_every_station_exactly_one_shard():
    snap = validate_snapshot(STATIONS, [])
    # 漏分站点
    with pytest.raises(ShardPlanError, match="未分配"):
        validate_assignments(snap, [("north", [1, 2])])
    # 一站多片
    with pytest.raises(ShardPlanError, match="多个分片"):
        validate_assignments(snap, [("north", [1]), ("south", [1, 2, 3])])
    # 归属不存在的站点
    with pytest.raises(ShardPlanError, match="不存在"):
        validate_assignments(snap, [("north", [1, 2, 3, 9])])
    # 分片名重复
    with pytest.raises(ShardPlanError, match="分片名重复"):
        validate_assignments(
            snap, [("x", [1]), ("x", [2, 3])]
        )
    # 空分片列表 + 全空快照是合法的
    empty_snap = validate_snapshot([], [])
    assert validate_assignments(empty_snap, []) == []
    # 有站点时空分片名非法
    with pytest.raises(ShardPlanError):
        validate_assignments(snap, [(" ", [1, 2, 3])])


def test_empty_shard_produces_loading_ready_manifest():
    ir = parse_query(
        "SELECT s.id FROM stations s "
        "LEFT JOIN readings r ON s.id = r.station_id"
    )
    snap = validate_snapshot(STATIONS, READINGS)
    manifest = build_manifest(ir, snap, "empty", [])
    assert manifest.station_ids == ()
    assert manifest.matched_reading_rows == ()
    assert manifest.extra_station_ids == ()
    assert "s.id IS NULL" in manifest.shard_query
