"""Backend API tests for Italian Cashflow app."""
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://expense-analyzer-93.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"
MONTH = "2026-02"


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    yield s
    # cleanup: best-effort delete TEST_ fixed expenses
    try:
        r = s.get(f"{API}/fixed-expenses")
        for it in r.json():
            if it.get("name", "").startswith("TEST_"):
                s.delete(f"{API}/fixed-expenses/{it['id']}")
        r = s.get(f"{API}/extra-expenses", params={"month": MONTH})
        for it in r.json():
            if it.get("name", "").startswith("TEST_"):
                s.delete(f"{API}/extra-expenses/{it['id']}")
    except Exception:
        pass


# ---------- Salary ----------
def test_salary_put_and_get(session):
    r = session.put(f"{API}/salary/{MONTH}", json={"amount": 2500})
    assert r.status_code == 200, r.text
    assert r.json()["amount"] == 2500
    assert r.json()["month"] == MONTH

    r2 = session.get(f"{API}/salary/{MONTH}")
    assert r2.status_code == 200
    assert r2.json()["amount"] == 2500

    # upsert update
    r3 = session.put(f"{API}/salary/{MONTH}", json={"amount": 3000})
    assert r3.status_code == 200
    assert session.get(f"{API}/salary/{MONTH}").json()["amount"] == 3000


def test_salary_invalid_month(session):
    assert session.get(f"{API}/salary/2026-13").status_code == 400
    assert session.get(f"{API}/salary/abc").status_code == 400


# ---------- Fixed Expenses ----------
def test_fixed_expense_crud(session):
    r = session.post(f"{API}/fixed-expenses", json={"name": "TEST_Affitto", "amount": 800})
    assert r.status_code == 200, r.text
    item = r.json()
    assert item["name"] == "TEST_Affitto"
    assert item["amount"] == 800
    assert "id" in item
    fid = item["id"]

    lst = session.get(f"{API}/fixed-expenses").json()
    assert any(x["id"] == fid for x in lst)

    d = session.delete(f"{API}/fixed-expenses/{fid}")
    assert d.status_code == 200

    d2 = session.delete(f"{API}/fixed-expenses/{fid}")
    assert d2.status_code == 404

    # unknown id
    assert session.delete(f"{API}/fixed-expenses/nonexistent-id").status_code == 404


# ---------- Extra Expenses ----------
def test_extra_expense_scoped_to_month(session):
    r = session.post(f"{API}/extra-expenses",
                     json={"name": "TEST_Cena", "amount": 45.5, "category": "cibo", "month": MONTH})
    assert r.status_code == 200, r.text
    eid = r.json()["id"]

    items = session.get(f"{API}/extra-expenses", params={"month": MONTH}).json()
    assert any(x["id"] == eid for x in items)

    other = session.get(f"{API}/extra-expenses", params={"month": "2026-03"}).json()
    assert not any(x["id"] == eid for x in other)

    assert session.delete(f"{API}/extra-expenses/{eid}").status_code == 200
    assert session.delete(f"{API}/extra-expenses/{eid}").status_code == 404


def test_extra_invalid_month_query(session):
    assert session.get(f"{API}/extra-expenses", params={"month": "2026-13"}).status_code == 400
    assert session.get(f"{API}/extra-expenses", params={"month": "abc"}).status_code == 400


# ---------- Summary ----------
def test_summary_empty_month(session):
    # use a clean future-month
    m = "2030-05"
    r = session.get(f"{API}/summary/{m}")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["salary"] == 0
    assert d["fixed_total"] == 0
    assert d["extra_total"] == 0
    assert d["balance"] == 0
    assert d["by_category"] == []


def test_summary_computes_balance_and_categories(session):
    m = "2029-07"
    session.put(f"{API}/salary/{m}", json={"amount": 1000})
    f = session.post(f"{API}/fixed-expenses", json={"name": "TEST_Fix1", "amount": 200}).json()
    e1 = session.post(f"{API}/extra-expenses",
                      json={"name": "TEST_E1", "amount": 50, "category": "cibo", "month": m}).json()
    e2 = session.post(f"{API}/extra-expenses",
                      json={"name": "TEST_E2", "amount": 30, "category": "cibo", "month": m}).json()
    e3 = session.post(f"{API}/extra-expenses",
                      json={"name": "TEST_E3", "amount": 20, "category": "trasporti", "month": m}).json()

    s = session.get(f"{API}/summary/{m}").json()
    assert s["salary"] == 1000
    assert s["fixed_total"] >= 200  # global fixed expenses might include leftovers
    assert s["extra_total"] == 100
    # balance = salary - fixed_total - extra_total
    assert abs(s["balance"] - (s["salary"] - s["fixed_total"] - s["extra_total"])) < 0.001
    cats = {c["category"]: c["total"] for c in s["by_category"]}
    assert cats.get("cibo") == 80
    assert cats.get("trasporti") == 20

    # cleanup
    for eid in [e1["id"], e2["id"], e3["id"]]:
        session.delete(f"{API}/extra-expenses/{eid}")
    session.delete(f"{API}/fixed-expenses/{f['id']}")


def test_summary_invalid_month(session):
    assert session.get(f"{API}/summary/2026-13").status_code == 400
    assert session.get(f"{API}/summary/abc").status_code == 400
