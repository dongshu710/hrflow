"""请假单智能预审（AI 辅助）。

设计思路：
- 主体是一个**确定性规则引擎**，产出结构化风险项。任何时候都能跑，不依赖网络；
- 如果配置了大模型密钥，再叠一层**自然语言建议**，帮审批人快速读懂风险；
- 任何情况下大模型都不参与"能不能批"的判断，只做提示，避免 AI 直接决策带来的合规风险。
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

from .. import config
from ..domain.models import LeaveRequestStatus
from ..repository import employee_repo, leave_repo
from . import leave_service

LEVEL_ORDER = {"high": 0, "medium": 1, "low": 2}


def review_request(conn: sqlite3.Connection, req_id: int) -> dict:
    """对一张请假单做预审，返回风险项与建议。"""
    row = conn.execute(
        """SELECT r.*, e.name AS emp_name, e.dept_id, lt.name AS leave_type_name, lt.code AS leave_type_code
           FROM leave_requests r
           JOIN employees e ON e.id = r.emp_id
           JOIN leave_types lt ON lt.id = r.leave_type_id
           WHERE r.id = ?""",
        (req_id,),
    ).fetchone()
    if not row:
        return {"items": [], "risk_level": "low", "summary": "单据不存在"}

    items = []
    items.append(_check_balance(conn, row))
    items.append(_check_team_conflict(conn, row))
    items.append(_check_bridge_holiday(row))
    items.append(_check_attachment(conn, row))
    items.append(_check_history(conn, row))
    items.append(_check_cross_month(row))
    items = [i for i in items if i]

    items.sort(key=lambda x: LEVEL_ORDER[x["level"]])
    level = items[0]["level"] if items else "low"

    result = {
        "request_id": req_id,
        "code": row["code"],
        "risk_level": level,
        "items": items,
        "summary": f"共 {len(items)} 项提示，最高等级：{ {'high': '高', 'medium': '中', 'low': '低'}[level] }",
    }

    if config.AI_REVIEW_ENABLED:
        advice, model = _llm_advice(row, items)
        if advice:
            result["ai_advice"] = advice
            result["ai_source"] = model
    return result


# ------------------------------ 规则 ------------------------------

def _check_balance(conn: sqlite3.Connection, row) -> dict | None:
    bal = leave_repo.get_balance(conn, row["emp_id"], row["leave_type_id"],
                                 int(row["start_date"][:4]))
    if bal["granted"] <= 0:
        return None
    if row["days"] > bal["balance"]:
        return {
            "level": "high", "rule": "balance",
            "title": "假期额度不足",
            "detail": f"{row['leave_type_name']}剩余 {bal['balance']} 天，本次申请 {row['days']} 天",
            "advice": "建议改为部分事假，或请申请人说明特殊情况后走特批",
        }
    if bal["balance"] - row["days"] <= 0:
        return {
            "level": "medium", "rule": "balance",
            "title": "本次将用完全部额度",
            "detail": f"余额 {bal['balance']} 天，用完后本年度不再有{row['leave_type_name']}",
            "advice": "提醒申请人预留额度应对突发情况",
        }
    return None


def _check_team_conflict(conn: sqlite3.Connection, row) -> dict | None:
    """同部门同期请假人数占比（借鉴排班冲突检测思路）。"""
    dept_id = row["dept_id"]
    if not dept_id:
        return None
    headcount = conn.execute(
        "SELECT COUNT(*) AS c FROM employees WHERE dept_id = ? AND status IN ('probation','active')",
        (dept_id,),
    ).fetchone()["c"]
    if headcount <= 1:
        return None
    overlapping = conn.execute(
        """SELECT COUNT(DISTINCT r.emp_id) AS c FROM leave_requests r
           JOIN employees e ON e.id = r.emp_id
           WHERE e.dept_id = ? AND r.status IN ('pending','approved')
             AND r.id != ? AND r.start_date <= ? AND r.end_date >= ?""",
        (dept_id, row["id"], row["end_date"], row["start_date"]),
    ).fetchone()["c"]
    ratio = overlapping / headcount
    if ratio >= config.TEAM_CONFLICT_RATIO:
        return {
            "level": "high" if ratio >= 0.5 else "medium",
            "rule": "team_conflict",
            "title": "同期请假集中",
            "detail": f"该部门共 {headcount} 人，同期另有 {overlapping} 人请假（占比 {ratio:.0%}）",
            "advice": "确认关键岗位是否有备份人，必要时请申请人错峰或交接后再批",
        }
    return None


def _check_bridge_holiday(row) -> dict | None:
    """是否形成"搭桥"长连休：请假区间紧邻周末或法定节假日。"""
    try:
        start = datetime.strptime(row["start_date"], "%Y-%m-%d").date()
        end = datetime.strptime(row["end_date"], "%Y-%m-%d").date()
    except ValueError:
        return None
    off = 0
    cursor = start - timedelta(days=1)   # 先看请假开始前紧邻的休息日
    while cursor >= start - timedelta(days=7):
        if not leave_service.is_workday(cursor):
            off += 1
            cursor -= timedelta(days=1)
        else:
            break
    cursor = end + timedelta(days=1)
    while cursor <= end + timedelta(days=7):
        if not leave_service.is_workday(cursor):
            off += 1
            cursor += timedelta(days=1)
        else:
            break
    if off >= 4:
        return {
            "level": "low",
            "rule": "bridge_leave",
            "title": "形成连休",
            "detail": f"本次请假与休息日相连，累计可连续休息 {row['days'] + off} 天",
            "advice": "属于正常休假安排，注意交接即可",
        }
    return None


def _check_attachment(conn: sqlite3.Connection, row) -> dict | None:
    if row["attachment"]:
        return None
    lt = leave_repo.get_leave_type(conn, row["leave_type_id"])
    if lt and lt.require_attachment:
        return {
            "level": "medium", "rule": "attachment",
            "title": "缺少证明材料",
            "detail": f"{row['leave_type_name']}要求上传证明材料，当前未上传",
            "advice": "请补充材料后再审批；病假超过 2 天原则上需提供医疗证明",
        }
    return None


def _check_history(conn: sqlite3.Connection, row) -> dict | None:
    recent = conn.execute(
        """SELECT COUNT(*) AS c, COALESCE(SUM(days), 0) AS d FROM leave_requests
           WHERE emp_id = ? AND status = 'approved' AND created_at >= ?""",
        (row["emp_id"], (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchone()
    if recent["c"] >= 3:
        return {
            "level": "medium", "rule": "frequency",
            "title": "近期请假偏频繁",
            "detail": f"近 90 天已通过 {recent['c']} 次，累计 {recent['d']} 天",
            "advice": "建议主管了解下实际原因，确认是否存在需要支持的困难",
        }
    return None


def _check_cross_month(row) -> dict | None:
    if row["start_date"][:7] != row["end_date"][:7]:
        return {
            "level": "low", "rule": "cross_month",
            "title": "跨月假期",
            "detail": f"{row['start_date']} 至 {row['end_date']} 跨月",
            "advice": "考勤核算时需按自然月拆分，已提示 HR 复核",
        }
    return None


# --------------------------- 大模型增强 ---------------------------

def _llm_advice(row, items: list) -> tuple:
    """可选的大模型建议：没有密钥、超时或出错时静默降级为纯规则输出。

    返回 (建议文本, 模型名)；降级时返回 ("", "")。
    """
    cfg = config.get_llm_config()
    if not cfg or not items:
        return "", ""

    prompt = (
        "你是公司 HR 的审批助理。下面是系统规则引擎对一张请假单的检查结果，"
        "请用 2-3 句话给出给审批人的中文处理建议，语气专业克制，不要复述字段，不要下最终结论。\n\n"
        f"员工：{row['emp_name']}　假别：{row['leave_type_name']}　"
        f"时间：{row['start_date']} 至 {row['end_date']}（{row['days']} 天）\n"
        f"事由：{row['reason'] or '未填写'}\n"
        "检查项：\n" + "\n".join(f"- [{i['level']}] {i['title']}：{i['detail']}" for i in items)
    )
    payload = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 300,
    }
    req = urllib.request.Request(
        f"{cfg['base_url']}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {cfg['api_key']}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip(), cfg["model"]
    except (urllib.error.URLError, KeyError, ValueError, TimeoutError, IndexError):
        return "", ""  # 网络不可用时静默降级，不影响主流程


def smart_review_for_draft(conn: sqlite3.Connection, emp_id: int, leave_type_id: int,
                           start_date: str, end_date: str, days: float) -> dict:
    """提交前的预审：不落库，只给提示，用于前端实时校验。"""
    items = []
    lt = leave_repo.get_leave_type(conn, leave_type_id)
    emp = employee_repo.get_employee(conn, emp_id)
    if not lt or not emp:
        return {"items": [], "risk_level": "low", "summary": ""}

    year = int(start_date[:4])
    bal = leave_repo.get_balance(conn, emp_id, leave_type_id, year)
    if bal["granted"] > 0:
        if days > bal["balance"]:
            items.append({
                "level": "high", "rule": "balance", "title": "假期额度不足",
                "detail": f"{lt.name}剩余 {bal['balance']} 天，本次 {days} 天",
                "advice": "提交将被系统拦截，请调整天数或改选其他假别",
            })
    if lt.require_attachment:
        items.append({
            "level": "medium", "rule": "attachment", "title": "需要证明材料",
            "detail": f"{lt.name}要求上传附件", "advice": "提交前请上传，否则会被退回",
        })
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        if (start - date.today()).days < lt.min_notice_days:
            items.append({
                "level": "medium", "rule": "notice", "title": "未满足提前申请天数",
                "detail": f"{lt.name}需提前 {lt.min_notice_days} 天申请",
                "advice": "请提前提交或与主管沟通",
            })
    except ValueError:
        pass
    level = items[0]["level"] if items else "low"
    items.sort(key=lambda x: LEVEL_ORDER[x["level"]])
    return {"items": items, "risk_level": level, "summary": f"{len(items)} 项提示"}
