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
PROMPT_DEFAULTS: Dict[str, Dict] = {
    "report_overview": {
        "label": "周报综述与下周计划",
        "system": "你是严谨、专业的 PMO 项目周报撰写专家，只输出纯文本，数字一律引用给定数据。",
        "user": (
            "你是资深的 PMO 项目管理人员，负责撰写项目周报。以下是一个项目的结构性数据：\n"
            "{stats_json}\n\n"
            "请输出两段纯文字（不要用任何 markdown 符号、不要用 # 或 * 或列表符号）：\n"
            "第一段标题：进展综述 —— 3-5 句话的本周进展概述，结论先行，突出完成情况和推进节奏。\n"
            "第二段标题：下周计划 —— 2-4 句，给出下周工作重点建议。\n"
            "严格约束：文中出现的所有百分比和任务数字，只能来自上面给定的数据，禁止编造任何数字；"
            "提到任务名只能用数据中的任务名。\n"
            "必须遵守【生成要求】（用户对文稿的固定要求）：\n{generation_requirements}\n"
            "用以下格式分隔两段：\n[综述]...内容...\n[计划]...内容..."
        ),
        "docs": {"{stats_json}": "项目统计 JSON（完成率/风险/任务清单等），由系统自动填充",
                 "{generation_requirements}": "规则库生成要求"},
        "examples": [],   # 面板可贴您期望的样稿（每份用 "---" 分隔）
    },
    "enrich_tasks": {
        "label": "文本提炼为任务",
        "system": "你是高效的项目管理数据提炼助手，只输出 JSON。",
        "user": (
            "你是项目管理助手。下面是已用规则初步筛选出的项目进度候选行，请从中提炼任务/进度条目。\n"
            "要求：输出 JSON 数组，每个元素含 name(任务名)、owner(负责人，可空)、"
            "progress(进度0-100，可空)、status(可选：已完成/进行中/未开始/已滞后/有风险)、"
            "plan_end(计划完成日期字符串，可空)、note(备注，可空)。\n"
            "只输出 JSON 数组，不要其他文字，不要编造候选行中不存在的信息；不确定的字段输出 null，不要猜。\n\n"
            "示例：\n输入候选行：\n- 财务模块开发 张三 负责，当前进度60%\n"
            "输出：\n[{\"name\":\"财务模块开发\",\"owner\":\"张三\",\"progress\":60,"
            "\"status\":\"进行中\",\"plan_end\":null,\"note\":\"当前进度60%\"}]\n\n"
            "候选行：\n{candidate_text}"
        ),
        "docs": {"{candidate_text}": "规则预筛后的候选行文本（系统自动填充）"},
        "examples": [],
    },
    "template_parse": {
        "label": "模板文件转 HTML",
        "system": "你是 HTML 排版专家，只输出干净、无 markdown 的 HTML。",
        "user": (
            "你是资深的技术文档 / 周报排版专家。下面是用户提供的一份项目周报模板（可能是 "
            "Word/PDF 抽取的文本、HTML 或 markdown 原文）。\n"
            "请把它转换成一份 **结构化的 HTML 周报模板**，要求：\n"
            "1. 使用标准 HTML 标签（h1/h2/p/table/ul/li/strong），不要使用任何 markdown "
            "符号（不用 #、*、>、- 等）。\n"
            "2. 保留以下占位符原样（放在合适位置，可重复使用）：{placeholders}\n"
            "   其中 {stats_html} 放核心数据表格、{risks_html} 放风险列表、{overview} 放"
            "综述、{status_html} 放完成情况、{next_plan} 放下周计划。\n"
            "3. 大致保留用户原文的章节标题和顺序，但用语义化 HTML 表达。\n"
            "4. 只输出 HTML 模板本身，不要包裹代码块、不要解释。\n\n"
            "原文：\n{source_text}"
        ),
        "docs": {"{placeholders}": "应保留的占位符列表（系统自动填充）",
                 "{source_text}": "用户上传的模板原文（系统自动填充，截断 6000 字）"},
        "examples": [],
    },
    "template_tune": {
        "label": "模板对话微调",
        "system": "你是 HTML 排版专家。基于当前模板按用户的调整要求重写，只输出完整 HTML，不要 markdown 符号、不要代码块包裹。",
        "user": (
            "下面是一份项目周报模板（HTML）。用户想对它做调整。\n\n"
            "【当前模板】\n{template_html}\n\n"
            "【用户调整要求】\n{tune_request}\n\n"
            "要求：\n"
            "1. 保留模板整体结构与占位符（如 {project_name}/{period}/{today} 等），只按用户要求修改相应部分。\n"
            "2. 用户没要求改的部分保持不变；不要擅自增删章节。\n"
            "3. 只输出修改后的完整 HTML 模板本身，不要解释、不要代码块包裹。"
        ),
        "docs": {"{template_html}": "当前模板 HTML", "{tune_request}": "用户对模板的调整要求"},
        "examples": [],
    },
    "ai_review": {
        "label": "AI 深度解析（校验修正任务字段）",
        "system": "你是严谨的数据校验助手，只输出 JSON 数组。",
        "user": (
            "以下是规则解析出的任务 JSON 数组，请逐条校验并修正明显错误：\n"
            "- owner（负责人）、progress（进度0-100）、status（可选：已完成/进行中/未开始/已滞后/有风险）、"
            "plan_end（日期）\n"
            "- 只修正数据内部不一致或明显笔误；不确定的字段保持原值；不要删减任务；不要编造新任务\n"
            "只输出修正后的 JSON 数组，不要其他文字。\n\n任务：\n{tasks_json}"
        ),
        "docs": {"{tasks_json}": "规则解析出的任务 JSON（系统自动填充）"},
        "examples": [],
    },
    "agent_intent": {
        "label": "Agent 意图识别",
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
    "dataset_report": {
        "label": "模板+数据集生成周报",
        "system": "你是资深的 PMO 周报撰写专家。你必须严格按照用户提供的周报模板的结构、章节顺序和格式制作文稿；所有数据只能来自给定的数据集，禁止编造任何数字、日期、人名。",
        "user": (
            "请根据【周报模板】与【数据集】生成一份完整的项目周报。\n\n"
            "【周报模板】（需严格遵循其章节结构、标题、表格列与排版；模板中的示例内容只是格式示意，必须替换为数据集中的真实数据）：\n"
            "{template_text}\n\n"
            "【数据集】（按板块组织的原始数据，所有数字/日期/编号均以此为准）：\n"
            "{dataset_text}\n\n"
            "要求：\n"
            "1. 严格按模板的章节顺序与标题生成，模板有几节就写几节；模板里没有的板块不要擅自新增。\n"
            "2. 每个章节的内容必须从【数据集】对应板块抓取：指标数值用数据集的「本周值/上周值/变化/目标值」；"
            "成果用「成果事项/完成度/状态/负责人」；里程碑用「里程碑/计划日期/实际日期/偏差/状态」；"
            "风险用「风险描述/级别/影响/应对措施/责任人/目标关闭日期」；依赖用「依赖事项/当前状态/预计交付/对接人」；"
            "资源人力/预算成本/下周计划/决策请求 同理一一对应。\n"
            "3. 模板中的示例数字/日期/人名只是格式示意，一律以数据集为准替换；数据集里没有的信息留白或用「—」，绝不编造。\n"
            "4. 叙述性文字（概述、结论）可参考模板的写法风格，但所有数字、日期、编号必须核对数据集。\n"
            "5. 必须遵守【生成要求】（用户对文稿的固定要求）。\n"
            "6. 输出完整 HTML（h1/h2/p/table/ul/li），不用 markdown 符号，不要代码块包裹。\n\n"
            "【生成要求】\n{generation_requirements}"
        ),
        "docs": {"{template_text}": "用户上传的周报模板原文",
                 "{dataset_text}": "数据集板块文本（按板块组织，系统自动填充）",
                 "{generation_requirements}": "规则库生成要求"},
        "examples": [],
    },
    "rule_intent": {
        "label": "规则需求→结构化规则（对话式管理）",
        "system": "你是项目管理制度助手。把用户用自然语言表达的规则/工作要求，转换为结构化规则 JSON，只输出 JSON。",
        "user": (
            "用户想给周报系统添加一条规则或工作要求。请理解意图并输出 JSON，字段：\n"
            "- type（必填）：规则类型，可选数值阈值 rule / 分析要求 requirement（影响数据筛选分析）/ 生成要求 generation_requirement（影响文稿生成） / 状态词映射 status_map / 列名映射 column_map / 忽略词 ignore / 其他 other\n"
            "- title（必填）：规则的中文短标题\n"
            "- description（必填）：用户原始需求的忠实概括（中文，不要曲解）\n"
            "- key（可选）：若是对已有规则库字段的修改，给出字段名（如 delay_days_danger / slow_progress_pct / risk_near_end_days）\n"
            "- value（可选）：数值型规则的目标值（数字）；状态词映射填 {变体词: 标准状态}；列名映射填 {表头: 字段}；忽略词填字符串列表\n"
            "- scope（可选）：适用环节，可选 解析/分析/呈现/全部，默认全部\n"
            "- applies_to（可选）：适用对象，可选 任务/风险/周报/看板/全部，默认全部\n"
            "判断依据：涉及「如何筛选/抽取/归类数据」→ 分析要求 requirement；涉及「周报怎么写/格式/排序/口径」→ 生成要求 generation_requirement。\n"
            "只输出 JSON 对象，不要其他文字。若用户不是表达规则而是其它意图，输出 {\"type\":\"other\",\"title\":\"\",\"description\":\"\"}。\n\n用户说：{instruction}"
        ),
        "docs": {"{instruction}": "用户用自然语言描述的规则需求"},
        "examples": [],
    },
    "rule_batch_intent": {
        "label": "规则文档批量理解（一键导入）",
        "system": "你是项目管理制度助手。从规则文档中批量提取规则，输出 JSON 数组，每项含 type/title/description，只输出数组。",
        "user": (
            "下面是一份项目管理制度/规则文档。请把其中的每一条规则/工作要求提取为 JSON 数组元素：\n"
            "{\"type\":\"rule|requirement|generation_requirement|status_map|column_map|ignore\","
            "\"title\":\"中文短标题\",\"description\":\"忠实概括原文（不要曲解）\",\"value\":null,\"key\":null}\n"
            "判断依据：涉及「如何筛选/抽取/归类数据」→ requirement（分析要求）；"
            "涉及「周报怎么写/格式/排序/口径」→ generation_requirement（生成要求）；"
            "数值阈值 → rule（value 填数字，key 尽量填对应字段名：风险超期天数→delay_days_danger、进度偏慢阈值→slow_progress_pct、临近预警天数→risk_near_end_days）；"
            "状态词/列名/忽略词 → 对应类型。\n"
            "{force_hint}"
            "规则文档：\n{rule_text}"
        ),
        "docs": {"{rule_text}": "用户上传的规则文档文本", "{force_hint}": "用户指定的类型强制说明（空则 AI 自动判断）"},
        "examples": [],
    },
    "ai_analysis": {
        "label": "分析 AI（上传时辅助结构化）",
        "system": "你是严谨的数据分析助手，只输出 JSON。所有抽取内容必须来自文档原文，禁止编造。",
        "user": (
            "下面是一份项目文档的 markdown 内容。请按【分析要求】抽取结构化数据，输出 JSON：\n"
            "{\"sections\":[{\"kind\":\"task|risk|issue|decision|milestone|metric\","
            "\"name\":\"条目名\",\"fields\":{\"关键字段\":\"值\"},\"source_note\":\"来源行片段\"}],"
            "\"summary\":\"一句话数据概览\"}\n"
            "【分析要求】\n{requirements}\n\n"
            "要求：\n"
            "1. 任务/成果/计划 → kind=task；风险/预警 → risk；里程碑 → milestone；"
            "指标/数值 → metric；决策/申请 → decision；依赖/协作 → issue。\n"
            "2. fields 保留原文关键列（进度/负责人/状态/日期/级别/影响等），列名用中文。\n"
            "3. 数字/日期/人名必须来自原文，禁止编造；不确定的字段留空。\n"
            "4. 每个板块最多 6 条，总量 60 条以内，保证 JSON 完整闭合。\n"
            "5. 只输出 JSON，不要其他文字。\n\n文档内容：\n{document_text}"
        ),
        "docs": {"{requirements}": "规则库分析要求（requirements）", "{document_text}": "markitdown 转换后的文档 markdown"},
        "examples": [],
    },
    "ai_pipeline_filter": {
        "label": "AI 主链路·数据筛选",
        "system": "你是严谨的数据抽取助手，只输出 JSON。",
        "user": (
            "下面是一份项目文档的 markdown 内容。请从中抽取结构化数据，输出 JSON：\n"
            "{\"sections\":[{\"kind\":\"task|risk|issue|decision|milestone|metric\","
            "\"name\":\"条目名\",\"fields\":{\"关键字段\":\"值\"},\"source_note\":\"来源行片段\"}],"
            "\"summary\":\"一句话数据概览\"}\n"
            "要求：\n"
            "1. 只抽取文档中明确存在的信息；数字/日期/人名必须来自原文，禁止编造。\n"
            "2. 任务类（任务/成果/计划）→ kind=task；风险/预警 → risk；里程碑 → milestone；"
            "指标/数值看板 → metric；决策/申请 → decision；依赖/协作 → issue。\n"
            "3. fields 保留原文中的关键列（进度/负责人/状态/日期/级别/影响等），列名用中文。\n"
            "4. **控制输出规模**：每个板块最多抽 6 条最重要的；指标板块每条保留本周值/上周值/变化；"
            "总量控制在 60 条以内，保证 JSON 完整闭合。\n"
            "5. 只输出 JSON，不要其他文字。\n\n文档内容：\n{document_text}"
        ),
        "docs": {"{document_text}": "markitdown 转换后的文档 markdown"},
        "examples": [],
    },
    "ai_pipeline_analyze": {
        "label": "AI 主链路·数据分析",
        "system": "你是资深的 PMO 分析专家。基于给定数据做分析，数字只能引用数据中出现过的，禁止编造。",
        "user": (
            "请基于以下结构化项目数据，按「工作要求」进行分析，输出一段纯文本分析（3-6 句，结论先行）：\n"
            "【工作要求】\n{requirements}\n\n"
            "【结构化数据】\n{data_json}\n\n"
            "分析要点：进度是否受控、主要风险与影响、需要管理层关注的事项、下一步建议。"
            "严格约束：文中所有数字/日期必须能在结构化数据中找到。"
        ),
        "docs": {"{requirements}": "规则库中的工作要求", "{data_json}": "AI 筛选出的结构化数据 JSON"},
        "examples": [],
    },
    "ai_pipeline_present": {
        "label": "AI 主链路·文稿呈现",
        "system": "你是专业的 PMO 周报撰写专家。严格按模板结构输出完整 HTML 周报；所有数字必须来自给定数据，禁止编造。",
        "user": (
            "请生成一份{report_type}。要求：\n"
            "1. 严格按【模板】的章节结构与标题组织（模板有几节就写几节，不要擅自增删章节）。\n"
            "2. 内容使用【分析结论】的要点 + 【结构化数据】的真实值填充；数字/日期/人名一律来自数据。\n"
            "3. 必须遵守【生成要求】（用户对文稿的固定要求，所有周报都要满足）。\n"
            "4. 输出完整 HTML（h1/h2/p/table/ul/li），不用 markdown 符号，不要代码块包裹。\n\n"
            "【生成要求】\n{generation_requirements}\n\n"
            "【模板】\n{template_text}\n\n"
            "【分析结论】\n{analysis}\n\n"
            "【结构化数据】\n{data_json}"
        ),
        "docs": {"{report_type}": "周报/日报", "{template_text}": "周报模板",
                 "{analysis}": "AI 分析结论", "{data_json}": "结构化数据 JSON",
                 "{generation_requirements}": "规则库生成要求"},
        "examples": [],
    },
}


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


def get_prompt(key: str) -> Dict:
    return load_prompts().get(key, {})


def render_user(entry: Dict, data: Dict[str, str]) -> str:
    """渲染 user 模板：替换 {占位符}，并追加用户示例（few-shot）。
    示例之间用空行分隔；在文本末尾以「参考示例（请模仿其风格与结构）」提示。"""
    text = entry.get("user", "")
    for k, v in (data or {}).items():
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
        if key not in PROMPT_DEFAULTS or not isinstance(o, dict):
            continue
        cleaned = {}
        if isinstance(o.get("system"), str) and o["system"].strip():
            cleaned["system"] = o["system"]
        if isinstance(o.get("user"), str) and o["user"].strip():
            cleaned["user"] = o["user"]
        if isinstance(o.get("examples"), list):
            cleaned["examples"] = [str(e) for e in o["examples"]]
        if cleaned:
            over[key] = cleaned
        else:
            over.pop(key, None)   # 空覆盖 = 恢复默认
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
            if k in over:
                del over[k]
                changed = True
        if changed:
            with open(PROMPTS_FILE, "w", encoding="utf-8") as f:
                json.dump(over, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# 提示词用途分组（前端展开式用途选择用）
PROMPT_PURPOSE: Dict[str, str] = {
    "ai_analysis": "分析 AI",
    "ai_pipeline_filter": "分析 AI",       # 已并入 ai_analysis（隐藏）
    "ai_pipeline_analyze": "分析 AI",      # 已并入 ai_analysis（隐藏）
    "dataset_report": "生成 AI",
    "ai_pipeline_present": "生成 AI",
    "report_overview": "生成 AI",
    "template_parse": "模板解析",
    "template_tune": "模板解析",
    "enrich_tasks": "解析兜底",
    "ai_review": "数据校对",
    "agent_intent": "Agent",
    "rule_intent": "规则管理",
    "rule_batch_intent": "规则管理",
}
# 已废弃（与 ai_analysis 重叠，用途选择中隐藏但保留代码）
HIDDEN_PROMTPTS = {"ai_pipeline_filter", "ai_pipeline_analyze"}


def prompts_status() -> Dict:
    """面板展示：每份提示词的当前值/默认值/是否被修改/占位符说明/用途分组。"""
    cur = load_prompts()
    over_keys = set(_read_overrides().keys())
    items = []
    for key, e in cur.items():
        d = PROMPT_DEFAULTS[key]
        items.append({
            "key": key, "label": e["label"],
            "purpose": PROMPT_PURPOSE.get(key, "其他"),
            "hidden": key in HIDDEN_PROMTPTS,
            "system": e["system"], "user": e["user"], "examples": e["examples"],
            "docs": e["docs"],
            "modified": key in over_keys,
            "default_system": d["system"], "default_user": d["user"],
            "default_examples": list(d.get("examples", [])),
        })
    return {"items": items, "example_sep": "---",
            "purposes": sorted(set(PROMPT_PURPOSE.values()))}
