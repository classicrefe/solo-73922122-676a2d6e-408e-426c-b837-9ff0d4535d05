"""/shard-check API 测试：400 校验、ERROR 降级（打桩），成功路径在 test_e2e 中覆盖。"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _base_payload():
    return {
        "query": "SELECT s.id, r.value FROM stations s "
                 "LEFT JOIN readings r ON s.id = r.station_id WHERE r.value > 10",
        "stations": [
            {"id": 1, "threshold": 20},
            {"id": 2, "threshold": 30},
        ],
        "readings": [
            {"station_id": 1, "value": 11},
            {"station_id": 1, "value": 5},
        ],
        "assignments": [
            {"shard": "north", "station_ids": [1]},
            {"shard": "south", "station_ids": [2]},
        ],
    }


def test_rejected_sql_returns_400():
    payload = _base_payload()
    payload["query"] = "SELECT * FROM stations"
    resp = client.post("/shard-check", json=payload)
    assert resp.status_code == 400
    assert "受支持子集" in resp.json()["detail"]


def test_inner_join_rejected():
    payload = _base_payload()
    payload["query"] = (
        "SELECT s.id FROM stations s INNER JOIN readings r "
        "ON s.id = r.station_id"
    )
    resp = client.post("/shard-check", json=payload)
    assert resp.status_code == 400
    assert "LEFT" in resp.json()["detail"]


def test_stations_must_be_left_table():
    payload = _base_payload()
    payload["query"] = "SELECT station_id FROM readings"
    resp = client.post("/shard-check", json=payload)
    assert resp.status_code == 400
    assert "stations 为左表" in resp.json()["detail"]


def test_dangling_fk_snapshot_returns_400():
    payload = _base_payload()
    payload["readings"].append({"station_id": 99, "value": 1})
    resp = client.post("/shard-check", json=payload)
    assert resp.status_code == 400
    assert "外键" in resp.json()["detail"]


def test_station_assigned_to_two_shards_returns_400():
    payload = _base_payload()
    payload["assignments"][1]["station_ids"] = [1, 2]
    resp = client.post("/shard-check", json=payload)
    assert resp.status_code == 400
    assert "多个分片" in resp.json()["detail"]


def test_missing_assignment_returns_400():
    payload = _base_payload()
    payload["assignments"] = [{"shard": "north", "station_ids": [1]}]
    resp = client.post("/shard-check", json=payload)
    assert resp.status_code == 400
    assert "未分配" in resp.json()["detail"]


def test_duplicate_station_pk_returns_400():
    payload = _base_payload()
    payload["stations"].append({"id": 1, "threshold": 9})
    resp = client.post("/shard-check", json=payload)
    assert resp.status_code == 400
    assert "主键" in resp.json()["detail"]


def test_int4_out_of_range_returns_422():
    payload = _base_payload()
    payload["stations"][0]["threshold"] = 2**31
    resp = client.post("/shard-check", json=payload)
    assert resp.status_code == 422


def test_error_when_db_unavailable(monkeypatch):
    """隔离 PostgreSQL 不可达 -> ERROR 且不给清单。"""
    from app import main

    class DownShardVerifier:
        def __init__(self, dsn):
            pass

        def verify(self, snapshot, manifests, original_query):
            from app.verifier import ShardVerifyOutcome

            return ShardVerifyOutcome(
                ok=False, detail="PostgreSQL 分片核对失败: 连接被拒"
            )

    monkeypatch.setattr(main, "ShardVerifier", DownShardVerifier)
    resp = client.post("/shard-check", json=_base_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ERROR"
    assert body["manifests"] is None
    assert "连接被拒" in body["message"]


def test_mismatch_reported_with_reason_and_no_manifests(monkeypatch):
    """核对未通过 -> MISMATCH，说明原因并给差异样例，不返回清单。"""
    from app import main

    class DiffShardVerifier:
        def __init__(self, dsn):
            pass

        def verify(self, snapshot, manifests, original_query):
            from app.verifier import ShardVerifyOutcome

            return ShardVerifyOutcome(
                ok=True,
                equivalent=False,
                detail="合并后的分片结果与全量查询结果多重集不一致（按列位置比较、忽略行序、保留重复）。",
                full_result=[[1, 11]],
                merged_result=[],
                mismatch={
                    "full_total": 1,
                    "merged_total": 0,
                    "examples": [
                        {"row": [1, 11], "full_count": 1, "merged_count": 0}
                    ],
                },
            )

    monkeypatch.setattr(main, "ShardVerifier", DiffShardVerifier)
    resp = client.post("/shard-check", json=_base_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "MISMATCH"
    assert body["manifests"] is None
    assert body["mismatch"][0]["row"] == [1, 11]
    assert body["mismatch"][0]["full_count"] == 1


def test_openapi_contains_shard_endpoint_and_examples():
    spec = client.get("/openapi.json").json()
    assert "/shard-check" in spec["paths"]
    # 命名示例挂在请求体上，/docs 可直接 Try it out
    content = spec["paths"]["/shard-check"]["post"]["requestBody"]["content"]
    examples = content["application/json"]["examples"]
    assert "cross-shard-left-join" in examples
    assert "cross-shard-extra-fk-stations" in examples
