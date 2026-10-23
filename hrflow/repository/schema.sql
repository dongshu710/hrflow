-- 人事流程 mini 版 · 数据库结构
-- 设计要点：
--   1) 单据类（leave_requests / approval_tasks）与档案类（employees）分表；
--   2) 假期余额不落字段，由 leave_allocations（发放）与 leave_ledger（消耗）汇总得出，可追溯；
--   3) 每张表都带时间字段，员工档案变动走 employee_events 单独留痕。

CREATE TABLE IF NOT EXISTS departments (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT NOT NULL,
    parent_id INTEGER
);

CREATE TABLE IF NOT EXISTS employees (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    emp_no     TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    dept_id    INTEGER REFERENCES departments(id),
    title      TEXT,
    manager_id INTEGER REFERENCES employees(id),
    hire_date  TEXT,
    status     TEXT NOT NULL DEFAULT 'probation',
    email      TEXT,
    phone      TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS employee_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    emp_id         INTEGER NOT NULL REFERENCES employees(id),
    event_type     TEXT NOT NULL,
    from_value     TEXT,
    to_value       TEXT,
    effective_date TEXT,
    remark         TEXT,
    created_at     TEXT
);

CREATE TABLE IF NOT EXISTS leave_types (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    code                 TEXT NOT NULL UNIQUE,
    name                 TEXT NOT NULL,
    annual_quota         REAL NOT NULL DEFAULT 0,
    max_days_per_request REAL NOT NULL DEFAULT 0,
    min_notice_days      INTEGER NOT NULL DEFAULT 0,
    paid                 INTEGER NOT NULL DEFAULT 1,
    require_attachment   INTEGER NOT NULL DEFAULT 0,
    carry_over           INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS leave_allocations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    emp_id        INTEGER NOT NULL REFERENCES employees(id),
    leave_type_id INTEGER NOT NULL REFERENCES leave_types(id),
    year          INTEGER NOT NULL,
    days          REAL NOT NULL,
    source        TEXT NOT NULL DEFAULT 'annual_grant',
    created_at    TEXT
);

CREATE TABLE IF NOT EXISTS leave_ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    emp_id        INTEGER NOT NULL REFERENCES employees(id),
    leave_type_id INTEGER NOT NULL REFERENCES leave_types(id),
    request_id    INTEGER REFERENCES leave_requests(id),
    days          REAL NOT NULL,        -- 正数发放 / 负数消耗
    reason        TEXT,
    created_at    TEXT
);

CREATE TABLE IF NOT EXISTS leave_requests (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    code           TEXT NOT NULL UNIQUE,
    emp_id         INTEGER NOT NULL REFERENCES employees(id),
    leave_type_id  INTEGER NOT NULL REFERENCES leave_types(id),
    start_date     TEXT NOT NULL,
    end_date       TEXT NOT NULL,
    days           REAL NOT NULL,
    reason         TEXT,
    attachment     INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'draft',
    approval_chain TEXT NOT NULL,       -- JSON 数组，如 ["manager","hr"]
    current_step   INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT,
    updated_at     TEXT
);

CREATE TABLE IF NOT EXISTS approval_tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id    INTEGER NOT NULL REFERENCES leave_requests(id),
    step          INTEGER NOT NULL,
    role          TEXT NOT NULL,
    approver_name TEXT,
    action        TEXT NOT NULL,
    from_status   TEXT,
    to_status     TEXT,
    comment       TEXT,
    acted_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_leave_req_emp ON leave_requests(emp_id);
CREATE INDEX IF NOT EXISTS idx_leave_req_status ON leave_requests(status);
CREATE INDEX IF NOT EXISTS idx_ledger_emp ON leave_ledger(emp_id, leave_type_id);
