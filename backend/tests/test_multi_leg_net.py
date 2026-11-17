"""multi_leg_net：多腿冲正捆扎「全成或全进未平」端到端测试。"""
from pathlib import Path
import os
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    data_dir = ROOT / "backend" / "data_test_bundle"
    if data_dir.exists():
        for f in data_dir.glob("*.db"):
            f.unlink()
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["FIXTURES_DIR"] = str(ROOT / "fixtures")
    with TestClient(app) as c:
        yield c


def _balance(client, account_id):
    for a in client.get("/api/accounts").json():
        if a["id"] == account_id:
            return a["balance_cents"]
    raise AssertionError("账户不存在")


def _entry(client, entry_key):
    rows = client.get("/api/entries").json()
    return next(e for e in rows if e["entry_key"] == entry_key)


def test_health_and_empty_bundle_view(client):
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/reversal-bundles").json() == []


def test_declare_validation(client):
    # 捆扎键不能为空
    r = client.post("/api/reversal-bundles", json={"bundle_key": "", "entry_keys": ["REV-20261116-001"]})
    assert r.status_code == 400
    # 成员不存在
    r = client.post("/api/reversal-bundles", json={"bundle_key": "B-X", "entry_keys": ["NOPE"]})
    assert r.status_code == 400
    # 正向不可入捆
    r = client.post("/api/reversal-bundles", json={"bundle_key": "B-X", "entry_keys": ["FWD-20261116-001"]})
    assert r.status_code == 400
    # 重复成员
    r = client.post(
        "/api/reversal-bundles",
        json={"bundle_key": "B-X", "entry_keys": ["REV-20261116-001", "REV-20261116-001"]},
    )
    assert r.status_code == 400


def test_bundle_all_success_settles_together_and_changes_balance(client):
    # REV-001 全额冲 FWD-001(10000)，REV-002 部分冲 FWD-002(25000 中的 5000)：同捆两腿都可配平
    r = client.post(
        "/api/reversal-bundles",
        json={
            "bundle_key": "BUNDLE-OK",
            "entry_keys": ["REV-20261116-001", "REV-20261116-002"],
        },
    )
    assert r.status_code == 200, r.text
    bundle = r.json()
    assert bundle["status"] == "待归捆"
    assert bundle["member_count"] == 2
    assert [m["entry_key"] for m in bundle["members"]] == [
        "REV-20261116-001",
        "REV-20261116-002",
    ]

    bal_before = _balance(client, 1)
    run = client.post("/api/reversal-bundles/BUNDLE-OK/settle")
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["scope"] == "按捆"
    assert body["bundle_key"] == "BUNDLE-OK"
    assert body["bundle_success_count"] == 1
    assert body["bundle_fail_count"] == 0
    assert body["bundle_member_count"] == 2
    assert body["quarantine_count"] == 0
    # 报告按捆解释
    explain = body["捆扎解释"]
    assert len(explain) == 1 and explain[0]["status"] == "整捆已归并"

    # 成员全部已归并，账户 1 余额减少 15000
    assert _entry(client, "REV-20261116-001")["status"] == "已归并"
    assert _entry(client, "REV-20261116-002")["status"] == "已归并"
    assert _balance(client, 1) == bal_before - 15000

    # 捆状态落库可查
    b = client.get("/api/reversal-bundles/BUNDLE-OK").json()
    assert b["status"] == "整捆已归并"
    assert b["settle_run_id"] == body["id"]

    # 已处理捆不能再按捆归并
    again = client.post("/api/reversal-bundles/BUNDLE-OK/settle")
    assert again.status_code == 400


def test_bundle_one_bad_leg_quarantines_whole_bundle_without_balance_change(client):
    # REV-003 可配平（冲 FWD-003 全额）；REV-004 无原单。同捆 => 整捆隔离
    r = client.post(
        "/api/reversal-bundles",
        json={
            "bundle_key": "BUNDLE-BAD",
            "entry_keys": ["REV-20261116-003", "REV-20261116-004"],
        },
    )
    assert r.status_code == 200, r.text

    bal_acc2_before = _balance(client, 2)
    run = client.post("/api/reversal-bundles/BUNDLE-BAD/settle")
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["bundle_success_count"] == 0
    assert body["bundle_fail_count"] == 1
    assert body["bundle_member_count"] == 2
    assert body["quarantine_count"] == 2
    assert body["settled_count"] == 0

    # 没有任何正式余额被改（即使 REV-003 本可独立配平）
    assert _balance(client, 2) == bal_acc2_before

    # 两腿全部已隔离，且共享同一捆扎键可查
    assert _entry(client, "REV-20261116-003")["status"] == "已隔离"
    assert _entry(client, "REV-20261116-004")["status"] == "已隔离"
    q = client.get("/api/quarantine", params={"bundle_key": "BUNDLE-BAD"}).json()
    assert len(q) == 2
    assert {x["entry_key"] for x in q} == {"REV-20261116-003", "REV-20261116-004"}
    assert all(x["bundle_key"] == "BUNDLE-BAD" for x in q)
    # 好腿的隔离原因须标明连带
    good = next(x for x in q if x["entry_key"] == "REV-20261116-003")
    assert "连带隔离" in good["reason"]

    b = client.get("/api/reversal-bundles/BUNDLE-BAD").json()
    assert b["status"] == "整捆已隔离"
    assert "冲正无原单" in b["fail_reason"]


def test_bundle_excess_leg_fails_closed(client):
    # 造一笔超额冲正入捆：FWD-005 仅 8000，冲正 9000 => 超额，整捆不得改余额
    from app.db import connect
    from app.modules import multi_leg_net as mln
    with connect() as conn:
        mln.ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO ledger_entries(entry_key, account_id, kind, amount_cents, biz_ref,
              target_entry_key, booked_at, status)
            VALUES ('REV-EXTRA', 1, '冲正', 9000, 'ORD-9003', 'FWD-20261116-005',
              '2026-11-16T18:00:00Z', '待归并')
            """
        )
        conn.commit()

    r = client.post(
        "/api/reversal-bundles",
        json={"bundle_key": "BUNDLE-EXCESS", "entry_keys": ["REV-EXTRA"]},
    )
    assert r.status_code == 200, r.text
    bal_before = _balance(client, 1)
    run = client.post("/api/reversal-bundles/BUNDLE-EXCESS/settle").json()
    assert run["bundle_fail_count"] == 1
    assert run["quarantine_count"] == 1
    assert _balance(client, 1) == bal_before
    q = client.get("/api/quarantine", params={"bundle_key": "BUNDLE-EXCESS"}).json()
    assert q[0]["reason"] == "冲正超额"


def test_bundle_members_isolated_from_loose_run(client):
    # 再声明一捆但不按捆归并；跑全量归并时成员必须随捆处理而非散笔
    from app.db import connect
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO ledger_entries(entry_key, account_id, kind, amount_cents, biz_ref,
              target_entry_key, booked_at, status)
            VALUES ('REV-LOOSE-A', 1, '冲正', 1000, 'ORD-9003', 'FWD-20261116-005',
              '2026-11-16T19:00:00Z', '待归并')
            """
        )
        conn.commit()
    r = client.post(
        "/api/reversal-bundles",
        json={"bundle_key": "BUNDLE-MIX", "entry_keys": ["REV-LOOSE-A"]},
    )
    assert r.status_code == 200, r.text

    run = client.post("/api/settle-runs").json()
    # 待归捆在全量轮中整捆成功
    assert run["bundle_success_count"] == 1
    assert run["bundle_fail_count"] == 0
    assert run["bundle_member_count"] == 1
    assert _entry(client, "REV-LOOSE-A")["status"] == "已归并"
    b = client.get("/api/reversal-bundles/BUNDLE-MIX").json()
    assert b["status"] == "整捆已归并"


def test_verify_report_explains_bundles_and_stays_consistent(client):
    report = client.post("/api/verify-reports").json()
    assert report["consistent"] == 1
    assert report["ledger_net_cents"] == report["balance_sum_cents"]
    d = report["detail"]
    assert d["累计成功捆数"] == 2
    assert d["累计失败捆数"] == 2
    assert d["待归捆数"] == 0
    assert d["本轮成功捆数"] + d["本轮失败捆数"] >= 1


def test_quarantine_filter_unknown_key_returns_empty(client):
    assert client.get("/api/quarantine", params={"bundle_key": "NOT-EXIST"}).json() == []


def test_mid_transaction_failure_rolls_back_whole_bundle(client, monkeypatch):
    # 故障注入：成员状态已写、提交前抛错 => 单事务整体回滚，无半捆改余额
    from app.db import connect
    from app.services import settle as settle_svc
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO ledger_entries(entry_key, account_id, kind, amount_cents, biz_ref,
              target_entry_key, booked_at, status)
            VALUES ('REV-FAIL-A', 1, '冲正', 2000, 'ORD-9002', 'FWD-20261116-002',
              '2026-11-16T20:00:00Z', '待归并')
            """
        )
        conn.execute(
            """
            INSERT INTO ledger_entries(entry_key, account_id, kind, amount_cents, biz_ref,
              target_entry_key, booked_at, status)
            VALUES ('REV-FAIL-B', 1, '冲正', 3000, 'ORD-9002', 'FWD-20261116-002',
              '2026-11-16T20:05:00Z', '待归并')
            """
        )
        conn.commit()
    r = client.post(
        "/api/reversal-bundles",
        json={"bundle_key": "BUNDLE-FAIL", "entry_keys": ["REV-FAIL-A", "REV-FAIL-B"]},
    )
    assert r.status_code == 200, r.text

    runs_before = len(client.get("/api/settle-runs").json())
    bal_before = _balance(client, 1)

    def boom(*args, **kwargs):
        raise RuntimeError("模拟提交前故障")

    monkeypatch.setattr(settle_svc.mln, "mark_bundle_result", boom)
    with pytest.raises(RuntimeError, match="模拟提交前故障"):
        client.post("/api/reversal-bundles/BUNDLE-FAIL/settle")

    # 一切回到归并前：捆仍待归捆、成员仍待归并、余额未动、无隔离、无残留运行
    assert client.get("/api/reversal-bundles/BUNDLE-FAIL").json()["status"] == "待归捆"
    assert _entry(client, "REV-FAIL-A")["status"] == "待归并"
    assert _entry(client, "REV-FAIL-B")["status"] == "待归并"
    assert _balance(client, 1) == bal_before
    assert client.get("/api/quarantine", params={"bundle_key": "BUNDLE-FAIL"}).json() == []
    assert len(client.get("/api/settle-runs").json()) == runs_before
