"""员工档案仓储层：只做数据的存取，不做业务判断。"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import List, Optional

from ..domain.models import Employee, EmployeeEvent


def _row_to_employee(row: sqlite3.Row) -> Employee:
    return Employee(
        id=row["id"],
        emp_no=row["emp_no"],
        name=row["name"],
        dept_id=row["dept_id"],
        dept_name=row["dept_name"] if "dept_name" in row.keys() else "",
        title=row["title"],
        manager_id=row["manager_id"],
        manager_name=row["manager_name"] if "manager_name" in row.keys() else "",
        hire_date=_parse_date(row["hire_date"]),
        status=row["status"],
        email=row["email"] or "",
        phone=row["phone"] or "",
    )


def _parse_date(value: Optional[str]):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------ 部门 ------------------------------

def create_department(conn: sqlite3.Connection, name: str, parent_id: Optional[int] = None) -> int:
    cur = conn.execute("INSERT INTO departments(name, parent_id) VALUES(?, ?)", (name, parent_id))
    return cur.lastrowid


def list_departments(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute("SELECT * FROM departments ORDER BY id").fetchall()


# ------------------------------ 员工 ------------------------------

def create_employee(conn: sqlite3.Connection, emp: Employee) -> int:
    cur = conn.execute(
        """INSERT INTO employees(emp_no, name, dept_id, title, manager_id, hire_date,
           status, email, phone, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            emp.emp_no, emp.name, emp.dept_id, emp.title, emp.manager_id,
            emp.hire_date.isoformat() if emp.hire_date else None,
            emp.status, emp.email, emp.phone, _now(), _now(),
        ),
    )
    return cur.lastrowid


def update_employee(conn: sqlite3.Connection, emp_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [emp_id]
    conn.execute(f"UPDATE employees SET {cols} WHERE id = ?", values)


def get_employee(conn: sqlite3.Connection, emp_id: int) -> Optional[Employee]:
    row = conn.execute(
        """SELECT e.*, d.name AS dept_name, m.name AS manager_name
           FROM employees e
           LEFT JOIN departments d ON d.id = e.dept_id
           LEFT JOIN employees m ON m.id = e.manager_id
           WHERE e.id = ?""",
        (emp_id,),
    ).fetchone()
    return _row_to_employee(row) if row else None


def list_employees(conn: sqlite3.Connection, keyword: str = "", dept_id: Optional[int] = None,
                   status: str = "") -> List[Employee]:
    sql = """SELECT e.*, d.name AS dept_name, m.name AS manager_name
             FROM employees e
             LEFT JOIN departments d ON d.id = e.dept_id
             LEFT JOIN employees m ON m.id = e.manager_id
             WHERE 1 = 1"""
    params = []
    if keyword:
        sql += " AND (e.name LIKE ? OR e.emp_no LIKE ? OR e.title LIKE ?)"
        params += [f"%{keyword}%"] * 3
    if dept_id:
        sql += " AND e.dept_id = ?"
        params.append(dept_id)
    if status:
        sql += " AND e.status = ?"
        params.append(status)
    sql += " ORDER BY e.emp_no"
    return [_row_to_employee(r) for r in conn.execute(sql, params).fetchall()]


def next_emp_no(conn: sqlite3.Connection, prefix: str = "E") -> str:
    row = conn.execute("SELECT COUNT(*) AS c FROM employees").fetchone()
    return f"{prefix}{row['c'] + 1:04d}"


# --------------------------- 入转调离事件 ---------------------------

def add_event(conn: sqlite3.Connection, event: EmployeeEvent) -> int:
    cur = conn.execute(
        """INSERT INTO employee_events(emp_id, event_type, from_value, to_value,
           effective_date, remark, created_at) VALUES(?,?,?,?,?,?,?)""",
        (
            event.emp_id, event.event_type, event.from_value, event.to_value,
            event.effective_date.isoformat() if event.effective_date else None,
            event.remark, _now(),
        ),
    )
    return cur.lastrowid


def list_events(conn: sqlite3.Connection, emp_id: Optional[int] = None) -> List[sqlite3.Row]:
    sql = """SELECT ev.*, e.name AS emp_name FROM employee_events ev
             JOIN employees e ON e.id = ev.emp_id"""
    params = []
    if emp_id:
        sql += " WHERE ev.emp_id = ?"
        params.append(emp_id)
    sql += " ORDER BY ev.effective_date DESC, ev.id DESC"
    return conn.execute(sql, params).fetchall()
