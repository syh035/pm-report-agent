# -*- coding: utf-8 -*-
"""提示词管理：默认提示词集中定义，用户可在面板覆盖（config/prompts.json，gitignored）。

设计：
  - PROMPT_DEFAULTS：每份提示词的默认模板（system/user）+ 占位符说明 + 少样本示例
  - 用户覆盖存 config/prompts.json（面板可编辑，便于调试/调优）
  - render_user()：把 user 模板中的 {占位符} 替换为实际数据，并追加用户示例（few-shot）
"""
from __future__ import annotations
import os
import json
from typing import Dict, List, Optional

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_FILE = os.path.join(_BASE, "config", "prompts.json")

# 示例分隔符：面板里多个示例用该行分隔
EXAMPLE_SEP = "\n---\n"

# ---------------- 默认提示词（唯一事实源） ----------------
# 4 类 AI（用户确认版）：分析 / 生成 / 规则配置 / 意图
# 每类一份 system+user，用 {task_type} 区分任务（输出格式/目标）。
PROMPT_DEFAULTS: Dict[str, Dict] = {
    "ai_analysis": {
        "label": "分析 AI（数据筛选/结构化/校验）",
        "system": "你是严谨的数据分析助手。只提取给定内容中明确存在的信息，数字/日期/人名必须来自原文，禁止编造；不确定的字段留空。",
        "user": (
            "请完成分析任务（任务类型：{task_type}，默认=结构化抽取 structure）。\n"
            "把内容结构化抽取为 JSON sections：{\"sections\":[{\"kind\":\"task|risk|issue|decision|milestone|metric\","
            "\"name\":\"条目名\",\"fields\":{关键字段},\"source_note\":\"来源\"}],\"summary\":\"一句话概览\"}；"
            "校验修正任务时逐条检查 owner/progress/status/plan_end，只修明显错误；\n"
            "只提取明确存在的信息；数字/日期/人名必须来自原文；禁止编造；不确定的字段留空；\n"
            "需要输出 JSON 时只输出 JSON，不要其他文字。\n\n{task_input}"
        ),
        "docs": {"{task_type}": "任务类型：structure/review/refine",
                 "{task_instruction}": "按任务的详细指令（系统按类型填充）",
                 "{task_input}": "待分析的内容（文档/任务JSON/候选行）"},
        "examples": [],
    },
    "ai_generation": {
        "label": "生成 AI（周报/模板生成与调试）",
        "system": "你是资深的 PMO 周报撰写与模板排版专家。严格按模板结构输出；所有数字/日期/人名必须来自给定数据，禁止编造；遵守【生成要求】。",
        "user": (
            "请完成生成任务（任务类型：{task_type}）。\n"
            "严格按模板结构输出；所有数字/日期/人名必须来自给定数据，禁止编造；遵守生成要求；\n"
            "输出 HTML 时用 h1/h2/p/table/ul/li，不用 markdown 符号、不要代码块包裹。\n\n{task_input}"
        ),
        "docs": {"{task_type}": "任务类型", "{task_instruction}": "按任务的详细指令",
                 "{task_input}": "模板+数据+分析等输入"},
        "examples": [],
    },
    "ai_rules": {
        "label": "规则配置 AI（规则理解/批量导入）",
        "system": "你是项目管理制度助手。把规则/工作要求转换为结构化 JSON，只输出 JSON。",
        "user": (
            "请把规则/工作要求整理为结构化 JSON（任务类型：{task_type}）。\n"
            "字段约定：type ∈ rule|requirement|generation_requirement|status_map|column_map|ignore；"
            "title 中文短标题；description 忠实概括；value 数值或 null；key 字段名或 null。\n"
            "涉及如何筛选/抽取/归类数据→requirement（分析要求）；涉及周报怎么写/格式/排序/口径→generation_requirement（生成要求）。\n"
            "只输出 JSON。\n\n{task_input}"
        ),
        "docs": {"{task_type}": "任务类型：single/batch", "{task_instruction}": "按任务的详细指令",
                 "{task_input}": "用户需求或规则文档"},
        "examples": [],
    },
    "ai_intent": {
        "label": "意图 AI（看板/Agent 一句话入口）",
        "system": "你是意图识别助手，只输出 JSON。",
        "user": (
            "从用户指令中识别意图与参数。\n"
            "意图（intent）∈ generate_report（生成周报/日报）、dashboard（看板概览）、risks（列出风险/预警）、help（帮助）。\n"
            "参数（params）可含：project_name（项目名，可为空）、report_type（week/day，生成时用）、period（周期/日期，可为空）。\n"
            "只输出 JSON：{\"intent\":\"...\",\"params\":{...}}，不要其他文字。\n\n指令：{instruction}"
        ),
        "docs": {"{instruction}": "用户的一句话指令"},
        "examples": [],
    },
}

# ---------------- 旧提示词 → 新 4 类映射（兼容旧调用方） ----------------
# key: (新键, task_type, 该任务的 user 指令前缀)
LEGACY_PROMPT_MAP: Dict[str, tuple] = {
    "report_overview": ("ai_generation", "overview",
        "请输出两段纯文字（不要 markdown 符号）：第一段「进展综述」3-5句概述结论先行；第二段「下周计划」2-4句。\n"
        "严格约束：所有百分比和任务数字只能来自给定数据；提到任务名只能用数据中的任务名。\n"
        "用以下格式分隔两段：[综述]...内容...\n[计划]...内容..."),
    "dataset_report": ("ai_generation", "week_report",
        "请把【周报模板】原地更新为本周周报，而不是重写：\n"
        "1. 逐段对照模板原文与【数据集】，找到模板中每个与数据对应的位置（数字/百分比/日期/状态/任务名/指标值/结论句等），"
        "用数据集中的真实值替换旧值；\n"
        "2. 模板中没有对应数据的文字（论述风格、用词、句式、章节结构、格式）一律原样保留，不要改写、不要增删章节、不要改变排版；\n"
        "3. 数据集中有、但模板没提到的重要数据（如新增风险/里程碑）可补充到对应章节；\n"
        "4. 所有数字/日期/人名必须来自【数据集】，禁止编造；必须遵守【生成要求】；\n"
        "5. 输出完整 HTML（沿用模板原有结构与样式，h1/h2/p/table/ul/li），不用 markdown 符号。"),
    "ai_pipeline_present": ("ai_generation", "week_report",
        "请生成一份{report_type}：严格按【模板】章节结构；内容用【分析结论】要点+【结构化数据】真实值；"
        "必须遵守【生成要求】；输出完整 HTML，不用 markdown 符号。"),
    "template_parse": ("ai_generation", "template_parse",
        "把下面的模板原文转换成结构化 HTML 周报模板：使用标准 HTML 标签；"
        "保留占位符（{project_name}/{period}/{today} 等）原样；大致保留原文章节标题和顺序；只输出 HTML 本身。"),
    "template_tune": ("ai_generation", "template_tune",
        "基于当前模板按用户的调整要求重写：保留模板整体结构与占位符；只修改用户要求的部分；"
        "用户没要求改的部分保持不变；只输出修改后的完整 HTML 模板。"),
    "ai_analysis": ("ai_analysis", "structure",
        "请按【分析要求】从文档中抽取结构化数据，输出 JSON："
        "{\"sections\":[{\"kind\":\"task|risk|issue|decision|milestone|metric\","
        "\"name\":\"条目名\",\"fields\":{...},\"source_note\":\"来源\"}],\"summary\":\"一句话概览\"}。"
        "每板块最多6条，总量60条以内，保证JSON完整闭合。"),
    "ai_pipeline_filter": ("ai_analysis", "structure",
        "从文档 markdown 中抽取结构化数据，输出 JSON sections 数组（kind/name/fields/source_note）+ summary；"
        "每板块最多6条，总量60条以内。"),
    "ai_pipeline_analyze": ("ai_analysis", "analyze",
        "基于结构化数据按【工作要求】分析，输出 3-6 句纯文本（结论先行）：进度是否受控、主要风险与影响、"
        "需管理层关注事项、下一步建议。严格约束：所有数字/日期必须能在数据中找到。"),
    "ai_review": ("ai_analysis", "review",
        "以下是规则解析出的任务 JSON，请逐条校验修正明显错误：owner/progress/status/plan_end 字段；"
        "只修正数据内部不一致或明显笔误；不确定的保持原值；不要删减任务、不要编造；只输出修正后的 JSON 数组。"),
    "enrich_tasks": ("ai_analysis", "refine",
        "从候选行中提炼任务/进度条目，输出 JSON 数组，每项含 name/owner/progress/status/plan_end/note；"
        "不确定的字段输出 null，不要猜；只输出 JSON 数组。"),
    "rule_intent": ("ai_rules", "single",
        "用户想添加一条规则或工作要求，输出 JSON：{\"type\":\"rule|requirement|generation_requirement|status_map|column_map|ignore|other\","
        "\"title\":\"中文短标题\",\"description\":\"忠实概括\",\"key\":字段名或null,\"value\":数值或null,\"scope\":\"\",\"applies_to\":\"\"}。"
        "涉及如何筛选/抽取/归类数据→requirement（分析要求）；涉及周报怎么写/格式/排序/口径→generation_requirement（生成要求）。"
        "只输出 JSON 对象。"),
    "rule_batch_intent": ("ai_rules", "batch",
        "从规则文档中批量提取规则为 JSON 数组，每项含 type/title/description/value/key；"
        "判断依据同单条；数值阈值 key 尽量填 delay_days_danger/slow_progress_pct/risk_near_end_days。\n{force_hint}"),
    "agent_intent": ("ai_intent", "intent",
        ""),
    "dialogue_finalize": ("ai_rules", "finalize_dialogue",
        "根据完整对话记录按【总结要求】整理最终结果，只提取用户明确确认的内容，不要编造；只输出要求的格式（JSON 或 HTML）。"),
}

# 各类可用的 task_type 列表（前端提示词库下拉用）
PROMPT_TASK_TYPES: Dict[str, List[str]] = {
    "ai_analysis": ["structure", "analyze", "review", "refine"],
    "ai_generation": ["week_report", "day_report", "overview", "template_parse", "template_tune"],
    "ai_rules": ["single", "batch", "finalize_dialogue"],
    "ai_intent": ["intent"],
}


def get_prompt(key: str, task_type: str = "") -> Dict:
    """获取提示词。新键直接返回；旧键经 LEGACY_PROMPT_MAP 映射到新键+task_type+指令。"""
    if key in PROMPT_DEFAULTS:
        entry = load_prompts().get(key, {})
        entry = dict(entry)
        entry["_from_defaults"] = True
        if task_type:
            entry["task_type"] = task_type
        return entry
    if key in LEGACY_PROMPT_MAP:
        new_key, tt, instruction = LEGACY_PROMPT_MAP[key]
        entry = load_prompts().get(new_key, {})
        entry = dict(entry)
        entry["task_type"] = task_type or tt
        entry["_task_instruction"] = instruction
        entry["_legacy_key"] = key
        return entry
    return {}


def _read_overrides() -> Dict:
    """读取用户覆盖（config/prompts.json），失败返回空。"""
    try:
        with open(PROMPTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_prompts() -> Dict[str, Dict]:
    """当前生效的提示词 = 默认 + 用户覆盖（system/user/examples 逐项覆盖）。"""
    over = _read_overrides()
    out = {}
    for key, default in PROMPT_DEFAULTS.items():
        entry = {"label": default.get("label", key),
                 "system": default["system"], "user": default["user"],
                 "docs": default.get("docs", {}), "examples": list(default.get("examples", []))}
        o = over.get(key) or {}
        if isinstance(o, dict):
            if isinstance(o.get("system"), str) and o["system"].strip():
                entry["system"] = o["system"]
            if isinstance(o.get("user"), str) and o["user"].strip():
                entry["user"] = o["user"]
            if isinstance(o.get("examples"), list):
                entry["examples"] = [str(e) for e in o["examples"]]
        out[key] = entry
    return out


def render_user(entry: Dict, data: Dict[str, str]) -> str:
    """渲染 user 模板：替换 {占位符}，并追加用户示例（few-shot）。
    兼容旧调用方：entry 带 _legacy_key 时，把旧占位符数据合成为 {task_input}，指令合为 {task_instruction}。"""
    text = entry.get("user", "")
    d = dict(data or {})
    # 兼容：旧调用方传的是旧占位符键（stats_json 等），新 user 模板用 task_* 占位符 → 合成
    _OLD_KEYS = ("template_text", "template", "stats_json", "data_json", "dataset_text",
                 "analysis", "document_text", "tasks_json", "candidate_text",
                 "source_text", "rule_text", "instruction")
    if entry.get("_legacy_key") or (entry.get("_from_defaults") and any(k in d for k in _OLD_KEYS)):
        d["task_type"] = entry.get("task_type") or entry.get("_legacy_default_tt", "")
        instr = entry.get("_task_instruction") or ""
        instr = instr.replace("{report_type}", str(d.get("report_type") or "周报"))
        # 指令内嵌占位符用实际值替换（如 rule_batch_intent 的 {force_hint}）
        for pk in ("force_hint", "placeholders"):
            if "{" + pk + "}" in instr and d.get(pk):
                instr = instr.replace("{" + pk + "}", str(d[pk]))
        parts = []
        # 任务指令优先（旧调用方的格式/约束要求）
        if instr:
            parts.append("【任务要求】\n" + instr)
        for src in ("template_text", "template", "stats_json", "data_json", "dataset_text",
                    "analysis", "document_text", "tasks_json", "candidate_text",
                    "source_text", "rule_text"):
            if src in d and d[src]:
                label = {"template_text": "【周报模板】", "template": "【周报模板】",
                         "stats_json": "【结构性数据】", "data_json": "【结构化数据】",
                         "dataset_text": "【数据集】", "analysis": "【分析结论】",
                         "document_text": "【文档内容】", "tasks_json": "【任务】",
                         "candidate_text": "【候选行】", "source_text": "【模板原文】",
                         "rule_text": "【规则文档】"}.get(src, "【输入】")
                parts.append(f"{label}\n{d[src]}")
        # instruction 始终进 task_input（规则对话/意图等）
        if d.get("instruction"):
            parts.append("【用户指令】\n" + d["instruction"])
        extra = []
        for k in ("requirements", "generation_requirements", "processing_rules",
                  "placeholders", "force_hint", "finalize_hint", "transcript",
                  "report_type", "tune_request", "template_html"):
            if k in d and d[k]:
                # 模板含对应占位符则保留直接替换，否则按固定小节渲染（不再堆进其它参数）
                if "{" + k + "}" in text:
                    continue
                if k == "requirements":
                    parts.append("【分析要求】\n" + str(d[k]))
                elif k == "generation_requirements":
                    parts.append("【生成要求】\n" + str(d[k]))
                elif k == "processing_rules":
                    parts.append("【处理约定】\n" + str(d[k]))
                else:
                    extra.append(f"{k}={d[k]}")
        if extra:
            parts.append("【其它参数】\n" + "\n".join(extra))
        d["task_input"] = "\n\n".join(parts) if parts else "（无输入内容）"
        # 模板直接用 {instruction}/{force_hint} 等占位符时保留在 d 中供替换
        consumed = {"template_text", "template", "stats_json", "data_json", "dataset_text",
                    "analysis", "document_text", "tasks_json", "candidate_text",
                    "source_text", "rule_text", "requirements",
                    "generation_requirements", "processing_rules", "placeholders",
                    "finalize_hint", "transcript", "report_type", "tune_request", "template_html"}
        keep = {k for k in ("instruction", "force_hint") if "{" + k + "}" in text}
        consumed -= keep
        d = {k: v for k, v in d.items() if k not in consumed}
    for k, v in d.items():
        text = text.replace("{" + k + "}", str(v))
    examples = entry.get("examples") or []
    if examples:
        block = "\n\n" + "\n\n".join(f"参考示例：\n{e}" for e in examples) + \
                "\n\n（请严格模仿上面示例的风格、句式和结构来输出本任务内容）"
        text = text + block
    return text


def save_prompts(overrides: Dict, record_versions: bool = True) -> Dict:
    """保存用户覆盖（只保存被修改的键；examples 可为空列表=清空）。
    record_versions=True 时每个被修改的键记一个版本（供历史回退）。"""
    over = _read_overrides()
    for key, o in (overrides or {}).items():
        # 旧键（report_overview 等）映射到新键保存（新键名存覆盖，旧键只作为别名）
        save_key = key
        if key not in PROMPT_DEFAULTS:
            if key in LEGACY_PROMPT_MAP:
                save_key = LEGACY_PROMPT_MAP[key][0]
            else:
                continue
        if not isinstance(o, dict):
            continue
        cleaned = {}
        if isinstance(o.get("system"), str) and o["system"].strip():
            cleaned["system"] = o["system"]
        if isinstance(o.get("user"), str) and o["user"].strip():
            cleaned["user"] = o["user"]
        if isinstance(o.get("examples"), list):
            cleaned["examples"] = [str(e) for e in o["examples"]]
        if cleaned:
            over[save_key] = cleaned
        else:
            over.pop(save_key, None)   # 空覆盖 = 恢复默认
        if record_versions:
            try:
                from . import datastore
                datastore.save_prompt_version(
                    key,
                    str(o.get("system") or ""),
                    str(o.get("user") or ""),
                    json.dumps([str(e) for e in (o.get("examples") or [])], ensure_ascii=False),
                )
            except Exception:
                pass
    os.makedirs(os.path.dirname(PROMPTS_FILE), exist_ok=True)
    with open(PROMPTS_FILE, "w", encoding="utf-8") as f:
        json.dump(over, f, ensure_ascii=False, indent=2)
    return load_prompts()


def rollback_prompt(key: str, vid: int) -> Dict:
    """回退某提示词到指定版本（只恢复该 key 的内容，不影响其他）。"""
    from . import datastore
    v = datastore.get_prompt_version(vid)
    if not v or v.get("key") != key:
        raise ValueError("版本不存在或不属于该提示词")
    over = _read_overrides()
    # 版本内容写入覆盖（空=恢复默认）
    cleaned = {}
    if (v.get("system") or "").strip():
        cleaned["system"] = v["system"]
    if (v.get("user") or "").strip():
        cleaned["user"] = v["user"]
    examples = v.get("examples_list") or []
    cleaned["examples"] = [str(e) for e in examples]
    if cleaned:
        over[key] = cleaned
    else:
        over.pop(key, None)
    os.makedirs(os.path.dirname(PROMPTS_FILE), exist_ok=True)
    with open(PROMPTS_FILE, "w", encoding="utf-8") as f:
        json.dump(over, f, ensure_ascii=False, indent=2)
    # 回退本身也记一版（可再次回退）
    try:
        datastore.save_prompt_version(key, str(cleaned.get("system") or ""),
                                      str(cleaned.get("user") or ""),
                                      json.dumps(cleaned.get("examples") or [], ensure_ascii=False))
    except Exception:
        pass
    return load_prompts()


def reset_prompts(keys: Optional[List[str]] = None) -> None:
    """恢复默认。keys=None 全部恢复；否则只恢复指定键。"""
    try:
        if keys is None:
            if os.path.exists(PROMPTS_FILE):
                os.remove(PROMPTS_FILE)
            return
        over = _read_overrides()
        changed = False
        for k in keys:
            rk = k
            if k not in PROMPT_DEFAULTS and k in LEGACY_PROMPT_MAP:
                rk = LEGACY_PROMPT_MAP[k][0]
            if rk in over:
                del over[rk]
                changed = True
        if changed:
            with open(PROMPTS_FILE, "w", encoding="utf-8") as f:
                json.dump(over, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# 提示词 4 类分组（前端展开式用途选择用）
PROMPT_PURPOSE: Dict[str, str] = {
    "ai_analysis": "分析 AI",
    "ai_generation": "生成 AI",
    "ai_rules": "规则配置 AI",
    "ai_intent": "意图 AI",
}
HIDDEN_PROMTPTS = set()   # 4 类结构下无隐藏项


def prompts_status() -> Dict:
    """面板展示：每份提示词的当前值/默认值/是否被修改/占位符说明/用途分组。"""
    cur = load_prompts()
    over_keys = set(_read_overrides().keys())
    items = []
    for key, e in cur.items():
        if key not in PROMPT_DEFAULTS:
            continue
        d = PROMPT_DEFAULTS[key]
        items.append({
            "key": key, "label": e["label"],
            "purpose": PROMPT_PURPOSE.get(key, "其他"),
            "hidden": key in HIDDEN_PROMTPTS,
            "task_types": PROMPT_TASK_TYPES.get(key, []),
            "system": e["system"], "user": e["user"], "examples": e["examples"],
            "docs": e["docs"],
            "modified": key in over_keys,
            "default_system": d["system"], "default_user": d["user"],
            "default_examples": list(d.get("examples", [])),
        })
    return {"items": items, "example_sep": "---",
            "purposes": sorted(set(PROMPT_PURPOSE.values()))}
