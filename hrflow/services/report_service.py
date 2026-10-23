"""报表与看板：花名册、请假明细、部门统计、审批效率。

导出统一用 CSV（Excel 可直接打开），保证零依赖。
"""

from __future__ import annotations

import csv
import os
import sqlite3
from datetime import datetime

from ..domain.models import EmployeeStatus
from ..repository import employee_repo, leave_repo

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXPORT_DIR = os.path.join(BASE_DIR, "data", "exports")


def overview(conn: sqlite3.Connection) -> dict:
    """首页看板指标。"""
    today = datetime.now()
    month_prefix = today.strftime("%Y-%m")

    headcount = conn.execute(
        "SELECT COUNT(*) AS c FROM employees WHERE status IN ('probation','active')"
    ).fetchone()["c"]
    probation = conn.execute(
        "SELECT COUNT(*) AS c FROM employees WHERE status = 'probation'"
    ).fetchone()["c"]
    new_hire = conn.execute(
        "SELECT COUNT(*) AS c FROM employees WHERE substr(hire_date, 1, 7) = ?", (month_prefix,)
    ).fetchone()["c"]
    pending = conn.execute(
        "SELECT COUNT(*) AS c FROM leave_requests WHERE status = 'pending'"
    ).fetchone()["c"]
    approved_month = conn.execute(
        "SELECT COUNT(*) AS c FROM leave_requests WHERE status = 'approved' AND substr(updated_at, 1, 7) = ?",
        (month_prefix,),
    ).fetchone()["c"]

    return {
        "org_name": _org_name(),
        "headcount": headcount,
        "probation": probation,
        "new_hire_this_month": new_hire,
        "pending_requests": pending,
        "approved_this_month": approved_month,
        "avg_approval_hours": avg_approval_hours(conn),
        "generated_at": today.strftime("%Y-%m-%d %H:%M"),
    }


def _org_name() -> str:
    from .. import config
    return config.ORG_NAME


def avg_approval_hours(conn: sqlite3.Connection) -> float:
    """平均审批时长：从提交到终审动作的平均小时数。

    这是衡量"流程顺不顺"的核心指标，也是做流程优化前后对比的依据。
    """
    rows = conn.execute(
        """SELECT request_id,
                  MIN(CASE WHEN action = 'submit' THEN acted_at END) AS t0,
                  MAX(CASE WHEN action IN ('approve','reject') THEN acted_at END) AS t1
           FROM approval_tasks GROUP BY request_id"""
    ).fetchall()
    durations = []
    for r in rows:
        if not r["t0"] or not r["t1"]:
            continue
        try:
            delta = datetime.strptime(r["t1"], "%Y-%m-%d %H:%M:%S") - \
                datetime.strptime(r["t0"], "%Y-%m-%d %H:%M:%S")
            durations.append(delta.total_seconds() / 3600)
        except ValueError:
            continue
    return round(sum(durations) / len(durations), 1) if durations else 0.0


def department_leave_stats(conn: sqlite3.Connection) -> list:
    """按部门统计请假人次与天数。"""
    return [dict(r) for r in conn.execute(
        """SELECT COALESCE(d.name, '未分配') AS dept_name,
                  COUNT(*) AS requests,
                  SUM(CASE WHEN r.status = 'approved' THEN 1 ELSE 0 END) AS approved_cnt,
                  ROUND(SUM(CASE WHEN r.status = 'approved' THEN r.days ELSE 0 END), 1) AS approved_days
           FROM leave_requests r
           JOIN employees e ON e.id = r.emp_id
           LEFT JOIN departments d ON d.id = e.dept_id
           GROUP BY d.name ORDER BY approved_days DESC"""
    ).fetchall()]


def monthly_leave_stats(conn: sqlite3.Connection) -> list:
    return [dict(r) for r in conn.execute(
        """SELECT substr(start_date, 1, 7) AS month,
                  COUNT(*) AS requests,
                  ROUND(SUM(days), 1) AS days
           FROM leave_requests WHERE status = 'approved'
           GROUP BY month ORDER BY month"""
    ).fetchall()]


def export_roster(conn: sqlite3.Connection) -> str:
    """导出花名册 CSV，返回文件路径。"""
    os.makedirs(EXPORT_DIR, exist_ok=True)
    path = os.path.join(EXPORT_DIR, f"roster_{datetime.now():%Y%m%d}.csv")
    rows = employee_repo.list_employees(conn)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["工号", "姓名", "部门", "岗位", "直属上级", "入职日期", "状态", "邮箱", "手机"])
        for e in rows:
            w.writerow([
                e.emp_no, e.name, e.dept_name, e.title, e.manager_name,
                e.hire_date.isoformat() if e.hire_date else "",
                EmployeeStatus.LABELS.get(e.status, e.status), e.email, e.phone,
            ])
    return path


def export_leave_requests(conn: sqlite3.Connection) -> str:
    """导出请假明细 CSV（含审批链与审批进度），供月度核对。"""
    os.makedirs(EXPORT_DIR, exist_ok=True)
    path = os.path.join(EXPORT_DIR, f"leave_requests_{datetime.now():%Y%m%d}.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["单号", "姓名", "部门", "假别", "开始", "结束", "天数", "状态",
                    "审批链", "当前节点", "事由"])
        for r in leave_repo.list_requests(conn):
            w.writerow([
                r.code, r.emp_name, r.dept_name, r.leave_type_name,
                r.start_date.isoformat(), r.end_date.isoformat(), r.days,
                {"draft": "草稿", "pending": "审批中", "approved": "已通过",
                 "rejected": "已驳回", "cancelled": "已撤回"}.get(r.status, r.status),
                "→".join(r.approval_chain), r.current_step, r.reason,
            ])
    return path
