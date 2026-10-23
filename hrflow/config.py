"""全局配置。

真实系统里这些都应该是后台可配的（对应 frappe/hrms 的 HR Settings、Leave Type、
Holiday List 三张配置表）。这里为了保持 mini 版的体量，集中放在一个文件里，
但业务代码一律通过 config 读取，不写死常量。
"""

from __future__ import annotations

# 法定节假日（示例年），用于计算请假天数时剔除。
# 真实做法是从 Holiday List 表读取，按年份维护。
HOLIDAYS = {
    "2026-01-01", "2026-01-02", "2026-01-03",
    "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",
    "2026-04-04", "2026-04-05", "2026-04-06",
    "2026-05-01", "2026-05-02", "2026-05-03",
    "2026-06-19", "2026-06-20", "2026-06-21",
    "2026-09-25", "2026-09-26", "2026-09-27",
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07",
}

# 组织默认配置
ORG_NAME = "示例科技有限公司"

# AI 预审：可选的大模型增强，默认关闭。
# 只依赖 OpenAI Chat Completions 协议（业界通用），具体接哪家由环境变量决定，
# 不把服务商与密钥写死进代码：LLM_API_KEY / LLM_BASE_URL / LLM_MODEL。
# 三项缺任一项即整体跳过，功能退化为纯规则引擎。
AI_REVIEW_ENABLED = True
LLM_API_KEY = ""
LLM_BASE_URL = ""
LLM_MODEL = ""

# 团队风险阈值：同一部门同期请假人数占比超过该值则提示
TEAM_CONFLICT_RATIO = 0.3


def get_llm_config() -> dict | None:
    """返回可用的大模型配置；任一项缺失返回 None，调用方据此跳过增强。"""
    import os
    key = os.environ.get("LLM_API_KEY") or LLM_API_KEY
    base = os.environ.get("LLM_BASE_URL") or LLM_BASE_URL
    model = os.environ.get("LLM_MODEL") or LLM_MODEL
    if not (key and base and model):
        return None
    return {"api_key": key, "base_url": base.rstrip("/"), "model": model}
