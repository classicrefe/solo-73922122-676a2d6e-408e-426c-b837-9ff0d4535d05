"""端到端测试：需要真实 PostgreSQL（环境变量 DATABASE_URL）。

本地无 Docker 时可用 pgserver 提供临时实例：
    DATABASE_URL=$(python -c ...) pytest tests/test_e2e.py
在 docker compose 环境中，DATABASE_URL 指向 db 服务。
"""
import os

import pytest
from fastapi.testclient import TestClient

from app.main import app

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL 未设置，跳过端到端测试"
)

client = TestClient(app)


def test_e2e_not_equivalent_reproduced_in_postgres():
    resp = client.post(
        "/check",
        json={
            "query_a": "SELECT id FROM stations WHERE threshold = 5",
            "query_b": "SELECT id FROM stations WHERE threshold = 5 OR threshold IS NULL",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] == "NOT_EQUIVALENT"
    cx = body["counterexample"]
    assert "CREATE TABLE stations" in cx["setup_sql"]
    # 两侧结果确实不同，且 b 侧多出 NULL threshold 的站点
    assert sorted(map(tuple, cx["query_a_result"])) != sorted(
        map(tuple, cx["query_b_result"])
    )


def test_e2e_equivalent():
    resp = client.post(
        "/check",
        json={
            "query_a": "SELECT s.id FROM stations s LEFT JOIN readings r "
                       "ON s.id = r.station_id WHERE r.value IS NULL",
            "query_b": "SELECT s.id FROM stations s LEFT JOIN readings r "
                       "ON s.id = r.station_id WHERE r.value IS NULL "
                       "AND (s.threshold > -2147483648 OR s.threshold <= -2147483648 "
                       "OR s.threshold IS NULL)",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["verdict"] == "EQUIVALENT"


def test_e2e_left_join_duplicate_witness():
    resp = client.post(
        "/check",
        json={
            "query_a": "SELECT s.id FROM stations s LEFT JOIN readings r "
                       "ON s.id = r.station_id",
            "query_b": "SELECT id FROM stations",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] == "NOT_EQUIVALENT"
    # 反例装载 SQL 可直接执行（在复现事务里已跑通过）
    assert "INSERT INTO readings" in body["counterexample"]["setup_sql"]


def test_e2e_health_db_up():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["database"] == "up"


# ---------- /shard-check 端到端（真实 PostgreSQL） ----------

def test_e2e_shard_check_verified_cross_shard():
    """跨片匹配 + WHERE 过滤读数仍随片装载，合并结果与全量一致。"""
    resp = client.post(
        "/shard-check",
        json={
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
                {"station_id": 1, "value": 5},    # 匹配但被 WHERE 滤掉，仍随片
                {"station_id": 2, "value": 22},
                {"station_id": 3, "value": 9},    # 匹配但被 WHERE 滤掉
            ],
            "assignments": [
                {"shard": "north", "station_ids": [1, 2]},
                {"shard": "south", "station_ids": [3]},
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "VERIFIED", body
    by_name = {m["shard"]: m for m in body["manifests"]}
    assert by_name["north"]["matched_reading_rows"] == [1, 2, 3]
    assert by_name["south"]["matched_reading_rows"] == [4]
    # 各片 SELECT 都把左表限定到本片归属站点
    assert "s.id IN (1, 2)" in by_name["north"]["shard_query"]
    assert "s.id IN (3)" in by_name["south"]["shard_query"]
    # 实际执行行数：north 两条 >10，south 零条（LEFT JOIN 补的 NULL 行也被 WHERE 滤掉）
    assert body["shard_row_counts"] == {"north": 2, "south": 0}
    # 多重集一致
    assert sorted(map(tuple, body["full_result"])) == sorted(
        map(tuple, body["merged_result"])
    )


def test_e2e_shard_check_extra_fk_stations_loaded():
    """ON 非外键列时，清单读数的外键额外站点必须随片装载，否则违反 FK。"""
    resp = client.post(
        "/shard-check",
        json={
            "query": (
                "SELECT s.id, r.station_id, r.value FROM stations s "
                "LEFT JOIN readings r ON s.threshold = r.value"
            ),
            "stations": [
                {"id": 1, "threshold": 20},
                {"id": 2, "threshold": 30},
                {"id": 3, "threshold": 40},
            ],
            # 行1 外键指站点3，但 ON(threshold=value) 匹配归属 north 的站点1
            "readings": [
                {"station_id": 3, "value": 20},
                {"station_id": 1, "value": 30},
            ],
            "assignments": [
                {"shard": "north", "station_ids": [1, 2]},
                {"shard": "south", "station_ids": [3]},
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "VERIFIED", body
    by_name = {m["shard"]: m for m in body["manifests"]}
    assert by_name["north"]["matched_reading_rows"] == [1, 2]
    assert by_name["north"]["extra_station_ids"] == [3]
    assert by_name["south"]["matched_reading_rows"] == []
    assert sorted(map(tuple, body["full_result"])) == sorted(
        map(tuple, body["merged_result"])
    )


def test_e2e_shard_check_duplicate_readings_multiset():
    """重复读数逐条保留：合并后重复行数必须与全量一致。"""
    resp = client.post(
        "/shard-check",
        json={
            "query": (
                "SELECT s.id, r.value FROM stations s "
                "LEFT JOIN readings r ON s.id = r.station_id"
            ),
            "stations": [
                {"id": 1, "threshold": 1},
                {"id": 2, "threshold": 2},
            ],
            "readings": [
                {"station_id": 1, "value": 7},
                {"station_id": 1, "value": 7},
                {"station_id": 1, "value": 7},
                {"station_id": 2, "value": 8},
            ],
            "assignments": [
                {"shard": "a", "station_ids": [1]},
                {"shard": "b", "station_ids": [2]},
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "VERIFIED", body
    # 站点1 的三条重复读数全部进入 a 片，合并结果保留三个重复行
    assert body["shard_row_counts"] == {"a": 3, "b": 1}
    merged = [tuple(r) for r in body["merged_result"]]
    assert merged.count((1, 7)) == 3


def test_e2e_shard_check_single_table_and_empty_shard():
    """单表查询与空分片：空片不产出左表驱动行。"""
    resp = client.post(
        "/shard-check",
        json={
            "query": "SELECT id, threshold FROM stations WHERE threshold >= 30",
            "stations": [
                {"id": 1, "threshold": 20},
                {"id": 2, "threshold": 30},
            ],
            "readings": [],
            "assignments": [
                {"shard": "north", "station_ids": [1]},
                {"shard": "south", "station_ids": [2]},
                {"shard": "empty", "station_ids": []},
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "VERIFIED", body
    assert body["shard_row_counts"] == {"north": 0, "south": 1, "empty": 0}
    by_name = {m["shard"]: m for m in body["manifests"]}
    assert "s.id IS NULL" in by_name["empty"]["shard_query"]


def test_e2e_shard_check_null_reading_fk():
    """station_id 为 NULL 的读数不匹配任何站点，不进任何清单；LEFT JOIN 保留站点行。"""
    resp = client.post(
        "/shard-check",
        json={
            "query": (
                "SELECT s.id, r.station_id FROM stations s "
                "LEFT JOIN readings r ON s.id = r.station_id"
            ),
            "stations": [{"id": 1, "threshold": 1}, {"id": 2, "threshold": 2}],
            "readings": [
                {"station_id": None, "value": 100},
                {"station_id": 1, "value": 5},
            ],
            "assignments": [
                {"shard": "a", "station_ids": [1]},
                {"shard": "b", "station_ids": [2]},
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "VERIFIED", body
    by_name = {m["shard"]: m for m in body["manifests"]}
    assert by_name["a"]["matched_reading_rows"] == [2]
    assert by_name["b"]["matched_reading_rows"] == []
    # 站点2 无匹配，LEFT JOIN 补 NULL 扩展行，仍在全量与合并结果中各出现一次
    full = [tuple(r) for r in body["full_result"]]
    merged = [tuple(r) for r in body["merged_result"]]
    assert full.count((2, None)) == 1
    assert sorted(full) == sorted(merged)
