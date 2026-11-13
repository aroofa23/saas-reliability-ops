"""Support Desk SaaS: a small multi-tenant ticketing API.

Built to be observed and broken on purpose:
  * every request is logged as one JSON line (docker compose logs api)
  * /metrics exposes Prometheus counters and latency histograms
  * role-based access control: viewer < agent < admin
"""
import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from typing import Optional

import psycopg2
import psycopg2.errors
import psycopg2.extras
import requests
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, Field

DB_DSN = os.environ.get("DATABASE_URL", "postgresql://api_user:apisecret@db:5432/saas")
NOTIFY_URL = os.environ.get("NOTIFY_URL", "http://mock-webhook:9000/notify")
NOTIFY_TIMEOUT = float(os.environ.get("NOTIFY_TIMEOUT", "3"))

# ---------------------------------------------------------------- logging
log = logging.getLogger("api")
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(message)s"))
log.addHandler(_handler)
log.setLevel(logging.INFO)
log.propagate = False


def log_event(level: str, event: str, **fields):
    """One JSON object per line, so logs can be grepped or parsed."""
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "level": level, "event": event}
    record.update(fields)
    log.info(json.dumps(record))


# ---------------------------------------------------------------- metrics
REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "path", "status"])
LATENCY = Histogram(
    "http_request_duration_seconds",
    "Request latency in seconds",
    ["method", "path"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
DB_ERRORS = Counter("db_errors_total", "Database errors", ["kind"])
INTEGRATION_ERRORS = Counter("integration_errors_total", "Notification webhook failures", ["kind"])

app = FastAPI(title="Support Desk SaaS")


@app.middleware("http")
async def observe(request: Request, call_next):
    """Time every request, count it by route and status, and log it."""
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        elapsed = time.perf_counter() - start
        route = request.scope.get("route")
        endpoint = request.scope.get("endpoint")
        path = getattr(route, "path", None) or getattr(endpoint, "__name__", "unmatched")
        if path != "/metrics":
            REQUESTS.labels(request.method, path, str(status)).inc()
            LATENCY.labels(request.method, path).observe(elapsed)
            log_event(
                "INFO" if status < 500 else "ERROR",
                "request",
                method=request.method,
                path=request.url.path,
                route=path,
                status=status,
                ms=round(elapsed * 1000, 1),
                user=request.scope.get("state", {}).get("user"),
            )


# ---------------------------------------------------------------- database
@contextmanager
def db():
    """Open a connection, give back a dict cursor, commit or roll back, close.

    A new connection per request keeps the lab simple and makes connection
    limits visible (scenario 6). A real service would use a pool.
    """
    try:
        conn = psycopg2.connect(DB_DSN, connect_timeout=3)
    except psycopg2.OperationalError as exc:
        DB_ERRORS.labels("connect").inc()
        log_event("ERROR", "db_connect_failed", error=str(exc).strip().splitlines()[0])
        raise HTTPException(503, "database unavailable")
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                yield cur
    except psycopg2.errors.UniqueViolation:
        raise HTTPException(409, "a ticket with this external_ref already exists")
    except psycopg2.Error as exc:
        DB_ERRORS.labels("query").inc()
        log_event("ERROR", "db_query_failed", error=str(exc).strip().splitlines()[0])
        raise HTTPException(500, "database error")
    finally:
        conn.close()


# ---------------------------------------------------------------- auth / RBAC
ROLE_RANK = {"viewer": 1, "agent": 2, "admin": 3}


def current_user(request: Request, x_api_key: Optional[str] = Header(default=None)):
    if not x_api_key:
        log_event("WARN", "auth_rejected", reason="missing key")
        raise HTTPException(401, "missing API key")
    with db() as cur:
        cur.execute(
            "SELECT id, email, role, org_id, active FROM users WHERE api_key = %s",
            (x_api_key,),
        )
        user = cur.fetchone()
    if user is None or not user["active"]:
        log_event(
            "WARN",
            "auth_rejected",
            reason="unknown key" if user is None else "inactive user",
            user=user["email"] if user else None,
        )
        raise HTTPException(401, "invalid or inactive API key")
    request.state.user = user["email"]
    return user


def require(role: str):
    def check(user=Depends(current_user)):
        if ROLE_RANK[user["role"]] < ROLE_RANK[role]:
            log_event("WARN", "auth_forbidden", user=user["email"], role=user["role"], needed=role)
            raise HTTPException(403, f"requires {role} role")
        return user

    return check


# ---------------------------------------------------------------- models
EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
PRIORITY = "^(low|normal|high|urgent)$"
STATUS = "^(open|pending|resolved|closed)$"
COLS = "id, org_id, external_ref, title, body, customer_email, status, priority, created_at, updated_at"


class TicketIn(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    body: str = Field(default="", max_length=5000)
    customer_email: str = Field(pattern=EMAIL)
    priority: str = Field(default="normal", pattern=PRIORITY)
    external_ref: Optional[str] = Field(default=None, max_length=64)


class TicketPatch(BaseModel):
    status: Optional[str] = Field(default=None, pattern=STATUS)
    priority: Optional[str] = Field(default=None, pattern=PRIORITY)


# ---------------------------------------------------------------- endpoints
@app.get("/health")
def health():
    start = time.perf_counter()
    try:
        with db() as cur:
            cur.execute("SELECT 1")
        ok = True
    except HTTPException:
        ok = False
    body = {
        "status": "ok" if ok else "degraded",
        "database": "up" if ok else "down",
        "db_ms": round((time.perf_counter() - start) * 1000, 1),
    }
    return Response(json.dumps(body), status_code=200 if ok else 503, media_type="application/json")


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/tickets")
def list_tickets(
    status: Optional[str] = Query(default=None, pattern=STATUS),
    limit: int = Query(default=50, ge=1, le=200),
    user=Depends(require("viewer")),
):
    sql = f"SELECT {COLS} FROM tickets WHERE org_id = %s"
    params = [user["org_id"]]
    if status:
        sql += " AND status = %s"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT %s"
    params.append(limit)
    with db() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


@app.get("/tickets/search")
def search_tickets(
    customer_email: str = Query(pattern=EMAIL),
    limit: int = Query(default=50, ge=1, le=200),
    user=Depends(require("viewer")),
):
    """All tickets for one customer, newest first. Slow without an index (scenario 1)."""
    with db() as cur:
        cur.execute(
            f"SELECT {COLS} FROM tickets WHERE customer_email = %s AND org_id = %s "
            "ORDER BY created_at DESC LIMIT %s",
            (customer_email, user["org_id"], limit),
        )
        return cur.fetchall()


def _get_ticket(cur, ticket_id: int, org_id: int):
    cur.execute(f"SELECT {COLS} FROM tickets WHERE id = %s AND org_id = %s", (ticket_id, org_id))
    ticket = cur.fetchone()
    if ticket is None:  # another org's ticket looks exactly like a missing one
        raise HTTPException(404, "ticket not found")
    return ticket


@app.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: int, user=Depends(require("viewer"))):
    with db() as cur:
        return _get_ticket(cur, ticket_id, user["org_id"])


@app.post("/tickets", status_code=201)
def create_ticket(ticket: TicketIn, user=Depends(require("agent"))):
    with db() as cur:
        cur.execute(
            "INSERT INTO tickets (org_id, external_ref, title, body, customer_email, priority) "
            f"VALUES (%s, %s, %s, %s, %s, %s) RETURNING {COLS}",
            (user["org_id"], ticket.external_ref, ticket.title, ticket.body,
             ticket.customer_email, ticket.priority),
        )
        created = cur.fetchone()
    log_event("INFO", "ticket_created", id=created["id"], user=user["email"])
    return created


@app.patch("/tickets/{ticket_id}")
def update_ticket(ticket_id: int, patch: TicketPatch, user=Depends(require("agent"))):
    changes = patch.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(422, "nothing to update")
    with db() as cur:
        _get_ticket(cur, ticket_id, user["org_id"])
        sets = ", ".join(f"{field} = %s" for field in changes)  # field names come from the model, not the user
        cur.execute(
            f"UPDATE tickets SET {sets}, updated_at = now() WHERE id = %s RETURNING {COLS}",
            (*changes.values(), ticket_id),
        )
        return cur.fetchone()


@app.delete("/tickets/{ticket_id}", status_code=204)
def delete_ticket(ticket_id: int, user=Depends(require("admin"))):
    with db() as cur:
        _get_ticket(cur, ticket_id, user["org_id"])
        cur.execute("DELETE FROM tickets WHERE id = %s", (ticket_id,))
    log_event("INFO", "ticket_deleted", id=ticket_id, user=user["email"])
    return Response(status_code=204)


@app.post("/tickets/{ticket_id}/notify")
def notify_customer(ticket_id: int, user=Depends(require("agent"))):
    """Call the third-party notification service (scenario 3 makes it slow)."""
    with db() as cur:
        ticket = _get_ticket(cur, ticket_id, user["org_id"])
    start = time.perf_counter()
    try:
        resp = requests.post(
            NOTIFY_URL,
            json={"ticket_id": ticket["id"], "email": ticket["customer_email"], "status": ticket["status"]},
            timeout=NOTIFY_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.Timeout:
        INTEGRATION_ERRORS.labels("timeout").inc()
        log_event("ERROR", "integration_timeout", url=NOTIFY_URL, timeout_s=NOTIFY_TIMEOUT, ticket=ticket_id)
        raise HTTPException(504, "notification service timed out")
    except requests.RequestException as exc:
        INTEGRATION_ERRORS.labels("error").inc()
        log_event("ERROR", "integration_failed", url=NOTIFY_URL, error=str(exc)[:200], ticket=ticket_id)
        raise HTTPException(502, "notification service error")
    return {"notified": True, "ms": round((time.perf_counter() - start) * 1000, 1)}


@app.get("/admin/integrity")
def integrity_report(user=Depends(require("admin"))):
    """Data checks an on-call engineer would run (scenario 5)."""
    with db() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM (SELECT external_ref FROM tickets WHERE external_ref IS NOT NULL "
            "GROUP BY external_ref HAVING count(*) > 1) d"
        )
        duplicates = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM tickets WHERE created_at > now() + interval '1 minute'")
        future = cur.fetchone()["n"]
    return {
        "duplicate_external_refs": duplicates,
        "future_created_at": future,
        "healthy": duplicates == 0 and future == 0,
    }


@app.get("/admin/stats")
def stats(user=Depends(require("admin"))):
    with db() as cur:
        cur.execute(
            "SELECT status, count(*) AS n FROM tickets WHERE org_id = %s GROUP BY status ORDER BY status",
            (user["org_id"],),
        )
        return {row["status"]: row["n"] for row in cur.fetchall()}
