"""SQLite 连接与建表。

持久化只保留三张表：

* ``vehicle_models``  —— 已入库车型主表（含在册规模 part_count/condition_count）
* ``model_parts``     —— 拆出的备件表（车型 + 备件号）
* ``model_conditions`` —— 拆出的工况表（车型 + 工况）

按需求，逐对的"备件 × 工况"配对明细不落库；拼回关系由两张表的
笛卡尔积隐式表达。

规模列（part_count / condition_count）与两张维表在同一个写事务内
一起生效，使批量更正的核定只需读取"本批成员 + 车型规模"，不必
COUNT 全表。接入旧库（没有规模列的 vehicle_models）时，``init_db``
会对两张原维表做**一次**聚合扫描回填，之后不再全表扫描。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# 允许通过环境变量指定数据库文件；默认写到项目 data 目录，方便挂卷。
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "warehouse.db"


def db_path() -> str:
    return os.environ.get("WAREHOUSE_DB", str(DEFAULT_DB_PATH))


def get_connection() -> sqlite3.Connection:
    path = db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    # 外键 + WAL，多线程读取与单写事务更稳。
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS vehicle_models (
    model_id    TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    part_count       INTEGER NOT NULL DEFAULT 0,
    condition_count  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS model_parts (
    model_id    TEXT NOT NULL REFERENCES vehicle_models(model_id),
    part_no     TEXT NOT NULL,
    PRIMARY KEY (model_id, part_no)
);

CREATE TABLE IF NOT EXISTS model_conditions (
    model_id    TEXT NOT NULL REFERENCES vehicle_models(model_id),
    condition_code TEXT NOT NULL,
    PRIMARY KEY (model_id, condition_code)
);
"""

# 旧库迁移：规模列缺失时补列，并对原维表扫描一次回填。
_SCALE_COLUMNS = (
    ("part_count", "INTEGER NOT NULL DEFAULT 0"),
    ("condition_count", "INTEGER NOT NULL DEFAULT 0"),
)


def _migrate_scale_columns(conn: sqlite3.Connection) -> bool:
    """补齐规模列；若本次确有新增列，对原维表做一次聚合扫描回填。

    返回是否发生了旧库接入迁移（新增了至少一个规模列）。
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(vehicle_models)")}
    added = False
    for name, ddl in _SCALE_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE vehicle_models ADD COLUMN {name} {ddl}")
            added = True

    if added:
        # 接入旧库时允许的唯一一次原表扫描：按车型聚合后回填规模。
        conn.execute(
            """
            UPDATE vehicle_models
               SET part_count = (
                       SELECT COUNT(*) FROM model_parts p
                        WHERE p.model_id = vehicle_models.model_id
                   ),
                   condition_count = (
                       SELECT COUNT(*) FROM model_conditions c
                        WHERE c.model_id = vehicle_models.model_id
                   )
            """
        )
    return added


def init_db() -> None:
    conn = get_connection()
    try:
        conn.executescript(SCHEMA_SQL)
        # 旧库（基线版本建的三张表）就地补规模列并扫描一次回填。
        _migrate_scale_columns(conn)
        conn.commit()
    finally:
        conn.close()
