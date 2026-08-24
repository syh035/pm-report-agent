# -*- coding: utf-8 -*-
"""
AI 层：DeepSeek 客户端 + 周报生成 + 文本提炼。

- call_deepseek()     底层 API 调用
- generate_report()   基于统计结果 + 模板生成周报
- enrich_tasks_from_text()  用 AI 把文本叙述提炼成结构化任务（供 text_parser 使用）
"""
from __future__ import annotations
import os
import json
import re
from typing import Dict, List, Optional

import requests

from .models import Task


# ---------- 配置 ----------
def get_api_key() -> Optional[str]:
    """优先环境变量，其次配置文件 config/keys.json。"""
    key = os.getenv("DEEPSEEK_API_KEY")
    if key:
        return key.strip()
    cfg_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "keys.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("deepseek_api_key") or None
        except Exception:
            return None
    return None


DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-chat"


def call_deepseek(
    messages: List[Dict[str, str]],
    model: str = DEFAULT_MODEL,
    temperature: float = 0.6,
    max_tokens: int = 2000,
    api_key: Optional[str] = None,
) -> str:
    """调用 DeepSeek，返回文本。若未配置 key 抛 RuntimeError。"""
    key = api_key or get_api_key()
    if not key:
        raise RuntimeError("未配置 DeepSeek API Key。请设置环境变量 DEEPSEEK_API_KEY 或编辑 config/keys.json")

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    resp = requests.post(DEEPSEEK_URL, json=payload, headers=headers, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


# ---------- 文本提炼（供解析层调用） ----------
def enrich_tasks_from_text(text: str, fallback_tasks: List[Task]) -> List[Task]:
    """让 AI 从文本提炼任务。失败时仍返回 fallback_tasks。"""
    try:
        prompt = (
            "你是项目管理助手。请从下面的项目文字叙述中，提炼出任务/进度条目。\n"
            "要求：输出 JSON 数组，每个元素含 name(任务名)、owner(负责人，可空)、"
            "progress(进度0-100，可空)、status(可选：已完成/进行中/未开始/已滞后/有风险)、"
            "plan_end(计划完成日期字符串，可空)、note(备注，可空)。\n"
            "只输出 JSON，不要其他文字。\n\n"
            f"文本：\n{text[:6000]}"
        )
        out = call_deepseek(
            [
                {"role": "system", "content": "你是高效的项目管理数据提炼助手。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=1500,
        )
        # 容错解析：提取 [] 部分
        m = re.search(r"\[.*\]", out, re.S)
        if not m:
            return fallback_tasks
        data = json.loads(m.group(0))
        tasks = []
        for item in data:
            t = Task(
                name=str(item.get("name", "")).strip(),
                owner=str(item.get("owner", "")).strip(),
                progress=item.get("progress"),
                status=str(item.get("status", "")).strip(),
                note=str(item.get("note", "")).strip(),
            )
            if t.name:
                # plan_end 简单解析
                pe = item.get("plan_end")
                if pe:
                    from .parsers._date_util import parse_date
                    t.plan_end = parse_date(pe)
                tasks.append(t)
        return tasks if tasks else fallback_tasks
    except Exception:
        return fallback_tasks


# 模板占位符（供 AI 解析时提示保留）
TEMPLATE_PLACEHOLDERS = [
    "{project_name}", "{period}", "{today}",
    "{overview}", "{stats_html}", "{status_html}", "{risks_html}", "{next_plan}",
]


def parse_template_to_html(text: str) -> str:
    """让 AI 把用户提供的模板文本/原始文档，转换为结构化 HTML 周报模板。

    保留占位符，用 <h1>/<h2>/<table>/<p> 等 HTML 标签组织，无 markdown 符号。
    失败时抛异常，由调用方处理。
    """
    placeholders_hint = "、".join(TEMPLATE_PLACEHOLDERS)
    prompt = (
        "你是资深的技术文档 / 周报排版专家。下面是用户提供的一份项目周报模板（可能是 "
        "Word/PDF 抽取的文本、HTML 或 markdown 原文）。\n"
        "请把它转换成一份 **结构化的 HTML 周报模板**，要求：\n"
        "1. 使用标准 HTML 标签（h1/h2/p/table/ul/li/strong），不要使用任何 markdown "
        "符号（不用 #、*、>、- 等）。\n"
        f"2. 保留以下占位符原样（放在合适位置，可重复使用）：{placeholders_hint}\n"
        "   其中 {stats_html} 放核心数据表格、{risks_html} 放风险列表、{overview} 放"
        "综述、{status_html} 放完成情况、{next_plan} 放下周计划。\n"
        "3. 大致保留用户原文的章节标题和顺序，但用语义化 HTML 表达。\n"
        "4. 只输出 HTML 模板本身，不要包裹代码块、不要解释。\n\n"
        f"原文：\n{text[:6000]}"
    )
    out = call_deepseek(
        [
            {"role": "system", "content": "你是 HTML 排版专家，只输出干净、无 markdown 的 HTML。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        max_tokens=3000,
    )
    # 去掉可能的代码块包裹
    out = out.strip()
    m = re.search(r"```(?:html)?\s*(.*?)```", out, re.S)
    if m:
        out = m.group(1).strip()
    # 若没产出可用的 HTML（缺 <），退回简单分段
    if "<" not in out or ">" not in out:
        raise ValueError("AI 未产出有效 HTML 模板")
    return out
