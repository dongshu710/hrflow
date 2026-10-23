"""端到端演示：一条命令跑完"入职 → 请假 → 审批 → 落账 → 报表"全链路。

用法：
    python scripts/demo.py

演示用的是独立数据库 data/demo.db，不会影响 web 界面使用的 data/hrflow.db。
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hrflow.console import enable_utf8  # noqa: E402
from hrflow.domain.errors import BusinessError  # noqa: E402
from hrflow.domain.models import ApproverRole, EmployeeStatus  # noqa: E402
from hrflow.repository.db import DEFAULT_DB_PATH, connect  # noqa: E402
from hrflow.services import employee_service, leave_service, report_service, smart_review  # noqa: E402

DEMO_DB = os.path.join(os.path.dirname(DEFAULT_DB_PATH), "demo.db")

LINE = "─" * 68


def title(text: str) -> None:
    print(f"\n{LINE}\n{text}\n{LINE}")


def show(label: str, value) -> None:
    print(f"  {label:<22}{value}")


def next_workday(offset: int) -> date:
    d = date.today() + timedelta(days=offset)
    while not leave_service.is_workday(d):
        d += timedelta(days=1)
    return d


def main():
    enable_utf8()
    # 0. 准备演示库
    from scripts.seed import seed
    conn = seed(DEMO_DB, reset=True)
    conn = connect(DEMO_DB)

    title("0 · 组织现状（看板）")
    ov = report_service.overview(conn)
    for k, v in ov.items():
        show(k, v)

    # 1. 新同事入职
    title("1 · 新员工入职：建档 + 入转调离留痕 + 年假折算")
    dept = conn.execute("SELECT id FROM departments WHERE name = '技术部'").fetchone()
    mgr = conn.execute("SELECT id FROM employees WHERE emp_no = 'E0001'").fetchone()
    emp = employee_service.create_employee(conn, {
        "name": "林一鸣", "dept_id": dept["id"], "title": "数据分析师",
        "manager_id": mgr["id"], "hire_date": date.today().isoformat(),
        "email": "lin.yiming@example.com",
    })
    show("工号 / 姓名", f"{emp.emp_no} {emp.name}")
    show("状态", EmployeeStatus.LABELS[emp.status])
    bals = leave_service.balances(conn, emp.id)
    show("年假额度（当年折算）", f"{bals[0]['balance']} 天" if bals else "—")
    show("入转调离事件", f"{len(employee_service.employee_profile(conn, emp.id)['events'])} 条")

    # 2. 提交请假 —— 先看规则拦截
    title("2 · 提交请假：规则拦截演示")
    annual_id = conn.execute("SELECT id FROM leave_types WHERE code = 'annual'").fetchone()["id"]
    d1 = next_workday(1)  # 只提前 1 天，年假要求提前 3 天
    try:
        leave_service.submit_request(conn, {
            "emp_id": emp.id, "leave_type_id": annual_id,
            "start_date": d1.isoformat(), "end_date": d1.isoformat(), "reason": "临时有事"})
    except BusinessError as e:
        show("被拦截", f"[{e.code}] {e.message}")

    # 3. 正常提交
    title("3 · 正常提交：系统自动路由审批链")
    s = next_workday(10)
    e = s
    while leave_service.calc_workdays(s, e) < 2:   # 取 2 个工作日（新员工当年折算额度有限）
        e += timedelta(days=1)
    req = leave_service.submit_request(conn, {
        "emp_id": emp.id, "leave_type_id": annual_id,
        "start_date": s.isoformat(), "end_date": e.isoformat(),
        "reason": "回家处理户籍材料"})
    show("单号", req.code)
    show("请假区间", f"{req.start_date} ~ {req.end_date}")
    show("计假天数", f"{req.days} 天（已剔除周末与法定节假日）")
    show("审批链", " → ".join(ApproverRole.LABELS[r] for r in req.approval_chain))
    show("当前节点", ApproverRole.LABELS[req.approval_chain[req.current_step]])

    # 4. AI 预审
    title("4 · AI 预审（规则引擎为主，可叠加大模型建议）")
    review = smart_review.review_request(conn, req.id)
    show("风险等级", review["risk_level"])
    if not review["items"]:
        print("  · 无风险提示：额度充足、无同期冲突、材料齐全")
    for item in review["items"]:
        print(f"  · [{item['level']}] {item['title']}：{item['detail']}")
        print(f"      建议 → {item['advice']}")
    # 再看一张"有内容"的单据：种子数据里 6 天的长假
    long_req = conn.execute(
        "SELECT id FROM leave_requests WHERE days >= 5 ORDER BY id LIMIT 1").fetchone()
    if long_req:
        r = smart_review.review_request(conn, long_req["id"])
        print(f"  对照（{r['code']}，{r['risk_level']} 风险）：")
        if not r["items"]:
            print("    · 无风险提示")
        for item in r["items"]:
            print(f"    · [{item['level']}] {item['title']}：{item['detail']}")
    if review.get("ai_advice"):
        print(f"  大模型建议：{review['ai_advice']}")
    else:
        print("  大模型建议：未配置 LLM_API_KEY，已降级为纯规则引擎（功能不受影响）")

    # 5. 越权审批
    title("5 · 越权校验：当前是主管节点，HR 直接审批会被拒绝")
    try:
        leave_service.approve(conn, req.id, ApproverRole.HR, "孙芳", "HR 先批")
    except BusinessError as ex:
        show("被拦截", f"[{ex.code}] {ex.message}")

    # 6. 逐级审批
    title("6 · 逐级审批：主管 → HR → 通过并落账")
    before = leave_service.balances(conn, emp.id)[0]
    show("审批前年假余额", f"{before['balance']} 天（已用 {before['used']}，审批中 {before['pending']}）")

    leave_service.approve(conn, req.id, ApproverRole.MANAGER, "张伟", "已安排 A/B 角")
    mid = leave_service.get_request(conn, req.id)
    show("主管通过后", f"状态 {mid['status_label']}，进度 {mid['progress']}，待 {mid['current_role_label']}")

    leave_service.approve(conn, req.id, ApproverRole.HR, "孙芳", "额度与考勤口径核对无误")
    after = leave_service.get_request(conn, req.id)
    after_bal = leave_service.balances(conn, emp.id)[0]
    show("HR 通过后", f"状态 {after['status_label']}，进度 {after['progress']}")
    show("审批后年假余额", f"{after_bal['balance']} 天（已用 {after_bal['used']}）")
    show("账本流水", f"{len(after['tasks'])} 条审批留痕，额度扣减可追溯到单据 {after['code']}")

    # 7. 离职拦截
    title("7 · 离职校验：有未结单据时不允许办离职")
    # 年假已用完，改请事假（事假不计额度，但仍需主管 + HR 两级审批）
    personal_id = conn.execute("SELECT id FROM leave_types WHERE code = 'personal'").fetchone()["id"]
    d2 = next_workday(30)
    r2 = leave_service.submit_request(conn, {
        "emp_id": emp.id, "leave_type_id": personal_id,
        "start_date": d2.isoformat(), "end_date": d2.isoformat(), "reason": "体检"})
    show("新增单据", f"{r2.code}（{r2.status}）")
    try:
        employee_service.resign(conn, emp.id)
    except BusinessError as ex:
        show("被拦截", f"[{ex.code}] {ex.message}")
    leave_service.cancel(conn, r2.id, emp.name, "演示：先撤回")
    show("撤回后", f"{r2.code} → 已撤回，离职校验解除")

    # 8. 报表
    title("8 · 报表导出（CSV，Excel 可直接打开）")
    show("花名册", report_service.export_roster(conn))
    show("请假明细", report_service.export_leave_requests(conn))
    print("\n  部门请假统计：")
    for row in report_service.department_leave_stats(conn):
        print(f"    {row['dept_name']:<8} 单据 {row['requests']} 张，已通过 {row['approved_cnt']} 张 / {row['approved_days']} 天")

    print(f"\n{LINE}\n演示结束。数据库：{os.path.relpath(DEMO_DB)}\n{LINE}")


if __name__ == "__main__":
    main()
