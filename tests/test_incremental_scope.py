"""增量更正优化的性质测试。

对应任务要求：

* 小批更正不再整表重写：未涉及行的 rowid 不变，持久 DML 只命中
  状态变化的成员行 + vehicle_models 规模行；
* 核定读范围只有"车型规模 + 本批涉及成员"，写连接上对两张持久
  维表的 SELECT 都必须由 batch_* 临时表限定；
* 重复 plus 已有配对按保留处理（幂等通过、零持久写入）；
* 拒收反例在语义上必须是"目标矩形会拼出、但更正后集合没有"的组合；
* 旧库（无规模列）启动时扫描一次原表回填，之后更正正常；
* 写入故障时两表与规模保持原值；
* 随机批次暴力预言机：成功后的两表笛卡尔积 == 期望集合，
  失败时两表与规模不变且反例合法。
"""

from __future__ import annotations

import random
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import service
from app.database import db_path
from app.main import app


@pytest.fixture()
def env_db(tmp_path, monkeypatch):
    path = tmp_path / "scope.db"
    monkeypatch.setenv("WAREHOUSE_DB", str(path))
    return path


@pytest.fixture()
def client(env_db):
    with TestClient(app) as c:
        yield c


def _pairs(spec):
    return [{"partNo": p, "conditionCode": c} for p, c in spec]


def _rowids(path: str, table: str, mid: str):
    conn = sqlite3.connect(path)
    try:
        return dict(
            conn.execute(
                f"SELECT {'part_no' if table == 'model_parts' else 'condition_code'}, rowid "
                f"FROM {table} WHERE model_id = ?",
                (mid,),
            ).fetchall()
        )
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 1. 重复 plus 已有配对 = 保留，幂等通过
# --------------------------------------------------------------------------- #
def test_repeat_plus_existing_pair_is_retained(client):
    spec = [("P1", "A"), ("P1", "B"), ("P2", "A"), ("P2", "B")]
    client.post("/api/v1/models/load", json={"modelId": "M", "pairs": _pairs(spec)})

    r = client.post(
        "/api/v1/models/correct",
        json={"modelId": "M", "plus": _pairs([("P1", "A")]), "minus": []},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["parts"] == ["P1", "P2"]
    assert body["conditions"] == ["A", "B"]
    assert body["effectivePairCount"] == 4
    assert body["before"] == {
        "partCount": 2,
        "conditionCount": 2,
        "effectivePairCount": 4,
    }


# --------------------------------------------------------------------------- #
# 2. 只新增一个备件：只多一行备件维表，其他行 rowid 不变
# --------------------------------------------------------------------------- #
def test_add_one_part_only_inserts_that_part_row(client, env_db):
    parts = [f"P{i:03d}" for i in range(100)]
    spec = [(p, cc) for p in parts for cc in ("A", "B", "C")]
    client.post("/api/v1/models/load", json={"modelId": "BIG", "pairs": _pairs(spec)})

    before_p = _rowids(str(env_db), "model_parts", "BIG")
    before_c = _rowids(str(env_db), "model_conditions", "BIG")

    r = client.post(
        "/api/v1/models/correct",
        json={
            "modelId": "BIG",
            "plus": _pairs([("PNEW", "A"), ("PNEW", "B"), ("PNEW", "C")]),
            "minus": [],
        },
    )
    assert r.status_code == 200, r.text

    after_p = _rowids(str(env_db), "model_parts", "BIG")
    after_c = _rowids(str(env_db), "model_conditions", "BIG")

    # 100 个在册备件行的物理位置一行都没有被重写
    assert all(after_p[p] == rid for p, rid in before_p.items())
    assert all(after_c[c] == rid for c, rid in before_c.items())
    assert "PNEW" in after_p and len(after_p) == 101
    assert len(after_c) == 3


# --------------------------------------------------------------------------- #
# 3. 只补一个工况：只多一行工况维表
# --------------------------------------------------------------------------- #
def test_add_one_condition_only_inserts_condition_row(client, env_db):
    spec = [("P1", "A"), ("P2", "A")]
    client.post("/api/v1/models/load", json={"modelId": "M", "pairs": _pairs(spec)})
    before_p = _rowids(str(env_db), "model_parts", "M")
    before_c = _rowids(str(env_db), "model_conditions", "M")

    r = client.post(
        "/api/v1/models/correct",
        json={
            "modelId": "M",
            "plus": _pairs([("P1", "B"), ("P2", "B")]),
            "minus": [],
        },
    )
    assert r.status_code == 200, r.text
    after_p = _rowids(str(env_db), "model_parts", "M")
    after_c = _rowids(str(env_db), "model_conditions", "M")
    assert before_p == after_p  # 备件表完全不动
    assert all(after_c[c] == rid for c, rid in before_c.items())
    assert "B" in after_c and len(after_c) == 2


# --------------------------------------------------------------------------- #
# 4. 写范围 + 核定读范围（SQL 追踪，按连接分流）
# --------------------------------------------------------------------------- #
@pytest.fixture()
def traced_service(monkeypatch):
    streams: list[list[str]] = []
    real_get_connection = service.get_connection

    def factory():
        conn = real_get_connection()
        buf: list[str] = []
        streams.append(buf)
        conn.set_trace_callback(
            lambda sql: buf.append(" ".join(sql.split()).upper())
        )
        return conn

    monkeypatch.setattr(service, "get_connection", factory)
    return streams


def _is_persistent_dml(sql: str) -> bool:
    head = sql.split(" ", 1)[0]
    return head in {"INSERT", "DELETE", "UPDATE"} and (
        "MODEL_PARTS" in sql
        or "MODEL_CONDITIONS" in sql
        or "VEHICLE_MODELS" in sql
    )


def test_successful_tiny_batch_write_and_read_scope(
    client, env_db, traced_service
):
    spec = [(f"P{i:03d}", cc) for i in range(60) for cc in ("A", "B", "C")]
    client.post("/api/v1/models/load", json={"modelId": "BIG", "pairs": _pairs(spec)})

    traced_service.clear()
    r = client.post(
        "/api/v1/models/correct",
        json={
            "modelId": "BIG",
            "plus": _pairs([("PNEW", "A"), ("PNEW", "B"), ("PNEW", "C")]),
            "minus": [],
        },
    )
    assert r.status_code == 200, r.text

    write_stream = next(
        stream
        for stream in traced_service
        if any(_is_persistent_dml(s) for s in stream)
    )

    # 持久 DML 恰好：1 行新备件 INSERT + 1 行规模 UPDATE；
    # 不允许出现工况表 DML，也不允许无成员条件的整车型 DELETE。
    dml = [s for s in write_stream if _is_persistent_dml(s)]
    assert sum(s.startswith("INSERT INTO MODEL_PARTS") for s in dml) == 1
    assert not any("MODEL_CONDITIONS" in s and s.split()[0] != "SELECT" for s in dml)
    assert any(s.startswith("UPDATE VEHICLE_MODELS") for s in dml)
    assert not any(
        s.startswith("DELETE FROM MODEL_PARTS WHERE MODEL_ID = ?")
        and "PART_NO" not in s
        for s in dml
    )

    # 核定阶段对两张维表的 SELECT 必须由 batch_* 临时表限定，
    # 不允许裸读该车型的全部备件/工况行。
    for sql in write_stream:
        if sql.startswith("SELECT") and "MODEL_PARTS" in sql:
            assert "BATCH_PARTS" in sql, sql
        if sql.startswith("SELECT") and "MODEL_CONDITIONS" in sql:
            assert "BATCH_CONDS" in sql, sql

    # 任何连接上都不得同时把两张持久维表作为行源（不展开配对）。
    import re

    def joins_two_tables(sql: str) -> bool:
        m = re.search(r"FROM(.+?)(?:WHERE|GROUP BY|ORDER BY|LIMIT|$)", sql)
        if not m:
            return False
        src = m.group(1)
        return "MODEL_PARTS" in src and "MODEL_CONDITIONS" in src

    offenders = [s for stream in traced_service for s in stream if joins_two_tables(s)]
    assert not offenders


def test_rejected_batch_issues_no_persistent_writes(
    client, env_db, traced_service
):
    spec = [(p, c) for p in ("P1", "P2") for c in ("A", "B")]
    client.post("/api/v1/models/load", json={"modelId": "M", "pairs": _pairs(spec)})
    before_p = _rowids(str(env_db), "model_parts", "M")

    traced_service.clear()
    r = client.post(
        "/api/v1/models/correct",
        json={"modelId": "M", "plus": [], "minus": _pairs([("P2", "B")])},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["extraCombinationExample"] == {
        "partNo": "P2",
        "conditionCode": "B",
    }

    # 所有连接上都没有持久表 DML（批次 TEMP 表写入不算）。
    persistent_dml = [
        s
        for stream in traced_service
        for s in stream
        if _is_persistent_dml(s)
    ]
    assert persistent_dml == []
    assert _rowids(str(env_db), "model_parts", "M") == before_p


# --------------------------------------------------------------------------- #
# 5. 拒收反例的语义合法性（多组形状）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "parts,conds,plus,minus",
    [
        (["P1", "P2"], ["A", "B"], [], [("P2", "B")]),
        (["P1", "P2"], ["A", "B"], [("P1", "C")], []),
        (["P1"], ["A", "B"], [("P9", "A")], []),
        (
            ["P1", "P2", "P3"],
            ["A", "B", "C"],
            [("P1", "D")],
            [("P2", "A")],
        ),
    ],
)
def test_witness_is_actually_missing_combination(
    client, parts, conds, plus, minus
):
    spec = [(p, c) for p in parts for c in conds]
    client.post("/api/v1/models/load", json={"modelId": "W", "pairs": _pairs(spec)})

    # 预言机推导更正后的候选维表
    desired = set(spec)
    desired -= set(minus)
    desired |= set(plus)
    target_parts = {p for p, _ in desired}
    target_conds = {c for _, c in desired}
    is_rectangle = len(desired) == len(target_parts) * len(target_conds)
    if is_rectangle:
        pytest.skip("parametrized shape happens to be a rectangle")

    r = client.post(
        "/api/v1/models/correct",
        json={"modelId": "W", "plus": _pairs(plus), "minus": _pairs(minus)},
    )
    assert r.status_code == 422, r.text
    w = r.json()["detail"]["extraCombinationExample"]
    witness = (w["partNo"], w["conditionCode"])
    assert witness[0] in target_parts
    assert witness[1] in target_conds
    assert witness not in desired
    # 原两表不变
    body = client.get("/api/v1/models/W").json()
    assert body["parts"] == sorted(parts)
    assert body["conditions"] == sorted(conds)


# --------------------------------------------------------------------------- #
# 6. 接入旧库：无规模列 -> 启动扫描一次回填 -> 增量更正可用
# --------------------------------------------------------------------------- #
def test_legacy_database_one_scan_backfill_and_correct(tmp_path, monkeypatch):
    db_file = tmp_path / "legacy.db"
    monkeypatch.setenv("WAREHOUSE_DB", str(db_file))

    raw = sqlite3.connect(db_file)
    raw.executescript(
        """
        CREATE TABLE vehicle_models (
            model_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE model_parts (
            model_id TEXT NOT NULL, part_no TEXT NOT NULL,
            PRIMARY KEY (model_id, part_no)
        );
        CREATE TABLE model_conditions (
            model_id TEXT NOT NULL, condition_code TEXT NOT NULL,
            PRIMARY KEY (model_id, condition_code)
        );
        INSERT INTO vehicle_models(model_id) VALUES ('OLD');
        """
    )
    raw.executemany(
        "INSERT INTO model_parts VALUES ('OLD', ?)", [("P1",), ("P2",)]
    )
    raw.executemany(
        "INSERT INTO model_conditions VALUES ('OLD', ?)", [("A",), ("B",)]
    )
    raw.commit()
    raw.close()

    with TestClient(app) as client:
        # 启动迁移已扫描原表并回填规模
        conn = sqlite3.connect(db_path())
        cols = {r[1] for r in conn.execute("PRAGMA table_info(vehicle_models)")}
        assert {"part_count", "condition_count"} <= cols
        assert conn.execute(
            "SELECT part_count, condition_count FROM vehicle_models"
        ).fetchone() == (2, 2)
        conn.close()

        r = client.post(
            "/api/v1/models/correct",
            json={
                "modelId": "OLD",
                "plus": _pairs([("P1", "C"), ("P2", "C")]),
                "minus": [],
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["conditions"] == ["A", "B", "C"]
        conn = sqlite3.connect(db_path())
        assert conn.execute(
            "SELECT part_count, condition_count FROM vehicle_models"
        ).fetchone() == (2, 3)
        conn.close()


# --------------------------------------------------------------------------- #
# 7. 写入故障：触发器令备件插入失败 -> 500，两表与规模原样
# --------------------------------------------------------------------------- #
def test_write_failure_keeps_tables_and_scale(tmp_path, monkeypatch):
    db_file = tmp_path / "fault.db"
    monkeypatch.setenv("WAREHOUSE_DB", str(db_file))

    with TestClient(app) as seed:
        r = seed.post(
            "/api/v1/models/load",
            json={"modelId": "F", "pairs": _pairs([("P1", "A"), ("P1", "B")])},
        )
        assert r.status_code == 200

    raw = sqlite3.connect(db_file)
    raw.execute(
        """
        CREATE TRIGGER inject_fault BEFORE INSERT ON model_parts
        BEGIN SELECT RAISE(FAIL, 'injected write fault'); END
        """
    )
    raw.commit()
    raw.close()

    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.post(
            "/api/v1/models/correct",
            json={
                "modelId": "F",
                "plus": _pairs([("P9", "A"), ("P9", "B")]),
                "minus": [],
            },
        )
        assert r.status_code == 500

        body = client.get("/api/v1/models/F").json()
        assert body["parts"] == ["P1"]
        assert body["conditions"] == ["A", "B"]
        assert body["partCount"] == 1
        assert body["conditionCount"] == 2

    conn = sqlite3.connect(db_file)
    try:
        assert conn.execute(
            "SELECT part_count, condition_count FROM vehicle_models WHERE model_id='F'"
        ).fetchone() == (1, 2)
        assert conn.execute(
            "SELECT COUNT(*) FROM model_parts WHERE model_id='F'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 7b. 未经 lifespan 直接调 service 遇到旧库也能自愈
# --------------------------------------------------------------------------- #
def test_direct_service_call_on_unmigrated_old_db(tmp_path, monkeypatch):
    db_file = tmp_path / "old_direct.db"
    monkeypatch.setenv("WAREHOUSE_DB", str(db_file))
    raw = sqlite3.connect(db_file)
    raw.executescript(
        """
        CREATE TABLE vehicle_models (
            model_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE model_parts (
            model_id TEXT NOT NULL, part_no TEXT NOT NULL,
            PRIMARY KEY (model_id, part_no)
        );
        CREATE TABLE model_conditions (
            model_id TEXT NOT NULL, condition_code TEXT NOT NULL,
            PRIMARY KEY (model_id, condition_code)
        );
        INSERT INTO vehicle_models(model_id) VALUES ('OLD');
        INSERT INTO model_parts VALUES ('OLD', 'P1');
        INSERT INTO model_conditions VALUES ('OLD', 'A');
        """
    )
    raw.commit()
    raw.close()

    from app.schemas import Pair

    result = service.apply_correction(
        "OLD", [Pair(partNo="P1", conditionCode="B")], []
    )
    assert result["conditions"] == ["A", "B"]
    assert result["before"]["conditionCount"] == 1
    conn = sqlite3.connect(db_file)
    try:
        assert conn.execute(
            "SELECT condition_count FROM vehicle_models WHERE model_id='OLD'"
        ).fetchone() == (2,)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 8. 随机批次暴力预言机（固定种子，可复现）
# --------------------------------------------------------------------------- #
def test_randomized_batches_match_set_oracle(client):
    rng = random.Random(20261126)
    for it in range(120):
        mid = f"R{it}"
        n_parts = rng.randint(1, 4)
        n_conds = rng.randint(1, 4)
        parts = [f"p{i}" for i in range(n_parts)]
        conds = [f"c{j}" for j in range(n_conds)]
        base = [(p, c) for p in parts for c in conds]
        assert client.post(
            "/api/v1/models/load",
            json={"modelId": mid, "pairs": _pairs(base)},
        ).status_code == 200

        universe = base + [
            (f"g{i}", c) for i in range(2) for c in conds + ["newc"]
        ] + [(p, "newc") for p in parts]
        plus = set(rng.sample(universe, rng.randint(0, 3)))
        minus = set(rng.sample([q for q in universe if q not in plus], rng.randint(0, 3)))
        if not plus and not minus:
            continue

        desired = (set(base) - minus) | plus
        target_parts = {p for p, _ in desired}
        target_conds = {c for _, c in desired}
        rectangular = bool(desired) and len(desired) == len(target_parts) * len(
            target_conds
        )

        r = client.post(
            "/api/v1/models/correct",
            json={
                "modelId": mid,
                "plus": _pairs(list(plus)),
                "minus": _pairs(list(minus)),
            },
        )

        if rectangular:
            assert r.status_code == 200, (it, plus, minus, r.text)
            body = r.json()
            assert set(body["parts"]) == target_parts
            assert set(body["conditions"]) == target_conds
            assert body["before"] == {
                "partCount": n_parts,
                "conditionCount": n_conds,
                "effectivePairCount": n_parts * n_conds,
            }
        else:
            assert r.status_code == 422, (it, r.status_code, r.text)
            detail = r.json()["detail"]
            if desired:
                w = detail["extraCombinationExample"]
                wt = (w["partNo"], w["conditionCode"])
                assert wt[0] in target_parts
                assert wt[1] in target_conds
                assert wt not in desired
            unchanged = client.get(f"/api/v1/models/{mid}").json()
            assert set(unchanged["parts"]) == set(parts)
            assert set(unchanged["conditions"]) == set(conds)
