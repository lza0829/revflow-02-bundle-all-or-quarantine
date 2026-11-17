from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import HTTPException

from app.db import connect, rows_to_dicts
from app.modules import multi_leg_net as mln


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------- 查询 ----------------

def dashboard() -> dict:
    with connect() as conn:
        mln.ensure_schema(conn)
        accounts = rows_to_dicts(conn.execute("SELECT * FROM accounts ORDER BY id").fetchall())
        fwd = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE kind = '正向'"
        ).fetchone()["c"]
        rev = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE kind = '冲正'"
        ).fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE status = '待归并'"
        ).fetchone()["c"]
        q = conn.execute("SELECT COUNT(*) AS c FROM quarantine_items").fetchone()["c"]
        bal = conn.execute("SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts").fetchone()["s"]
        bundle_pending = conn.execute(
            "SELECT COUNT(*) AS c FROM reversal_bundles WHERE status = '待归捆'"
        ).fetchone()["c"]
        last = conn.execute("SELECT * FROM settle_runs ORDER BY id DESC LIMIT 1").fetchone()
        return {
            "账户数": len(accounts),
            "正向笔数": fwd,
            "冲正笔数": rev,
            "待归并": pending,
            "待归捆": bundle_pending,
            "未平隔离": q,
            "余额合计分": bal,
            "最近归并": dict(last) if last else None,
            "账户": accounts,
        }


def list_accounts() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(conn.execute("SELECT * FROM accounts ORDER BY id").fetchall())


def list_entries(kind: str | None = None) -> list[dict]:
    with connect() as conn:
        mln.ensure_schema(conn)
        where = "WHERE e.kind = ?" if kind else ""
        params = (kind,) if kind else ()
        rows = conn.execute(
            f"""
            SELECT e.*, a.code AS account_code, a.name AS account_name,
                   b.bundle_key AS bundle_key, b.status AS bundle_status
            FROM ledger_entries e
            JOIN accounts a ON a.id = e.account_id
            LEFT JOIN reversal_bundle_members m ON m.entry_key = e.entry_key
            LEFT JOIN reversal_bundles b ON b.id = m.bundle_id
            {where}
            ORDER BY e.id
            """,
            params,
        ).fetchall()
        return rows_to_dicts(rows)


def list_quarantine(bundle_key: str | None = None) -> list[dict]:
    with connect() as conn:
        mln.ensure_schema(conn)
        if bundle_key:
            rows = conn.execute(
                """
                SELECT q.*, a.code AS account_code
                FROM quarantine_items q
                JOIN accounts a ON a.id = q.account_id
                WHERE q.bundle_key = ?
                ORDER BY q.id DESC
                """,
                (bundle_key,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT q.*, a.code AS account_code
                FROM quarantine_items q
                JOIN accounts a ON a.id = q.account_id
                ORDER BY q.id DESC
                """
            ).fetchall()
        return rows_to_dicts(rows)


def list_settle_runs() -> list[dict]:
    with connect() as conn:
        mln.ensure_schema(conn)
        return rows_to_dicts(
            conn.execute("SELECT * FROM settle_runs ORDER BY id DESC").fetchall()
        )


def list_verify_reports() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(
            conn.execute("SELECT * FROM verify_reports ORDER BY id DESC").fetchall()
        )


# ---------------- 捆扎声明 / 查询 ----------------

def declare_bundle(bundle_key: str, entry_keys: list[str]) -> dict:
    with connect() as conn:
        mln.ensure_schema(conn)
        bundle = mln.declare_bundle(conn, bundle_key, entry_keys)
        conn.commit()
        return bundle


def list_bundles() -> list[dict]:
    with connect() as conn:
        mln.ensure_schema(conn)
        return mln.list_bundles(conn)


def get_bundle(bundle_key: str) -> dict:
    with connect() as conn:
        mln.ensure_schema(conn)
        return mln.get_bundle(conn, bundle_key)


# ---------------- 归并引擎 ----------------

def _quarantine_one(
    conn,
    entry: dict,
    reason: str,
    run_id: int,
    now: str,
    bundle_key: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO quarantine_items(
          entry_key, account_id, reason, amount_cents, settle_run_id, created_at, bundle_key
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry["entry_key"],
            entry["account_id"],
            reason,
            entry["amount_cents"],
            run_id,
            now,
            bundle_key,
        ),
    )
    conn.execute(
        "UPDATE ledger_entries SET status = '已隔离', settle_run_id = ? WHERE id = ?",
        (run_id, entry["id"]),
    )


def _open_run(conn, now: str, scope: str, bundle_key: str | None) -> int:
    cur = conn.execute(
        """
        INSERT INTO settle_runs(status, started_at, scope, bundle_key)
        VALUES ('进行中', ?, ?, ?)
        """,
        (now, scope, bundle_key),
    )
    return cur.lastrowid


def _finish_run(conn, run_id: int, *, forward_count: int, reversal_count: int,
                settled: int, quarantined: int, bundle_success: int,
                bundle_fail: int, bundle_members: int, account_deltas: dict[int, int]) -> None:
    for acc_id, delta in account_deltas.items():
        if delta:
            conn.execute(
                "UPDATE accounts SET balance_cents = balance_cents + ?, last_settle_run_id = ? WHERE id = ?",
                (delta, run_id, acc_id),
            )
        else:
            conn.execute(
                "UPDATE accounts SET last_settle_run_id = ? WHERE id = ?",
                (run_id, acc_id),
            )
    # 本轮净额 = 本轮各账户增量合计；累计余额恒等以互证报告为准
    net_total = sum(account_deltas.values())
    conn.execute(
        """
        UPDATE settle_runs
        SET status = '完成',
            forward_count = ?,
            reversal_count = ?,
            settled_count = ?,
            quarantine_count = ?,
            net_cents = ?,
            bundle_success_count = ?,
            bundle_fail_count = ?,
            bundle_member_count = ?,
            finished_at = ?
        WHERE id = ?
        """,
        (
            forward_count,
            reversal_count,
            settled,
            quarantined,
            net_total,
            bundle_success,
            bundle_fail,
            bundle_members,
            _now(),
            run_id,
        ),
    )


def _load_capacity(conn) -> tuple[dict[str, dict], dict[str, int]]:
    """全量正向索引 + 每个原单扣除历史已归并冲正后的剩余额度。"""
    forwards = rows_to_dicts(
        conn.execute("SELECT * FROM ledger_entries WHERE kind = '正向' ORDER BY id").fetchall()
    )
    forwards_by_key = {f["entry_key"]: f for f in forwards}
    capacity = {f["entry_key"]: f["amount_cents"] for f in forwards}
    used_rows = conn.execute(
        """
        SELECT target_entry_key AS k, COALESCE(SUM(amount_cents), 0) AS s
        FROM ledger_entries
        WHERE kind = '冲正' AND status = '已归并' AND target_entry_key IS NOT NULL
        GROUP BY target_entry_key
        """
    ).fetchall()
    for r in used_rows:
        if r["k"] in capacity:
            capacity[r["k"]] -= r["s"]
    return forwards_by_key, capacity


def run_settle() -> dict:
    """全量净额归并：散笔逐笔成败；待归捆按捆「全成或全进未平」。

    成员状态、余额、隔离、捆状态全部在同一事务内提交，任一步抛错整体回滚，
    不会出现先成功几笔再隔离其余却保留已改余额的半捆。
    """
    with connect() as conn:
        mln.ensure_schema(conn)
        now = _now()
        run_id = _open_run(conn, now, "散笔", None)

        forwards = rows_to_dicts(
            conn.execute(
                "SELECT * FROM ledger_entries WHERE kind = '正向' AND status = '待归并' ORDER BY id"
            ).fetchall()
        )
        reversals = rows_to_dicts(
            conn.execute(
                "SELECT * FROM ledger_entries WHERE kind = '冲正' AND status = '待归并' ORDER BY id"
            ).fetchall()
        )
        forwards_by_key, capacity = _load_capacity(conn)

        # 已挂捆的冲正不按散笔处理
        bundled_keys = {
            r["entry_key"]
            for r in conn.execute("SELECT entry_key FROM reversal_bundle_members").fetchall()
        }

        reserved: dict[str, int] = {}
        account_deltas: dict[int, int] = {}
        settled = 0
        quarantined = 0
        bundle_success = 0
        bundle_fail = 0
        bundle_members = 0

        # 正向全部入账（全量轮）
        for f in forwards:
            account_deltas[f["account_id"]] = account_deltas.get(f["account_id"], 0) + f["amount_cents"]
            conn.execute(
                "UPDATE ledger_entries SET status = '已归并', settle_run_id = ? WHERE id = ?",
                (run_id, f["id"]),
            )
            settled += 1

        # 1) 散笔冲正：逐笔独立成败
        for r in reversals:
            if r["entry_key"] in bundled_keys:
                continue
            ok, reason = mln.evaluate_leg(r, forwards_by_key, capacity, reserved)
            if not ok:
                _quarantine_one(conn, r, reason, run_id, now)
                quarantined += 1
                continue
            reserved[r["target_entry_key"]] = reserved.get(r["target_entry_key"], 0) + r["amount_cents"]
            account_deltas[r["account_id"]] = account_deltas.get(r["account_id"], 0) - r["amount_cents"]
            conn.execute(
                "UPDATE ledger_entries SET status = '已归并', settle_run_id = ? WHERE id = ?",
                (run_id, r["id"]),
            )
            settled += 1

        # 2) 待归捆：整捆预演 → 全成整落，任一腿败则整捆隔离
        for item in mln.load_pending_bundles(conn):
            bundle = item["bundle"]
            legs = item["legs"]
            bundle_members += len(legs)
            if not legs:
                # 防御：待归捆却无待归并成员（正常流程不会出现），不允许空捆过账
                mln.mark_bundle_result(
                    conn, bundle["id"], mln.BUNDLE_QUARANTINED, run_id,
                    fail_reason="捆内无待归并成员，整捆拒绝过账",
                )
                bundle_fail += 1
                continue
            all_ok, reasons = mln.evaluate_bundle(legs, forwards_by_key, capacity, reserved)
            if all_ok:
                for leg in legs:
                    reserved[leg["target_entry_key"]] = (
                        reserved.get(leg["target_entry_key"], 0) + leg["amount_cents"]
                    )
                    account_deltas[leg["account_id"]] = (
                        account_deltas.get(leg["account_id"], 0) - leg["amount_cents"]
                    )
                    conn.execute(
                        "UPDATE ledger_entries SET status = '已归并', settle_run_id = ? WHERE id = ?",
                        (run_id, leg["id"]),
                    )
                mln.mark_bundle_result(conn, bundle["id"], mln.BUNDLE_SETTLED, run_id)
                bundle_success += 1
                continue

            first_reason = next((x for x in reasons if x), "整捆校验失败")
            for leg, leg_reason in zip(legs, reasons):
                reason = leg_reason or f"整捆失败连带隔离（{first_reason}）"
                _quarantine_one(conn, leg, reason, run_id, now, bundle["bundle_key"])
            mln.mark_bundle_result(
                conn, bundle["id"], mln.BUNDLE_QUARANTINED, run_id,
                fail_reason=f"{first_reason}（整捆失败，全部成员隔离）",
            )
            bundle_fail += 1

        _finish_run(
            conn, run_id,
            forward_count=len(forwards),
            reversal_count=len(reversals),
            settled=settled,
            quarantined=quarantined,
            bundle_success=bundle_success,
            bundle_fail=bundle_fail,
            bundle_members=bundle_members,
            account_deltas=account_deltas,
        )
        conn.commit()
        return get_settle_run(run_id)


def run_bundle_settle(bundle_key: str) -> dict:
    """按捆归并：仅处理指定捆，整捆全成改余额，任一腿败整捆进隔离。单事务。"""
    with connect() as conn:
        mln.ensure_schema(conn)
        bundle = mln.get_bundle(conn, bundle_key)
        if bundle["status"] != mln.BUNDLE_PENDING:
            raise HTTPException(400, f"捆扎不在待归捆状态：{bundle_key}（{bundle['status']}）")

        now = _now()
        run_id = _open_run(conn, now, "按捆", bundle_key)
        forwards_by_key, capacity = _load_capacity(conn)

        leg_rows = conn.execute(
            """
            SELECT e.*
            FROM reversal_bundle_members m
            JOIN ledger_entries e ON e.entry_key = m.entry_key
            WHERE m.bundle_id = ?
            ORDER BY m.seq
            """,
            (bundle["id"],),
        ).fetchall()
        legs = rows_to_dicts(leg_rows)
        if any(leg["status"] != "待归并" for leg in legs):
            raise HTTPException(400, "捆内存在非待归并冲正，整捆拒绝处理")

        account_deltas: dict[int, int] = {}
        all_ok, reasons = mln.evaluate_bundle(legs, forwards_by_key, capacity, {})

        if all_ok:
            for leg in legs:
                account_deltas[leg["account_id"]] = (
                    account_deltas.get(leg["account_id"], 0) - leg["amount_cents"]
                )
                conn.execute(
                    "UPDATE ledger_entries SET status = '已归并', settle_run_id = ? WHERE id = ?",
                    (run_id, leg["id"]),
                )
            mln.mark_bundle_result(conn, bundle["id"], mln.BUNDLE_SETTLED, run_id)
            settled = len(legs)
            quarantined = 0
            bundle_success = 1
            bundle_fail = 0
        else:
            first_reason = next((x for x in reasons if x), "整捆校验失败")
            for leg, leg_reason in zip(legs, reasons):
                reason = leg_reason or f"整捆失败连带隔离（{first_reason}）"
                _quarantine_one(conn, leg, reason, run_id, now, bundle_key)
            mln.mark_bundle_result(
                conn, bundle["id"], mln.BUNDLE_QUARANTINED, run_id,
                fail_reason=f"{first_reason}（整捆失败，全部成员隔离）",
            )
            settled = 0
            quarantined = len(legs)
            bundle_success = 0
            bundle_fail = 1

        _finish_run(
            conn, run_id,
            forward_count=0,
            reversal_count=len(legs),
            settled=settled,
            quarantined=quarantined,
            bundle_success=bundle_success,
            bundle_fail=bundle_fail,
            bundle_members=len(legs),
            account_deltas=account_deltas,
        )
        conn.commit()
        return get_settle_run(run_id)


def get_settle_run(run_id: int) -> dict:
    with connect() as conn:
        mln.ensure_schema(conn)
        row = conn.execute("SELECT * FROM settle_runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            raise HTTPException(404, "归并运行不存在")
        out = dict(row)
        # 按捆解释：与 reversal_bundles / 流水 / 隔离同源
        out["捆扎解释"] = rows_to_dicts(
            conn.execute(
                """
                SELECT id, bundle_key, status, member_count, settle_run_id,
                       fail_reason, settled_at
                FROM reversal_bundles
                WHERE settle_run_id = ?
                ORDER BY id
                """,
                (run_id,),
            ).fetchall()
        )
        return out


def run_verify(settle_run_id: int | None = None) -> dict:
    with connect() as conn:
        mln.ensure_schema(conn)
        if settle_run_id is None:
            last = conn.execute("SELECT id FROM settle_runs ORDER BY id DESC LIMIT 1").fetchone()
            if not last:
                raise HTTPException(400, "尚无归并运行，无法互证")
            settle_run_id = last["id"]
        run = conn.execute("SELECT * FROM settle_runs WHERE id = ?", (settle_run_id,)).fetchone()
        if not run:
            raise HTTPException(404, "归并运行不存在")

        # 已归并正向 − 已归并冲正
        fwd_sum = conn.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries
            WHERE kind = '正向' AND status = '已归并' AND settle_run_id = ?
            """,
            (settle_run_id,),
        ).fetchone()["s"]
        rev_sum = conn.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries
            WHERE kind = '冲正' AND status = '已归并' AND settle_run_id = ?
            """,
            (settle_run_id,),
        ).fetchone()["s"]
        ledger_net = fwd_sum - rev_sum
        balance_sum = conn.execute(
            "SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts WHERE last_settle_run_id = ?",
            (settle_run_id,),
        ).fetchone()["s"]
        q_count = conn.execute(
            "SELECT COUNT(*) AS c FROM quarantine_items WHERE settle_run_id = ?",
            (settle_run_id,),
        ).fetchone()["c"]

        # 全量已归并净额 vs 全量账户余额（增量余额下多轮恒等）
        all_fwd = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries WHERE kind = '正向' AND status = '已归并'"
        ).fetchone()["s"]
        all_rev = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries WHERE kind = '冲正' AND status = '已归并'"
        ).fetchone()["s"]
        all_ledger_net = all_fwd - all_rev
        all_bal = conn.execute(
            "SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts"
        ).fetchone()["s"]

        bundle_cols = [r["name"] for r in conn.execute("PRAGMA table_info(settle_runs)")]
        run_bundle_success = run["bundle_success_count"] if "bundle_success_count" in bundle_cols else 0
        run_bundle_fail = run["bundle_fail_count"] if "bundle_fail_count" in bundle_cols else 0
        run_bundle_members = run["bundle_member_count"] if "bundle_member_count" in bundle_cols else 0
        total_bundle_success = conn.execute(
            "SELECT COUNT(*) AS c FROM reversal_bundles WHERE status = ?",
            (mln.BUNDLE_SETTLED,),
        ).fetchone()["c"]
        total_bundle_fail = conn.execute(
            "SELECT COUNT(*) AS c FROM reversal_bundles WHERE status = ?",
            (mln.BUNDLE_QUARANTINED,),
        ).fetchone()["c"]
        bundle_pending = conn.execute(
            "SELECT COUNT(*) AS c FROM reversal_bundles WHERE status = ?",
            (mln.BUNDLE_PENDING,),
        ).fetchone()["c"]

        consistent = 1 if all_ledger_net == all_bal else 0
        detail = {
            "本轮正向分": fwd_sum,
            "本轮冲正分": rev_sum,
            "本轮流水净额分": ledger_net,
            "本轮触及余额合计分": balance_sum,
            "全量流水净额分": all_ledger_net,
            "全量账户余额分": all_bal,
            "本轮隔离数": q_count,
            "本轮成功捆数": run_bundle_success,
            "本轮失败捆数": run_bundle_fail,
            "本轮捆内成员笔数": run_bundle_members,
            "累计成功捆数": total_bundle_success,
            "累计失败捆数": total_bundle_fail,
            "待归捆数": bundle_pending,
        }
        now = _now()
        cur = conn.execute(
            """
            INSERT INTO verify_reports(
              settle_run_id, ledger_net_cents, balance_sum_cents,
              quarantine_count, consistent, detail_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                settle_run_id,
                all_ledger_net,
                all_bal,
                q_count,
                consistent,
                json.dumps(detail, ensure_ascii=False),
                now,
            ),
        )
        conn.commit()
        rid = cur.lastrowid
        row = conn.execute("SELECT * FROM verify_reports WHERE id = ?", (rid,)).fetchone()
        out = dict(row)
        out["detail"] = detail
        return out
