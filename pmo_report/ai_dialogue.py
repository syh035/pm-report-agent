# -*- coding: utf-8 -*-
"""通用 AI 对话弹窗后端：多轮对话 + 确认总结 + 对话配置。

设计（用户确认版）：
  - 对话弹窗：弹出窗口 + 有来有回的上下文对话，AI 逐步理解并回显确认
  - 每个用途可单独配置 model / temperature / 对话系统提示词（config/ai_dialogue.json）
  - 对话记忆仅限弹窗窗口内（前端维护 messages，本模块不持久化）
  - 用户点「确认完成」时调 finalize：AI 把完整对话总结为结构化结果 JSON，调用方执行
"""
from __future__ import annotations
import json
import os
from typing import Dict, List, Optional

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIALOGUE_CONFIG_FILE = os.path.join(_BASE, "config", "ai_dialogue.json")

# 默认对话系统提示词（用途通用；各用途可覆盖）
DEFAULT_DIALOGUE_SYSTEM = (
    "你是项目助手的对话确认助手。用户正在通过对话描述需求，你要做到：\n"
    "1. 用自然语言回显你理解到的内容，并询问用户是否正确（一次回显 1~2 条，不要一次列一堆）。\n"
    "2. 只确认用户明确说过的内容，不要替用户推断他没说的；有歧义就提问。\n"
    "3. 用户说「对/是的/确认」→ 简短确认并提示可继续或点完成；用户修正 → 重新回显。\n"
    "4. 每次回复保持简短（2~3 句），用中文。"
)

# 各用途的对话目标说明（finalize 时告诉 AI 怎么总结）
PURPOSE_FINALIZE_HINT: Dict[str, str] = {
    "rule_dialogue": (
        "把对话中用户确认的全部规则整理为 JSON 数组，每项："
        '{"type":"rule|requirement|generation_requirement|status_map|column_map|ignore",'
        '"title":"中文短标题","description":"忠实概括","value":数值或null,"key":字段名或null}。'
        "仅包含用户明确确认的规则；未确认的不要加入。只输出 JSON 数组。"
    ),
    "template_tune": (
        "把对话中用户确认的模板调整要求汇总，输出修改后的完整 HTML 模板。"
        "保留模板结构与占位符；只应用用户确认的调整。只输出 HTML。"
    ),
    "rule_doc_import": (
        "把对话中用户确认的全部规则整理为 JSON 数组，每项："
        '{"type":"rule|requirement|generation_requirement|status_map|column_map|ignore",'
        '"title":"中文短标题","description":"忠实概括","value":数值或null,"key":字段名或null}。'
        "仅包含用户确认的规则；未确认的不要加入。只输出 JSON 数组。"
    ),
}


def _load_dialogue_configs() -> Dict:
    """读取对话配置 {purpose: {model, temperature, system}}。"""
    try:
        if os.path.exists(DIALOGUE_CONFIG_FILE):
            with open(DIALOGUE_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save_dialogue_configs(configs: Dict) -> None:
    os.makedirs(os.path.dirname(DIALOGUE_CONFIG_FILE), exist_ok=True)
    with open(DIALOGUE_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(configs, f, ensure_ascii=False, indent=2)


def get_dialogue_config(purpose: str) -> Dict:
    """某用途的对话配置（默认继承全局 AI 配置 + 通用对话提示词）。"""
    from . import ai as ai_mod
    cfg = _load_dialogue_configs().get(purpose) or {}
    gcfg = ai_mod.ai_config()
    return {
        "model": cfg.get("model") or gcfg.get("model") or "deepseek-chat",
        "temperature": float(cfg.get("temperature", 0.4)),
        "system": cfg.get("system") or DEFAULT_DIALOGUE_SYSTEM,
    }


def save_dialogue_config(purpose: str, model: str = "", temperature: Optional[float] = None,
                         system: str = "") -> Dict:
    """保存某用途的对话配置（空值=用默认）。"""
    configs = _load_dialogue_configs()
    cur = dict(configs.get(purpose) or {})
    if model:
        cur["model"] = model
    if temperature is not None and temperature != "":
        cur["temperature"] = float(temperature)
    if system:
        cur["system"] = system
    configs[purpose] = cur
    _save_dialogue_configs(configs)
    return get_dialogue_config(purpose)


def dialogue_chat(purpose: str, messages: List[Dict], ai_module) -> Dict:
    """多轮对话：messages = [{role:user/assistant, content}...]，前端维护（窗口内记忆）。
    返回 {reply, config}。system 提示词 = 用途配置的对话系统提示词。"""
    cfg = get_dialogue_config(purpose)
    msgs = [{"role": "system", "content": cfg["system"]}] + [
        {"role": m.get("role"), "content": m.get("content", "")}
        for m in (messages or []) if m.get("role") in ("user", "assistant")
    ]
    out = ai_module.call_with_cache(
        "dialogue",
        msgs,
        model=cfg["model"], temperature=cfg["temperature"], max_tokens=600,
    )
    return {"reply": (out or "").strip(), "config": cfg}


def dialogue_finalize(purpose: str, messages: List[Dict], ai_module, prompts_module,
                      extra_context: str = "") -> Dict:
    """确认完成：AI 把完整对话总结为结构化结果。
    purpose 决定总结格式（规则列表 / HTML 模板）。返回 {ok, result}。"""
    hint = PURPOSE_FINALIZE_HINT.get(purpose, PURPOSE_FINALIZE_HINT.get("rule_dialogue", ""))
    transcript = "\n".join(
        f"{'用户' if m.get('role')=='user' else 'AI'}: {m.get('content','')}"
        for m in (messages or [])
    )
    if extra_context:
        transcript += f"\n\n补充材料:\n{extra_context[:4000]}"
    entry = prompts_module.get_prompt("dialogue_finalize")
    prompt = prompts_module.render_user(entry, {"finalize_hint": hint,
                                                "transcript": transcript[:12000]})
    cfg = get_dialogue_config(purpose)
    out = ai_module.call_with_cache(
        "dialogue_finalize",
        [{"role": "system", "content": entry.get("system", "")},
         {"role": "user", "content": prompt}],
        model=cfg["model"], temperature=0.1, max_tokens=2000,
    )
    out = (out or "").strip()
    if out.startswith("```"):
        out = out.replace("```json", "").replace("```", "").strip()
    return {"ok": True, "result": out}
