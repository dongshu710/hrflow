"""员工档案业务用例。

职责边界：这一层负责"业务规则"，不直接拼 SQL，也不关心 HTTP。
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import List, Optional

from ..domain.errors import BusinessError
from ..domain.models import (
    Employee,
    EmployeeEvent,
    EmployeeEventType,
    EmployeeStatus,
)
from ..repository import employee_repo, leave_repo


def create_employee(conn: sqlite3.Connection, data: dict) -> Employee:
    """新员工入职：建档案 + 记入职事件 + 折算当年年假额度。"""
    name = (data.get("name") or "").strip()
    if not name:
        raise BusinessError("姓名不能为空", "invalid_param")

    emp_no = (data.get("emp_no") or "").strip() or employee_repo.next_emp_no(conn)
    exists = conn.execute("SELECT id FROM employees WHERE emp_no = ?", (emp_no,)).fetchone()
    if exists:
        raise BusinessError(f"工号 {emp_no} 已存在", "duplicate_emp_no")

    hire_date = _parse_date(data.get("hire_date")) or date.today()
    emp = Employee(
        emp_no=emp_no,
        name=name,
        dept_id=data.get("dept_id"),
        title=(data.get("title") or "").strip(),
        manager_id=data.get("manager_id"),
        hire_date=hire_date,
        status=data.get("status") or EmployeeStatus.PROBATION,
        email=(data.get("email") or "").strip(),
        phone=(data.get("phone") or "").strip(),
    )
    emp.id = employee_repo.create_employee(conn, emp)

    employee_repo.add_event(conn, EmployeeEvent(
        emp_id=emp.id,
        event_type=EmployeeEventType.HIRE,
        to_value=f"{emp.title} / {emp.emp_no}",
        effective_date=hire_date,
        remark=data.get("remark") or "系统入职登记",
    ))

    _grant_annual_leave(conn, emp, hire_date)
    return _must_get(conn, emp.id)  # 回填部门名、上级名等关联字段


def _grant_annual_leave(conn: sqlite3.Connection, emp: Employee, hire_date: date) -> None:
    """入职当年年假按剩余自然月折算（真实 HR 规则），转正/调岗不重复发放。"""
    from ..domain.models import LeaveAllocation

    types = {lt.code: lt for lt in leave_repo.list_leave_types(conn)}
    for code in ("annual", "sick", "personal"):
        lt = types.get(code)
        if not lt or lt.annual_quota <= 0:
            continue
        if code == "annual":
            remaining_months = 12 - hire_date.month + 1
            days = round(lt.annual_quota * remaining_months / 12 * 2) / 2  # 折算到 0.5 天
            source = "annual_grant"
        else:
            days = lt.annual_quota
            source = "annual_grant"
        if days > 0:
            leave_repo.add_allocation(conn, LeaveAllocation(
                emp_id=emp.id, leave_type_id=lt.id, year=hire_date.year,
                days=days, source=source,
            ))


def regularize(conn: sqlite3.Connection, emp_id: int, effective_date: Optional[date] = None) -> Employee:
    """转正。"""
    emp = _must_get(conn, emp_id)
    if emp.status != EmployeeStatus.PROBATION:
        raise BusinessError("只有试用期员工可以转正", "invalid_status")
    old = emp.status
    employee_repo.update_employee(conn, emp_id, status=EmployeeStatus.ACTIVE)
    employee_repo.add_event(conn, EmployeeEvent(
        emp_id=emp_id, event_type=EmployeeEventType.PROBATION_PASS,
        from_value=old, to_value=EmployeeStatus.ACTIVE,
        effective_date=effective_date or date.today(), remark="试用期考核通过",
    ))
    return _must_get(conn, emp_id)


def transfer(conn: sqlite3.Connection, emp_id: int, dept_id: int, title: str = "",
             effective_date: Optional[date] = None) -> Employee:
    """调岗/调部门。"""
    emp = _must_get(conn, emp_id)
    if not dept_id:
        raise BusinessError("目标部门不能为空", "invalid_param")
    old_dept = emp.dept_name or str(emp.dept_id)
    employee_repo.update_employee(conn, emp_id, dept_id=dept_id, title=title or emp.title)
    new_row = conn.execute("SELECT name FROM departments WHERE id = ?", (dept_id,)).fetchone()
    employee_repo.add_event(conn, EmployeeEvent(
        emp_id=emp_id, event_type=EmployeeEventType.TRANSFER,
        from_value=f"{old_dept} / {emp.title}",
        to_value=f"{new_row['name'] if new_row else dept_id} / {title or emp.title}",
        effective_date=effective_date or date.today(), remark="组织调整",
    ))
    return _must_get(conn, emp_id)


def resign(conn: sqlite3.Connection, emp_id: int, effective_date: Optional[date] = None,
           remark: str = "") -> Employee:
    """离职。有未结单据时必须先处理完，这是 HR 日常最常踩的坑。"""
    emp = _must_get(conn, emp_id)
    if emp.status == EmployeeStatus.RESIGNED:
        raise BusinessError("该员工已离职", "invalid_status")
    open_requests = leave_repo.list_requests(conn, emp_id=emp_id, status="pending")
    if open_requests:
        raise BusinessError(
            f"该员工还有 {len(open_requests)} 张审批中的请假单，请先处理",
            "open_request_exists",
            details=[r.code for r in open_requests],
        )
    old = emp.status
    employee_repo.update_employee(conn, emp_id, status=EmployeeStatus.RESIGNED)
    employee_repo.add_event(conn, EmployeeEvent(
        emp_id=emp_id, event_type=EmployeeEventType.LEAVE,
        from_value=old, to_value=EmployeeStatus.RESIGNED,
        effective_date=effective_date or date.today(), remark=remark or "主动离职",
    ))
    return _must_get(conn, emp_id)


def list_employees(conn: sqlite3.Connection, keyword: str = "", dept_id: Optional[int] = None,
                   status: str = "") -> List[Employee]:
    return employee_repo.list_employees(conn, keyword=keyword, dept_id=dept_id, status=status)


def get_employee(conn: sqlite3.Connection, emp_id: int) -> Employee:
    return _must_get(conn, emp_id)


def employee_profile(conn: sqlite3.Connection, emp_id: int) -> dict:
    """员工详情页：档案 + 入转调离记录 + 当前假期余额。"""
    from datetime import date as _date
    emp = _must_get(conn, emp_id)
    return {
        "employee": to_dict(emp),
        "events": [_row_to_event_dict(r) for r in employee_repo.list_events(conn, emp_id)],
        "balances": leave_repo.list_balances(conn, emp_id, _date.today().year),
    }


def _must_get(conn: sqlite3.Connection, emp_id: int) -> Employee:
    emp = employee_repo.get_employee(conn, emp_id)
    if not emp:
        raise BusinessError(f"员工 {emp_id} 不存在", "not_found")
    return emp


def _parse_date(value) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date):
        return value
    from datetime import datetime
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        raise BusinessError(f"日期格式应为 YYYY-MM-DD：{value}", "invalid_param")


def to_dict(emp: Employee) -> dict:
    """员工实体 → 可直接序列化的字典（API 层使用）。"""
    return {
        "id": emp.id, "emp_no": emp.emp_no, "name": emp.name, "dept_id": emp.dept_id,
        "dept_name": emp.dept_name, "title": emp.title, "manager_id": emp.manager_id,
        "manager_name": emp.manager_name,
        "hire_date": emp.hire_date.isoformat() if emp.hire_date else None,
        "status": emp.status, "status_label": EmployeeStatus.LABELS.get(emp.status, emp.status),
        "email": emp.email, "phone": emp.phone,
    }


def _row_to_event_dict(row) -> dict:
    return {
        "id": row["id"], "emp_id": row["emp_id"], "emp_name": row["emp_name"],
        "event_type": row["event_type"],
        "event_label": EmployeeEventType.LABELS.get(row["event_type"], row["event_type"]),
        "from_value": row["from_value"], "to_value": row["to_value"],
        "effective_date": row["effective_date"], "remark": row["remark"],
    }
