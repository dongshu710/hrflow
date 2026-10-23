"""请假业务用例：申请 → 校验 → 路由审批链 → 多级审批 → 额度落账。

设计要点（对标的成熟实践）：
1. 校验集中在一个 `_validate_submit()`，新增规则只改一处；
2. 审批链按"角色"路由而不是写死人，换人不用改代码；
3. 额度只在"最终通过"时写账本，中间态不占用，撤回无需回冲；
4. 状态推进交给状态机，落库动作交给仓储，两者用事务包住。
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from typing import List, Optional

from ..config import HOLIDAYS
from ..domain.errors import BusinessError
from ..domain.models import (
    ApprovalTask,
    ApproverRole,
    EmployeeStatus,
    LeaveLedger,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveType,
)
from ..domain.workflow import build_leave_state_machine, route_approval_chain
from ..repository import employee_repo, leave_repo
from ..repository.db import transaction

_SM = build_leave_state_machine()


# --------------------------- 工作日计算 ---------------------------

def calc_workdays(start: date, end: date) -> float:
    """计算请假天数：去掉周末与法定节假日。

    真实系统里节假日来自 Holiday List（按地区/年度维护），这里从 config 读取。
    """
    if start > end:
        raise BusinessError("开始日期不能晚于结束日期", "invalid_date_range")
    days = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5 and cur.isoformat() not in HOLIDAYS:
            days += 1
        cur += timedelta(days=1)
    return float(days)


def is_workday(day: date) -> bool:
    return day.weekday() < 5 and day.isoformat() not in HOLIDAYS


# --------------------------- 提交申请 ---------------------------

def submit_request(conn: sqlite3.Connection, data: dict) -> LeaveRequest:
    emp_id = int(data.get("emp_id") or 0)
    emp = employee_repo.get_employee(conn, emp_id)
    if not emp:
        raise BusinessError(f"员工 {emp_id} 不存在", "not_found")
    if emp.status not in EmployeeStatus.ONBOARD:
        raise BusinessError("非在职员工不能提交请假申请", "employee_not_active")

    lt_id = int(data.get("leave_type_id") or 0)
    lt = leave_repo.get_leave_type(conn, lt_id)
    if not lt:
        raise BusinessError(f"假别 {lt_id} 不存在", "not_found")

    start = _parse_date(data.get("start_date"), "开始日期")
    end = _parse_date(data.get("end_date"), "结束日期")
    days = calc_workdays(start, end)
    if days <= 0:
        raise BusinessError("所选区间内没有工作日，请检查日期或节假日设置", "no_workday")

    attachment = bool(data.get("attachment"))
    _validate_submit(conn, emp, lt, start, end, days, attachment)

    chain = route_approval_chain(days, lt.code)
    req = LeaveRequest(
        code=leave_repo.next_request_code(conn),
        emp_id=emp_id, leave_type_id=lt.id,
        start_date=start, end_date=end, days=days,
        reason=(data.get("reason") or "").strip(),
        attachment=attachment,
        status=LeaveRequestStatus.DRAFT,
        approval_chain=chain, current_step=0,
    )

    with transaction(conn):
        req.id = leave_repo.create_request(conn, req)
        _SM.transition("submit", {"status": req.status}, actor_roles={"employee"})
        req.status = LeaveRequestStatus.PENDING
        leave_repo.update_request_status(conn, req.id, req.status, 0)
        leave_repo.add_approval_task(conn, ApprovalTask(
            request_id=req.id, step=0, role="employee",
            approver_name=emp.name, action="submit",
            from_status=LeaveRequestStatus.DRAFT, to_status=LeaveRequestStatus.PENDING,
            comment="提交申请",
        ))

    req.emp_name = emp.name
    req.dept_name = emp.dept_name
    req.leave_type_name = lt.name
    return req


def _validate_submit(conn: sqlite3.Connection, emp, lt: LeaveType, start: date, end: date,
                    days: float, attachment: bool) -> None:
    """提交前的全部校验。返回 None 表示通过，否则抛 BusinessError。"""
    today = date.today()

    if lt.max_days_per_request and days > lt.max_days_per_request:
        raise BusinessError(
            f"{lt.name}单次最多 {lt.max_days_per_request} 天，本次 {days} 天", "exceed_max_days")

    if days > (lt.annual_quota or 0) > 0:
        raise BusinessError(f"{lt.name}年度额度仅 {lt.annual_quota} 天", "exceed_quota")

    # 余额校验：只有计入额度的假别才校验（事假通常无额度限制）
    if lt.annual_quota > 0:
        bal = leave_repo.get_balance(conn, emp.id, lt.id, start.year)
        if days > bal["balance"]:
            raise BusinessError(
                f"{lt.name}剩余 {bal['balance']} 天，不足本次 {days} 天", "insufficient_balance")

    overlap = leave_repo.find_overlapping(conn, emp.id, start.isoformat(), end.isoformat())
    if overlap:
        raise BusinessError(
            "该时间段内已有请假单：" + "、".join(f"{o.code}({o.start_date}~{o.end_date})" for o in overlap),
            "date_overlap", details=[o.code for o in overlap])

    if start < today:
        raise BusinessError("请假开始日期早于今天，请走补假流程并说明原因", "back_dated")

    if lt.min_notice_days and (start - today).days < lt.min_notice_days:
        raise BusinessError(
            f"{lt.name}需提前 {lt.min_notice_days} 天申请，当前仅提前 {(start - today).days} 天",
            "insufficient_notice")

    if lt.require_attachment and not attachment:
        raise BusinessError(f"{lt.name}需上传证明材料", "attachment_required")


# --------------------------- 审批动作 ---------------------------

def approve(conn: sqlite3.Connection, req_id: int, role: str, approver_name: str = "",
            comment: str = "") -> LeaveRequest:
    req = _must_get(conn, req_id)
    _check_actor(role, req)

    from_status = req.status
    with transaction(conn):
        _SM.transition("approve", {"status": req.status}, actor_roles={role, "admin"})
        req.current_step += 1
        is_last = req.current_step >= len(req.approval_chain)

        if is_last:
            req.status = LeaveRequestStatus.APPROVED
            # 通过才扣额度：写账本而不是直接减余额字段，保证可追溯
            leave_repo.add_ledger(conn, LeaveLedger(
                emp_id=req.emp_id, leave_type_id=req.leave_type_id,
                request_id=req.id, days=-req.days,
                reason=f"{req.code} 请假扣减",
            ))
        else:
            req.status = LeaveRequestStatus.PENDING

        leave_repo.update_request_status(conn, req.id, req.status, req.current_step)
        leave_repo.add_approval_task(conn, ApprovalTask(
            request_id=req.id, step=req.current_step - 1, role=role,
            approver_name=approver_name or ApproverRole.LABELS.get(role, role),
            action="approve", from_status=from_status, to_status=req.status,
            comment=comment or "同意",
        ))
    return _must_get(conn, req_id)


def reject(conn: sqlite3.Connection, req_id: int, role: str, approver_name: str = "",
           comment: str = "") -> LeaveRequest:
    req = _must_get(conn, req_id)
    _check_actor(role, req)
    from_status = req.status
    with transaction(conn):
        _SM.transition("reject", {"status": req.status}, actor_roles={role, "admin"})
        req.status = LeaveRequestStatus.REJECTED
        leave_repo.update_request_status(conn, req.id, req.status, req.current_step)
        leave_repo.add_approval_task(conn, ApprovalTask(
            request_id=req.id, step=req.current_step, role=role,
            approver_name=approver_name or ApproverRole.LABELS.get(role, role),
            action="reject", from_status=from_status, to_status=req.status,
            comment=comment or "不同意",
        ))
    return _must_get(conn, req_id)


def cancel(conn: sqlite3.Connection, req_id: int, operator_name: str = "", comment: str = "") -> LeaveRequest:
    """申请人撤回。已通过的不能撤回，需走销假流程（真实系统里销假是另一条流程）。"""
    req = _must_get(conn, req_id)
    if req.status == LeaveRequestStatus.APPROVED:
        raise BusinessError("已通过的请假单不能撤回，请走销假流程", "cannot_cancel_approved")
    from_status = req.status
    with transaction(conn):
        _SM.transition("cancel", {"status": req.status}, actor_roles={"employee", "admin"})
        req.status = LeaveRequestStatus.CANCELLED
        leave_repo.update_request_status(conn, req.id, req.status, req.current_step)
        leave_repo.add_approval_task(conn, ApprovalTask(
            request_id=req.id, step=req.current_step, role="employee",
            approver_name=operator_name or req.emp_name, action="cancel",
            from_status=from_status, to_status=req.status, comment=comment or "申请人撤回",
        ))
    return _must_get(conn, req_id)


def _check_actor(role: str, req: LeaveRequest) -> None:
    if req.status != LeaveRequestStatus.PENDING:
        raise BusinessError("只有审批中的单据可以处理", "not_pending")
    if req.current_step >= len(req.approval_chain):
        raise BusinessError("审批链异常：已无待办审批节点", "chain_error")
    expected = req.approval_chain[req.current_step]
    if role != expected and role != "admin":
        raise BusinessError(
            f"当前应由【{ApproverRole.LABELS.get(expected, expected)}】处理，"
            f"不是【{ApproverRole.LABELS.get(role, role)}】", "wrong_approver")


# --------------------------- 查询 ---------------------------

def list_requests(conn: sqlite3.Connection, emp_id: Optional[int] = None, status: str = "",
                  dept_id: Optional[int] = None) -> List[dict]:
    return [_req_dict(conn, r) for r in leave_repo.list_requests(conn, emp_id, status, dept_id)]


def get_request(conn: sqlite3.Connection, req_id: int) -> dict:
    return _req_dict(conn, _must_get(conn, req_id), with_tasks=True)


def balances(conn: sqlite3.Connection, emp_id: int, year: Optional[int] = None) -> List[dict]:
    return leave_repo.list_balances(conn, emp_id, year or date.today().year)


def leave_types(conn: sqlite3.Connection) -> List[dict]:
    return [{
        "id": lt.id, "code": lt.code, "name": lt.name, "annual_quota": lt.annual_quota,
        "max_days_per_request": lt.max_days_per_request, "min_notice_days": lt.min_notice_days,
        "paid": lt.paid, "require_attachment": lt.require_attachment, "carry_over": lt.carry_over,
    } for lt in leave_repo.list_leave_types(conn)]


def _must_get(conn: sqlite3.Connection, req_id: int) -> LeaveRequest:
    req = leave_repo.get_request(conn, req_id)
    if not req:
        raise BusinessError(f"请假单 {req_id} 不存在", "not_found")
    return req


def _req_dict(conn: sqlite3.Connection, r: LeaveRequest, with_tasks: bool = False) -> dict:
    chain_labels = [ApproverRole.LABELS.get(s, s) for s in r.approval_chain]
    current_role = r.approval_chain[r.current_step] if r.status == LeaveRequestStatus.PENDING \
        and r.current_step < len(r.approval_chain) else None
    data = {
        "id": r.id, "code": r.code, "emp_id": r.emp_id, "emp_name": r.emp_name,
        "dept_name": r.dept_name, "leave_type_id": r.leave_type_id,
        "leave_type_name": r.leave_type_name,
        "start_date": r.start_date.isoformat(), "end_date": r.end_date.isoformat(),
        "days": r.days, "reason": r.reason, "attachment": r.attachment,
        "status": r.status, "status_label": LeaveRequestStatus.LABELS.get(r.status, r.status),
        "approval_chain": r.approval_chain, "approval_chain_labels": chain_labels,
        "current_step": r.current_step,
        "current_role": current_role,
        "current_role_label": ApproverRole.LABELS.get(current_role, "") if current_role else "",
        "progress": f"{min(r.current_step + 1, len(r.approval_chain))}/{len(r.approval_chain)}",
        "created_at": r.created_at, "updated_at": r.updated_at,
    }
    if with_tasks:
        data["tasks"] = [{
            "step": t["step"], "role": t["role"],
            "role_label": ApproverRole.LABELS.get(t["role"], t["role"]),
            "approver_name": t["approver_name"], "action": t["action"],
            "comment": t["comment"], "acted_at": t["acted_at"],
            "from_status": t["from_status"], "to_status": t["to_status"],
        } for t in leave_repo.list_tasks(conn, r.id)]
    return data


def _parse_date(value, field_name: str) -> date:
    if not value:
        raise BusinessError(f"{field_name}不能为空", "invalid_param")
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        raise BusinessError(f"{field_name}格式应为 YYYY-MM-DD", "invalid_param")
