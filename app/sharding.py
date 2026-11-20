"""质控查询的分片装载清单规划。

场景：同一条以 stations 为左表的 LEFT 等值连接质控 SELECT，要按站点分片归属
在多个隔离节点（此处用独立临时 schema 模拟）上分别执行，再把各片结果合并，
核对其与全量快照上原查询的结果多重集（按列位置、忽略行序、保留重复）一致。

规划规则（与需求逐条对应）：
  1. 每个站点恰好归属一个分片；
  2. 对每个分片，列出与本片归属站点满足 ON 等值条件的读数**输入行号**
     （readings 快照从 1 开始编号，源表中的重复读数逐条保留）；
     读数可跨片使用——同一条读数若匹配多个分片的站点，会出现在每个分片的
     清单里，但每个分片内只装载一份；
  3. WHERE 会滤掉的匹配读数同样随片装载（清单只由 ON 决定，与 WHERE 无关）；
  4. 对装载的读数，列出满足外键 readings.station_id -> stations.id 所需、
     但不属于本片归属站点的额外站点 ID（去重、升序）；
  5. 分片 SELECT 在原 SELECT 基础上，把左表驱动范围限定为本片归属站点。
"""
from __future__ import annotations

from dataclasses import dataclass

from .ir import (
    INT_MAX,
    INT_MIN,
    BoolNode,
    ColRef,
    Compare,
    IsNull,
    And as IrAnd,
    Not as IrNot,
    Or as IrOr,
    QueryIR,
)

# 渲染分片 SQL 时固定使用的表别名
STATIONS_ALIAS = "s"
READINGS_ALIAS = "r"


class ShardPlanError(ValueError):
    """分片请求数据不合法（快照违反约束或站点分片归属不满足每站一片）。"""


@dataclass(frozen=True)
class Snapshot:
    """全量快照；readings 按输入顺序保留，重复读数逐条保留。"""

    stations: list[tuple[int, int | None]]          # (id, threshold)
    readings: list[tuple[int | None, int | None]]   # (station_id, value)


@dataclass(frozen=True)
class ShardManifest:
    """单个分片的装载清单与改写后的分片 SELECT。"""

    shard: str
    station_ids: tuple[int, ...]          # 本片归属站点 ID（输入顺序去重前已校验唯一）
    matched_reading_rows: tuple[int, ...]  # 满足 ON 的读数输入行号（1 起，升序、去重）
    extra_station_ids: tuple[int, ...]    # 外键所需的额外站点 ID（升序、去重）
    shard_query: str                      # 左表仅驱动本片归属站点的 SELECT


# ---------- 输入校验 ----------

def validate_snapshot(
    stations: list[tuple[int, int | None]],
    readings: list[tuple[int | None, int | None]],
) -> Snapshot:
    """校验快照沿用固定表约束：int4 值域、主键唯一非空、外键引用完整。"""
    seen_ids: set[int] = set()
    for idx, (sid, threshold) in enumerate(stations, start=1):
        if not isinstance(sid, int) or isinstance(sid, bool):
            raise ShardPlanError(f"stations 第 {idx} 行 id 必须是整数")
        if not (INT_MIN <= sid <= INT_MAX):
            raise ShardPlanError(f"stations 第 {idx} 行 id 超出 int4 值域")
        if sid in seen_ids:
            raise ShardPlanError(f"stations 主键 id={sid} 重复，违反 PRIMARY KEY 约束")
        seen_ids.add(sid)
        if threshold is not None and not (INT_MIN <= threshold <= INT_MAX):
            raise ShardPlanError(
                f"stations 第 {idx} 行 threshold 超出 int4 值域"
            )
    for idx, (station_id, value) in enumerate(readings, start=1):
        if station_id is not None:
            if not isinstance(station_id, int) or isinstance(station_id, bool):
                raise ShardPlanError(
                    f"readings 第 {idx} 行 station_id 必须是整数或 null"
                )
            if station_id not in seen_ids:
                raise ShardPlanError(
                    f"readings 第 {idx} 行 station_id={station_id} 在 stations 中"
                    "不存在，违反外键约束"
                )
        if value is not None and not (INT_MIN <= value <= INT_MAX):
            raise ShardPlanError(f"readings 第 {idx} 行 value 超出 int4 值域")
    return Snapshot(stations=list(stations), readings=list(readings))


def validate_assignments(
    snapshot: Snapshot, assignments: list[tuple[str, list[int]]]
) -> list[tuple[str, list[int]]]:
    """校验分片归属：分片名非空不重复，每站恰好归一片且所有站点均被覆盖。"""
    all_ids = {sid for sid, _ in snapshot.stations}
    shard_names: set[str] = set()
    covered: set[int] = set()
    for name, ids in assignments:
        if not isinstance(name, str) or not name.strip():
            raise ShardPlanError("分片名不能为空")
        if name in shard_names:
            raise ShardPlanError(f"分片名重复: {name}")
        shard_names.add(name)
        local_seen: set[int] = set()
        for sid in ids:
            if not isinstance(sid, int) or isinstance(sid, bool):
                raise ShardPlanError(f"分片 {name} 的站点 ID 必须是整数")
            if sid not in all_ids:
                raise ShardPlanError(
                    f"分片 {name} 包含快照中不存在的站点 id={sid}"
                )
            if sid in local_seen:
                raise ShardPlanError(f"站点 id={sid} 在分片 {name} 内重复列出")
            if sid in covered:
                raise ShardPlanError(
                    f"站点 id={sid} 被分配给多个分片，违反每站只归一片"
                )
            local_seen.add(sid)
            covered.add(sid)
    missing = all_ids - covered
    if missing:
        raise ShardPlanError(
            "存在未分配分片的站点 id="
            + ", ".join(str(i) for i in sorted(missing))
        )
    return [(name, list(ids)) for name, ids in assignments]


# ---------- 清单规划 ----------

def _on_match(
    ir: QueryIR,
    station_row: tuple[int, int | None],
    reading_row: tuple[int | None, int | None],
) -> bool:
    """判断一条 stations 行与一条 readings 行是否满足 ON 等值条件。

    SQL 等值语义：任一侧为 NULL 则不匹配（NULL = NULL 结果为 UNKNOWN）。
    """
    assert ir.join is not None
    station_by_col = {"id": station_row[0], "threshold": station_row[1]}
    reading_by_col = {"station_id": reading_row[0], "value": reading_row[1]}

    def cell_value(col: ColRef):
        if col.table == "stations":
            return station_by_col[col.column]
        return reading_by_col[col.column]

    lv = cell_value(ir.join.left_col)
    rv = cell_value(ir.join.right_col)
    if lv is None or rv is None:
        return False
    return lv == rv


def build_manifest(
    ir: QueryIR,
    snapshot: Snapshot,
    shard_name: str,
    owned_ids: list[int],
) -> ShardManifest:
    """为单个分片生成装载清单与分片 SELECT。"""
    stations_by_id: dict[int, tuple[int, int | None]] = {
        row[0]: row for row in snapshot.stations
    }
    owned_set = set(owned_ids)

    # 1) 满足 ON 的读数输入行号（1 起）。WHERE 不参与判定——会被 WHERE 滤掉的
    #    匹配读数同样随片装载。
    matched_rows: list[int] = []
    if ir.join is not None:
        for row_no, reading in enumerate(snapshot.readings, start=1):
            if any(
                _on_match(ir, stations_by_id[sid], reading) for sid in owned_ids
            ):
                matched_rows.append(row_no)

    # 2) 这些读数的外键所需、但本片不归属的额外站点 ID。
    extra_ids: set[int] = set()
    for row_no in matched_rows:
        fk = snapshot.readings[row_no - 1][0]
        if fk is not None and fk not in owned_set:
            extra_ids.add(fk)

    return ShardManifest(
        shard=shard_name,
        station_ids=tuple(owned_ids),
        matched_reading_rows=tuple(matched_rows),
        extra_station_ids=tuple(sorted(extra_ids)),
        shard_query=render_shard_query(ir, tuple(owned_ids)),
    )


def plan_shards(
    ir: QueryIR,
    snapshot: Snapshot,
    assignments: list[tuple[str, list[int]]],
) -> list[ShardManifest]:
    """逐片生成装载清单。"""
    return [
        build_manifest(ir, snapshot, name, ids) for name, ids in assignments
    ]


# ---------- 分片 SELECT 渲染 ----------

def _alias(table: str) -> str:
    if table == "stations":
        return STATIONS_ALIAS
    if table == "readings":
        return READINGS_ALIAS
    raise ValueError(f"未知表: {table}")  # pragma: no cover - 白名单已保证


def _col_ref(col: ColRef) -> str:
    return f"{_alias(col.table)}.{col.column}"


def _int_lit(value: int) -> str:
    return str(int(value))


def _render_bool(node: BoolNode) -> str:
    """把 WHERE 布尔 IR 渲染回 SQL（AND/OR 复合条件加括号保语义）。"""
    if isinstance(node, Compare):
        return f"{_col_ref(node.col)} {node.op} {_int_lit(node.value)}"
    if isinstance(node, IsNull):
        predicate = "IS NOT NULL" if node.negated else "IS NULL"
        return f"{_col_ref(node.col)} {predicate}"
    if isinstance(node, IrAnd):
        return f"({_render_bool(node.left)} AND {_render_bool(node.right)})"
    if isinstance(node, IrOr):
        return f"({_render_bool(node.left)} OR {_render_bool(node.right)})"
    if isinstance(node, IrNot):
        return f"NOT ({_render_bool(node.child)})"
    raise TypeError(f"未知布尔节点: {node!r}")  # pragma: no cover


def render_shard_query(ir: QueryIR, owned_ids: tuple[int, ...]) -> str:
    """把原 SELECT 的左表驱动范围限定为给定站点集合，渲染分片 SELECT。

    限定方式：在 WHERE 上追加 stations.id IN (...)；空集合用恒假条件，
    保证该片不产出任何左表驱动行（LEFT JOIN 也不会凭空补 NULL 行）。
    别名固定为 s / r，列投影与 WHERE 与原查询完全同构。
    """
    parts: list[str] = []
    projections = ", ".join(_col_ref(c) for c in ir.projections)
    parts.append(f"SELECT {projections}")
    parts.append(f"FROM stations {STATIONS_ALIAS}")
    if ir.join is not None:
        join_kw = "LEFT JOIN" if ir.join.kind == "left" else "INNER JOIN"
        parts.append(
            f"{join_kw} readings {READINGS_ALIAS} "
            f"ON {_col_ref(ir.join.left_col)} = {_col_ref(ir.join.right_col)}"
        )
    if owned_ids:
        driver_pred = (
            f"{STATIONS_ALIAS}.id IN ("
            + ", ".join(_int_lit(i) for i in owned_ids)
            + ")"
        )
    else:
        # 主键非空，该条件恒假
        driver_pred = f"{STATIONS_ALIAS}.id IS NULL"
    where_text = (
        driver_pred
        if ir.where is None
        else f"{driver_pred} AND ({_render_bool(ir.where)})"
    )
    parts.append(f"WHERE {where_text}")
    return " ".join(parts)
