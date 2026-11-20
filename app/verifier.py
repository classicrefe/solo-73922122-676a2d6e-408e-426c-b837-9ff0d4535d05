"""在隔离的 PostgreSQL 模式中实际复现反例。

z3 给出的候选反例必须在真实 PostgreSQL 中重放并确认两侧结果多重集确实不同，
才允许上报"不等价"；复现失败或未复现差异时一律按"未知"处理（由调用方决定）。
隔离方式：每次复现使用独立的临时 SCHEMA，整个流程在单事务内完成并回滚，
不在数据库中留下任何对象。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import psycopg

from .sharding import ShardManifest, Snapshot
from .z3check import CounterexampleData

_CREATE_STATIONS = "CREATE TABLE stations (id INTEGER PRIMARY KEY, threshold INTEGER)"
_CREATE_READINGS = (
    "CREATE TABLE readings (station_id INTEGER REFERENCES stations(id), value INTEGER)"
)

# 单结果集最多回传的行数（防止超大快照把响应体撑爆；多重集比较不受此限）
RESULT_ROW_CAP = 200


@dataclass
class ReproOutcome:
    ok: bool                     # 复现流程是否成功执行
    differs: bool = False        # 两侧结果多重集是否确实不同
    detail: str = ""
    query_a_result: list[list[Any]] | None = None
    query_b_result: list[list[Any]] | None = None


def setup_sql(witness: CounterexampleData) -> str:
    """生成可装载的 SQL 脚本（重建表结构与反例数据）。"""
    lines = [
        "DROP TABLE IF EXISTS readings;",
        "DROP TABLE IF EXISTS stations;",
        _CREATE_STATIONS + ";",
        _CREATE_READINGS + ";",
    ]
    if witness.stations:
        vals = ", ".join(f"({sid}, {_lit(th)})" for sid, th in witness.stations)
        lines.append(f"INSERT INTO stations (id, threshold) VALUES {vals};")
    if witness.readings:
        vals = ", ".join(f"({_lit(sid)}, {_lit(v)})" for sid, v in witness.readings)
        lines.append(f"INSERT INTO readings (station_id, value) VALUES {vals};")
    return "\n".join(lines) + "\n"


def _lit(v: Any) -> str:
    return "NULL" if v is None else str(int(v))


def _canon(rows: list[list[Any]]) -> list[tuple]:
    """规范化结果多重集：NULL 可比较、忽略行序、保留重复。"""
    return sorted(
        tuple((0, 0) if v is None else (1, v) for v in row) for row in rows
    )


class Verifier:
    def __init__(self, dsn: str, statement_timeout_ms: int = 10000) -> None:
        self._dsn = dsn
        self._timeout_ms = statement_timeout_ms

    def ping(self) -> bool:
        try:
            with psycopg.connect(self._dsn, connect_timeout=3) as conn:
                conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    def reproduce(
        self, witness: CounterexampleData, query_a: str, query_b: str
    ) -> ReproOutcome:
        schema = "cc_" + uuid.uuid4().hex
        try:
            with psycopg.connect(self._dsn, connect_timeout=10) as conn:
                with conn.cursor() as cur:
                    cur.execute(f'CREATE SCHEMA "{schema}"')
                    cur.execute(f'SET search_path TO "{schema}"')
                    cur.execute(f"SET LOCAL statement_timeout TO {self._timeout_ms}")
                    cur.execute(_CREATE_STATIONS)
                    cur.execute(_CREATE_READINGS)
                    if witness.stations:
                        cur.executemany(
                            "INSERT INTO stations (id, threshold) VALUES (%s, %s)",
                            witness.stations,
                        )
                    if witness.readings:
                        cur.executemany(
                            "INSERT INTO readings (station_id, value) VALUES (%s, %s)",
                            witness.readings,
                        )
                    cur.execute(query_a)
                    res_a = [list(r) for r in cur.fetchall()]
                    cur.execute(query_b)
                    res_b = [list(r) for r in cur.fetchall()]
                conn.rollback()  # 隔离：事务内建临时模式，回滚即清理
        except Exception as exc:  # 数据库不可达 / 执行失败 -> 未知
            return ReproOutcome(ok=False, detail=f"PostgreSQL 复现失败: {exc}")
        return ReproOutcome(
            ok=True,
            differs=_canon(res_a) != _canon(res_b),
            query_a_result=res_a,
            query_b_result=res_b,
        )


# ---------- 分片装载执行与结果核对 ----------

@dataclass
class ShardVerifyOutcome:
    """逐片装载、执行并与全量查询核对后的结果。"""

    ok: bool                       # 整个执行流程是否成功
    equivalent: bool = False       # 合并结果与全量结果的多重集是否一致
    detail: str = ""
    full_result: list[list[Any]] | None = None
    full_result_truncated: bool = False
    merged_result: list[list[Any]] | None = None
    shard_row_counts: dict[str, int] = field(default_factory=dict)
    mismatch: dict[str, Any] | None = None


def _load_table(cur, table: str, columns: tuple[str, ...], rows: list[tuple]) -> None:
    if not rows:
        return
    placeholders = ", ".join(["%s"] * len(columns))
    col_list = ", ".join(columns)
    cur.executemany(
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})", rows
    )


def _multiset_diff_detail(
    full: list[list[Any]], merged: list[list[Any]], limit: int = 10
) -> dict[str, Any]:
    """按多重集（忽略行序、保留重复、NULL 可比较）给出首个取值的计数差异。"""
    from collections import Counter

    cf = Counter(tuple((0, 0) if v is None else (1, v) for v in row) for row in full)
    cm = Counter(tuple((0, 0) if v is None else (1, v) for v in row) for row in merged)
    examples: list[dict[str, Any]] = []
    for key in sorted(set(cf) | set(cm)):
        if cf[key] != cm[key]:
            examples.append(
                {
                    "row": [None if tag == 0 else v for tag, v in key],
                    "full_count": cf[key],
                    "merged_count": cm[key],
                }
            )
            if len(examples) >= limit:
                break
    return {
        "full_total": len(full),
        "merged_total": len(merged),
        "examples": examples,
    }


class ShardVerifier:
    """在隔离 PostgreSQL 中按清单逐片装载、执行分片 SELECT 并核对多重集。

    每个分片与全量基线分别使用独立临时 schema；整个流程在单事务内完成并
    回滚，不在数据库中留下任何对象。
    """

    def __init__(self, dsn: str, statement_timeout_ms: int = 30000) -> None:
        self._dsn = dsn
        self._timeout_ms = statement_timeout_ms

    def verify(
        self,
        snapshot: Snapshot,
        manifests: list[ShardManifest],
        original_query: str,
    ) -> ShardVerifyOutcome:
        stations_by_id = {row[0]: row for row in snapshot.stations}
        try:
            with psycopg.connect(self._dsn, connect_timeout=10) as conn:
                with conn.cursor() as cur:
                    # 1) 全量基线：独立临时 schema 装载完整快照并执行原 SELECT
                    full_schema = "sc_" + uuid.uuid4().hex
                    cur.execute(f'CREATE SCHEMA "{full_schema}"')
                    cur.execute(f'SET search_path TO "{full_schema}"')
                    cur.execute(
                        f"SET LOCAL statement_timeout TO {self._timeout_ms}"
                    )
                    cur.execute(_CREATE_STATIONS)
                    cur.execute(_CREATE_READINGS)
                    _load_table(
                        cur, "stations", ("id", "threshold"), snapshot.stations
                    )
                    _load_table(
                        cur,
                        "readings",
                        ("station_id", "value"),
                        snapshot.readings,
                    )
                    cur.execute(original_query)
                    full_rows = [list(r) for r in cur.fetchall()]

                    # 2) 逐片：独立临时 schema 按清单装载，执行各片 SELECT
                    merged: list[list[Any]] = []
                    shard_counts: dict[str, int] = {}
                    for manifest in manifests:
                        shard_schema = "sc_" + uuid.uuid4().hex
                        cur.execute(f'CREATE SCHEMA "{shard_schema}"')
                        cur.execute(f'SET search_path TO "{shard_schema}"')
                        cur.execute(_CREATE_STATIONS)
                        cur.execute(_CREATE_READINGS)
                        owned = [
                            stations_by_id[sid] for sid in manifest.station_ids
                        ]
                        extras = [
                            stations_by_id[sid]
                            for sid in manifest.extra_station_ids
                        ]
                        # 先父表后子表，满足外键约束
                        _load_table(
                            cur,
                            "stations",
                            ("id", "threshold"),
                            owned + extras,
                        )
                        shard_readings = [
                            snapshot.readings[row_no - 1]
                            for row_no in manifest.matched_reading_rows
                        ]
                        _load_table(
                            cur,
                            "readings",
                            ("station_id", "value"),
                            shard_readings,
                        )
                        cur.execute(manifest.shard_query)
                        part = [list(r) for r in cur.fetchall()]
                        shard_counts[manifest.shard] = len(part)
                        merged.extend(part)
                conn.rollback()  # 隔离：事务内建临时模式，回滚即清理
        except Exception as exc:
            return ShardVerifyOutcome(
                ok=False, detail=f"PostgreSQL 分片核对失败: {exc}"
            )

        canon_full = _canon(full_rows)
        canon_merged = _canon(merged)
        equivalent = canon_full == canon_merged
        outcome = ShardVerifyOutcome(
            ok=True,
            equivalent=equivalent,
            full_result=full_rows[:RESULT_ROW_CAP],
            full_result_truncated=len(full_rows) > RESULT_ROW_CAP,
            merged_result=merged[:RESULT_ROW_CAP],
            shard_row_counts=shard_counts,
        )
        if not equivalent:
            outcome.detail = (
                "合并后的分片结果与全量查询结果多重集不一致"
                "（按列位置比较、忽略行序、保留重复）。"
            )
            outcome.mismatch = _multiset_diff_detail(full_rows, merged)
        return outcome
