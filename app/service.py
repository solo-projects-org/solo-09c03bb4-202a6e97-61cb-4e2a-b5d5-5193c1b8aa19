"""业务逻辑：装载、批量更正、取数。

核心约束
========
拆库后只持久化备件表 ``model_parts`` 与工况表 ``model_conditions``，
不保存任何逐对配对明细；两表按车型的笛卡尔积即为"拼回"的配对集合。

因此可入库的配对集合必须是一个"完整矩形"：

    配对数 = 备件种数 × 工况种数

核定（rectangularity check）全程不展开在库配对：

* 装载时在批次 TEMP 表上做一次三 COUNT 比较，不产生配对行；
* 更正时**从不 JOIN 两张持久表**，也**不扫描与本批无关的在库成员**：
  只读 ``vehicle_models`` 一行规模（part_count × condition_count，
  与两表在同一事务内维护），加上本批涉及的备件/工况在两张维表中的
  成员关系（行数 ≤ 本批去重成员数），所有增量推导都在批次大小的
  集合上完成。复杂度与两表拼回的配对总数、与在库成员总数均无关。

写入同样是增量的：核定通过后只 INSERT/DELETE "在册状态发生变化"
的维表行，并在同一事务内更新规模行；不再整表 DELETE + 重灌。

仅当核定不通过、需要给出反例时，才执行一条 ``LIMIT 1`` 的取证查询
（最多另读一张维表 / 一个未涉及备件名），不遍历两表全部配对。
"""

from __future__ import annotations

import sqlite3
import threading
from typing import TYPE_CHECKING, Optional

from fastapi import HTTPException

from .database import _migrate_scale_columns, get_connection

if TYPE_CHECKING:
    from .schemas import Pair

# SQLite 单文件多连接写入需串行化，所有写事务走同一把进程内锁。
_write_lock = threading.RLock()


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def _normalize_pairs(pairs: list["Pair"]) -> list[tuple[str, str]]:
    """去空白、校验非空、按 (备件号, 工况) 去重，重复配对只算一条。"""
    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []
    for item in pairs:
        part = item.part_no.strip()
        cond = item.condition_code.strip()
        if not part or not cond:
            raise HTTPException(
                status_code=422,
                detail="备件号与工况均不能为空字符串",
            )
        key = (part, cond)
        if key not in seen:
            seen.add(key)
            ordered.append(key)
    return ordered


def _create_stage(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(
        f"""
        CREATE TEMP TABLE {name} (
            part_no        TEXT NOT NULL,
            condition_code TEXT NOT NULL,
            PRIMARY KEY (part_no, condition_code)
        ) WITHOUT ROWID
        """
    )


def _insert_stage(conn: sqlite3.Connection, name: str, pairs: list[tuple[str, str]]) -> None:
    conn.executemany(
        f"INSERT INTO {name}(part_no, condition_code) VALUES (?, ?)",
        pairs,
    )


def _rectangularity_witness(
    conn: sqlite3.Connection, stage: str
) -> Optional[tuple[str, str]]:
    """核定 stage 中的配对集合能否由两张表原样拼回。

    第一步（核定判定）：纯 COUNT 聚合。配对集合是完整矩形
    当且仅当 ``配对数 = 备件数 × 工况数``，过程中不产生任何配对行。

    第二步（仅失败时取证）：找第一个"条件数 < 工况总数"的备件，
    再在该备件的候选工况里用 ``LIMIT 1`` 找一个它缺失的工况，
    作为"两表会拼出、但拟入库数据中不存在"的一对反例。
    取证范围限定在单个备件，不遍历两表全部配对。
    """
    row = conn.execute(
        f"""
        SELECT
            (SELECT COUNT(*) FROM {stage}) AS pair_cnt,
            (SELECT COUNT(DISTINCT part_no) FROM {stage}) AS part_cnt,
            (SELECT COUNT(DISTINCT condition_code) FROM {stage}) AS cond_cnt
        """
    ).fetchone()
    if row["pair_cnt"] == row["part_cnt"] * row["cond_cnt"]:
        return None

    witness = conn.execute(
        f"""
        WITH
        part_counts AS (
            SELECT part_no, COUNT(DISTINCT condition_code) AS cnt
            FROM {stage}
            GROUP BY part_no
        ),
        total_conds AS (
            SELECT COUNT(DISTINCT condition_code) AS total FROM {stage}
        ),
        bad_part AS (
            SELECT pc.part_no
            FROM part_counts pc CROSS JOIN total_conds tc
            WHERE pc.cnt < tc.total
            LIMIT 1
        )
        SELECT b.part_no AS part_no, c.condition_code AS condition_code
        FROM bad_part b
        JOIN (SELECT DISTINCT condition_code FROM {stage}) c
        WHERE NOT EXISTS (
            SELECT 1 FROM {stage} s
            WHERE s.part_no = b.part_no
              AND s.condition_code = c.condition_code
        )
        LIMIT 1
        """
    ).fetchone()
    return witness["part_no"], witness["condition_code"]


# --------------------------------------------------------------------------- #
# 1. 车型装载（初次入库）
# --------------------------------------------------------------------------- #
def load_model(model_id: str, pairs: list["Pair"]) -> dict:
    model_id = model_id.strip()
    if not model_id:
        raise HTTPException(status_code=422, detail="model_id 不能为空")
    normalized = _normalize_pairs(pairs)
    if not normalized:
        raise HTTPException(status_code=422, detail="配对列表不能为空")

    with _write_lock:
        conn = get_connection()
        try:
            exists = conn.execute(
                "SELECT 1 FROM vehicle_models WHERE model_id = ?", (model_id,)
            ).fetchone()
            if exists:
                raise HTTPException(
                    status_code=409,
                    detail=f"车型 {model_id} 已入库，更正请走批量更正接口",
                )

            # 拟入库配对先放 TEMP 表：事务结束自动销毁，不进入持久库。
            _create_stage(conn, "stage_pairs")
            _insert_stage(conn, "stage_pairs", normalized)

            witness = _rectangularity_witness(conn, "stage_pairs")
            if witness is not None:
                # 尚未写任何持久化表，原数据（本车型此前不存在）不受影响。
                raise HTTPException(
                    status_code=422,
                    detail={
                        "message": "配对集合不是完整矩形，拆成两表后会拼出拟入库数据中没有的组合",
                        "extraCombinationExample": {
                            "partNo": witness[0],
                            "conditionCode": witness[1],
                        },
                    },
                )

            part_set = {p for p, _ in normalized}
            cond_set = {c for _, c in normalized}
            conn.execute(
                """
                INSERT INTO vehicle_models(model_id, part_count, condition_count)
                VALUES (?, ?, ?)
                """,
                (model_id, len(part_set), len(cond_set)),
            )
            # normalized 按配对去重，备件号/工况在其中会重复出现，入库两表需再去重。
            conn.executemany(
                "INSERT INTO model_parts(model_id, part_no) VALUES (?, ?)",
                [(model_id, p) for p in part_set],
            )
            conn.executemany(
                "INSERT INTO model_conditions(model_id, condition_code) VALUES (?, ?)",
                [(model_id, c) for c in cond_set],
            )
            conn.commit()

            result = get_model(model_id)
            result["status"] = "loaded"
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# --------------------------------------------------------------------------- #
# 2. 已入库车型的批量更正
# --------------------------------------------------------------------------- #
def _make_one_col_temp(conn: sqlite3.Connection, name: str, col: str) -> None:
    conn.execute(
        f"CREATE TEMP TABLE {name} ({col} TEXT PRIMARY KEY) WITHOUT ROWID"
    )


def _plan_delta(
    *,
    before_parts: int,
    before_conds: int,
    registered_batch_parts: set[str],
    registered_batch_conds: set[str],
    plus: set[tuple[str, str]],
    minus: set[tuple[str, str]],
) -> dict:
    """纯函数：只凭"规模 + 本批成员关系 + 批次配对"推导更正方案。

    现行状态是完整矩形 P × C。设：

    * ``P_b`` / ``C_b`` —— 本批涉及且确属在库的备件/工况；
    * 一条 minus 仅当备件 ∈ P 且工况 ∈ C 时才真正删除一对
      （矩形下等价于配对原本存在；针对幽灵成员的 minus 是无效操作）；
    * 一条 plus 若配对原本已存在，则是**保留**（不得重复计数）。

    新工况集 ``C' = (C − gone) ∪ new_conds``，其中
    ``gone`` 是被全体在册备件删空、且未被 plus 重新带回的工况，
    ``new_conds`` 是 plus 引入的、原不在 C 中的工况。

    对每个在库备件 x，更正后工况数
    ``target = |C| − removed_x + |plus_x − C|``；
    恒有 ``S_x ⊆ C'``，故矩形核定只需 ``target == |C'|``
    （整备件删空 target=0 合法）。未出现在批次中的备件保有原 C，
    只有"plus 引入新工况"才会让它违规——用规模差即可判定，
    无需读出这些成员。
    """
    p = before_parts
    c = before_conds
    p_b = set(registered_batch_parts)
    c_b = set(registered_batch_conds)

    plus_by_part: dict[str, set[str]] = {}
    for part, cond in plus:
        plus_by_part.setdefault(part, set()).add(cond)
    minus_by_part: dict[str, set[str]] = {}
    for part, cond in minus:
        minus_by_part.setdefault(part, set()).add(cond)

    plus_conds = {cond for _, cond in plus}
    # plus 工况不在"本批涉及的在库工况"集合中，就必然是全新工况
    # （plus 的所有工况都属于本批工况）。
    new_conds = plus_conds - c_b

    # 每个在库工况被哪些"在册备件"的有效 minus 命中。
    removers: dict[str, set[str]] = {}
    for part, conds in minus_by_part.items():
        if part not in p_b:
            continue
        for cond in conds:
            if cond in c_b:
                removers.setdefault(cond, set()).add(part)

    removed_global = {cond for cond, xs in removers.items() if len(xs) == p}
    # 被全体删空、又没有被本批 plus（任意备件）带回的工况才真正注销。
    gone = removed_global - plus_conds
    c_new = c - len(gone) + len(new_conds)

    plan: dict = {
        "c_new": c_new,
        "gone": gone,
        "new_conds": new_conds,
        "delete_conds": sorted(gone),
        "insert_conds": sorted(new_conds),
        "delete_parts": [],
        "insert_parts": [],
        "targets": {},
        # 核定失败信息：
        #   empty=True                 工况将被清空
        #   bad_batch=(part, cond|None) 本批内在册备件违规
        #   untouched_bad=True         存在未涉及备件但规模对不上
        #   untouched_cond             未涉及备件缺失的工况
        #   bad_new=(part, cond|None)  批次新备件未覆盖全部工况
        "empty": False,
        "bad_batch": None,
        "untouched_bad": False,
        "untouched_cond": min(new_conds) if new_conds else None,
        "bad_new": None,
    }

    if c_new == 0:
        plan["empty"] = True
        return plan

    # ---- 本批涉及的在册备件 ----
    bad_batch: Optional[tuple[str, Optional[str]]] = None
    for part in sorted(p_b):
        removed = {cond for cond in minus_by_part.get(part, set()) if cond in c_b}
        added_new = plus_by_part.get(part, set()) - c_b
        target = c - len(removed) + len(added_new)
        plan["targets"][part] = target
        if target == 0:
            plan["delete_parts"].append(part)
            continue
        if target != c_new and bad_batch is None:
            # 该备件丢失、但在 C' 中仍存在的工况：
            #   ① 被该备件 minus、却未整体注销的工况；
            #   ② 别的备件 plus 进来的新工况、该备件没有 plus。
            missing = (removed - gone) | (new_conds - plus_by_part.get(part, set()))
            bad_batch = (part, min(missing) if missing else None)
    plan["bad_batch"] = bad_batch

    # ---- 未出现在批次中的在册备件：规模判定，不读其身份 ----
    # 存在未涉及备件  <=>  本批命中的在册备件数 < 在册备件总规模。
    if p > len(p_b) and c_new != c:
        plan["untouched_bad"] = True

    # ---- 批次引入的新备件（只出现在 minus 中的幽灵忽略）----
    bad_new: Optional[tuple[str, Optional[str]]] = None
    for part in sorted(set(plus_by_part) - p_b):
        held = plus_by_part[part]
        if len(held) == c_new:
            plan["insert_parts"].append(part)
        elif bad_new is None:
            missing_new = new_conds - held
            bad_new = (part, min(missing_new) if missing_new else None)
    plan["bad_new"] = bad_new

    plan["changed"] = bool(
        plan["delete_parts"]
        or plan["insert_parts"]
        or plan["delete_conds"]
        or plan["insert_conds"]
    )
    return plan


def _resolve_witness_cond(
    conn: sqlite3.Connection,
    model_id: str,
    part: str,
    gone: set[str],
    *,
    new_part: bool,
) -> str:
    """核定失败取证：在单个问题备件范围内 LIMIT 1 找一个缺失工况。

    最多另读 ``model_conditions`` 一张维表；``gone`` 是批次大小的
    已注销工况集合，放 TEMP 表排除，避免超长 IN 参数列表。
    """
    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS cond_gone "
        "(condition_code TEXT PRIMARY KEY) WITHOUT ROWID"
    )
    conn.executemany(
        "INSERT OR IGNORE INTO cond_gone(condition_code) VALUES (?)",
        [(cond,) for cond in sorted(gone)],
    )

    if new_part:
        # 新备件没有"原持有工况"：它缺少的在库工况 =
        # model_conditions 中存在、未整体注销、且该备件没有 plus 的工况。
        row = conn.execute(
            """
            SELECT mc.condition_code AS cc
            FROM model_conditions mc
            WHERE mc.model_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM batch_plus b
                  WHERE b.part_no = ? AND b.condition_code = mc.condition_code
              )
              AND NOT EXISTS (
                  SELECT 1 FROM cond_gone g
                  WHERE g.condition_code = mc.condition_code
              )
            ORDER BY mc.condition_code
            LIMIT 1
            """,
            (model_id, part),
        ).fetchone()
        if row is not None:
            return row["cc"]
    else:
        # 在册备件：找一条它 minus 掉、但未整体注销（故仍在 C' 中）
        # 的在库工况，即为多拼出的组合。
        row = conn.execute(
            """
            SELECT mc.condition_code AS cc
            FROM model_conditions mc
            WHERE mc.model_id = ?
              AND EXISTS (
                  SELECT 1 FROM batch_minus m
                  WHERE m.part_no = ? AND m.condition_code = mc.condition_code
              )
              AND NOT EXISTS (
                  SELECT 1 FROM cond_gone g
                  WHERE g.condition_code = mc.condition_code
              )
            ORDER BY mc.condition_code
            LIMIT 1
            """,
            (model_id, part),
        ).fetchone()
        if row is not None:
            return row["cc"]

    # 兜底：从批次工况里任取一个（理论上不会走到）。
    row = conn.execute("SELECT condition_code AS cc FROM batch_conds LIMIT 1").fetchone()
    if row is not None:
        return row["cc"]
    raise HTTPException(status_code=422, detail="整批更正后核定失败，且无法构造反例")


def apply_correction(
    model_id: str, plus_pairs: list["Pair"], minus_pairs: list["Pair"]
) -> dict:
    """整批更正并核定，通过后增量增删两张维表的变化行并更新规模。

    读范围（核定）：``vehicle_models`` 单行规模 + 本批涉及成员在两张
    维表中的行；不扫描其他在库成员、不 JOIN 两张持久表、不展开配对。
    写范围：仅在册状态变化的维表行 + 规模行，同一事务提交；
    核定拒绝或写库异常时整体回滚，继续使用原两表与原规模。
    """
    model_id = model_id.strip()
    plus_list = _normalize_pairs(plus_pairs)
    minus_list = _normalize_pairs(minus_pairs)
    if not plus_list and not minus_list:
        raise HTTPException(status_code=422, detail="更正批次不能为空")

    plus = set(plus_list)
    minus = set(minus_list)
    conflict = plus & minus
    if conflict:
        sample = next(iter(conflict))
        raise HTTPException(
            status_code=422,
            detail={
                "message": "同一配对不能在同一批次中同时标记为有和无",
                "conflictExample": {
                    "partNo": sample[0],
                    "conditionCode": sample[1],
                },
            },
        )

    with _write_lock:
        conn = get_connection()
        try:
            try:
                model = conn.execute(
                    """
                    SELECT model_id, part_count, condition_count
                    FROM vehicle_models WHERE model_id = ?
                    """,
                    (model_id,),
                ).fetchone()
            except sqlite3.OperationalError as exc:
                # 直接调用 service、没经过 lifespan init_db 时遇到旧库：
                # 就地补规模列并扫描一次原表回填。
                if "no such column" not in str(exc).lower():
                    raise
                _migrate_scale_columns(conn)
                conn.commit()
                model = conn.execute(
                    """
                    SELECT model_id, part_count, condition_count
                    FROM vehicle_models WHERE model_id = ?
                    """,
                    (model_id,),
                ).fetchone()
            if not model:
                raise HTTPException(status_code=404, detail=f"车型 {model_id} 不存在")

            before_parts = model["part_count"]
            before_conds = model["condition_count"]

            # 旧库接入兜底：规模列为 0（理论上在册车型不可能为 0）说明
            # 迁移回填未跑过，对此车型的原表扫描一次并持久化规模。
            if not before_parts or not before_conds:
                before_parts = conn.execute(
                    "SELECT COUNT(*) AS n FROM model_parts WHERE model_id = ?",
                    (model_id,),
                ).fetchone()["n"]
                before_conds = conn.execute(
                    "SELECT COUNT(*) AS n FROM model_conditions WHERE model_id = ?",
                    (model_id,),
                ).fetchone()["n"]
                conn.execute(
                    """
                    UPDATE vehicle_models
                       SET part_count = ?, condition_count = ?
                     WHERE model_id = ?
                    """,
                    (before_parts, before_conds, model_id),
                )
                conn.commit()

            # ---- 批次 TEMP 表（仅本连接内存活，不进入持久库）----
            _create_stage(conn, "batch_plus")
            _create_stage(conn, "batch_minus")
            _insert_stage(conn, "batch_plus", plus_list)
            _insert_stage(conn, "batch_minus", minus_list)

            batch_parts = {p for p, _ in plus | minus}
            batch_conds = {c for _, c in plus | minus}
            _make_one_col_temp(conn, "batch_parts", "part_no")
            _make_one_col_temp(conn, "batch_conds", "condition_code")
            conn.executemany(
                "INSERT INTO batch_parts(part_no) VALUES (?)",
                [(p,) for p in sorted(batch_parts)],
            )
            conn.executemany(
                "INSERT INTO batch_conds(condition_code) VALUES (?)",
                [(c,) for c in sorted(batch_conds)],
            )

            # ---- 只查本批涉及成员的在册情况（行数 ≤ 批次去重成员数）----
            registered_batch_parts = {
                r["part_no"]
                for r in conn.execute(
                    """
                    SELECT mp.part_no AS part_no
                    FROM model_parts mp
                    JOIN batch_parts bp ON bp.part_no = mp.part_no
                    WHERE mp.model_id = ?
                    """,
                    (model_id,),
                )
            }
            registered_batch_conds = {
                r["condition_code"]
                for r in conn.execute(
                    """
                    SELECT mc.condition_code AS condition_code
                    FROM model_conditions mc
                    JOIN batch_conds bc ON bc.condition_code = mc.condition_code
                    WHERE mc.model_id = ?
                    """,
                    (model_id,),
                )
            }

            plan = _plan_delta(
                before_parts=before_parts,
                before_conds=before_conds,
                registered_batch_parts=registered_batch_parts,
                registered_batch_conds=registered_batch_conds,
                plus=plus,
                minus=minus,
            )

            # ---- 核定失败：整批拒绝，不写任何维表行 ----
            if plan["empty"]:
                raise HTTPException(
                    status_code=422,
                    detail="整批更正后工况集合为空，拒绝清空车型；如需废弃车型请另行处理",
                )

            witness_part: Optional[str] = None
            witness_cond: Optional[str] = None

            bad_batch = plan["bad_batch"]
            if bad_batch is not None:
                witness_part, witness_cond = bad_batch

            # 未涉及备件违规时，取一个具体的"其他在库备件"做反例部件。
            untouched_part: Optional[str] = None
            if plan["untouched_bad"]:
                row = conn.execute(
                    """
                    SELECT mp.part_no AS part_no
                    FROM model_parts mp
                    WHERE mp.model_id = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM batch_parts bp
                          WHERE bp.part_no = mp.part_no
                      )
                    ORDER BY mp.part_no
                    LIMIT 1
                    """,
                    (model_id,),
                ).fetchone()
                untouched_part = row["part_no"] if row is not None else None
                # 与批次内首个坏备件按名取先，保持确定的取证顺序。
                if untouched_part is not None and (
                    witness_part is None or untouched_part < witness_part
                ):
                    witness_part = untouched_part
                    witness_cond = plan["untouched_cond"]

            if witness_part is not None:
                if witness_cond is None:
                    witness_cond = _resolve_witness_cond(
                        conn,
                        model_id,
                        witness_part,
                        plan["gone"],
                        new_part=False,
                    )
                raise HTTPException(
                    status_code=422,
                    detail={
                        "message": "按整批更正后的结果核定失败，两表会拼出更正后集合中不存在的组合，本批已整体拒绝",
                        "extraCombinationExample": {
                            "partNo": witness_part,
                            "conditionCode": witness_cond,
                        },
                    },
                )

            bad_new = plan["bad_new"]
            if bad_new is not None:
                new_part, new_cond = bad_new
                if new_cond is None:
                    new_cond = _resolve_witness_cond(
                        conn,
                        model_id,
                        new_part,
                        plan["gone"],
                        new_part=True,
                    )
                raise HTTPException(
                    status_code=422,
                    detail={
                        "message": "按整批更正后的结果核定失败，两表会拼出更正后集合中不存在的组合，本批已整体拒绝",
                        "extraCombinationExample": {
                            "partNo": new_part,
                            "conditionCode": new_cond,
                        },
                    },
                )

            # ---- 核定通过：只增删状态变化的维表行 ----
            if plan["delete_parts"]:
                conn.executemany(
                    "DELETE FROM model_parts WHERE model_id = ? AND part_no = ?",
                    [(model_id, part) for part in plan["delete_parts"]],
                )
            if plan["insert_parts"]:
                conn.executemany(
                    "INSERT INTO model_parts(model_id, part_no) VALUES (?, ?)",
                    [(model_id, part) for part in plan["insert_parts"]],
                )
            if plan["delete_conds"]:
                conn.executemany(
                    "DELETE FROM model_conditions "
                    "WHERE model_id = ? AND condition_code = ?",
                    [(model_id, cond) for cond in plan["delete_conds"]],
                )
            if plan["insert_conds"]:
                conn.executemany(
                    "INSERT INTO model_conditions(model_id, condition_code) "
                    "VALUES (?, ?)",
                    [(model_id, cond) for cond in plan["insert_conds"]],
                )

            after_parts = before_parts - len(plan["delete_parts"]) + len(
                plan["insert_parts"]
            )
            after_conds = plan["c_new"]
            # 两表规模与维表行在同一事务内生效；无实际状态变化时
            # （例如重复 plus 已有配对、幽灵 minus）不产生任何写操作。
            if plan["changed"]:
                conn.execute(
                    """
                    UPDATE vehicle_models
                       SET part_count = ?,
                           condition_count = ?,
                           updated_at = datetime('now')
                     WHERE model_id = ?
                    """,
                    (after_parts, after_conds, model_id),
                )
            conn.commit()

            after = get_model(model_id)
            after["status"] = "corrected"
            after["before"] = {
                "partCount": before_parts,
                "conditionCount": before_conds,
                "effectivePairCount": before_parts * before_conds,
            }
            return after
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# --------------------------------------------------------------------------- #
# 3. 取数：一次返回该车型同时生效的两张表
# --------------------------------------------------------------------------- #
def get_model(model_id: str) -> dict:
    model_id = model_id.strip()
    conn = get_connection()
    try:
        model = conn.execute(
            "SELECT model_id, created_at, updated_at FROM vehicle_models WHERE model_id = ?",
            (model_id,),
        ).fetchone()
        if not model:
            raise HTTPException(status_code=404, detail=f"车型 {model_id} 不存在")

        parts = [
            r["part_no"]
            for r in conn.execute(
                "SELECT part_no FROM model_parts WHERE model_id = ? ORDER BY part_no",
                (model_id,),
            )
        ]
        conds = [
            r["condition_code"]
            for r in conn.execute(
                "SELECT condition_code FROM model_conditions "
                "WHERE model_id = ? ORDER BY condition_code",
                (model_id,),
            )
        ]
        return {
            "model_id": model["model_id"],
            "status": "active",
            "created_at": model["created_at"],
            "updated_at": model["updated_at"],
            "part_count": len(parts),
            "condition_count": len(conds),
            "effective_pair_count": len(parts) * len(conds),
            "parts": parts,
            "conditions": conds,
        }
    finally:
        conn.close()


def list_models() -> dict:
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT m.model_id,
                   m.created_at,
                   m.updated_at,
                   (SELECT COUNT(*) FROM model_parts p WHERE p.model_id = m.model_id) AS part_count,
                   (SELECT COUNT(*) FROM model_conditions c WHERE c.model_id = m.model_id) AS cond_count
            FROM vehicle_models m
            ORDER BY m.model_id
            """
        ).fetchall()
        return {
            "models": [
                {
                    "model_id": r["model_id"],
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                    "part_count": r["part_count"],
                    "condition_count": r["cond_count"],
                    "effective_pair_count": r["part_count"] * r["cond_count"],
                }
                for r in rows
            ]
        }
    finally:
        conn.close()
