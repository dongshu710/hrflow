"""请假域仓储层：假别、额度账本、请假单、审批留痕。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import List, Optional

from ..domain.models import (
    ApprovalTask,
    LeaveAllocation,
    LeaveLedger,
    LeaveRequest,
    LeaveType,
)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _to_type(row: sqlite3.Row) -> LeaveType:
    return LeaveType(
        id=row["id"], code=row["code"], name=row["name"],
        annual_quota=row["annual_quota"], max_days_per_request=row["max_days_per_request"],
        min_notice_days=row["min_notice_days"], paid=bool(row["paid"]),
        require_attachment=bool(row["require_attachment"]), carry_over=bool(row["carry_over"]),
    )


def _to_request(row: sqlite3.Row) -> LeaveRequest:
    return LeaveRequest(
        id=row["id"], code=row["code"], emp_id=row["emp_id"],
        emp_name=row["emp_name"] if "emp_name" in row.keys() else "",
        dept_name=row["dept_name"] if "dept_name" in row.keys() else "",
        leave_type_id=row["leave_type_id"],
        leave_type_name=row["leave_type_name"] if "leave_type_name" in row.keys() else "",
        start_date=datetime.strptime(row["start_date"], "%Y-%m-%d").date(),
        end_date=datetime.strptime(row["end_date"], "%Y-%m-%d").date(),
        days=row["days"], reason=row["reason"] or "", attachment=bool(row["attachment"]),
        status=row["status"],
        approval_chain=json.loads(row["approval_chain"] or "[]"),
        current_step=row["current_step"], created_at=row["created_at"], updated_at=row["updated_at"],
    )


# ------------------------------ 假别 ------------------------------

def list_leave_types(conn: sqlite3.Connection) -> List[LeaveType]:
    return [_to_type(r) for r in conn.execute("SELECT * FROM leave_types ORDER BY id").fetchall()]


def get_leave_type(conn: sqlite3.Connection, type_id: int) -> Optional[LeaveType]:
    row = conn.execute("SELECT * FROM leave_types WHERE id = ?", (type_id,)).fetchone()
    return _to_type(row) if row else None


def create_leave_type(conn: sqlite3.Connection, lt: LeaveType) -> int:
    cur = conn.execute(
        """INSERT INTO leave_types(code, name, annual_quota, max_days_per_request,
           min_notice_days, paid, require_attachment, carry_over)
           VALUES(?,?,?,?,?,?,?,?)""",
        (lt.code, lt.name, lt.annual_quota, lt.max_days_per_request, lt.min_notice_days,
         int(lt.paid), int(lt.require_attachment), int(lt.carry_over)),
    )
    return cur.lastrowid


# --------------------------- 额度账本 ---------------------------

def add_allocation(conn: sqlite3.Connection, alloc: LeaveAllocation) -> int:
    cur = conn.execute(
        """INSERT INTO leave_allocations(emp_id, leave_type_id, year, days, source, created_at)
           VALUES(?,?,?,?,?,?)""",
        (alloc.emp_id, alloc.leave_type_id, alloc.year, alloc.days, alloc.source, _now()),
    )
    return cur.lastrowid


def add_ledger(conn: sqlite3.Connection, entry: LeaveLedger) -> int:
    cur = conn.execute(
        """INSERT INTO leave_ledger(emp_id, leave_type_id, request_id, days, reason, created_at)
           VALUES(?,?,?,?,?,?)""",
        (entry.emp_id, entry.leave_type_id, entry.request_id, entry.days, entry.reason,
         entry.created_at or _now()),
    )
    return cur.lastrowid


def get_balance(conn: sqlite3.Connection, emp_id: int, leave_type_id: int, year: int) -> dict:
    """余额 = 年度发放 - 已消耗。消耗以账本流水为准，任何一笔都能追到具体单据。"""
    granted = conn.execute(
        "SELECT COALESCE(SUM(days), 0) AS s FROM leave_allocations WHERE emp_id=? AND leave_type_id=? AND year=?",
        (emp_id, leave_type_id, year),
    ).fetchone()["s"]
    used = conn.execute(
        """SELECT COALESCE(SUM(-days), 0) AS s FROM leave_ledger
           WHERE emp_id=? AND leave_type_id=? AND days < 0 AND substr(created_at, 1, 4) = ?""",
        (emp_id, leave_type_id, str(year)),
    ).fetchone()["s"]
    pending = conn.execute(
        """SELECT COALESCE(SUM(r.days), 0) AS s FROM leave_requests r
           WHERE r.emp_id=? AND r.leave_type_id=? AND r.status='pending'""",
        (emp_id, leave_type_id),
    ).fetchone()["s"]
    return {"granted": granted, "used": used, "balance": granted - used, "pending": pending}


def list_balances(conn: sqlite3.Connection, emp_id: int, year: int) -> List[dict]:
    result = []
    for lt in list_leave_types(conn):
        b = get_balance(conn, emp_id, lt.id, year)
        if b["granted"] <= 0:
            continue
        result.append({"leave_type_id": lt.id, "code": lt.code, "name": lt.name, **b})
    return result


# --------------------------- 请假单据 ---------------------------

def create_request(conn: sqlite3.Connection, req: LeaveRequest) -> int:
    cur = conn.execute(
        """INSERT INTO leave_requests(code, emp_id, leave_type_id, start_date, end_date, days,
           reason, attachment, status, approval_chain, current_step, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            req.code, req.emp_id, req.leave_type_id,
            req.start_date.isoformat(), req.end_date.isoformat(), req.days,
            req.reason, int(req.attachment), req.status,
            json.dumps(req.approval_chain, ensure_ascii=False), req.current_step, _now(), _now(),
        ),
    )
    return cur.lastrowid


def get_request(conn: sqlite3.Connection, req_id: int) -> Optional[LeaveRequest]:
    row = conn.execute(
        """SELECT r.*, e.name AS emp_name, d.name AS dept_name, lt.name AS leave_type_name
           FROM leave_requests r
           JOIN employees e ON e.id = r.emp_id
           LEFT JOIN departments d ON d.id = e.dept_id
           JOIN leave_types lt ON lt.id = r.leave_type_id
           WHERE r.id = ?""",
        (req_id,),
    ).fetchone()
    return _to_request(row) if row else None


def list_requests(conn: sqlite3.Connection, emp_id: Optional[int] = None, status: str = "",
                  dept_id: Optional[int] = None) -> List[LeaveRequest]:
    sql = """SELECT r.*, e.name AS emp_name, d.name AS dept_name, lt.name AS leave_type_name
             FROM leave_requests r
             JOIN employees e ON e.id = r.emp_id
             LEFT JOIN departments d ON d.id = e.dept_id
             JOIN leave_types lt ON lt.id = r.leave_type_id
             WHERE 1 = 1"""
    params = []
    if emp_id:
        sql += " AND r.emp_id = ?"
        params.append(emp_id)
    if status:
        sql += " AND r.status = ?"
        params.append(status)
    if dept_id:
        sql += " AND e.dept_id = ?"
        params.append(dept_id)
    sql += " ORDER BY r.id DESC"
    return [_to_request(r) for r in conn.execute(sql, params).fetchall()]


def find_overlapping(conn: sqlite3.Connection, emp_id: int, start: str, end: str,
                     exclude_id: Optional[int] = None) -> List[LeaveRequest]:
    """同一员工在同一时间区间内是否已有未作废的单据（借鉴 frappe 的 validate_leave_overlap）。"""
    sql = """SELECT r.*, e.name AS emp_name, d.name AS dept_name, lt.name AS leave_type_name
             FROM leave_requests r
             JOIN employees e ON e.id = r.emp_id
             LEFT JOIN departments d ON d.id = e.dept_id
             JOIN leave_types lt ON lt.id = r.leave_type_id
             WHERE r.emp_id = ? AND r.status IN ('pending', 'approved')
               AND r.start_date <= ? AND r.end_date >= ?"""
    params = [emp_id, end, start]
    if exclude_id:
        sql += " AND r.id != ?"
        params.append(exclude_id)
    return [_to_request(r) for r in conn.execute(sql, params).fetchall()]


def update_request_status(conn: sqlite3.Connection, req_id: int, status: str, step: int) -> None:
    conn.execute(
        "UPDATE leave_requests SET status = ?, current_step = ?, updated_at = ? WHERE id = ?",
        (status, step, _now(), req_id),
    )


def next_request_code(conn: sqlite3.Connection) -> str:
    today = datetime.now().strftime("%Y%m%d")
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM leave_requests WHERE code LIKE ?", (f"LV-{today}-%",)
    ).fetchone()
    return f"LV-{today}-{row['c'] + 1:04d}"


# --------------------------- 审批留痕 ---------------------------

def add_approval_task(conn: sqlite3.Connection, task: ApprovalTask) -> int:
    cur = conn.execute(
        """INSERT INTO approval_tasks(request_id, step, role, approver_name, action,
           from_status, to_status, comment, acted_at) VALUES(?,?,?,?,?,?,?,?,?)""",
        (task.request_id, task.step, task.role, task.approver_name, task.action,
         task.from_status, task.to_status, task.comment, task.acted_at or _now()),
    )
    return cur.lastrowid


def list_tasks(conn: sqlite3.Connection, request_id: int) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM approval_tasks WHERE request_id = ? ORDER BY step, id", (request_id,)
    ).fetchall()
