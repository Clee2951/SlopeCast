"""Access tests through the real HTTP server: public, resident, committee and operator
can only see and change what they should."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from backend.app import App, serve
from backend.pipeline import Pipeline
from tests.conftest import calm_features, features


@pytest.fixture
def server(settings, db, clock):
    p = Pipeline(settings, db, clock)
    f = calm_features(clock.now())
    f["N01"] = features("N01", wet=0.9, tilt=5, rate=30, now=clock.now())
    p.features = f
    p.tick(force=True)
    app = App(settings, db, clock, pipeline=p)
    httpd = serve(app, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", p
    httpd.shutdown()


def call(base, path, token=None, body=None):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def test_public_dashboard_has_no_personal_details(server):
    base, _ = server
    code, d = call(base, "/api/dashboard")
    assert code == 200 and d["rows"]
    text = json.dumps(d["rows"])
    for key in ('"assistance"', '"checklist"', '"name"', '"message"'):
        assert key not in text
    assert "wheelchair" not in text


def test_operator_sees_details(server):
    base, _ = server
    code, d = call(base, "/api/dashboard", "operator-demo")
    assert code == 200
    assert any(r["assistance"] for r in d["rows"])


def test_unknown_token_rejected(server):
    base, _ = server
    assert call(base, "/api/dashboard", "made-up")[0] == 403


def test_resident_sees_only_own_plan(server):
    base, _ = server
    code, d = call(base, "/api/me/plan", "resident-h02")
    assert code == 200 and d["household_id"] == "H02"
    assert call(base, "/api/committee/queue", "resident-h02")[0] == 403


def test_resident_cannot_update_another_household(server):
    base, _ = server
    code, _ = call(base, "/api/progress", "resident-h02", {"kind": "acknowledged", "household_id": "H03", "event_id": "x1"})
    assert code == 403
    code, d = call(base, "/api/progress", "resident-h02", {"kind": "acknowledged", "event_id": "x2"})
    assert code == 200 and d["recorded"] is True


def test_only_operator_approves(server):
    base, p = server
    plan = p.db.latest_plan()
    assert call(base, f"/api/plans/{plan['plan_id']}/approve", "committee-demo", {"version": plan["version"]})[0] == 403
    assert call(base, f"/api/plans/{plan['plan_id']}/approve", None, {"version": plan["version"]})[0] == 403
    assert call(base, f"/api/plans/{plan['plan_id']}/approve", "operator-demo", {"version": plan["version"]})[0] == 200


def test_stale_plan_version_rejected(server):
    base, p = server
    plan = p.db.latest_plan()
    code, d = call(base, f"/api/plans/{plan['plan_id']}/approve", "operator-demo", {"version": plan["version"] - 1})
    assert code == 409


def test_only_committee_records_team_arrival(server):
    base, _ = server
    assert call(base, "/api/progress", "resident-h02", {"kind": "team_arrived", "event_id": "y"})[0] == 403


def test_csv_export_needs_role(server):
    base, p = server
    pid = p.db.latest_plan()["plan_id"]
    req = urllib.request.Request(f"{base}/api/plans/{pid}/export.csv", headers={"Authorization": "Bearer committee-demo"})
    with urllib.request.urlopen(req) as r:
        body = r.read().decode()
    assert body.splitlines()[0].startswith("household,name,people")
    assert call(base, f"/api/plans/{pid}/export.csv")[0] == 403
