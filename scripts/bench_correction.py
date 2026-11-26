"""小批更正写放大 / 耗时实测脚本（不依赖 pytest）。

在当前工作树构造 N 备件 × 4 工况的大矩形，测量"新增 1 个备件（4 条 plus）"
与"拒收单个配对"，并通过 rowid 比对给出被物理重写的在册行数。

用法：
    pip install -r requirements.txt
    python3 scripts/bench_correction.py [N]

对比基线（HEAD）：
    git worktree add /tmp/baseline HEAD
    cd /tmp/baseline && python3 /workspace/scripts/bench_correction.py 20000
    # 脚本优先导入当前工作目录下的 app 包
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time

# 让"当前工作目录的 app 包"优先（对比基线时 cd 到基线 worktree 即可）。
sys.path.insert(0, os.getcwd())

os.environ.setdefault("WAREHOUSE_DB", tempfile.mktemp(suffix=".db"))

from fastapi.testclient import TestClient  # noqa: E402

from app import service  # noqa: E402
from app.database import db_path  # noqa: E402
from app.main import app  # noqa: E402
from app.schemas import Pair  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
CONDS = ["C1", "C2", "C3", "C4"]


def _pairs(spec):
    return [{"partNo": p, "conditionCode": c} for p, c in spec]


def _rowids(table: str):
    key = "part_no" if table == "model_parts" else "condition_code"
    conn = sqlite3.connect(db_path())
    try:
        return dict(
            conn.execute(
                f"SELECT {key}, rowid FROM {table} WHERE model_id='BIG'"
            ).fetchall()
        )
    finally:
        conn.close()


def main() -> None:
    parts = [f"P{i:05d}" for i in range(N)]
    with TestClient(app) as client:
        t0 = time.perf_counter()
        resp = client.post(
            "/api/v1/models/load",
            json={
                "modelId": "BIG",
                "pairs": _pairs([(p, c) for p in parts for c in CONDS]),
            },
        )
        load_ms = (time.perf_counter() - t0) * 1000
        assert resp.status_code == 200, resp.text

        before_parts = _rowids("model_parts")
        before_conds = _rowids("model_conditions")

        t0 = time.perf_counter()
        resp = client.post(
            "/api/v1/models/correct",
            json={
                "modelId": "BIG",
                "plus": _pairs([("PNEW", c) for c in CONDS]),
                "minus": [],
            },
        )
        e2e_ms = (time.perf_counter() - t0) * 1000
        assert resp.status_code == 200, resp.text

        after_parts = _rowids("model_parts")
        after_conds = _rowids("model_conditions")
        moved_parts = sum(
            1 for k, rid in before_parts.items() if after_parts.get(k) != rid
        )
        moved_conds = sum(
            1 for k, rid in before_conds.items() if after_conds.get(k) != rid
        )

        t0 = time.perf_counter()
        bad = client.post(
            "/api/v1/models/correct",
            json={"modelId": "BIG", "plus": [], "minus": _pairs([("P00007", "C2")])},
        )
        reject_ms = (time.perf_counter() - t0) * 1000
        assert bad.status_code == 422

    # 隔离"核定 + 写入"（回执整表读取打桩），连续 5 个新备件。
    service.get_model = lambda model_id: {}  # type: ignore[assignment]
    write_ms = []
    for k in range(5):
        plus = [Pair(partNo=f"BENCH{k}", conditionCode=c) for c in CONDS]
        t0 = time.perf_counter()
        service.apply_correction("BIG", plus, [])
        write_ms.append((time.perf_counter() - t0) * 1000)

    root = os.getcwd()
    print(f"app tree      : {root}")
    print(f"model scale   : {N} parts x {len(CONDS)} conds = {N * len(CONDS)} pairs")
    print(f"load          : {load_ms:.1f} ms")
    print(
        f"add-one-part  : e2e {e2e_ms:.1f} ms | "
        f"existing part rows rewritten {moved_parts}/{N} | "
        f"cond rows rewritten {moved_conds}/{len(CONDS)}"
    )
    print(f"reject-one    : e2e {reject_ms:.1f} ms")
    print(
        "check+write x5: "
        + " ".join(f"{x:.1f}ms" for x in write_ms)
        + f"  (median {sorted(write_ms)[2]:.1f}ms)"
    )


if __name__ == "__main__":
    main()
