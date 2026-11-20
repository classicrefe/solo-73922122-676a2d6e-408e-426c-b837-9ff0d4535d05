"""API 请求 / 响应模型。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .ir import MAX_ROWS


class CheckRequest(BaseModel):
    query_a: str = Field(..., description="原始 SELECT（当前在用的质控 SQL）")
    query_b: str = Field(..., description="改写后的 SELECT")
    solver_timeout_ms: int | None = Field(
        None,
        ge=100,
        le=120000,
        description="z3 求解超时（毫秒）；缺省取环境变量 SOLVER_TIMEOUT_MS 或 10000",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "query_a": (
                        "SELECT s.id, r.value FROM stations s "
                        "INNER JOIN readings r ON s.id = r.station_id "
                        "WHERE r.value > 0"
                    ),
                    "query_b": (
                        "SELECT s.id, r.value FROM stations s "
                        "INNER JOIN readings r ON s.id = r.station_id "
                        "WHERE r.value >= 1"
                    ),
                }
            ]
        }
    }


class Counterexample(BaseModel):
    setup_sql: str = Field(..., description="可装载的建表与数据 SQL 脚本")
    query_a_result: list[list[Any]] = Field(..., description="反例数据下 query_a 的实际结果")
    query_b_result: list[list[Any]] = Field(..., description="反例数据下 query_b 的实际结果")


class CheckResponse(BaseModel):
    verdict: Literal["EQUIVALENT", "NOT_EQUIVALENT", "UNKNOWN"] = Field(
        ...,
        description=(
            "EQUIVALENT=界内无反例；NOT_EQUIVALENT=差异已在隔离 PostgreSQL 复现；"
            "UNKNOWN=求解或复现失败"
        ),
    )
    bound_rows: int = Field(
        MAX_ROWS, description="等价结论成立的每表行数上界（含 0 行）"
    )
    message: str = Field(..., description="判定说明")
    counterexample: Counterexample | None = Field(
        None, description="verdict=NOT_EQUIVALENT 时给出可复现的反例"
    )


# ---------- 分片核对 /shard-check ----------

class StationRow(BaseModel):
    id: int = Field(..., ge=-(2**31), le=2**31 - 1, description="站点 ID（主键，非空唯一）")
    threshold: int | None = Field(
        ..., ge=-(2**31), le=2**31 - 1, description="阈值，可 NULL"
    )


class ReadingRow(BaseModel):
    station_id: int | None = Field(
        ...,
        ge=-(2**31),
        le=2**31 - 1,
        description="外键 -> stations.id，可 NULL；非 NULL 时必须引用快照内站点",
    )
    value: int | None = Field(
        ..., ge=-(2**31), le=2**31 - 1, description="读数，可 NULL"
    )


class ShardAssignment(BaseModel):
    shard: str = Field(..., min_length=1, description="分片名（唯一、非空）")
    station_ids: list[int] = Field(
        ..., description="本片归属站点 ID；每个站点恰好归属一个分片"
    )


class ShardCheckRequest(BaseModel):
    query: str = Field(
        ...,
        description=(
            "质控 SELECT：以 stations 为左表的 LEFT 等值连接"
            "（投影与 WHERE 沿用受支持语法）；也允许仅查 stations 单表"
        ),
    )
    stations: list[StationRow] = Field(..., description="stations 全量快照")
    readings: list[ReadingRow] = Field(
        ..., description="readings 全量快照（输入顺序即行号，从 1 开始；重复逐条保留）"
    )
    assignments: list[ShardAssignment] = Field(
        ..., description="各站的分片归属，每站只归一片，且所有站点均须被覆盖"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
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
                }
            ]
        }
    }


class ShardManifestOut(BaseModel):
    shard: str = Field(..., description="分片名")
    station_ids: list[int] = Field(..., description="本片归属站点 ID")
    matched_reading_rows: list[int] = Field(
        ...,
        description=(
            "与本片归属站点满足 ON 的读数输入行号（1 起，升序、去重）；"
            "读数可跨片使用，WHERE 会滤掉的匹配读数也在其中"
        ),
    )
    extra_station_ids: list[int] = Field(
        ..., description="清单读数外键所需、但不归属本片的额外站点 ID（升序、去重）"
    )
    shard_query: str = Field(
        ..., description="左表仅驱动本片归属站点的分片 SELECT"
    )


class MismatchExample(BaseModel):
    row: list[Any] = Field(..., description="取值不同的结果行（按列位置，NULL 可比较）")
    full_count: int = Field(..., description="全量结果中的出现次数")
    merged_count: int = Field(..., description="分片合并结果中的出现次数")


class ShardCheckResponse(BaseModel):
    status: Literal["VERIFIED", "MISMATCH", "ERROR"] = Field(
        ...,
        description=(
            "VERIFIED=多重集核对通过并返回清单；MISMATCH=核对未通过（含原因与差异）；"
            "ERROR=隔离 PostgreSQL 执行失败"
        ),
    )
    message: str = Field(..., description="结论说明；未通过时说明原因")
    manifests: list[ShardManifestOut] | None = Field(
        None, description="status=VERIFIED 时的逐片装载清单与分片 SELECT"
    )
    shard_row_counts: dict[str, int] | None = Field(
        None, description="各分片 SELECT 实际返回行数"
    )
    full_result: list[list[Any]] | None = Field(
        None, description="全量查询实际结果（超长截断，截断见 full_result_truncated）"
    )
    full_result_truncated: bool = Field(
        False, description="full_result 是否因行数上限被截断"
    )
    merged_result: list[list[Any]] | None = Field(
        None, description="各片结果合并后的实际结果（同样按上限截断）"
    )
    mismatch: list[MismatchExample] | None = Field(
        None, description="status=MISMATCH 时的多重集计数差异样例"
    )
