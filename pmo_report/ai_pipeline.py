# -*- coding: utf-8 -*-
"""AI 主导主链路（架构转型核心）。

背景：写死的规则引擎应对不了范围外的数据形态。本模块提供一条 AI 主导管线：
  文档解析（markitdown 转 markdown，供 AI 理解）
    → AI 数据筛选（从 markdown 提取结构化数据，schema 校验）
    → AI 分析（按规则库中的"工作要求 requirements"分析）
    → AI 呈现（生成周报文稿）

规则引擎降级为"工作要求"：rules.json 里的 requirements 就是给 AI 的工作指令，
可在主面板对话式添加（见 app.py /api/rules/converse）。

设计约束：
  - AI 只做理解/筛选/表达，不参与计算；所有数字最终来自源文档。
  - 无 Key 或失败时回退到规则引擎路径（调用方处理）。
  - 输出做 schema 校验 + 数字一致性提示（不静默信任 AI）。
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import tempfile
from typing import Dict, List, Optional

# markitdown 需要 Python >=3.10，用独立解释器子进程调用（当前项目 venv 是 3.9）
MARKITDOWN_PY = os.environ.get("MARKITDOWN_PY") or "python3.12"

_MD_HELPER = """
import sys, json
from markitdown import MarkItDown
path = sys.argv[1]
try:
    md = MarkItDown()
    r = md.convert(path)
    print(r.text_content)
except Exception as e:
    print(f"[MARKITDOWN_ERROR] {e}", file=sys.stderr)
    sys.exit(1)
"""


def doc_to_markdown(path: str, timeout: int = 120) -> str:
    """用 markitdown 把文档转成 markdown（Word/Excel/PDF/PPT/图片等）。"""
    ext = os.path.splitext(path)[1].lower()
    # 纯文本/HTML 直接读，不走子进程
    if ext in (".txt", ".md", ".html", ".htm"):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    helper = os.path.join(tempfile.gettempdir(), "_md_helper.py")
    with open(helper, "w", encoding="utf-8") as f:
        f.write(_MD_HELPER)
    try:
        r = subprocess.run([MARKITDOWN_PY, helper, path],
                           capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError(f"未找到 markitdown 解释器 {MARKITDOWN_PY}（需 Python>=3.10 安装 markitdown）")
    except subprocess.TimeoutExpired:
        raise RuntimeError("markitdown 转换超时")
    if r.returncode != 0:
        err = r.stderr.strip()
        raise RuntimeError(f"markitdown 转换失败: {err[:300]}")
    return r.stdout or ""


# ---------------- AI 环节（均需 API Key；失败抛异常由调用方回退） ----------------
def _parse_json_lenient(text: str) -> Optional[Dict]:
    """容错解析 AI 的 JSON 输出：直接解析失败时，尝试补全括号 / 截断到配对处。"""
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"[\[{]", text)
    if not m:
        return None
    frag = text[m.start():]
    for close in ("}", "]}", "}]}", "}}", "]", "}]"):
        try:
            d = json.loads(frag + close)
            return d if isinstance(d, dict) else None
        except Exception:
            continue
    depth, cut = 0, 0
    for i, ch in enumerate(frag):
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                cut = i + 1
    if cut:
        try:
            d = json.loads(frag[:cut])
            return d if isinstance(d, dict) else None
        except Exception:
            pass
    return None

