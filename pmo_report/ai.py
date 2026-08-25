# -*- coding: utf-8 -*-
"""
AI 层：DeepSeek 客户端 + 周报生成 + 文本提炼。

- call_deepseek()     底层 API 调用（记录 Token 用量，网络错误自动重试一次）
- call_with_cache()   带磁盘缓存的调用（相同输入不重复付费）
- enrich_tasks_from_text()  用 AI 把文本叙述提炼成结构化任务（供 text_parser 使用）
- usage_summary()     Token 用量统计（供面板展示）
"""
from __future__ import annotations
import os
import json
import re
import hashlib
import time
from datetime import datetime
from typing import Dict, List, Optional

import requests

from .models import Task


# ---------- 配置 ----------
CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")
KEYS_FILE = os.path.join(CONFIG_DIR, "keys.json")
_PLACEHOLDER_HINT = "在这里填入你的 DeepSeek API Key，或改填环境变量 DEEPSEEK_API_KEY"
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_FILE = os.path.join(_BASE, "history", "ai_cache.json")
USAGE_FILE = os.path.join(_BASE, "history", "token_usage.json")

CACHE_TTL_SECONDS = 7 * 24 * 3600      # 缓存有效期：7 天
CACHE_MAX_ENTRIES = 300                # 缓存条目上限（超出丢最旧）
USAGE_MAX_ENTRIES = 200                # 用量明细保留条数


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path: str, data) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------- AI 配置（模型 / API 地址，仅存本地 keys.json，gitignored） ----------
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_BASE_URL = "https://api.deepseek.com"
SUPPORTED_MODELS = [
    {"value": "deepseek-chat", "label": "deepseek-chat（通用，性价比高，推荐）"},
    {"value": "deepseek-reasoner", "label": "deepseek-reasoner（推理增强，更贵更慢，综述更深刻）"},
]


def ai_config() -> Dict[str, str]:
    """返回当前生效的 AI 配置（环境变量可覆盖 key；模型/地址从本地文件读）。"""
    cfg = _read_keys_file()
    model = (cfg.get("model") or "").strip() or DEFAULT_MODEL
    base_url = (cfg.get("base_url") or "").strip().rstrip("/") or DEFAULT_BASE_URL
    return {"model": model, "base_url": base_url}


def save_ai_config(model: str, base_url: str) -> Dict[str, str]:
    """保存模型与 API 地址到本地 keys.json（与 Key 同文件，均 gitignored）。"""
    model = (model or "").strip() or DEFAULT_MODEL
    base_url = (base_url or "").strip().rstrip("/") or DEFAULT_BASE_URL
    if base_url.startswith("http://") or base_url.startswith("https://"):
        base_url = base_url.rstrip("/")
    else:
        base_url = DEFAULT_BASE_URL
    cfg = _read_keys_file()
    cfg["model"] = model
    cfg["base_url"] = base_url
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(KEYS_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(KEYS_FILE, 0o600)
    except Exception:
        pass
    return ai_config()


def get_model() -> str:
    return ai_config()["model"]


def get_base_url() -> str:
    return ai_config()["base_url"]


# ---------- AI 结果缓存（相同输入不重复付费） ----------
def _cache_get(key: str) -> Optional[str]:
    store = _read_json(CACHE_FILE, {})
    item = store.get(key)
    if not item or not isinstance(item, dict):
        return None
    if time.time() - float(item.get("ts", 0)) > CACHE_TTL_SECONDS:
        return None
    return item.get("v")


def _cache_set(key: str, value: str) -> None:
    store = _read_json(CACHE_FILE, {})
    store[key] = {"v": value, "ts": time.time()}
    if len(store) > CACHE_MAX_ENTRIES:
        # 丢掉最早的条目（按 ts 排序）
        for k in sorted(store, key=lambda k: store[k].get("ts", 0))[: len(store) - CACHE_MAX_ENTRIES]:
            store.pop(k, None)
    _write_json(CACHE_FILE, store)


def _cache_key(site: str, model: str, messages: List[Dict], base_url: str = "", **kw) -> str:
    # 生成参数（max_tokens/temperature 等）也参与缓存键：调整参数不会命中旧缓存
    raw = json.dumps({"site": site, "model": model, "base_url": base_url,
                      "messages": messages, "params": kw}, ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# ---------- Token 用量统计 ----------
def _record_usage(site: str, tokens_in: int, tokens_out: int, cached: bool = False) -> None:
    usage = _read_json(USAGE_FILE, {"total_in": 0, "total_out": 0, "calls": 0, "cache_hits": 0, "entries": []})
    usage["total_in"] = int(usage.get("total_in", 0)) + int(tokens_in)
    usage["total_out"] = int(usage.get("total_out", 0)) + int(tokens_out)
    usage["calls"] = int(usage.get("calls", 0)) + (0 if cached else 1)
    usage["cache_hits"] = int(usage.get("cache_hits", 0)) + (1 if cached else 0)
    entries = usage.setdefault("entries", [])
    entries.insert(0, {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "site": site,
        "tokens_in": int(tokens_in),
        "tokens_out": int(tokens_out),
        "cached": cached,
    })
    usage["entries"] = entries[:USAGE_MAX_ENTRIES]
    _write_json(USAGE_FILE, usage)


def usage_summary() -> Dict:
    """返回 Token 用量统计（供面板展示）。"""
    u = _read_json(USAGE_FILE, {"total_in": 0, "total_out": 0, "calls": 0, "cache_hits": 0, "entries": []})
    return {
        "total_in": u.get("total_in", 0),
        "total_out": u.get("total_out", 0),
        "calls": u.get("calls", 0),
        "cache_hits": u.get("cache_hits", 0),
        "entries": u.get("entries", [])[:20],
    }


def clear_usage() -> None:
    try:
        if os.path.exists(USAGE_FILE):
            os.remove(USAGE_FILE)
    except Exception:
        pass


def _read_keys_file() -> dict:
    """读取 config/keys.json，失败返回空 dict。"""
    try:
        with open(KEYS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_api_key() -> Optional[str]:
    """优先环境变量，其次配置文件 config/keys.json。
    占位提示文本（未真正配置）视为未配置。"""
    key = os.getenv("DEEPSEEK_API_KEY")
    if key and key.strip():
        return key.strip()
    v = (_read_keys_file().get("deepseek_api_key") or "").strip()
    if v and v != _PLACEHOLDER_HINT and not v.startswith("在这里"):
        return v
    return None


def mask_key(key: str) -> str:
    """脱敏展示：sk-abc...xyz → sk-********xyz（保留前 3 位与后 4 位）。"""
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:3] + "*" * 8 + key[-4:]


def key_status() -> Dict[str, object]:
    """返回 Key 的脱敏状态（绝不返回完整 Key）。"""
    env_key = os.getenv("DEEPSEEK_API_KEY")
    if env_key and env_key.strip():
        return {"has_key": True, "masked": mask_key(env_key.strip()), "source": "env"}
    v = (_read_keys_file().get("deepseek_api_key") or "").strip()
    if v and v != _PLACEHOLDER_HINT and not v.startswith("在这里"):
        return {"has_key": True, "masked": mask_key(v), "source": "file"}
    return {"has_key": False, "masked": None, "source": "none"}


def save_api_key(api_key: str) -> Dict[str, object]:
    """校验并保存 API Key 到本地 config/keys.json。
    仅写入本机文件（已被 .gitignore 排除，不会进入 git / 上传任何服务器）。
    环境变量方式不受影响（环境变量优先级更高）。"""
    key = (api_key or "").strip()
    if not key:
        raise ValueError("API Key 不能为空")
    if not re.match(r"^sk-[A-Za-z0-9_-]{8,}$", key):
        raise ValueError("格式不正确：DeepSeek API Key 通常以 sk- 开头（如 sk-xxxx...），请检查是否粘贴完整")
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(KEYS_FILE, "w", encoding="utf-8") as f:
        json.dump({"deepseek_api_key": key}, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(KEYS_FILE, 0o600)  # 仅本用户可读写，进一步防泄漏
    except Exception:
        pass
    return key_status()


def clear_api_key() -> Dict[str, object]:
    """清除本地保存的 Key（删除 keys.json；不影响环境变量）。"""
    try:
        if os.path.exists(KEYS_FILE):
            os.remove(KEYS_FILE)
    except Exception:
        pass
    return key_status()


def test_api_key(api_key: Optional[str] = None) -> Dict[str, object]:
    """用指定 Key（缺省用已配置 Key）发一个最小请求验证有效性。"""
    try:
        out = call_deepseek(
            [{"role": "user", "content": "只回复两个字：正常"}],
            max_tokens=8,
            temperature=0,
            api_key=api_key,
        )
        return {"ok": True, "reply": (out or "")[:60]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def call_deepseek(
    messages: List[Dict[str, str]],
    model: str | None = None,
    temperature: float = 0.6,
    max_tokens: int = 2000,
    api_key: Optional[str] = None,
    site: str = "ai",
    retries: int = 1,
) -> str:
    """调用 AI 接口（模型/地址可在设置页配置，默认 deepseek-chat @ api.deepseek.com）。
    返回文本。若未配置 key 抛 RuntimeError。
    网络类错误（超时/连接）自动重试 retries 次；HTTP 错误（401 等）不重试。
    每次成功调用记录 Token 用量。"""
    key = api_key or get_api_key()
    if not key:
        raise RuntimeError("未配置 DeepSeek API Key。请设置环境变量 DEEPSEEK_API_KEY 或编辑 config/keys.json")

    model = model or get_model()
    base_url = get_base_url()
    url = base_url + "/chat/completions" if not base_url.endswith("/chat/completions") else base_url

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    last_err = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            usage = data.get("usage") or {}
            _record_usage(site,
                          int(usage.get("prompt_tokens", 0)),
                          int(usage.get("completion_tokens", 0)),
                          cached=False)
            return data["choices"][0]["message"]["content"].strip()
        except requests.exceptions.HTTPError as e:
            # 认证/限流等 HTTP 错误：不重试，直接抛出
            raise e
        except Exception as e:  # 超时/连接错误等：可重试
            last_err = e
    raise last_err


def call_with_cache(site: str, messages: List[Dict[str, str]], **kw) -> str:
    """带磁盘缓存的调用：相同输入命中缓存则 0 Token，否则调用并写入缓存。"""
    model = kw.get("model") or get_model()
    base_url = get_base_url()
    key = _cache_key(site, model, messages, base_url, **kw)
    hit = _cache_get(key)
    if hit is not None:
        _record_usage(site, 0, 0, cached=True)
        return hit
    text = call_deepseek(messages, site=site, **kw)
    _cache_set(key, text)
    return text


# ---------- 文本提炼（供解析层调用） ----------
def enrich_tasks_from_text(text: str, fallback_tasks: List[Task], max_chars: int = 3000) -> List[Task]:
    """让 AI 从文本提炼任务（建议传入规则预筛后的候选行，省 Token 且更准）。
    失败/无结果时仍返回 fallback_tasks。结果带磁盘缓存。"""
    if not text or not text.strip():
        return fallback_tasks
    try:
        from . import prompts as prompts_mod
        entry = prompts_mod.get_prompt("enrich_tasks")
        prompt = prompts_mod.render_user(entry, {"candidate_text": text[:max_chars]})
        out = call_with_cache(
            "enrich",
            [
                {"role": "system", "content": entry.get("system", "")},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=800,
        )
        # 容错解析：提取 [] 部分
        m = re.search(r"\[.*\]", out, re.S)
        if not m:
            return fallback_tasks
        try:
            data = json.loads(m.group(0))
        except Exception:
            return fallback_tasks
        if not isinstance(data, list):
            return fallback_tasks
        # Schema 严格校验（借鉴 schema-first LLM 思路）：非法条目丢弃并计数，防止半截数据污染统计
        _VALID_STATUS = {"已完成", "进行中", "未开始", "已滞后", "有风险"}
        tasks = []
        rejected = 0
        for item in data:
            if not isinstance(item, dict):
                rejected += 1
                continue
            name = str(item.get("name") or "").strip()
            if not name or len(name) < 2:
                rejected += 1
                continue
            t = Task(name=name, owner=str(item.get("owner") or "").strip(),
                     note=str(item.get("note") or "").strip())
            p = item.get("progress")
            if p is not None:
                try:
                    pv = float(p)
                    t.progress = None if pv < 0 or pv > 100 else (pv if pv > 1 else pv * 100)
                except (TypeError, ValueError):
                    t.progress = None
            s = str(item.get("status") or "").strip()
            t.status = s if s in _VALID_STATUS else ""
            pe = item.get("plan_end")
            if pe:
                from .parsers._date_util import parse_date
                t.plan_end = parse_date(str(pe))
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
    from . import prompts as prompts_mod
    entry = prompts_mod.get_prompt("template_parse")
    prompt = prompts_mod.render_user(entry, {"placeholders": placeholders_hint,
                                             "source_text": text[:6000]})
    out = call_with_cache(
        "template",
        [
            {"role": "system", "content": entry.get("system", "")},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        max_tokens=1500,
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
