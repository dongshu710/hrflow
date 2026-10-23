"""极简 REST 服务（标准库实现，零第三方依赖）。

之所以不引框架：本项目定位是"可随时打开就跑的流程原型"，
一个文件能说明清楚的路由层，不值得为此拉进 FastAPI + Uvicorn 的运行负担。
如果后续要接正式环境，把 handler 换到 FastAPI 路由即可，service 层一行不用改。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ..domain.errors import BusinessError
from ..repository.db import DEFAULT_DB_PATH, connect, init_db
from ..services import employee_service, leave_service, report_service, smart_review

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WEB_DIR = os.path.join(BASE_DIR, "web")

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


# ------------------------------ 路由表 ------------------------------

ROUTES = []


def route(method: str, pattern: str):
    def decorator(func):
        ROUTES.append((method.upper(), re.compile("^" + pattern + "$"), func))
        return func
    return decorator


def _json_default(obj):
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    raise TypeError(f"not serializable: {type(obj)}")


def _ok(data) -> tuple:
    return 200, data


# 业务错误码 → HTTP 状态码。全部返回 400 会让调用方无法区分"参数错"和"没找到"。
_STATUS_BY_CODE = {
    "not_found": 404,
    "duplicate_emp_no": 409,
    "date_overlap": 409,
    "already_closed": 409,
    "permission_denied": 403,
    "wrong_approver": 403,
    "internal_error": 500,
}


def _fail(err: BusinessError) -> tuple:
    status = _STATUS_BY_CODE.get(err.code, 422)
    return status, {"error": err.message, "code": err.code, "details": err.details}


# ------------------------------ 接口实现 ------------------------------

@route("GET", r"/api/health")
def api_health(conn, m, body, q):
    """存活探针：连一次库，确认服务与数据文件都正常。"""
    conn.execute("SELECT 1").fetchone()
    return _ok({"status": "ok", "version": __import__("hrflow").__version__})


@route("GET", r"/api/overview")
def api_overview(conn, m, body, q):
    return _ok(report_service.overview(conn))


@route("GET", r"/api/departments")
def api_departments(conn, m, body, q):
    from ..repository import employee_repo
    return _ok([dict(r) for r in employee_repo.list_departments(conn)])


@route("GET", r"/api/employees")
def api_list_employees(conn, m, body, q):
    dept = q.get("dept_id", [""])[0]
    return _ok([employee_service.to_dict(e) for e in employee_service.list_employees(
        conn, keyword=q.get("keyword", [""])[0],
        dept_id=int(dept) if dept else None,
        status=q.get("status", [""])[0],
    )])


@route("POST", r"/api/employees")
def api_create_employee(conn, m, body, q):
    emp = employee_service.create_employee(conn, body)
    return _ok(employee_service.to_dict(emp))


@route("GET", r"/api/employees/(\d+)")
def api_get_employee(conn, m, body, q):
    return _ok(employee_service.employee_profile(conn, int(m.group(1))))


@route("POST", r"/api/employees/(\d+)/(regularize|transfer|resign)")
def api_employee_action(conn, m, body, q):
    emp_id, action = int(m.group(1)), m.group(2)
    if action == "regularize":
        emp = employee_service.regularize(conn, emp_id)
    elif action == "transfer":
        emp = employee_service.transfer(conn, emp_id, int(body.get("dept_id") or 0),
                                         body.get("title", ""))
    else:
        emp = employee_service.resign(conn, emp_id, remark=body.get("remark", ""))
    return _ok(employee_service.to_dict(emp))


@route("GET", r"/api/leave/types")
def api_leave_types(conn, m, body, q):
    return _ok(leave_service.leave_types(conn))


@route("GET", r"/api/leave/balances")
def api_balances(conn, m, body, q):
    emp_id = int(q.get("emp_id", ["0"])[0])
    return _ok(leave_service.balances(conn, emp_id))


@route("POST", r"/api/leave/precheck")
def api_precheck(conn, m, body, q):
    """提交前预审：不落库，实时提示。"""
    from ..domain.models import LeaveRequest
    start = body.get("start_date")
    end = body.get("end_date")
    days = leave_service.calc_workdays(
        datetime.strptime(start, "%Y-%m-%d").date(), datetime.strptime(end, "%Y-%m-%d").date())
    return _ok({
        "days": days,
        **smart_review.smart_review_for_draft(
            conn, int(body.get("emp_id")), int(body.get("leave_type_id")), start, end, days),
    })


@route("GET", r"/api/leave/requests")
def api_list_requests(conn, m, body, q):
    emp = q.get("emp_id", [""])[0]
    return _ok(leave_service.list_requests(
        conn, emp_id=int(emp) if emp else None, status=q.get("status", [""])[0]))


@route("POST", r"/api/leave/requests")
def api_create_request(conn, m, body, q):
    return _ok(leave_service._req_dict(conn, leave_service.submit_request(conn, body)))


@route("GET", r"/api/leave/requests/(\d+)")
def api_get_request(conn, m, body, q):
    return _ok(leave_service.get_request(conn, int(m.group(1))))


@route("POST", r"/api/leave/requests/(\d+)/(approve|reject|cancel)")
def api_request_action(conn, m, body, q):
    req_id, action = int(m.group(1)), m.group(2)
    if action == "approve":
        req = leave_service.approve(conn, req_id, body.get("role", ""),
                                    body.get("approver_name", ""), body.get("comment", ""))
    elif action == "reject":
        req = leave_service.reject(conn, req_id, body.get("role", ""),
                                   body.get("approver_name", ""), body.get("comment", ""))
    else:
        req = leave_service.cancel(conn, req_id, body.get("approver_name", ""),
                                    body.get("comment", ""))
    return _ok(leave_service._req_dict(conn, req, with_tasks=True))


@route("GET", r"/api/leave/requests/(\d+)/review")
def api_review(conn, m, body, q):
    return _ok(smart_review.review_request(conn, int(m.group(1))))


@route("GET", r"/api/reports/departments")
def api_dept_stats(conn, m, body, q):
    return _ok(report_service.department_leave_stats(conn))


@route("GET", r"/api/reports/monthly")
def api_monthly_stats(conn, m, body, q):
    return _ok(report_service.monthly_leave_stats(conn))


@route("POST", r"/api/reports/export/(roster|leave)")
def api_export(conn, m, body, q):
    kind = m.group(1)
    path = report_service.export_roster(conn) if kind == "roster" \
        else report_service.export_leave_requests(conn)
    return _ok({"path": path, "filename": os.path.basename(path)})


# --------------------------- HTTP 处理器 ---------------------------

class Handler(BaseHTTPRequestHandler):
    db_path = DEFAULT_DB_PATH
    quiet = False

    def log_message(self, fmt, *args):  # 精简日志，避免演示时刷屏
        if not self.quiet:
            print(f"[{self.log_date_time_string()}] {fmt % args}")

    # ---- 请求分发 ----
    def _dispatch(self, method: str):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        body = self._read_body()

        if not path.startswith("/api/"):
            return self._serve_static(path)

        with closing(connect(self.db_path)) as conn:
            try:
                for m, pattern, func in ROUTES:
                    if m != method:
                        continue
                    match = pattern.match(path)
                    if match:
                        status, payload = func(conn, match, body, query)
                        return self._send_json(status, payload)
            except BusinessError as e:
                return self._send_json(*_fail(e))
            except Exception as e:  # noqa: BLE001
                return self._send_json(500, {"error": f"服务异常：{e}", "code": "internal_error"})
        return self._send_json(404, {"error": "接口不存在", "code": "not_found"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    # ---- 工具 ----
    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, status: int, payload):
        data = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, path: str):
        if path in ("/", ""):
            path = "/index.html"
        file_path = os.path.join(WEB_DIR, path.lstrip("/"))
        file_path = os.path.normpath(file_path)
        if not file_path.startswith(WEB_DIR) or not os.path.isfile(file_path):
            self.send_error(404, "not found")
            return
        ext = os.path.splitext(file_path)[1].lower()
        with open(file_path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _init_and_close(db_path: str) -> None:
    """建表后立刻关闭连接：Windows 上被占用的 db 文件无法被 seed 脚本删除重建。"""
    init_db(db_path).close()


def create_server(db_path: str = DEFAULT_DB_PATH, host: str = "127.0.0.1",
                  port: int = 8770, quiet: bool = False) -> ThreadingHTTPServer:
    _init_and_close(db_path)
    handler = type("BoundHandler", (Handler,), {"db_path": db_path, "quiet": quiet})
    return ThreadingHTTPServer((host, port), handler)


def run(host: str = "127.0.0.1", port: int = 8770, db_path: str = DEFAULT_DB_PATH,
        quiet: bool = False) -> None:
    server = create_server(db_path, host, port, quiet)
    print(f"人事流程 mini 版已启动：http://{host}:{port}")
    print(f"数据库：{db_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        server.server_close()
