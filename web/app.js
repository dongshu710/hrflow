/* hrflow 前端：原生 JS，无框架无构建。
   职责只有三件事：取数、渲染、绑定交互；业务规则全部在后端。 */

const $ = (id) => document.getElementById(id);
const state = { employees: [], types: [], depts: [], currentReq: null };

const LEVEL_LABEL = { high: "高", medium: "中", low: "低" };

/* ------------------------------ 基础工具 ------------------------------ */

async function api(path, method = "GET", body) {
  const opt = { method, headers: {} };
  if (body) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const res = await fetch(path, opt);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "请求失败");
  return data;
}

const esc = (v) => String(v ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const fmt = (n) => {
  const v = Number(n ?? 0);
  return Number.isInteger(v) ? String(v) : v.toFixed(1);
};

const pill = (cls, text) => `<span class="pill ${esc(cls)}">${esc(text)}</span>`;

const fact = (k, v) => `<div><div class="k">${esc(k)}</div><div class="v">${v}</div></div>`;

const emptyRow = (cols, text) =>
  `<tr><td class="cell-wide" colspan="${cols}"><div class="empty">${esc(text)}</div></td></tr>`;

let toastTimer;
function toast(message, isError) {
  const el = $("toast");
  el.textContent = message;
  el.style.background = isError ? "#b42318" : "#101828";
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
}

/* ------------------------------ 弹窗 ------------------------------ */

function openModal({ title, body, foot }) {
  $("modalTitle").textContent = title;
  $("modalBody").innerHTML = body;
  $("modalFoot").innerHTML = foot || `<button class="btn" data-close>关闭</button>`;
  $("modal").hidden = false;
  $("modalBody").scrollTop = 0;
  document.body.style.overflow = "hidden";
}

function closeModal() {
  $("modal").hidden = true;
  $("modalFoot").innerHTML = "";
  $("modalBody").innerHTML = "";
  document.body.style.overflow = "";
  state.currentReq = null;
}

/* ------------------------------ 指标 ------------------------------ */

async function loadOverview() {
  const ov = await api("/api/overview");
  $("orgName").textContent = ov.org_name;
  $("orgUpdated").textContent = `${ov.generated_at.slice(11, 16)} 更新`;

  const items = [
    { label: "在职人数", value: ov.headcount, unit: "人", meta: `试用期 ${ov.probation} 人` },
    {
      label: "待审批", value: ov.pending_requests, unit: "张",
      meta: ov.pending_requests ? "待处理" : "暂无待办", alert: ov.pending_requests > 0,
    },
    { label: "本月通过", value: ov.approved_this_month, unit: "张", meta: "本月累计" },
    { label: "平均审批时长", value: fmt(ov.avg_approval_hours), unit: "小时", meta: "提交到终审" },
  ];

  $("cards").innerHTML = items.map((i) => `
    <div class="kpi${i.alert ? " is-alert" : ""}">
      <div class="kpi-label">${esc(i.label)}</div>
      <div class="kpi-value">${esc(i.value)}<span>${esc(i.unit)}</span></div>
      <div class="kpi-meta">${esc(i.meta)}</div>
    </div>`).join("");

  const badge = $("pendingCount");
  badge.textContent = ov.pending_requests > 0 ? ov.pending_requests : "";
  badge.classList.toggle("on", ov.pending_requests > 0);
}

/* ------------------------------ 花名册 ------------------------------ */

async function loadEmployees() {
  const q = new URLSearchParams({
    keyword: $("kw").value.trim(), status: $("statusFilter").value,
  });
  state.employees = await api(`/api/employees?${q}`);

  const active = state.employees.filter((e) => e.status === "active").length;
  const probation = state.employees.filter((e) => e.status === "probation").length;
  $("empMeta").textContent = `${state.employees.length} 人 · 在职 ${active} · 试用期 ${probation}`;

  $("empRows").innerHTML = state.employees.length
    ? state.employees.map(empRow).join("")
    : emptyRow(8, "没有匹配的员工");
}

function empRow(e) {
  const actions = [
    `<button class="btn-link" data-emp="${e.id}">详情</button>`,
    e.status === "probation" ? `<button class="btn-link" data-regular="${e.id}">转正</button>` : "",
    e.status !== "resigned" ? `<button class="btn-link danger" data-resign="${e.id}">离职</button>` : "",
  ].join("");

  return `<tr>
    <td data-label="工号" class="hide-sm">${esc(e.emp_no)}</td>
    <td data-label="姓名" class="cell-title">${esc(e.name)}<span class="cell-sub only-sm">${esc(e.emp_no)}</span></td>
    <td data-label="部门">${esc(e.dept_name || "-")}</td>
    <td data-label="岗位">${esc(e.title || "-")}</td>
    <td data-label="直属上级">${esc(e.manager_name || "-")}</td>
    <td data-label="入职日期" class="hide-sm">${esc(e.hire_date || "-")}</td>
    <td data-label="状态">${pill(e.status, e.status_label)}</td>
    <td class="cell-actions">${actions}</td>
  </tr>`;
}

async function openEmployee(id) {
  const p = await api(`/api/employees/${id}`);
  const e = p.employee;

  const facts = `<div class="facts">
    ${fact("工号", esc(e.emp_no))}
    ${fact("部门", esc(e.dept_name || "-"))}
    ${fact("岗位", esc(e.title || "-"))}
    ${fact("直属上级", esc(e.manager_name || "-"))}
    ${fact("入职日期", esc(e.hire_date || "-"))}
    ${fact("状态", pill(e.status, e.status_label))}
  </div>`;

  const balances = p.balances.length
    ? p.balances.map(balItem).join("")
    : `<div class="empty">暂无额度记录</div>`;

  const events = p.events.length
    ? `<ol class="timeline">${p.events.map(eventItem).join("")}</ol>`
    : `<div class="empty">暂无变动记录</div>`;

  openModal({
    title: `${e.name} · ${e.emp_no}`,
    body: `${facts}
      <p class="section-label">假期余额</p>${balances}
      <p class="section-label">入转调离</p>${events}`,
  });
}

function balItem(b) {
  const pct = b.granted > 0 ? Math.min(100, (b.used / b.granted) * 100) : 0;
  return `<div class="bal">
    <div class="bal-top">
      <span class="bal-name">${esc(b.name)}</span>
      <span class="bal-num"><b>${fmt(b.balance)}</b> / ${fmt(b.granted)} 天</span>
    </div>
    <div class="bal-bar"><i style="width:${pct}%"></i></div>
    <div class="bal-meta">已用 ${fmt(b.used)}${b.pending ? ` · 审批中 ${fmt(b.pending)}` : ""}</div>
  </div>`;
}

function eventItem(row) {
  const detail = [row.from_value ? `${row.from_value} → ${row.to_value}` : row.to_value, row.remark]
    .filter(Boolean).join(" · ");
  return `<li>
    <span class="tl-dot"></span>
    <div class="tl-body">
      <div class="tl-head"><b>${esc(row.event_label)}</b><span class="tl-time">${esc((row.effective_date || "").slice(0, 10))}</span></div>
      <div class="tl-text">${esc(detail || "-")}</div>
    </div>
  </li>`;
}

function newEmployee() {
  const fields = [
    { name: "name", label: "姓名", type: "text", placeholder: "必填" },
    { name: "dept_id", label: "部门", type: "select", options: state.depts.map((d) => [d.id, d.name]) },
    { name: "title", label: "岗位", type: "text" },
    {
      name: "manager_id", label: "直属上级", type: "select",
      options: [["", "无"]].concat(state.employees.map((e) => [e.id, `${e.name} · ${e.title || "-"}`])),
    },
    { name: "hire_date", label: "入职日期", type: "date", value: new Date().toISOString().slice(0, 10) },
    { name: "email", label: "邮箱", type: "text" },
  ];

  const body = `<div class="form-grid">${fields.map((f) => `
    <label class="field">
      <span>${esc(f.label)}</span>
      ${f.type === "select"
        ? `<select data-name="${f.name}">${f.options.map(([v, t]) =>
            `<option value="${esc(v)}">${esc(t)}</option>`).join("")}</select>`
        : `<input type="${f.type}" data-name="${f.name}" value="${esc(f.value || "")}"
            placeholder="${esc(f.placeholder || "")}">`}
    </label>`).join("")}</div>`;

  openModal({
    title: "新员工入职",
    body,
    foot: `<button class="btn" data-close>取消</button><button class="btn primary" id="btnCreate">确认入职</button>`,
  });

  $("btnCreate").onclick = async () => {
    const value = {};
    $("modalBody").querySelectorAll("[data-name]").forEach((el) => { value[el.dataset.name] = el.value; });
    if (!value.name.trim()) return toast("姓名不能为空", true);
    try {
      await api("/api/employees", "POST", {
        name: value.name.trim(),
        dept_id: Number(value.dept_id),
        title: value.title.trim(),
        manager_id: value.manager_id ? Number(value.manager_id) : null,
        hire_date: value.hire_date,
        email: value.email.trim(),
      });
      closeModal();
      toast("已建档，年假按入职月份折算");
      await Promise.all([loadEmployees(), loadOverview(), loadSelectors()]);
    } catch (err) {
      toast(err.message, true);
    }
  };
}

/* ------------------------------ 发起请假 ------------------------------ */

async function loadSelectors() {
  [state.types, state.depts] = await Promise.all([
    api("/api/leave/types"), api("/api/departments"),
  ]);

  $("fType").innerHTML = state.types.map((t) =>
    `<option value="${t.id}">${esc(t.name)}${t.annual_quota ? ` · 额度 ${fmt(t.annual_quota)} 天` : ""}</option>`
  ).join("");

  const employees = await api("/api/employees");
  $("fEmp").innerHTML = employees
    .filter((e) => e.status !== "resigned")
    .map((e) => `<option value="${e.id}">${esc(e.name)} · ${esc(e.dept_name || "-")}</option>`)
    .join("");

  const d = new Date(Date.now() + 7 * 864e5).toISOString().slice(0, 10);
  $("fStart").value = $("fStart").value || d;
  $("fEnd").value = $("fEnd").value || d;
}

async function precheck(showResult) {
  const start = $("fStart").value;
  const end = $("fEnd").value;
  if (!start || !end) return;
  try {
    const r = await api("/api/leave/precheck", "POST", {
      emp_id: Number($("fEmp").value),
      leave_type_id: Number($("fType").value),
      start_date: start, end_date: end, reason: $("fReason").value,
    });
    $("calcHint").textContent = `计假 ${fmt(r.days)} 天，已剔除周末与法定节假日`;
    $("precheckResult").innerHTML = showResult ? reviewHTML(r) : "";
  } catch (err) {
    $("calcHint").textContent = err.message;
    $("precheckResult").innerHTML = "";
  }
}

async function submitLeave() {
  $("applyMsg").className = "msg";
  $("applyMsg").textContent = "";
  try {
    const r = await api("/api/leave/requests", "POST", {
      emp_id: Number($("fEmp").value),
      leave_type_id: Number($("fType").value),
      start_date: $("fStart").value,
      end_date: $("fEnd").value,
      attachment: $("fAttach").value === "1",
      reason: $("fReason").value,
    });
    $("precheckResult").innerHTML = "";
    $("applyMsg").className = "msg ok";
    $("applyMsg").textContent =
      `${r.code} 已提交 · 审批链 ${r.approval_chain_labels.join(" → ")} · 当前 ${r.current_role_label || "已完成"}`;
    await Promise.all([loadMyRequests(), loadRequests(), loadOverview()]);
  } catch (err) {
    $("applyMsg").className = "msg err";
    $("applyMsg").textContent = err.message;
  }
}

async function loadMyRequests() {
  const list = await api("/api/leave/requests");
  $("myRows").innerHTML = list.length
    ? list.slice(0, 12).map((r) => `
      <tr>
        <td data-label="单号" class="hide-sm">${esc(r.code)}</td>
        <td data-label="申请人" class="cell-title">${esc(r.emp_name)}<span class="cell-sub only-sm">${esc(r.code)}</span></td>
        <td data-label="假别">${esc(r.leave_type_name)}</td>
        <td data-label="请假区间" class="cell-wide">${esc(r.start_date)} ~ ${esc(r.end_date)}</td>
        <td data-label="天数" class="num">${fmt(r.days)}</td>
        <td data-label="状态">${pill(r.status, r.status_label)}</td>
        <td data-label="进度">${esc(r.progress)}</td>
      </tr>`).join("")
    : emptyRow(7, "还没有请假记录");
}

/* ------------------------------ 审批台 ------------------------------ */

async function loadRequests() {
  const status = $("reqFilter").value;
  const role = $("actorRole").value;
  const list = await api("/api/leave/requests" + (status ? `?status=${status}` : ""));

  $("reqRows").innerHTML = list.length
    ? list.map((r) => {
        const mine = r.status === "pending" && r.current_role === role;
        return `<tr>
          <td data-label="单号" class="hide-sm">${esc(r.code)}</td>
          <td data-label="申请人" class="cell-title">${esc(r.emp_name)}<span class="cell-sub only-sm">${esc(r.code)}</span></td>
          <td data-label="部门" class="hide-sm">${esc(r.dept_name || "-")}</td>
          <td data-label="假别">${esc(r.leave_type_name)}</td>
          <td data-label="请假区间" class="cell-wide">${esc(r.start_date)} ~ ${esc(r.end_date)}</td>
          <td data-label="天数" class="num">${fmt(r.days)}</td>
          <td data-label="状态">${pill(r.status, r.status_label)}</td>
          <td data-label="当前节点">${esc(r.current_role_label || "-")}${mine ? `<span class="mine">待我处理</span>` : ""}</td>
          <td class="cell-actions"><button class="btn-link" data-req="${r.id}">详情</button></td>
        </tr>`;
      }).join("")
    : emptyRow(9, "当前筛选下没有单据");
}

async function openRequest(id) {
  const [r, review] = await Promise.all([
    api(`/api/leave/requests/${id}`),
    api(`/api/leave/requests/${id}/review`),
  ]);
  state.currentReq = r;

  const facts = `<div class="facts">
    ${fact("申请人", esc(r.emp_name))}
    ${fact("部门", esc(r.dept_name || "-"))}
    ${fact("假别", esc(r.leave_type_name))}
    ${fact("请假天数", `${fmt(r.days)} 天`)}
    ${fact("状态", pill(r.status, r.status_label))}
    ${fact("请假区间", `${esc(r.start_date)} ~ ${esc(r.end_date)}`)}
  </div>`;

  const tasks = r.tasks.length
    ? `<ol class="timeline">${r.tasks.map(taskItem).join("")}</ol>`
    : `<div class="empty">暂无审批记录</div>`;

  openModal({
    title: `${r.code} · ${r.emp_name}`,
    body: `${facts}
      <p class="section-label">审批链</p>${chainHTML(r)}
      <p class="section-label">智能预审</p>
      <div class="review">${reviewHTML(review) || `<div class="empty">暂无预审结果</div>`}</div>
      <p class="section-label">审批轨迹</p>${tasks}
      ${r.reason ? `<p class="section-label">申请事由</p><div class="reason">${esc(r.reason)}</div>` : ""}`,
    foot: requestFoot(r),
  });

  bindRequestActions(r);
}

function chainHTML(r) {
  const steps = r.approval_chain_labels;
  return `<div class="chain">${steps.map((label, i) => {
    let cls = "todo";
    if (r.status === "approved") cls = "done";
    else if (r.status === "rejected" && i === r.current_step) cls = "fail";
    else if (i < r.current_step) cls = "done";
    else if (i === r.current_step && r.status === "pending") cls = "current";
    return `<span class="chain-step ${cls}"><i></i>${esc(label)}</span>`;
  }).join(`<span class="chain-sep"></span>`)}</div>`;
}

function taskItem(t) {
  const actionLabel = { submit: "提交", approve: "同意", reject: "驳回", cancel: "撤回" }[t.action] || t.action;
  return `<li>
    <span class="tl-dot ${esc(t.action)}"></span>
    <div class="tl-body">
      <div class="tl-head">
        <b>${esc(actionLabel)} · ${esc(t.approver_name || t.role_label)}</b>
        <span class="tl-time">${esc((t.acted_at || "").slice(5, 16))}</span>
      </div>
      <div class="tl-text">${esc(t.comment || "-")}</div>
    </div>
  </li>`;
}

function requestFoot(r) {
  if (r.status !== "pending") return `<button class="btn" data-close>关闭</button>`;
  const mine = r.current_role === $("actorRole").value;
  return `<input class="input grow" id="comment" placeholder="审批意见（可选）">
    <button class="btn" id="btnCancel">撤回</button>
    <button class="btn danger" id="btnReject"${mine ? "" : " disabled"}>驳回</button>
    <button class="btn primary" id="btnApprove"${mine ? "" : " disabled"}>同意</button>`;
}

function bindRequestActions(r) {
  if (r.status !== "pending") return;
  $("btnApprove").onclick = () => actRequest("approve");
  $("btnReject").onclick = () => actRequest("reject");
  $("btnCancel").onclick = () => actRequest("cancel");
}

async function actRequest(action) {
  const r = state.currentReq;
  if (!r) return;
  const roleEl = $("actorRole");
  try {
    const updated = await api(`/api/leave/requests/${r.id}/${action}`, "POST", {
      role: roleEl.value,
      approver_name: roleEl.selectedOptions[0].text,
      comment: ($("comment") && $("comment").value.trim()) || "",
    });
    toast(`${updated.code} · ${updated.status_label}`);
    await Promise.all([loadRequests(), loadMyRequests(), loadOverview()]);
    await openRequest(r.id);
  } catch (err) {
    toast(err.message, true);
  }
}

function reviewHTML(review) {
  if (!review || !review.items) return "";
  const items = review.items.length
    ? review.items.map((i) => `<div class="review-item ${esc(i.level)}">
        <div class="rt"><span class="lv">${esc(LEVEL_LABEL[i.level] || "")}</span>${esc(i.title)}</div>
        <div class="rd">${esc(i.detail)}</div>
        <div class="ra">建议 · ${esc(i.advice)}</div>
      </div>`).join("")
    : `<div class="review-item low"><div class="rt">无风险提示</div>
       <div class="rd">额度充足、无同期冲突、材料齐全</div></div>`;

  const ai = review.ai_advice
    ? `<div class="ai"><b>AI 建议</b> · ${esc(review.ai_advice)}</div>` : "";
  return items + ai;
}

/* ------------------------------ 统计 ------------------------------ */

async function loadStats() {
  const [dept, month] = await Promise.all([
    api("/api/reports/departments"), api("/api/reports/monthly"),
  ]);

  const maxDept = Math.max(1, ...dept.map((d) => d.approved_days || 0));
  $("deptRows").innerHTML = dept.length
    ? dept.map((d) => `<tr>
        <td data-label="部门" class="cell-title">${esc(d.dept_name)}</td>
        <td data-label="单据" class="num">${d.requests}</td>
        <td data-label="已通过" class="num">${d.approved_cnt}</td>
        <td data-label="已通过天数">
          <div class="barcell"><span class="bar"><i style="width:${(d.approved_days || 0) / maxDept * 100}%"></i></span>
          <b>${fmt(d.approved_days)}</b></div>
        </td>
      </tr>`).join("")
    : emptyRow(4, "暂无数据");

  const maxMonth = Math.max(1, ...month.map((m) => m.days || 0));
  $("monthRows").innerHTML = month.length
    ? month.map((m) => `<tr>
        <td data-label="月份" class="cell-title">${esc(m.month)}</td>
        <td data-label="单据" class="num">${m.requests}</td>
        <td data-label="天数">
          <div class="barcell"><span class="bar"><i style="width:${(m.days || 0) / maxMonth * 100}%"></i></span>
          <b>${fmt(m.days)}</b></div>
        </td>
      </tr>`).join("")
    : emptyRow(3, "暂无数据");
}

async function exportReport(kind) {
  const r = await api(`/api/reports/export/${kind}`, "POST", {});
  $("exportHint").textContent = `已导出 · ${r.path}`;
  toast("导出完成");
}

/* ------------------------------ 事件绑定 ------------------------------ */

const TABS = ["employees", "apply", "approve", "stats"];

function activateTab(name) {
  if (!TABS.includes(name)) name = "employees";
  document.querySelectorAll(".seg").forEach((t) => {
    t.classList.toggle("active", t.dataset.tab === name);
    t.setAttribute("aria-selected", String(t.dataset.tab === name));
  });
  document.querySelectorAll(".panel").forEach((p) => {
    p.classList.toggle("active", p.id === `tab-${name}`);
  });
  if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
}

document.querySelectorAll(".seg").forEach((tab) => {
  tab.onclick = () => activateTab(tab.dataset.tab);
});
window.addEventListener("hashchange", () => activateTab(location.hash.slice(1)));

$("modal").addEventListener("click", (e) => {
  if (e.target.matches("[data-close]") || e.target.classList.contains("modal-backdrop")) closeModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("modal").hidden) closeModal();
});

$("kw").oninput = () => loadEmployees().catch((e) => toast(e.message, true));
$("statusFilter").onchange = () => loadEmployees().catch((e) => toast(e.message, true));
$("btnNewEmp").onclick = newEmployee;

$("empRows").onclick = async (e) => {
  const t = e.target;
  try {
    if (t.dataset.emp) return await openEmployee(t.dataset.emp);
    if (t.dataset.regular) {
      await api(`/api/employees/${t.dataset.regular}/regularize`, "POST", {});
      toast("已转正");
      return await Promise.all([loadEmployees(), loadOverview()]);
    }
    if (t.dataset.resign) {
      if (!confirm("确认办理离职？将写入离职事件并置为已离职。")) return;
      await api(`/api/employees/${t.dataset.resign}/resign`, "POST", { remark: "界面办理离职" });
      toast("已办理离职");
      return await Promise.all([loadEmployees(), loadOverview()]);
    }
  } catch (err) {
    toast(err.message, true);
  }
};

["fEmp", "fType", "fStart", "fEnd"].forEach((id) => {
  $(id).onchange = () => precheck(false);
});
$("btnPrecheck").onclick = () => precheck(true);
$("btnSubmit").onclick = submitLeave;

$("reqFilter").onchange = () => loadRequests().catch((e) => toast(e.message, true));
$("actorRole").onchange = () => loadRequests().catch((e) => toast(e.message, true));
$("reqRows").onclick = (e) => {
  if (e.target.dataset.req) openRequest(e.target.dataset.req).catch((err) => toast(err.message, true));
};

$("btnExportRoster").onclick = () => exportReport("roster").catch((e) => toast(e.message, true));
$("btnExportLeave").onclick = () => exportReport("leave").catch((e) => toast(e.message, true));

/* ------------------------------ 启动 ------------------------------ */

(async function init() {
  activateTab(location.hash.slice(1));
  const tasks = [loadOverview, loadSelectors, loadEmployees, loadRequests, loadMyRequests, loadStats];
  for (const task of tasks) {
    try {
      await task();
    } catch (err) {
      toast(err.message, true);
    }
  }
})();
