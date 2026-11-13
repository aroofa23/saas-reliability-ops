"""API, negative, integration and regression tests (30 cases).

Run inside Docker:   docker compose run --rm tester
Run locally:         API_URL=http://localhost:8000 pytest -v tests/
"""
import os
import time
import uuid

import pytest
import requests

API = os.environ.get("API_URL", "http://localhost:8000")
ADMIN = "admin-key-123"
AGENT = "agent-key-123"
VIEWER = "viewer-key-123"
GLOBEX = "globex-agent-key"
DISABLED = "disabled-key-123"
SEARCH_BUDGET_S = float(os.environ.get("SEARCH_BUDGET_S", "0.3"))


def call(method, path, key=None, **kwargs):
    headers = kwargs.pop("headers", {})
    if key:
        headers["X-API-Key"] = key
    return requests.request(method, f"{API}{path}", headers=headers, timeout=15, **kwargs)


def new_ticket(**overrides):
    body = {"title": "Printer on fire", "customer_email": f"t-{uuid.uuid4().hex[:8]}@example.com"}
    body.update(overrides)
    return body


@pytest.fixture
def ticket():
    """A fresh ticket owned by Acme, deleted after the test."""
    resp = call("POST", "/tickets", AGENT, json=new_ticket())
    assert resp.status_code == 201, resp.text
    created = resp.json()
    yield created
    call("DELETE", f"/tickets/{created['id']}", ADMIN)


# ------------------------------------------------------------------ API (9)
def test_health_ok():
    resp = call("GET", "/health")
    assert resp.status_code == 200
    assert resp.json()["database"] == "up"


def test_metrics_exposed():
    resp = call("GET", "/metrics")
    assert resp.status_code == 200
    assert "http_requests_total" in resp.text


def test_viewer_can_list_tickets(ticket):
    resp = call("GET", "/tickets?limit=5", VIEWER)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_get_ticket(ticket):
    resp = call("GET", f"/tickets/{ticket['id']}", VIEWER)
    assert resp.status_code == 200
    assert resp.json()["title"] == "Printer on fire"


def test_agent_can_create_ticket():
    resp = call("POST", "/tickets", AGENT, json=new_ticket(priority="high"))
    assert resp.status_code == 201
    created = resp.json()
    assert created["status"] == "open" and created["priority"] == "high"
    call("DELETE", f"/tickets/{created['id']}", ADMIN)


def test_agent_can_update_status(ticket):
    resp = call("PATCH", f"/tickets/{ticket['id']}", AGENT, json={"status": "resolved"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "resolved"


def test_admin_can_delete_ticket():
    created = call("POST", "/tickets", AGENT, json=new_ticket()).json()
    assert call("DELETE", f"/tickets/{created['id']}", ADMIN).status_code == 204
    assert call("GET", f"/tickets/{created['id']}", VIEWER).status_code == 404


def test_search_returns_only_that_customer():
    email = f"search-{uuid.uuid4().hex[:8]}@example.com"
    ids = [call("POST", "/tickets", AGENT, json=new_ticket(customer_email=email)).json()["id"] for _ in range(2)]
    resp = call("GET", f"/tickets/search?customer_email={email}", VIEWER)
    assert resp.status_code == 200
    found = resp.json()
    assert len(found) == 2 and all(t["customer_email"] == email for t in found)
    for ticket_id in ids:
        call("DELETE", f"/tickets/{ticket_id}", ADMIN)


def test_tenant_isolation(ticket):
    # Globex must not see Acme's ticket, and must not learn that it exists.
    assert call("GET", f"/tickets/{ticket['id']}", GLOBEX).status_code == 404


# ------------------------------------------------------------- negative (13)
@pytest.mark.parametrize("key", [None, "wrong-key", ""])
def test_rejects_missing_or_bad_key(key):
    assert call("GET", "/tickets", key).status_code == 401


def test_viewer_cannot_create():
    assert call("POST", "/tickets", VIEWER, json=new_ticket()).status_code == 403


def test_agent_cannot_delete(ticket):
    assert call("DELETE", f"/tickets/{ticket['id']}", AGENT).status_code == 403


def test_agent_cannot_read_admin_report():
    assert call("GET", "/admin/integrity", AGENT).status_code == 403


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "ab", "customer_email": "a@example.com"},                       # title too short
        {"title": "Valid title", "customer_email": "not-an-email"},               # bad email
        {"title": "Valid title", "customer_email": "a@example.com", "priority": "asap"},  # bad priority
        {"customer_email": "a@example.com"},                                      # missing title
    ],
)
def test_rejects_invalid_ticket(payload):
    assert call("POST", "/tickets", AGENT, json=payload).status_code == 422


def test_rejects_invalid_status(ticket):
    assert call("PATCH", f"/tickets/{ticket['id']}", AGENT, json={"status": "done"}).status_code == 422


def test_missing_ticket_is_404():
    assert call("GET", "/tickets/999999999", VIEWER).status_code == 404


def test_limit_out_of_range():
    assert call("GET", "/tickets?limit=5000", VIEWER).status_code == 422


# ----------------------------------------------------------- integration (3)
def test_notify_succeeds(ticket):
    resp = call("POST", f"/tickets/{ticket['id']}/notify", AGENT)
    assert resp.status_code == 200, resp.text
    assert resp.json()["notified"] is True


def test_notify_missing_ticket_is_404():
    assert call("POST", "/tickets/999999999/notify", AGENT).status_code == 404


def test_notify_requires_agent(ticket):
    assert call("POST", f"/tickets/{ticket['id']}/notify", VIEWER).status_code == 403


# ------------------------------------------------------------ regression (5)
def test_duplicate_external_ref_rejected():
    """Scenario 5: the unique constraint must stay in place."""
    ref = f"REG-{uuid.uuid4().hex[:8]}"
    first = call("POST", "/tickets", AGENT, json=new_ticket(external_ref=ref))
    assert first.status_code == 201
    second = call("POST", "/tickets", AGENT, json=new_ticket(external_ref=ref))
    assert second.status_code == 409
    call("DELETE", f"/tickets/{first.json()['id']}", ADMIN)


def test_customer_search_within_budget():
    """Scenario 1: search must stay fast; fails if the index is dropped."""
    path = "/tickets/search?customer_email=customer4242@example.com"
    call("GET", path, VIEWER)  # warm-up
    start = time.perf_counter()
    resp = call("GET", path, VIEWER)
    elapsed = time.perf_counter() - start
    assert resp.status_code == 200
    assert elapsed < SEARCH_BUDGET_S, f"search took {elapsed:.3f}s (budget {SEARCH_BUDGET_S}s)"


def test_integrity_report_is_clean():
    """Scenario 5: no duplicates and no future-dated tickets."""
    resp = call("GET", "/admin/integrity", ADMIN)
    assert resp.status_code == 200
    assert resp.json()["healthy"] is True, resp.json()


def test_inactive_user_gets_401_not_500():
    """Scenario 2: a deactivated key is a clean 401, never a server error."""
    assert call("GET", "/tickets", DISABLED).status_code == 401


def test_health_reports_db_latency():
    body = call("GET", "/health").json()
    assert body["status"] == "ok" and isinstance(body["db_ms"], (int, float))
