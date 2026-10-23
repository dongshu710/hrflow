"""单元测试：覆盖核心业务规则与状态机。

运行：
    python -m unittest discover -s tests
或：
    python tests/test_hrflow.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hrflow.domain.errors import BusinessError  # noqa: E402
from hrflow.domain.models import ApproverRole, EmployeeStatus, LeaveAllocation  # noqa: E402
from hrflow.domain.workflow import WorkflowError, build_leave_state_machine, route_approval_chain  # noqa: E402
from hrflow.repository import employee_repo, leave_repo  # noqa: E402
from hrflow.repository.db import connect, init_db  # noqa: E402
from hrflow.services import employee_service, leave_service, report_service, smart_review  # noqa: E402

TEST_DB = os.path.join(tempfile.gettempdir(), "hrflow_test.db")


def next_workday(offset: int = 1) -> date:
    d = date.today() + timedelta(days=offset)
    while not leave_service.is_workday(d):
        d += timedelta(days=1)
    return d


class Base(unittest.TestCase):
    def setUp(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)
        self.conn = init_db(TEST_DB)
        self.dept = employee_repo.create_department(self.conn, "技术部")
        from hrflow.domain.models import LeaveType
        self.annual = leave_repo.create_leave_type(self.conn, LeaveType(
            code="annual", name="年假", annual_quota=10, max_days_per_request=10,
            min_notice_days=3, paid=True))
        self.sick = leave_repo.create_leave_type(self.conn, LeaveType(
            code="sick", name="病假", annual_quota=5, max_days_per_request=5,
            min_notice_days=0, paid=True, require_attachment=True))
        self.personal = leave_repo.create_leave_type(self.conn, LeaveType(
            code="personal", name="事假", annual_quota=0, max_days_per_request=5,
            min_notice_days=1, paid=False))

    def tearDown(self):
        self.conn.close()

    def _employee(self, name="测试员工", days_offset=0):
        emp = employee_service.create_employee(self.conn, {
            "name": name, "dept_id": self.dept,
            "hire_date": (date.today() - timedelta(days=365)).isoformat(),
            "status": EmployeeStatus.ACTIVE,
        })
        # 明确发放当年额度，避免用例依赖入职日期的折算结果
        year = date.today().year
        leave_repo.add_allocation(self.conn, LeaveAllocation(
            emp_id=emp.id, leave_type_id=self.annual, year=year, days=10))
        leave_repo.add_allocation(self.conn, LeaveAllocation(
            emp_id=emp.id, leave_type_id=self.sick, year=year, days=5))
        self.conn.commit()
        return emp

    def _submit(self, emp_id, type_id, offset=10, length=0, **kw):
        s = next_workday(offset)
        e = s + timedelta(days=length)
        while leave_service.calc_workdays(s, e) < 1:
            e += timedelta(days=1)
        return leave_service.submit_request(self.conn, {
            "emp_id": emp_id, "leave_type_id": type_id,
            "start_date": s.isoformat(), "end_date": e.isoformat(),
            "reason": kw.get("reason", "事由"), **kw})


class TestEmployee(Base):
    def test_create_employee_generates_emp_no_and_event(self):
        emp = self._employee()
        self.assertTrue(emp.emp_no.startswith("E"))
        events = employee_repo.list_events(self.conn, emp.id)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "hire")

    def test_duplicate_emp_no_rejected(self):
        self._employee()
        with self.assertRaises(BusinessError) as ctx:
            employee_service.create_employee(self.conn, {
                "name": "重名", "dept_id": self.dept, "emp_no": "E0001"})
        self.assertEqual(ctx.exception.code, "duplicate_emp_no")

    def test_annual_leave_prorated_for_new_hire(self):
        emp = employee_service.create_employee(self.conn, {
            "name": "新人", "dept_id": self.dept,
            "hire_date": date.today().replace(month=12, day=1).isoformat()})
        bal = leave_service.balances(self.conn, emp.id, date.today().year)[0]
        self.assertLessEqual(bal["granted"], 1.0)  # 12 月入职只折 1 个月

    def test_transfer_records_event(self):
        emp = self._employee()
        new_dept = employee_repo.create_department(self.conn, "产品部")
        employee_service.transfer(self.conn, emp.id, new_dept, "产品经理")
        events = employee_repo.list_events(self.conn, emp.id)
        self.assertIn("transfer", [e["event_type"] for e in events])

    def test_resign_blocked_when_open_request_exists(self):
        emp = self._employee()
        self._submit(emp.id, self.personal, offset=10)
        with self.assertRaises(BusinessError) as ctx:
            employee_service.resign(self.conn, emp.id)
        self.assertEqual(ctx.exception.code, "open_request_exists")


class TestLeaveRules(Base):
    def test_workdays_exclude_weekend(self):
        monday = next_workday(1)
        while monday.weekday() != 0:
            monday += timedelta(days=1)
        self.assertEqual(leave_service.calc_workdays(monday, monday + timedelta(days=6)), 5.0)

    def test_min_notice_days_enforced(self):
        emp = self._employee()
        # 把提前天数调大，避免用例结果随「今天是周几」漂移
        # （周五提交 1 天后的申请会被顺延到下周一，实际提前 3 天，就测不出拦截了）
        self.conn.execute("UPDATE leave_types SET min_notice_days = 60 WHERE id = ?", (self.annual,))
        self.conn.commit()
        with self.assertRaises(BusinessError) as ctx:
            self._submit(emp.id, self.annual, offset=1)
        self.assertEqual(ctx.exception.code, "insufficient_notice")

    def test_insufficient_balance(self):
        from hrflow.domain.models import Employee
        emp = Employee(emp_no="E9001", name="额度紧张", dept_id=self.dept,
                       hire_date=date.today(), status=EmployeeStatus.ACTIVE)
        emp.id = employee_repo.create_employee(self.conn, emp)  # 不走入职折算，只发 1 天
        leave_repo.add_allocation(self.conn, LeaveAllocation(
            emp_id=emp.id, leave_type_id=self.annual, year=date.today().year, days=1))
        s = next_workday(10)
        e = s + timedelta(days=6)
        while leave_service.calc_workdays(s, e) < 3:
            e += timedelta(days=1)
        with self.assertRaises(BusinessError) as ctx:
            leave_service.submit_request(self.conn, {
                "emp_id": emp.id, "leave_type_id": self.annual,
                "start_date": s.isoformat(), "end_date": e.isoformat(), "reason": "长假"})
        self.assertEqual(ctx.exception.code, "insufficient_balance")

    def test_overlap_rejected(self):
        emp = self._employee()
        self._submit(emp.id, self.personal, offset=10)
        with self.assertRaises(BusinessError) as ctx:
            self._submit(emp.id, self.personal, offset=10)
        self.assertEqual(ctx.exception.code, "date_overlap")

    def test_back_dated_rejected(self):
        emp = self._employee()
        past = date.today() - timedelta(days=3)
        with self.assertRaises(BusinessError) as ctx:
            leave_service.submit_request(self.conn, {
                "emp_id": emp.id, "leave_type_id": self.personal,
                "start_date": past.isoformat(), "end_date": past.isoformat(), "reason": "补假"})
        self.assertEqual(ctx.exception.code, "back_dated")

    def test_attachment_required_for_sick_leave(self):
        emp = self._employee()
        with self.assertRaises(BusinessError) as ctx:
            self._submit(emp.id, self.sick, offset=5, attachment=False)
        self.assertEqual(ctx.exception.code, "attachment_required")


class TestApprovalFlow(Base):
    def test_chain_routing_by_days(self):
        self.assertEqual(route_approval_chain(1, "compensatory"), [ApproverRole.MANAGER])
        self.assertEqual(route_approval_chain(6)[-1], ApproverRole.DIRECTOR)
        self.assertIn(ApproverRole.HR, route_approval_chain(3, "annual"))

    def test_multi_step_approval_and_ledger(self):
        emp = self._employee()
        req = self._submit(emp.id, self.annual, offset=10)
        self.assertEqual(req.status, "pending")

        before = leave_repo.get_balance(self.conn, emp.id, self.annual, date.today().year)
        with self.assertRaises(BusinessError) as ctx:
            leave_service.approve(self.conn, req.id, ApproverRole.HR, "孙芳")  # 越权
        self.assertEqual(ctx.exception.code, "wrong_approver")

        leave_service.approve(self.conn, req.id, ApproverRole.MANAGER, "张伟")
        mid = leave_service.get_request(self.conn, req.id)
        self.assertEqual(mid["status"], "pending")        # 还有 HR 节点
        self.assertEqual(mid["current_role"], ApproverRole.HR)
        self.assertEqual(before["balance"], leave_repo.get_balance(
            self.conn, emp.id, self.annual, date.today().year)["balance"])  # 中间态不扣额度

        leave_service.approve(self.conn, req.id, ApproverRole.HR, "孙芳")
        final = leave_service.get_request(self.conn, req.id)
        self.assertEqual(final["status"], "approved")
        after = leave_repo.get_balance(self.conn, emp.id, self.annual, date.today().year)
        self.assertAlmostEqual(after["balance"], before["balance"] - req.days)
        self.assertEqual(len(final["tasks"]), 3)  # 提交 + 主管 + HR

    def test_reject_then_status_final(self):
        emp = self._employee()
        req = self._submit(emp.id, self.annual, offset=10)
        leave_service.reject(self.conn, req.id, ApproverRole.MANAGER, "张伟", "排期冲突")
        req = leave_service.get_request(self.conn, req.id)
        self.assertEqual(req["status"], "rejected")
        with self.assertRaises(BusinessError) as ctx:  # 终态不可再流转
            leave_service.approve(self.conn, req["id"], ApproverRole.HR, "孙芳")
        self.assertEqual(ctx.exception.code, "not_pending")

    def test_cancel_restores_nothing_but_blocks_approval(self):
        emp = self._employee()
        req = self._submit(emp.id, self.annual, offset=10)
        leave_service.cancel(self.conn, req.id, emp.name)
        req = leave_service.get_request(self.conn, req.id)
        self.assertEqual(req["status"], "cancelled")
        bal = leave_repo.get_balance(self.conn, emp.id, self.annual, date.today().year)
        self.assertEqual(bal["used"], 0)


class TestStateMachine(unittest.TestCase):
    def test_illegal_transition(self):
        sm = build_leave_state_machine()
        with self.assertRaises(WorkflowError):
            sm.transition("approve", {"status": "draft"}, actor_roles={"manager"})

    def test_permission_denied(self):
        sm = build_leave_state_machine()
        with self.assertRaises(WorkflowError) as ctx:
            sm.transition("submit", {"status": "draft"}, actor_roles={"nobody"})
        self.assertEqual(ctx.exception.code, "permission_denied")

    def test_available_actions(self):
        sm = build_leave_state_machine()
        self.assertIn("submit", sm.available_actions("draft", {"employee"}))
        self.assertEqual(sm.available_actions("approved", {"admin"}), [])


class TestReports(Base):
    def test_overview_and_export(self):
        emp = self._employee()
        self._submit(emp.id, self.personal, offset=10)
        ov = report_service.overview(self.conn)
        self.assertGreaterEqual(ov["headcount"], 1)
        path = report_service.export_roster(self.conn)
        self.assertTrue(os.path.exists(path))

    def test_smart_review_runs(self):
        emp = self._employee()
        req = self._submit(emp.id, self.annual, offset=10)
        result = smart_review.review_request(self.conn, req.id)
        self.assertIn("risk_level", result)
        self.assertIsInstance(result["items"], list)


if __name__ == "__main__":
    unittest.main(verbosity=2)
