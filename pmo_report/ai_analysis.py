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


def ai_assist_analysis(source_id: str, stored_path: str, ext: str,
                       ai_module, prompts_module, rules_module,
                       existing_items: List[Dict]) -> Dict:
    """对已解析的源文件做 AI 辅助分析补全。

    existing_items: 规则解析已入库的 items（避免重复抽取）。
    返回 {"sections": [...], "added": n, "note": str}；失败返回空 dict（不抛异常）。
    """
    try:
        # 1) 文档转 markdown（分析 AI 的输入）
        from .ai_pipeline import doc_to_markdown
        md_text = doc_to_markdown(stored_path)
        if not md_text or len(md_text.strip()) < 30:
            return {"sections": [], "added": 0, "note": "文档内容过短，跳过 AI 分析"}
    except Exception:
        return {"sections": [], "added": 0, "note": "markitdown 转换失败，跳过 AI 分析"}

    try:
        # 2) 分析要求（requirements）作为 AI 筛选准则
        rules = rules_module.load_rules()
        reqs = rules.get("requirements") or []
        reqs_text = "\n".join(f"- {r.get('title')}: {r.get('description')}" for r in reqs) or "（无额外要求）"

        # 3) 调用分析 AI 抽取结构化数据
        entry = prompts_module.get_prompt("ai_analysis")
        prompt = prompts_module.render_user(entry, {
            "requirements": reqs_text,
            "document_text": md_text[:20000],
        })
        out = ai_module.call_with_cache(
            "ai_analysis",
            [{"role": "system", "content": entry.get("system", "")},
             {"role": "user", "content": prompt}],
            temperature=0.1, max_tokens=3000,
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
        new_items.append((kind, name, payload, ""))
    if new_items:
        datastore.add_items(source_id, new_items)
        added = len(new_items)
    return {"sections": sections, "added": added,
            "note": f"AI 辅助分析补全 {added} 条结构化数据"}


def _parse_analysis_json(text: str) -> Dict:
    """容错解析分析 AI 的 JSON 输出。"""
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        pass
    return {}
