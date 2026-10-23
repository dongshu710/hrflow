"""生成演示数据：部门、假别、员工、请假单（含各种状态）。

用法：
    python scripts/seed.py           # 重置并写入演示数据
    python scripts/seed.py --keep    # 在现有库上追加（通常不用）
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hrflow.console import enable_utf8  # noqa: E402
from hrflow.domain.models import (  # noqa: E402
    ApproverRole, EmployeeStatus, LeaveAllocation, LeaveType,
)
from hrflow.repository import employee_repo, leave_repo  # noqa: E402
from hrflow.repository.db import init_db, reset_db  # noqa: E402
from hrflow.services import leave_service  # noqa: E402

LEAVE_TYPES = [
    LeaveType(code="annual", name="年假", annual_quota=10, max_days_per_request=10,
              min_notice_days=3, paid=True, require_attachment=False, carry_over=True),
    LeaveType(code="sick", name="病假", annual_quota=5, max_days_per_request=5,
              min_notice_days=0, paid=True, require_attachment=True),
    LeaveType(code="personal", name="事假", annual_quota=0, max_days_per_request=5,
              min_notice_days=1, paid=False, require_attachment=False),
    LeaveType(code="compensatory", name="调休", annual_quota=0, max_days_per_request=3,
              min_notice_days=1, paid=True, require_attachment=False),
]

# (姓名, 部门, 岗位, 上级工号, 入职日期, 状态)
EMPLOYEES = [
    ("张伟", "技术部", "技术总监", None, "2024-03-01", EmployeeStatus.ACTIVE),
    ("李娜", "技术部", "后端工程师", "E0001", "2025-07-15", EmployeeStatus.ACTIVE),
    ("王强", "技术部", "前端工程师", "E0001", "2026-03-02", EmployeeStatus.PROBATION),
    ("刘洋", "技术部", "测试工程师", "E0001", "2026-06-01", EmployeeStatus.PROBATION),
    ("陈静", "产品部", "产品负责人", None, "2024-09-01", EmployeeStatus.ACTIVE),
    ("赵敏", "产品部", "产品设计师", "E0005", "2025-11-10", EmployeeStatus.ACTIVE),
    ("孙芳", "人力资源部", "HR 经理", None, "2024-05-06", EmployeeStatus.ACTIVE),
    ("周涛", "人力资源部", "HR 专员", "E0007", "2026-08-03", EmployeeStatus.PROBATION),
    ("林浩", "市场部", "市场经理", None, "2024-01-08", EmployeeStatus.ACTIVE),
    ("吴凯", "市场部", "市场专员", "E0009", "2025-02-17", EmployeeStatus.ACTIVE),
    ("郑爽", "市场部", "市场专员", "E0009", "2026-04-21", EmployeeStatus.ACTIVE),
]

DEPARTMENTS = ["技术部", "产品部", "人力资源部", "市场部"]


def seed(db_path: str = None, reset: bool = True) -> sqlite3.Connection:
    from hrflow.repository.db import DEFAULT_DB_PATH
    db_path = db_path or DEFAULT_DB_PATH
    conn = reset_db(db_path) if reset else init_db(db_path)

    # 1. 假别
    type_ids = {}
    for lt in LEAVE_TYPES:
        type_ids[lt.code] = leave_repo.create_leave_type(conn, lt)

    # 2. 部门
    dept_ids = {}
    for name in DEPARTMENTS:
        dept_ids[name] = employee_repo.create_department(conn, name)

    # 3. 员工（先建上级，再回填 manager_id）
    emp_ids = {}
    pending_manager = []
    for i, (name, dept, title, mgr_no, hire, status) in enumerate(EMPLOYEES, start=1):
        emp_no = f"E{i:04d}"
        from hrflow.domain.models import Employee
        emp = Employee(
            emp_no=emp_no, name=name, dept_id=dept_ids[dept], title=title,
            hire_date=datetime.strptime(hire, "%Y-%m-%d").date(), status=status,
            email=f"{emp_no.lower()}@example.com", phone=f"1380000{i:04d}",
        )
        emp.id = employee_repo.create_employee(conn, emp)
        emp_ids[emp_no] = emp.id
        pending_manager.append((emp.id, mgr_no))
        employee_repo.add_event(conn, _event(emp.id, "hire", f"{title} / {emp_no}", hire))

    for emp_id, mgr_no in pending_manager:
        if mgr_no and mgr_no in emp_ids:
            employee_repo.update_employee(conn, emp_id, manager_id=emp_ids[mgr_no])

    # 4. 年度额度发放：当年入职按剩余月份折算，老员工按全年发放
    _grant_current_year(conn, emp_ids, type_ids)

    # 5. 请假单（构造不同状态，方便界面演示）
    _seed_requests(conn, emp_ids, type_ids)
    conn.commit()
    return conn


def _event(emp_id, etype, to_value, effective):
    from hrflow.domain.models import EmployeeEvent, EmployeeEventType
    return EmployeeEvent(emp_id=emp_id, event_type=EmployeeEventType.HIRE, to_value=to_value,
                         effective_date=datetime.strptime(effective, "%Y-%m-%d").date(),
                         remark="系统入职登记")


def _grant_current_year(conn, emp_ids, type_ids):
    year = date.today().year
    for emp_no, emp_id in emp_ids.items():
        hire_row = conn.execute("SELECT hire_date FROM employees WHERE id = ?", (emp_id,)).fetchone()
        hire = datetime.strptime(hire_row["hire_date"], "%Y-%m-%d").date()
        for code in ("annual", "sick"):
            lt_id = type_ids[code]
            quota = dict((lt.code, lt.annual_quota) for lt in LEAVE_TYPES)[code]
            existing = conn.execute(
                "SELECT id FROM leave_allocations WHERE emp_id=? AND leave_type_id=? AND year=?",
                (emp_id, lt_id, year)).fetchone()
            if existing:
                continue
            if hire.year == year:
                months = 12 - hire.month + 1
                days = round(quota * months / 12 * 2) / 2
            else:
                days = float(quota)
            if days > 0:
                leave_repo.add_allocation(conn, LeaveAllocation(
                    emp_id=emp_id, leave_type_id=lt_id, year=year,
                    days=days, source="annual_grant"))


def _d(offset_days: int, length: int = 0):
    """取一个"从今天起第 N 天往后、且含工作日"的请假区间，避免落在周末/节假日上。"""
    start = date.today() + timedelta(days=offset_days)
    while not leave_service.is_workday(start):
        start += timedelta(days=1)
    end = start + timedelta(days=length)
    while leave_service.calc_workdays(start, end) <= 0:
        end += timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _seed_requests(conn, emp_ids, type_ids):
    """造 6 张单据覆盖：审批中(一级/二级)、已通过、已驳回、已撤回。"""
    def submit(emp_no, code, start, end, reason, attachment=False):
        return leave_service.submit_request(conn, {
            "emp_id": emp_ids[emp_no], "leave_type_id": type_ids[code],
            "start_date": start, "end_date": end, "reason": reason, "attachment": attachment,
        })

    # 1) 王强 · 事假 1 天 —— 主管批准 → HR 复核 → 已通过（事假涉及无薪折算，必过 HR）
    s, e = _d(6)
    r1 = submit("E0003", "personal", s, e, "老家办事，需本人到场")
    leave_service.approve(conn, r1.id, ApproverRole.MANAGER, "张伟", "已确认工作交接")
    leave_service.approve(conn, r1.id, ApproverRole.HR, "孙芳", "已登记无薪扣款口径")

    # 2) 李娜 · 病假 2 天（带证明）—— 主管批准 → HR 复核中
    s, e = _d(4, 1)
    r2 = submit("E0002", "sick", s, e, "低烧就医，医生建议休息", attachment=True)
    leave_service.approve(conn, r2.id, ApproverRole.MANAGER, "张伟", "已安排同事顶上")

    # 3) 赵敏 · 年假 6 天 —— 超过 5 天，自动升级为三级审批（主管 → HR → 总监）
    s, e = _d(10, 7)
    submit("E0006", "annual", s, e, "陪家人外出")

    # 4) 刘洋 · 年假 2 天 —— 待主管审批（一级链）
    s, e = _d(8, 1)
    submit("E0004", "annual", s, e, "家里有事")

    # 5) 吴凯 · 年假 1 天 —— 主管 → HR 复核 → 已通过（年假涉及额度，必过 HR）
    s, e = _d(3)
    r5 = submit("E0010", "annual", s, e, "体检")
    leave_service.approve(conn, r5.id, ApproverRole.MANAGER, "林浩", "同意")
    leave_service.approve(conn, r5.id, ApproverRole.HR, "孙芳", "额度核对无误")

    # 6) 郑爽 · 年假 3 天 —— 被驳回
    s, e = _d(5, 3)
    r6 = submit("E0011", "annual", s, e, "临时外出")
    leave_service.reject(conn, r6.id, ApproverRole.MANAGER, "林浩", "与活动窗口冲突，请改期")

    # 7) 周涛 · 调休 1 天 —— 提交后自行撤回（不超过 2 天，只需主管一级审批）
    s, e = _d(12)
    r7 = submit("E0008", "compensatory", s, e, "上周加班调休")
    leave_service.cancel(conn, r7.id, "周涛", "时间调整，先不请了")

    # 提交时间往前挪 2~20 小时，让"平均审批时长"指标有真实数值可看
    conn.execute(
        """UPDATE approval_tasks
           SET acted_at = datetime(acted_at, '-' || (ABS(RANDOM()) % 19 + 2) || ' hours')
           WHERE action = 'submit'"""
    )


def main():
    enable_utf8()
    parser = argparse.ArgumentParser(description="生成演示数据")
    parser.add_argument("--keep", action="store_true", help="不重置数据库")
    parser.add_argument("--db", default=None, help="数据库路径")
    args = parser.parse_args()
    conn = seed(args.db, reset=not args.keep)
    n_emp = conn.execute("SELECT COUNT(*) c FROM employees").fetchone()["c"]
    n_req = conn.execute("SELECT COUNT(*) c FROM leave_requests").fetchone()["c"]
    print(f"演示数据已生成：员工 {n_emp} 人，请假单 {n_req} 张")


if __name__ == "__main__":
    main()
