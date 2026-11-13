-- Runs once, the first time the database volume is created.

CREATE TABLE organizations (
    id   SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE users (
    id      SERIAL PRIMARY KEY,
    org_id  INT  NOT NULL REFERENCES organizations(id),
    email   TEXT NOT NULL UNIQUE,
    role    TEXT NOT NULL CHECK (role IN ('viewer', 'agent', 'admin')),
    api_key TEXT NOT NULL UNIQUE,   -- plain text for the lab; hash it in production
    active  BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE tickets (
    id             BIGSERIAL PRIMARY KEY,
    org_id         INT  NOT NULL REFERENCES organizations(id),
    external_ref   TEXT,
    title          TEXT NOT NULL,
    body           TEXT NOT NULL DEFAULT '',
    customer_email TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open', 'pending', 'resolved', 'closed')),
    priority       TEXT NOT NULL DEFAULT 'normal'
                   CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT tickets_external_ref_key UNIQUE (external_ref)
);
-- NOTE: deliberately NO index on tickets.customer_email. That is scenario 1.

INSERT INTO organizations (name) VALUES ('Acme Corp'), ('Globex');

INSERT INTO users (org_id, email, role, api_key, active) VALUES
    (1, 'admin@acme.test',    'admin',  'admin-key-123',     true),
    (1, 'agent@acme.test',    'agent',  'agent-key-123',     true),
    (1, 'viewer@acme.test',   'viewer', 'viewer-key-123',    true),
    (1, 'disabled@acme.test', 'agent',  'disabled-key-123',  false),
    (2, 'agent@globex.test',  'agent',  'globex-agent-key',  true);

-- Least privilege: the API gets data access only, no DDL.
CREATE ROLE api_user LOGIN PASSWORD 'apisecret';
GRANT CONNECT ON DATABASE saas TO api_user;
GRANT USAGE ON SCHEMA public TO api_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO api_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO api_user;
