# -*- coding: utf-8 -*-
"""
模板模块定义与默认 blocks。

模板 = 一串 blocks（JSON 数组）。每个 block 描述一个模块，
type 决定其渲染方式与数据绑定。详见 plan。
"""
from __future__ import annotations
from typing import Dict, List


# ---------- 默认模板 blocks ----------
DEFAULT_BLOCKS: List[Dict] = [
    {"id": "b-title", "type": "title", "title": "{project_name} 项目周报"},
    {"id": "b-meta", "type": "meta", "format": "周期：{period} ｜ 生成日期：{today}"},
    {"id": "b-h1", "type": "heading", "title": "一、本周总体进展"},
    {"id": "b-overview", "type": "overview", "placeholder": "overview"},
    {"id": "b-h2", "type": "heading", "title": "二、核心数据"},
    {"id": "b-kpi", "type": "kpi",
     "keys": ["total", "done", "completion_rate", "avg_progress", "risk"]},
    {"id": "b-h3", "type": "heading", "title": "三、完成情况"},
    {"id": "b-status", "type": "status"},
    {"id": "b-h4", "type": "heading", "title": "四、风险与需关注事项"},
    {"id": "b-risk", "type": "risk_list"},
    {"id": "b-h5", "type": "heading", "title": "五、下周计划"},
    {"id": "b-plan", "type": "plan"},
]


# ---------- 预置模块库（供前端拖拽面板展示） ----------
MODULE_LIBRARY: List[Dict] = [
    {"type": "title",  "label": "标题",  "desc": "周报大标题", "icon": "H1"},
    {"type": "meta",   "label": "元信息", "desc": "周期与日期行", "icon": "ⓘ"},
    {"type": "heading", "label": "小节标题", "desc": "自定义章节标题", "icon": "§"},
    {"type": "overview", "label": "进展综述", "desc": "本周进展段落（AI/规则）", "icon": "¶"},
    {"type": "kpi",    "label": "KPI 卡片", "desc": "关键数字卡片组", "icon": "▦"},
    {"type": "stats_table", "label": "数据表", "desc": "核心指标表", "icon": "☰"},
    {"type": "status", "label": "完成情况", "desc": "已完成/进行中/未开始", "icon": "✓"},
    {"type": "risk_list", "label": "风险清单", "desc": "风险与关注(带颜色)", "icon": "⚠"},
    {"type": "plan",   "label": "下周计划", "desc": "下周计划段落", "icon": "→"},
    {"type": "delta",  "label": "环比上周", "desc": "与上周对比（完成率/风险/滞后）", "icon": "⇄"},
    {"type": "transition", "label": "过渡叙述", "desc": "跨版本一致的衔接段（纯规则、确定性）", "icon": "➡"},
    {"type": "project_table", "label": "重点项目表", "desc": "按项目对比 任务/完成率/风险/滞后", "icon": "▦"},
    {"type": "project_sections", "label": "分项目区块", "desc": "每个项目独立小节（KPI+风险）", "icon": "📦"},
    {"type": "ai_block", "label": "自定义AI块", "desc": "提示词+数据源，AI 生成段落", "icon": "🧩"},
    {"type": "formula", "label": "公式指标卡", "desc": "由统计数据计算指标（如 risk/total*100）", "icon": "ƒ"},
    {"type": "custom", "label": "自定义块", "desc": "自由 HTML/文本", "icon": "+"},
]


# KPI 可选字段（供前端勾选）
KPI_OPTIONS = [
    {"key": "total", "label": "总任务数"},
    {"key": "done", "label": "已完成"},
    {"key": "in_progress", "label": "进行中"},
    {"key": "not_started", "label": "未开始"},
    {"key": "completion_rate", "label": "完成率"},
    {"key": "avg_progress", "label": "平均实际进度"},
    {"key": "avg_target_progress", "label": "平均目标进度"},
    {"key": "risk", "label": "风险项"},
]


# ---------- 预置模板（供面板一键切换） ----------
PRESET_TEMPLATES: Dict[str, Dict] = {
    "default": {
        "label": "标准周报",
        "blocks": DEFAULT_BLOCKS,
    },
    "decision": {
        "label": "决策简报",
        "desc": "面向决策层：KPI 前置、风险突出、下周决策点",
        "blocks": [
            {"id": "d1", "type": "title", "title": "{project_name} 决策简报"},
            {"id": "d2", "type": "meta", "format": "周期：{period} ｜ 生成日期：{today}"},
            {"id": "d3", "type": "kpi", "keys": ["completion_rate", "avg_progress", "risk", "delayed"]},
            {"id": "d4", "type": "heading", "title": "一、关键结论"},
            {"id": "d5", "type": "overview", "placeholder": "overview"},
            {"id": "d6", "type": "heading", "title": "二、风险与需决策事项"},
            {"id": "d7", "type": "risk_list"},
            {"id": "d8", "type": "heading", "title": "三、下周决策点"},
            {"id": "d9", "type": "plan"},
            {"id": "d10", "type": "delta"},
        ],
    },
    "management": {
        "label": "管理层周报",
        "desc": "面向管理层：环比前置、关注趋势与滞后",
        "blocks": [
            {"id": "m1", "type": "title", "title": "{project_name} 管理层周报"},
            {"id": "m2", "type": "meta", "format": "周期：{period} ｜ 生成日期：{today}"},
            {"id": "m3", "type": "heading", "title": "一、与上周对比"},
            {"id": "m4", "type": "delta"},
            {"id": "m5", "type": "heading", "title": "二、核心数据"},
            {"id": "m6", "type": "stats_table"},
            {"id": "m7", "type": "heading", "title": "三、完成情况"},
            {"id": "m8", "type": "status"},
            {"id": "m9", "type": "heading", "title": "四、风险与滞后"},
            {"id": "m10", "type": "risk_list"},
        ],
    },
}
