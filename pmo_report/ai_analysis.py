# -*- coding: utf-8 -*-
"""上传时 AI 辅助分析（一条线架构的分析环节）。

背景：上传文档时，规则解析（确定性）先做一遍；对解析不充分的内容（非结构化文本、
规则未识别的行、数据集未覆盖的部分），若有 API Key，调用分析 AI 补全结构化：
  - 文本/Word/PDF → markitdown 转 markdown → AI 抽取结构化数据（tasks/风险/指标/里程碑…）
  - 注入规则库 requirements（分析要求）作为筛选/抽取准则
结果写回分类数据仓 items，供生成环节直接复用（生成不重读原文）。

无 Key 或失败 → 静默跳过（保留规则解析结果），不影响上传。
"""
from __future__ import annotations
import json
import re
from typing import Dict, List, Optional


def build_processing_rules(rules: Dict) -> str:
    """把规则库的引擎参数转换为分析 AI 的「处理约定」指令。
    主体是 AI：AI 分析数据时按这些约定输出（风险等级/忽略/归一化），不是规则引擎执行。"""
    lines = []
    if rules.get("delay_days_danger"):
        lines.append(f"- 任务超期超过 {rules['delay_days_danger']} 天 → 标记为高风险")
    if rules.get("risk_near_end_days"):
        lines.append(f"- 计划结束前 {rules['risk_near_end_days']} 天仍未完成 → 标记为关注")
    if rules.get("slow_progress_pct"):
        lines.append(f"- 实际进度落后时间计划超过 {rules['slow_progress_pct']} 个百分点 → 标记为关注/偏慢")
    if rules.get("ignore_keywords"):
        lines.append("- 以下内容不作为条目（忽略）：" + "、".join(str(k) for k in rules["ignore_keywords"]))
    sw = rules.get("status_words") or {}
    if sw:
        lines.append("- 状态词归一化：" + "、".join(f"「{k}」→「{v}」" for k, v in sw.items()))
    ca = rules.get("column_aliases") or {}
    if ca:
        lines.append("- 表头列名映射：" + "、".join(f"「{k}」→「{v}」" for k, v in ca.items()))
    if lines:
        return "【处理约定】（遇到这些情况时按此处理）\n" + "\n".join(lines)
    return "（无特殊处理约定）"


def ai_assist_analysis(source_id: str, stored_path: str, ext: str,
                       ai_module, prompts_module, rules_module,
                       existing_items: List[Dict],
                       dataset_sections: Optional[List[Dict]] = None) -> Dict:
    """对源文件做 AI 辅助分析（一条线：上传即分析）。

    - xlsx 有 dataset_sections（表格结构）时直接用它作为 AI 输入，不再转 markdown
    - 其他格式 markdown 转文本
    - 注入分析要求 requirements 作为筛选准则
    返回 {"sections": [...], "added": n, "note": str}；失败返回空 dict（不抛异常）。
    """
    try:
        if dataset_sections:
            from .dataset import sections_to_markdown
            md_text = sections_to_markdown(dataset_sections)
        else:
            # 1) 文档转 markdown（分析 AI 的输入）
            from .ai_pipeline import doc_to_markdown
            md_text = doc_to_markdown(stored_path)
        if not md_text or len(md_text.strip()) < 30:
            return {"sections": [], "added": 0, "note": "文档内容过短，跳过 AI 分析"}
    except Exception:
        return {"sections": [], "added": 0, "note": "文档转换失败，跳过 AI 分析"}

    try:
        # 2) 处理约定（引擎参数转指令）+ 分析要求（requirements）作为 AI 筛选准则
        rules = rules_module.load_rules()
        reqs = rules.get("requirements") or []
        reqs_text = "\n".join(f"- {r.get('title')}: {r.get('description')}" for r in reqs) or "（无额外要求）"
        proc_text = build_processing_rules(rules)

        # 3) 调用分析 AI 抽取结构化数据（主体是 AI，按处理约定输出含风险等级/忽略/归一化）
        entry = prompts_module.get_prompt("ai_analysis")
        prompt = prompts_module.render_user(entry, {
            "requirements": reqs_text,
            "processing_rules": proc_text,
            "document_text": md_text[:20000],
        })
        out = ai_module.call_with_cache(
            "ai_analysis",
            [{"role": "system", "content": entry.get("system", "")},
             {"role": "user", "content": prompt}],
            temperature=0.1, max_tokens=6000,
        )
        data = _parse_analysis_json(out or "")
        sections = data.get("sections") or []
        if not sections:
            return {"sections": [], "added": 0, "note": "AI 未抽取到结构化数据"}
    except Exception as e:
        return {"sections": [], "added": 0, "note": f"AI 分析失败：{str(e)[:100]}"}

    # 4) 去重 + 入库（避免与规则解析结果重复）
    added = 0
    from pmo_report import datastore
    from .dataset import _section_kind_hint
    seen = set()
    for it in existing_items:
        seen.add((it.get("kind"), str(it.get("name", "")).strip()))
    new_items = []
    for sec in sections:
        kind = sec.get("kind") or _section_kind_hint(sec.get("section", ""))
        name = str(sec.get("name") or "").strip()
        if not name:
            continue
        key = (kind, name)
        if key in seen:
            continue
        seen.add(key)
        payload = {"section": sec.get("section", ""),
                   "source_note": str(sec.get("source_note") or "")[:120],
                   "fields": sec.get("fields") or {}}
        # AI 未给 section 时，从 source_note（"板块2-核心指标"）提取
        if not payload["section"]:
            m = re.search(r"板块\s*([0-9]+)?[-－—]?\s*([\u4e00-\u9fa5A-Za-z0-9（）()]+)", payload["source_note"])
            if m:
                payload["section"] = m.group(0)
        new_items.append((kind, name, payload, ""))
    if new_items:
        datastore.add_items(source_id, new_items)
        added = len(new_items)
    return {"sections": sections, "added": added,
            "note": f"AI 辅助分析补全 {added} 条结构化数据"}


def _parse_analysis_json(text: str) -> Dict:
    """容错解析分析 AI 的 JSON 输出（可能被 ```json 代码块包裹）。"""
    if not text:
        return {}
    text = text.strip()
    # 剥代码块包裹
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"```\s*$", "", text)
    try:
        return json.loads(text)
    except Exception:
        pass
    # 从第一个 { 到最后一个 }（跨行）
    si, ei = text.find("{"), text.rfind("}")
    if si != -1 and ei > si:
        try:
            return json.loads(text[si:ei + 1])
        except Exception:
            pass
    return {}
