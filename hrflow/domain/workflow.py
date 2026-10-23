"""通用单据状态机（审批流引擎）。

借鉴来源：
- frappe/hrms：审批流不写死在业务代码里，而是由 Workflow 配置（状态 + 转换 + 允许角色）
  驱动，业务代码只负责调用 `transition()`。
- horilla：审批链按角色组织，每一级留一条审批记录。

本文件不依赖任何具体业务，员工请假、加班、报销都可以复用同一套引擎。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set

from .errors import BusinessError


class WorkflowError(BusinessError):
    """非法的状态流转或越权操作。"""

    def __init__(self, message: str, code: str = "workflow_error"):
        super().__init__(message, code)


@dataclass(frozen=True)
class State:
    name: str
    label: str
    is_final: bool = False


@dataclass(frozen=True)
class Transition:
    """一次状态转换的定义。"""

    action: str          # 动作名，如 submit / approve / reject / cancel
    from_states: Set[str]
    to_state: str
    allowed_roles: Set[str] = field(default_factory=set)  # 空集表示不校验角色
    guard: Optional[Callable[[dict], None]] = None  # 额外业务前置校验


class StateMachine:
    """配置驱动的状态机。

    用法：
        sm = build_leave_state_machine()
        sm.transition("submit", {"status": "draft"}, actor_roles={"employee"})
    """

    def __init__(self, name: str, states: Iterable[State], transitions: Iterable[Transition]):
        self.name = name
        self.states: Dict[str, State] = {s.name: s for s in states}
        self.transitions: List[Transition] = list(transitions)
        self._index: Dict[str, List[Transition]] = {}
        for t in self.transitions:
            self._index.setdefault(t.action, []).append(t)

    def state(self, name: str) -> State:
        try:
            return self.states[name]
        except KeyError:
            raise WorkflowError(f"未知状态：{name}", "unknown_state")

    def available_actions(self, status: str, actor_roles: Set[str] = frozenset()) -> List[str]:
        """当前状态下该角色可以执行的动作，供前端按钮渲染使用。"""
        actions = []
        for t in self.transitions:
            if status in t.from_states and self._role_ok(t, actor_roles):
                actions.append(t.action)
        return sorted(set(actions))

    @staticmethod
    def _role_ok(t: Transition, actor_roles: Set[str]) -> bool:
        if not t.allowed_roles:
            return True
        return bool(actor_roles & t.allowed_roles)

    def transition(self, action: str, doc: dict, actor_roles: Set[str] = frozenset(),
                   context: Optional[dict] = None) -> str:
        """执行一次状态转换，返回转换后的状态。

        doc 需要提供 `status` 字段；校验通过后再由调用方持久化，保证"校验与落库"分离。
        """
        candidates = self._index.get(action)
        if not candidates:
            raise WorkflowError(f"未知动作：{action}", "unknown_action")

        current = doc.get("status")
        matched = [t for t in candidates if current in t.from_states]
        if not matched:
            label = self.state(current).label if current in self.states else current
            raise WorkflowError(f"当前状态（{label}）不能执行动作 {action}", "illegal_transition")

        allowed = [t for t in matched if self._role_ok(t, actor_roles)]
        if not allowed:
            raise WorkflowError("当前操作人无此审批权限", "permission_denied")

        transition = allowed[0]
        ctx = dict(context or {})
        ctx["doc"] = doc
        if transition.guard is not None:
            transition.guard(ctx)
        return transition.to_state


# --------------------------- 请假审批状态机 ---------------------------
#
#   draft ──submit──> pending ──approve(还有下一级)──> pending
#     │                 │
#     │                 ├──approve(最后一级)──> approved
#     │                 │
#     │                 └──reject──> rejected
#     └──cancel──> cancelled <──cancel──┘
#
from .models import ApproverRole, LeaveRequestStatus  # noqa: E402

STATES = [
    State(LeaveRequestStatus.DRAFT, "草稿"),
    State(LeaveRequestStatus.PENDING, "审批中"),
    State(LeaveRequestStatus.APPROVED, "已通过", is_final=True),
    State(LeaveRequestStatus.REJECTED, "已驳回", is_final=True),
    State(LeaveRequestStatus.CANCELLED, "已撤回", is_final=True),
]

ANY_ROLE = {ApproverRole.MANAGER, ApproverRole.HR, ApproverRole.DIRECTOR, "employee", "admin"}


def _guard_not_final(ctx: dict) -> None:
    doc = ctx["doc"]
    if doc.get("status") in LeaveRequestStatus.CLOSED:
        raise WorkflowError("单据已归档，不能再操作", "already_closed")


TRANSITIONS = [
    Transition("submit", {LeaveRequestStatus.DRAFT}, LeaveRequestStatus.PENDING, ANY_ROLE, _guard_not_final),
    # approve 的落地状态由服务层按审批链决定（还有下一级则仍是 pending），
    # 因此这里的目标状态是"当前状态"，真正的推进写在 LeaveService 里。
    Transition("approve", {LeaveRequestStatus.PENDING}, LeaveRequestStatus.PENDING,
               {ApproverRole.MANAGER, ApproverRole.HR, ApproverRole.DIRECTOR, "admin"}, _guard_not_final),
    Transition("reject", {LeaveRequestStatus.PENDING}, LeaveRequestStatus.REJECTED,
               {ApproverRole.MANAGER, ApproverRole.HR, ApproverRole.DIRECTOR, "admin"}, _guard_not_final),
    Transition("cancel", {LeaveRequestStatus.DRAFT, LeaveRequestStatus.PENDING},
               LeaveRequestStatus.CANCELLED, ANY_ROLE, _guard_not_final),
]


def build_leave_state_machine() -> StateMachine:
    return StateMachine("leave_request", STATES, TRANSITIONS)


# --------------------------- 审批链路由规则 ---------------------------
#
# 借鉴 frappe/hrms 的 Department Approver：审批人按"角色 + 条件"路由，
# 而不是在代码里写死某个人。这里用请假天数做条件，命中第一条即停止。
ROUTING_RULES = [
    {"max_days": 2, "chain": [ApproverRole.MANAGER]},
    {"max_days": 5, "chain": [ApproverRole.MANAGER, ApproverRole.HR]},
    {"max_days": 999, "chain": [ApproverRole.MANAGER, ApproverRole.HR, ApproverRole.DIRECTOR]},
]


def route_approval_chain(days: float, leave_type_code: str = "") -> List[str]:
    """根据请假天数路由审批链。事假、病假一律需要 HR 复核。"""
    base = [ApproverRole.MANAGER]
    for rule in ROUTING_RULES:
        if days <= rule["max_days"]:
            base = list(rule["chain"])
            break
    if leave_type_code in {"sick", "personal", "annual"} and ApproverRole.HR not in base:
        # 病假需要证明、事假涉及无薪折算、年假涉及额度，统一加 HR 复核
        base = [ApproverRole.MANAGER, ApproverRole.HR]
    return base
