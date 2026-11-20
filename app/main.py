"""FastAPI 入口：SQL 改写有界等价核对 API。"""
from __future__ import annotations

import os

from fastapi import Body, FastAPI, HTTPException

from .ir import MAX_ROWS
from .models import (
    CheckRequest,
    CheckResponse,
    Counterexample,
    MismatchExample,
    ShardCheckRequest,
    ShardCheckResponse,
    ShardManifestOut,
)
from .sharding import (
    ShardPlanError,
    plan_shards,
    validate_assignments,
    validate_snapshot,
)
from .sql_parser import SqlRejected, parse_query
from .verifier import ShardVerifier, Verifier, setup_sql
from .z3check import check_bounded_equivalence

DEFAULT_DSN = "postgresql://postgres:postgres@localhost:5432/postgres"

app = FastAPI(
    title="SQL 改写有界等价核对 API",
    description=(
        "面向数仓质控 SQL 改写的等价性核对：提交原始 SELECT 与改写 SELECT，"
        "在固定模式 stations(id 主键, threshold) / readings(station_id 外键, value)"
        "（全列 integer，非主键可 NULL）下，枚举每表 0~3 行的所有合法取值，"
        "按列位置比较结果多重集（忽略顺序、保留重复）。"
        "差异会在隔离 PostgreSQL 中实际复现后才报 NOT_EQUIVALENT；"
        "界内无反例报 EQUIVALENT；求解或复现失败报 UNKNOWN。"
    ),
    version="1.0.0",
)

_QUERY_EXAMPLES = {
    "equivalent-integer-rewrite": {
        "summary": "界内等价：整数域上 > 0 与 >= 1 同义",
        "value": {
            "query_a": "SELECT id FROM stations WHERE threshold > 0",
            "query_b": "SELECT id FROM stations WHERE threshold >= 1",
        },
    },
    "not-equivalent-null-semantics": {
        "summary": "不等价：OR IS NULL 改变了 NULL 行的去留",
        "value": {
            "query_a": "SELECT id FROM stations WHERE threshold = 5",
            "query_b": "SELECT id FROM stations WHERE threshold = 5 OR threshold IS NULL",
        },
    },
    "not-equivalent-left-vs-inner-join": {
        "summary": "不等价：LEFT JOIN 与 INNER JOIN（无读数站点）",
        "value": {
            "query_a": (
                "SELECT s.id FROM stations s "
                "LEFT JOIN readings r ON s.id = r.station_id"
            ),
            "query_b": (
                "SELECT s.id FROM stations s "
                "INNER JOIN readings r ON s.id = r.station_id"
            ),
        },
    },
    "equivalent-left-join-counting": {
        "summary": "界内等价：连接后过滤的两种写法",
        "value": {
            "query_a": (
                "SELECT s.id, r.value FROM stations s "
                "INNER JOIN readings r ON s.id = r.station_id "
                "WHERE r.value >= 10 AND s.threshold IS NOT NULL"
            ),
            "query_b": (
                "SELECT s.id, r.value FROM stations s "
                "INNER JOIN readings r ON s.id = r.station_id "
                "WHERE NOT (r.value < 10) AND NOT (s.threshold IS NULL)"
            ),
        },
    },
}


def _dsn() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DSN)


def _solver_timeout_ms(req: CheckRequest) -> int:
    if req.solver_timeout_ms is not None:
        return req.solver_timeout_ms
    return int(os.environ.get("SOLVER_TIMEOUT_MS", "10000"))


# /shard-check 的 Swagger 命名示例
_SHARD_EXAMPLES = {
    "cross-shard-left-join": {
        "summary": "跨片匹配：读数按 ON 进入非归属分片，WHERE 滤掉的匹配读数仍随片装载",
        "value": {
            "query": (
                "SELECT s.id, r.value FROM stations s "
                "LEFT JOIN readings r ON s.id = r.station_id "
                "WHERE r.value > 10"
            ),
            "stations": [
                {"id": 1, "threshold": 20},
                {"id": 2, "threshold": 30},
                {"id": 3, "threshold": 40},
            ],
            "readings": [
                {"station_id": 1, "value": 11},
                {"station_id": 1, "value": 5},
                {"station_id": 2, "value": 22},
                {"station_id": 3, "value": 9},
            ],
            "assignments": [
                {"shard": "north", "station_ids": [1, 2]},
                {"shard": "south", "station_ids": [3]},
            ],
        },
    },
    "cross-shard-extra-fk-stations": {
        "summary": "跨片外键：ON 非外键列时，清单读数外键指向的额外站点需随片装载",
        "value": {
            "query": (
                "SELECT s.id, r.station_id FROM stations s "
                "LEFT JOIN readings r ON s.threshold = r.value"
            ),
            "stations": [
                {"id": 1, "threshold": 20},
                {"id": 2, "threshold": 30},
                {"id": 3, "threshold": 40},
            ],
            "readings": [
                {"station_id": 3, "value": 20},
                {"station_id": 1, "value": 30},
            ],
            "assignments": [
                {"shard": "north", "station_ids": [1, 2]},
                {"shard": "south", "station_ids": [3]},
            ],
        },
    },
    "single-table-stations": {
        "summary": "单表退化场景：仅 stations 驱动，无读数清单",
        "value": {
            "query": "SELECT id, threshold FROM stations WHERE threshold >= 30",
            "stations": [
                {"id": 1, "threshold": 20},
                {"id": 2, "threshold": 30},
            ],
            "readings": [],
            "assignments": [
                {"shard": "north", "station_ids": [1]},
                {"shard": "south", "station_ids": [2]},
            ],
        },
    },
}


@app.get("/", summary="服务信息")
def root() -> dict:
    return {
        "service": "sql-equivalence-checker",
        "docs": "/docs",
        "check_endpoint": "POST /check",
        "shard_check_endpoint": "POST /shard-check",
        "bound_rows_per_table": MAX_ROWS,
    }


@app.get("/health", summary="健康检查（含数据库连通性）")
def health() -> dict:
    db_up = Verifier(_dsn()).ping()
    return {"status": "ok", "database": "up" if db_up else "down"}


@app.post(
    "/check",
    response_model=CheckResponse,
    summary="核对两条 SELECT 在界内是否等价",
)
def check_equivalence(
    req: CheckRequest = Body(openapi_examples=_QUERY_EXAMPLES),
) -> CheckResponse:
    # 1. 解析与白名单校验
    try:
        ir_a = parse_query(req.query_a)
        ir_b = parse_query(req.query_b)
    except SqlRejected as exc:
        raise HTTPException(status_code=400, detail=f"SQL 超出受支持子集: {exc}")
    if len(ir_a.projections) != len(ir_b.projections):
        raise HTTPException(
            status_code=400,
            detail="两条查询的投影列数不一致，无法按列位置比较",
        )

    # 2. z3 有界等价判定
    outcome = check_bounded_equivalence(ir_a, ir_b, _solver_timeout_ms(req))
    if outcome.status == "equivalent":
        return CheckResponse(
            verdict="EQUIVALENT",
            message=(
                f"在每表 0~{MAX_ROWS} 行的所有合法取值下，两侧结果多重集相同"
                "（有界等价，不构成更大表上的证明）。"
            ),
        )
    if outcome.status == "unknown":
        return CheckResponse(
            verdict="UNKNOWN", message=f"求解失败：{outcome.detail}"
        )

    # 3. 候选反例必须在隔离 PostgreSQL 中实际复现
    assert outcome.witness is not None
    repro = Verifier(_dsn()).reproduce(outcome.witness, req.query_a, req.query_b)
    if not repro.ok:
        return CheckResponse(verdict="UNKNOWN", message=repro.detail)
    if not repro.differs:
        return CheckResponse(
            verdict="UNKNOWN",
            message="内部不一致：z3 反例在 PostgreSQL 中未复现差异，按未知处理。",
        )
    return CheckResponse(
        verdict="NOT_EQUIVALENT",
        message="两侧结果存在差异，已在隔离 PostgreSQL 中实际复现。",
        counterexample=Counterexample(
            setup_sql=setup_sql(outcome.witness),
            query_a_result=repro.query_a_result or [],
            query_b_result=repro.query_b_result or [],
        ),
    )


def _validate_shard_query(ir) -> None:
    """分片核对仅接受 stations 为左表的 LEFT 等值连接（或 stations 单表）。"""
    if ir.tables[0] != "stations":
        raise HTTPException(
            status_code=400,
            detail="分片核对要求查询以 stations 为左表驱动（FROM stations ...）",
        )
    if ir.join is not None:
        if ir.join.kind != "left":
            raise HTTPException(
                status_code=400,
                detail="分片核对仅支持 LEFT [OUTER] JOIN，INNER JOIN 会丢失无匹配站点",
            )
        if ir.tables[1] != "readings":
            raise HTTPException(
                status_code=400,
                detail="分片核对的被连接表必须是 readings",
            )


@app.post(
    "/shard-check",
    response_model=ShardCheckResponse,
    summary="按站点分片生成装载清单，并在隔离 PostgreSQL 核对合并结果与全量结果",
)
def shard_check(
    req: ShardCheckRequest = Body(openapi_examples=_SHARD_EXAMPLES),
) -> ShardCheckResponse:
    # 1. 解析与白名单校验，并限定为 stations 左表 LEFT 等值连接
    try:
        ir = parse_query(req.query)
    except SqlRejected as exc:
        raise HTTPException(status_code=400, detail=f"SQL 超出受支持子集: {exc}")
    _validate_shard_query(ir)

    # 2. 快照与分片归属校验（沿用固定表约束；每站恰好归一片）
    stations = [(r.id, r.threshold) for r in req.stations]
    readings = [(r.station_id, r.value) for r in req.readings]
    try:
        snapshot = validate_snapshot(stations, readings)
        assignments = validate_assignments(
            snapshot, [(a.shard, list(a.station_ids)) for a in req.assignments]
        )
    except ShardPlanError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # 3. 逐片规划装载清单与分片 SELECT（纯计算，不触碰数据库）
    manifests = plan_shards(ir, snapshot, assignments)

    # 4. 在隔离 PostgreSQL 中按清单逐片装载、执行，合并后与全量查询核对
    outcome = ShardVerifier(_dsn()).verify(snapshot, manifests, req.query)
    if not outcome.ok:
        return ShardCheckResponse(status="ERROR", message=outcome.detail)
    if not outcome.equivalent:
        return ShardCheckResponse(
            status="MISMATCH",
            message=outcome.detail,
            full_result=outcome.full_result,
            full_result_truncated=outcome.full_result_truncated,
            merged_result=outcome.merged_result,
            mismatch=[
                MismatchExample(
                    row=ex["row"],
                    full_count=ex["full_count"],
                    merged_count=ex["merged_count"],
                )
                for ex in (outcome.mismatch or {}).get("examples", [])
            ],
        )

    return ShardCheckResponse(
        status="VERIFIED",
        message=(
            "各片按清单装载执行后合并，结果多重集与全量查询一致"
            "（按列位置比较、忽略行序、保留重复）。"
        ),
        manifests=[
            ShardManifestOut(
                shard=m.shard,
                station_ids=list(m.station_ids),
                matched_reading_rows=list(m.matched_reading_rows),
                extra_station_ids=list(m.extra_station_ids),
                shard_query=m.shard_query,
            )
            for m in manifests
        ],
        shard_row_counts=outcome.shard_row_counts,
        full_result=outcome.full_result,
        full_result_truncated=outcome.full_result_truncated,
        merged_result=outcome.merged_result,
    )
