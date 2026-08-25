# -*- coding: utf-8 -*-
"""
规则配置管理：把规则引擎的阈值参数化，支持读取/保存/重置/历史。

核心设计：
  - DEFAULT_RULES：内置默认规则（唯一事实源）
  - config/rules.json：用户自定义规则（面板可改）
  - config/rules_history.json：历史规则快照（供查看/回滚，"清理旧规则"不会丢失）
"""
from __future__ import annotations
import json
import os
from copy import deepcopy
from datetime import datetime
from typing import Dict, Any, List

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(_BASE, "config")
RULES_FILE = os.path.join(CONFIG_DIR, "rules.json")
RULES_HISTORY_FILE = os.path.join(CONFIG_DIR, "rules_history.json")


# 内置默认规则 —— 唯一事实源
DEFAULT_RULES: Dict[str, Any] = {
    "delay_days_danger": 7,      # 超期超过 N 天 → 高风险
    "risk_near_end_days": 3,     # 计划结束前 N 天仍未完成 → 预警关注
    "slow_progress_pct": 40,     # 实际进度落后时间计划超过 N 个百分点 → 关注
    "progress_curve": "linear",  # 目标进度曲线：linear（线性）/ s_curve（S 型，前期慢后期快）
    "color_risk": "#C0504D",     # 风险标注色
    "color_warning": "#E65100",  # 关注/偏慢标注色
    "color_normal": "#2E7D32",   # 正常标注色
    # —— 路线C：可编辑规则库（本地持久化，越用越准）——
    "ignore_keywords": [],       # 忽略词：命中该词的文本行/任务名不作为任务（如"下周重点"）
    "column_aliases": {},        # 自定义列名映射：{"表头名": "字段"}，字段 ∈ name/owner/progress/status/plan_start/plan_end/actual_end/note
    "status_words": {},          # 自定义状态词映射：{"变体词": "标准状态"}，如 {"搁置": "已滞后"}
    # —— 对话式管理：AI 的工作要求（分析 AI 的要求，注入上传时的 AI 辅助分析）——
    "requirements": [],          # [{"title","description","scope","applies_to"}]
    # —— 生成要求（生成文稿 AI 的要求，注入所有生成方式）——
    "generation_requirements": [],  # [{"title","description","scope","applies_to"}]
}

# 数值型字段（int）—— 颜色字段用字符串
_NUMERIC_FIELDS = {"delay_days_danger", "risk_near_end_days", "slow_progress_pct"}
_COLOR_FIELDS = {"color_risk", "color_warning", "color_normal"}
_SELECT_FIELDS = {"progress_curve"}  # 枚举型字段
_SELECT_OPTIONS = {"progress_curve": ["linear", "s_curve"]}
_LIST_FIELDS = {"ignore_keywords"}            # 字符串列表字段
_DICT_FIELDS = {"column_aliases", "status_words"}  # 字符串→字符串映射字段

# 允许的列名映射目标字段
FIELD_NAMES = ("name", "owner", "progress", "status", "plan_start", "plan_end", "actual_end", "note")
# 允许的标准状态值（状态词映射目标）
STATUS_VALUES = ("已完成", "进行中", "未开始", "已滞后", "有风险")

# 字段元信息（供面板展示说明）
RULE_META = {
    "delay_days_danger": {"label": "高风险超期天数", "unit": "天", "min": 0, "max": 60,
                          "desc": "任务超期超过该天数时，标记为「高风险」", "type": "number"},
    "risk_near_end_days": {"label": "临近完成预警天数", "unit": "天", "min": 0, "max": 30,
                           "desc": "计划结束前 N 天仍未完成，标记为「关注」", "type": "number"},
    "slow_progress_pct": {"label": "进度偏慢阈值", "unit": "%", "min": 0, "max": 100,
                          "desc": "实际进度落后时间计划超过该百分点时，标记为「关注」（任务可单独豁免）", "type": "number"},
    "progress_curve": {"label": "目标进度曲线", "unit": "", "type": "select",
                       "options": [{"value": "linear", "label": "线性（匀速推进）"},
                                   {"value": "s_curve", "label": "S 型（前期慢、后期快）"}],
                       "desc": "目标进度的计算方式。S 型更贴近真实项目节奏，可减少前期任务被误报偏慢"},
    "ignore_keywords": {"label": "忽略词库", "unit": "", "type": "keywords",
                        "desc": "每行一个词：文本行/任务名命中该词则不作为任务（如「下周重点」「整体推进」）。删除误抓任务后会自动回写到这里"},
    "column_aliases": {"label": "自定义列名映射", "unit": "", "type": "mapping",
                       "desc": "每行一条，格式：表头名=字段，字段可选：" + "/".join(FIELD_NAMES) + "（如 完成百分比=progress）"},
    "status_words": {"label": "自定义状态词映射", "unit": "", "type": "mapping",
                     "desc": "每行一条，格式：变体词=标准状态，标准状态可选：" + "/".join(STATUS_VALUES) + "（如 搁置=已滞后）"},
    "color_risk": {"label": "风险标注颜色", "unit": "", "type": "color",
                   "desc": "标记「风险」任务的背景/边框颜色", "default": "#C0504D"},
    "color_warning": {"label": "关注标注颜色", "unit": "", "type": "color",
                      "desc": "标记「关注/进度偏慢」任务的颜色", "default": "#E65100"},
    "color_normal": {"label": "正常标注颜色", "unit": "", "type": "color",
                     "desc": "标记「正常」任务的颜色", "default": "#2E7D32"},
}


def _normalize(rules: Dict[str, Any]) -> Dict[str, Any]:
    """校验并规范化规则：非法/缺失字段回落默认，避免脏数据堆积。"""
    out = {}
    for k, default in DEFAULT_RULES.items():
        v = rules.get(k, default)
        if k in _COLOR_FIELDS:
            # 颜色字段：必须是 #RRGGBB 格式，否则回落默认
            if isinstance(v, str) and len(v) == 7 and v.startswith("#") and all(c in "0123456789abcdefABCDEF" for c in v[1:]):
                out[k] = v
            else:
                out[k] = default
        elif k in _SELECT_FIELDS:
            # 枚举字段：必须在白名单内
            if v in _SELECT_OPTIONS.get(k, []):
                out[k] = v
            else:
                out[k] = default
        elif k in _LIST_FIELDS:
            # 忽略词：字符串列表，逐条清洗去空
            if isinstance(v, list):
                out[k] = [str(x).strip() for x in v if str(x).strip()]
            else:
                out[k] = []
        elif k in _DICT_FIELDS:
            # 映射字段：{源: 目标}，校验目标合法
            if isinstance(v, dict):
                cleaned = {}
                for src, dst in v.items():
                    src_s, dst_s = str(src).strip(), str(dst).strip()
                    if not src_s or not dst_s:
                        continue
                    if k == "column_aliases" and dst_s not in FIELD_NAMES:
                        continue
                    if k == "status_words" and dst_s not in STATUS_VALUES:
                        continue
                    cleaned[src_s] = dst_s
                out[k] = cleaned
            else:
                out[k] = {}
        elif k in ("requirements", "generation_requirements"):
            # AI 工作要求：dict 列表，逐条保留 title/description/scope/applies_to
            if isinstance(v, list):
                cleaned = []
                for item in v:
                    if isinstance(item, dict) and str(item.get("title") or "").strip():
                        cleaned.append({
                            "title": str(item.get("title") or "").strip(),
                            "description": str(item.get("description") or "").strip(),
                            "scope": str(item.get("scope") or "全部"),
                            "applies_to": str(item.get("applies_to") or "全部"),
                        })
                out[k] = cleaned
            else:
                out[k] = []
        else:
            try:
                out[k] = int(float(v))
            except (TypeError, ValueError):
                out[k] = default
    return out


def custom_ignore_keywords(rules: Dict[str, Any] | None = None) -> List[str]:
    """忽略词（含默认值合并）。"""
    r = rules or load_rules()
    return list(r.get("ignore_keywords") or [])


def custom_column_aliases(rules: Dict[str, Any] | None = None) -> Dict[str, str]:
    """自定义列名映射：{表头: 字段}。"""
    r = rules or load_rules()
    return dict(r.get("column_aliases") or {})


def custom_status_words(rules: Dict[str, Any] | None = None) -> Dict[str, str]:
    """自定义状态词映射：{变体: 标准}。"""
    r = rules or load_rules()
    return dict(r.get("status_words") or {})


def load_rules() -> Dict[str, Any]:
    """加载当前生效的规则（自定义优先，否则默认）。"""
    if os.path.exists(RULES_FILE):
        try:
            with open(RULES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return _normalize(data)
        except Exception:
            pass
    return deepcopy(DEFAULT_RULES)


def save_rules(rules: Dict[str, Any]) -> Dict[str, Any]:
    """保存规则：先记历史快照，再写入当前配置。返回规范化后的规则。"""
    normed = _normalize(rules)
    os.makedirs(CONFIG_DIR, exist_ok=True)
    _append_history(normed)
    with open(RULES_FILE, "w", encoding="utf-8") as f:
        json.dump(normed, f, ensure_ascii=False, indent=2)
    return normed


def reset_rules() -> Dict[str, Any]:
    """清理旧规则：删除自定义配置，恢复默认。保留历史快照。"""
    if os.path.exists(RULES_FILE):
        os.remove(RULES_FILE)
    return deepcopy(DEFAULT_RULES)


# ---------- 历史快照 ----------

def _append_history(rules: Dict[str, Any]) -> None:
    """把一次规则变更写入历史，最多保留 50 条。"""
    history = _load_history()
    entry = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rules": rules,
    }
    history.insert(0, entry)
    history = history[:50]
    try:
        with open(RULES_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _load_history() -> List[Dict[str, Any]]:
    if os.path.exists(RULES_HISTORY_FILE):
        try:
            with open(RULES_HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def history() -> List[Dict[str, Any]]:
    return _load_history()


def clear_history() -> None:
    """清空规则历史（彻底清理）。"""
    if os.path.exists(RULES_HISTORY_FILE):
        os.remove(RULES_HISTORY_FILE)


def restore_from_history(index: int = 0) -> Dict[str, Any]:
    """从历史第 index 条恢复规则（默认最新一条）。"""
    h = _load_history()
    if not h or index >= len(h):
        return load_rules()
    return save_rules(h[index]["rules"])
