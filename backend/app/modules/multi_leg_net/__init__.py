"""多腿冲正净额捆扎（multi_leg_net）。

落库形态（二选一中的「独立捆扎表」）：
- ``reversal_bundles``：捆扎身份与捆状态（待归捆/整捆已归并/整捆已隔离）；
- ``reversal_bundle_members``：成员冲正键挂接，一笔冲正至多属于一捆。

捆扎集合与散笔在落库层即可区分，不依赖请求体数组。
隔离项（``quarantine_items.bundle_key``）与归并运行（``settle_runs.scope/bundle_key``）
同样冗余捆扎键，保证捆扎记录、流水、隔离、余额、报告同源可查。

失败闭由归并引擎以单事务保证（见 ``app.services.settle``）：同捆全部冲正
可解析原单、账户一致且各自不超额，整捆才允许进入「已归并」并改余额；
任一腿不满足则整捆进未平隔离、正式余额分毫不动。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from fastapi import HTTPException

# ---- 捆状态 ----
BUNDLE_PENDING = "待归捆"
BUNDLE_SETTLED = "整捆已归并"
BUNDLE_QUARANTINED = "整捆已隔离"

BUNDLE_STATUSES = (BUNDLE_PENDING, BUNDLE_SETTLED, BUNDLE_QUARANTINED)

BUNDLE_DDL = """
CREATE TABLE IF NOT EXISTS reversal_bundles (
  id INTEGER PRIMARY KEY,
  bundle_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT '待归捆',
  member_count INTEGER NOT NULL,
  settle_run_id INTEGER,
  fail_reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  settled_at TEXT
);
CREATE TABLE IF NOT EXISTS reversal_bundle_members (
  id INTEGER PRIMARY KEY,
  bundle_id INTEGER NOT NULL REFERENCES reversal_bundles(id),
  entry_key TEXT NOT NULL UNIQUE,
  seq INTEGER NOT NULL,
  UNIQUE(bundle_id, entry_key)
);
"""

# 既有表追加列（幂等迁移）
_ALTER_COLUMNS = {
    "quarantine_items": [
        ("bundle_key", "TEXT"),
    ],
    "settle_runs": [
        ("scope", "TEXT NOT NULL DEFAULT '散笔'"),
        ("bundle_key", "TEXT"),
        ("bundle_success_count", "INTEGER NOT NULL DEFAULT 0"),
        ("bundle_fail_count", "INTEGER NOT NULL DEFAULT 0"),
        ("bundle_member_count", "INTEGER NOT NULL DEFAULT 0"),
    ],
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """建捆扎表并给既有表补列；可重复执行。"""
    conn.executescript(BUNDLE_DDL)
    for table, columns in _ALTER_COLUMNS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


# ---------------- 捆扎声明 / 查询 ----------------

def declare_bundle(
    conn: sqlite3.Connection,
    bundle_key: str,
    entry_keys: list[str],
) -> dict:
    """为若干待归并冲正声明同一捆扎键；校验与落库在调用方事务内完成。"""
    bundle_key = (bundle_key or "").strip()
    if not bundle_key:
        raise HTTPException(400, "捆扎键不能为空")
    if not entry_keys:
        raise HTTPException(400, "捆扎成员至少一笔冲正")
    if len(set(entry_keys)) != len(entry_keys):
        raise HTTPException(400, "捆扎成员存在重复冲正键")

    exists = conn.execute(
        "SELECT 1 FROM reversal_bundles WHERE bundle_key = ?", (bundle_key,)
    ).fetchone()
    if exists:
        raise HTTPException(400, f"捆扎键已存在：{bundle_key}")

    # 成员资格：存在、冲正、待归并、未挂其它捆
    placeholders = ",".join("?" for _ in entry_keys)
    rows = conn.execute(
        f"""
        SELECT entry_key, kind, status FROM ledger_entries
        WHERE entry_key IN ({placeholders})
        """,
        tuple(entry_keys),
    ).fetchall()
    found = {r["entry_key"]: r for r in rows}
    for key in entry_keys:
        row = found.get(key)
        if row is None:
            raise HTTPException(400, f"冲正流水不存在：{key}")
        if row["kind"] != "冲正":
            raise HTTPException(400, f"仅冲正流水可入捆，正向流水不可：{key}")
        if row["status"] != "待归并":
            raise HTTPException(400, f"冲正非待归并状态，不能入捆：{key}（{row['status']}）")

    owned = conn.execute(
        f"""
        SELECT m.entry_key, b.bundle_key
        FROM reversal_bundle_members m
        JOIN reversal_bundles b ON b.id = m.bundle_id
        WHERE m.entry_key IN ({placeholders})
        """,
        tuple(entry_keys),
    ).fetchall()
    if owned:
        first = owned[0]
        raise HTTPException(400, f"冲正已属于捆扎 {first['bundle_key']}：{first['entry_key']}")

    now = _now()
    cur = conn.execute(
        """
        INSERT INTO reversal_bundles(bundle_key, status, member_count, created_at)
        VALUES (?, '待归捆', ?, ?)
        """,
        (bundle_key, len(entry_keys), now),
    )
    bundle_id = cur.lastrowid
    for seq, key in enumerate(entry_keys, start=1):
        conn.execute(
            """
            INSERT INTO reversal_bundle_members(bundle_id, entry_key, seq)
            VALUES (?, ?, ?)
            """,
            (bundle_id, key, seq),
        )
    return get_bundle(conn, bundle_key)


def _row_to_bundle(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    bundle = dict(row)
    bundle["members"] = [
        dict(r)
        for r in conn.execute(
            """
            SELECT m.seq, m.entry_key, e.account_id, e.amount_cents,
                   e.target_entry_key, e.status AS entry_status,
                   a.code AS account_code
            FROM reversal_bundle_members m
            JOIN ledger_entries e ON e.entry_key = m.entry_key
            JOIN accounts a ON a.id = e.account_id
            WHERE m.bundle_id = ?
            ORDER BY m.seq
            """,
            (bundle["id"],),
        ).fetchall()
    ]
    return bundle


def get_bundle(conn: sqlite3.Connection, bundle_key: str) -> dict:
    row = conn.execute(
        "SELECT * FROM reversal_bundles WHERE bundle_key = ?", (bundle_key,)
    ).fetchone()
    if not row:
        raise HTTPException(404, f"捆扎不存在：{bundle_key}")
    return _row_to_bundle(conn, row)


def list_bundles(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM reversal_bundles ORDER BY id"
    ).fetchall()
    return [_row_to_bundle(conn, r) for r in rows]


def load_pending_bundles(conn: sqlite3.Connection) -> list[dict]:
    """全部待归捆（含成员流水行），按捆 id 稳定排序。"""
    bundle_rows = conn.execute(
        "SELECT * FROM reversal_bundles WHERE status = '待归捆' ORDER BY id"
    ).fetchall()
    out: list[dict] = []
    for b in bundle_rows:
        legs = conn.execute(
            """
            SELECT e.*
            FROM reversal_bundle_members m
            JOIN ledger_entries e ON e.entry_key = m.entry_key
            WHERE m.bundle_id = ? AND e.status = '待归并'
            ORDER BY m.seq
            """,
            (b["id"],),
        ).fetchall()
        out.append({"bundle": dict(b), "legs": [dict(r) for r in legs]})
    return out


def mark_bundle_result(
    conn: sqlite3.Connection,
    bundle_id: int,
    status: str,
    settle_run_id: int,
    fail_reason: str = "",
) -> None:
    conn.execute(
        """
        UPDATE reversal_bundles
        SET status = ?, settle_run_id = ?, fail_reason = ?,
            settled_at = CASE WHEN ? = '待归捆' THEN NULL ELSE ? END
        WHERE id = ?
        """,
        (status, settle_run_id, fail_reason, status, _now(), bundle_id),
    )


# ---------------- 腿级配平校验（散笔 / 捆内共用） ----------------

def evaluate_leg(
    leg: dict,
    forwards_by_key: dict[str, dict],
    capacity: dict[str, int],
    reserved: dict[str, int],
) -> tuple[bool, str]:
    """单腿是否可解析原单、账户一致且不超额。

    ``capacity`` 为原单扣除历史已归并冲正后的剩余额度；
    ``reserved`` 为本轮（含同捆先序腿）已临时占用额度。
    """
    target = leg.get("target_entry_key")
    if not target or target not in forwards_by_key:
        return False, "冲正无原单"
    fwd = forwards_by_key[target]
    if fwd["account_id"] != leg["account_id"]:
        return False, "冲正账户与原单不一致"
    remain = capacity[target] - reserved.get(target, 0)
    if leg["amount_cents"] > remain:
        return False, "冲正超额"
    return True, ""


def evaluate_bundle(
    legs: list[dict],
    forwards_by_key: dict[str, dict],
    capacity: dict[str, int],
    base_reserved: dict[str, int],
) -> tuple[bool, list[str]]:
    """整捆预演：通过临时占用表判断是否全腿满足；不动任何正式数据。

    返回 (是否整捆通过, 各腿原因)。失败时原因列表逐腿可解释。
    """
    tentative = dict(base_reserved)
    reasons: list[str] = []
    all_ok = True
    for leg in legs:
        ok, reason = evaluate_leg(leg, forwards_by_key, capacity, tentative)
        if not ok:
            all_ok = False
            reasons.append(reason)
        else:
            reasons.append("")
            tentative[leg["target_entry_key"]] = (
                tentative.get(leg["target_entry_key"], 0) + leg["amount_cents"]
            )
    return all_ok, reasons
