"""领域实体定义。

设计说明：
1. 实体用 dataclass 表达，只描述"业务上是什么"，不关心怎么存、怎么展示；
2. 状态值统一使用常量，避免散落在各层的魔法字符串；
3. 借鉴 frappe/hrms：单据类实体（请假申请）与档案类实体（员工）分离，
   单据带状态机，档案带生命周期事件（入转调离）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional


# ----------------------------- 员工域 -----------------------------

class EmployeeStatus:
    """员工在职状态（借鉴 frappe/hrms 的 Employee.status）。"""

    PROBATION = "probation"   # 试用期
    ACTIVE = "active"         # 正式在职
    SUSPENDED = "suspended"   # 待岗/停薪留职
    RESIGNED = "resigned"     # 已离职

    LABELS = {
        PROBATION: "试用期",
        ACTIVE: "在职",
        SUSPENDED: "待岗",
        RESIGNED: "已离职",
    }

    ONBOARD = {PROBATION, ACTIVE, SUSPENDED}  # 在编人数口径


class EmployeeEventType:
    """入转调离：员工档案的每一次变动都留一条事件，保证可追溯。"""

    HIRE = "hire"            # 入职
    PROBATION_PASS = "regularize"  # 转正
    TRANSFER = "transfer"    # 调岗/调部门
    PROMOTION = "promotion"  # 晋升
    LEAVE = "resign"         # 离职

    LABELS = {
        HIRE: "入职",
        PROBATION_PASS: "转正",
        TRANSFER: "调动",
        PROMOTION: "晋升",
        LEAVE: "离职",
    }


@dataclass
class Department:
    id: Optional[int]
    name: str
    parent_id: Optional[int] = None


@dataclass
class Employee:
    id: Optional[int] = None
    emp_no: str = ""
    name: str = ""
    dept_id: Optional[int] = None
    dept_name: str = ""
    title: str = ""
    manager_id: Optional[int] = None
    manager_name: str = ""
    hire_date: Optional[date] = None
    status: str = EmployeeStatus.PROBATION
    email: str = ""
    phone: str = ""


@dataclass
class EmployeeEvent:
    """入转调离流水。from_value / to_value 记录变动前后值，便于审计。"""

    id: Optional[int] = None
    emp_id: int = 0
    event_type: str = ""
    from_value: str = ""
    to_value: str = ""
    effective_date: Optional[date] = None
    remark: str = ""


# ----------------------------- 请假域 -----------------------------

class LeaveRequestStatus:
    """请假单状态（与 domain/workflow.py 中的状态机一一对应）。"""

    DRAFT = "draft"        # 草稿
    PENDING = "pending"    # 审批中
    APPROVED = "approved"  # 已通过
    REJECTED = "rejected"  # 已驳回
    CANCELLED = "cancelled"  # 已撤回

    LABELS = {
        DRAFT: "草稿",
        PENDING: "审批中",
        APPROVED: "已通过",
        REJECTED: "已驳回",
        CANCELLED: "已撤回",
    }

    OPEN = {DRAFT, PENDING}          # 未结单据
    CLOSED = {APPROVED, REJECTED, CANCELLED}  # 终态
    # 计入已消耗假期的状态：只有真正通过才扣额度
    CONSUMING = {APPROVED}


class ApproverRole:
    """审批角色。审批链由角色组成，而不是写死具体某个员工。"""

    MANAGER = "manager"    # 直属上级
    HR = "hr"              # HR 复核
    DIRECTOR = "director"  # 部门总监

    LABELS = {
        MANAGER: "直属上级",
        HR: "HR 复核",
        DIRECTOR: "部门总监",
    }


@dataclass
class LeaveType:
    """假别。额度、上限、是否需要附件全部配置化，避免写死在代码里。"""

    id: Optional[int] = None
    code: str = ""
    name: str = ""
    annual_quota: float = 0.0      # 年度额度（天）
    max_days_per_request: float = 0.0   # 单次申请上限，0 表示不限制
    min_notice_days: int = 0       # 最少提前申请天数
    paid: bool = True              # 是否带薪
    require_attachment: bool = False  # 是否需要证明材料
    carry_over: bool = False       # 是否可跨年结转


@dataclass
class LeaveRequest:
    id: Optional[int] = None
    code: str = ""                 # 单号，如 LV-20261022-0003
    emp_id: int = 0
    emp_name: str = ""
    dept_name: str = ""
    leave_type_id: int = 0
    leave_type_name: str = ""
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    days: float = 0.0
    reason: str = ""
    attachment: bool = False
    status: str = LeaveRequestStatus.DRAFT
    approval_chain: list = field(default_factory=list)  # 如 ["manager", "hr"]
    current_step: int = 0          # 当前审批序号（0 起）
    created_at: str = ""
    updated_at: str = ""


@dataclass
class ApprovalTask:
    """审批留痕：谁、在第几步、什么时候、做了什么、说了什么。"""

    id: Optional[int] = None
    request_id: int = 0
    step: int = 0
    role: str = ""
    approver_name: str = ""
    action: str = ""          # submit / approve / reject / cancel
    from_status: str = ""
    to_status: str = ""
    comment: str = ""
    acted_at: str = ""


@dataclass
class LeaveAllocation:
    """年度假期发放记录（对标 frappe/hrms 的 Leave Allocation）。"""

    id: Optional[int] = None
    emp_id: int = 0
    leave_type_id: int = 0
    year: int = 0
    days: float = 0.0
    source: str = "annual_grant"   # annual_grant 年度发放 / manual 手工补 / carry_over 结转


@dataclass
class LeaveLedger:
    """假期账本流水（对标 frappe/hrms 的 Leave Ledger Entry）。

    余额不存字段，而是由 allocation 与 ledger 汇总得出，任何一笔消耗都能追溯到单据。
    """

    id: Optional[int] = None
    emp_id: int = 0
    leave_type_id: int = 0
    request_id: Optional[int] = None
    days: float = 0.0     # 正数为发放，负数为消耗
    reason: str = ""
    created_at: str = ""
