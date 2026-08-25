# -*- coding: utf-8 -*-
"""自定义块库：用户保存的 AI 提示词块 / 公式计算块定义（本地 config/custom_blocks.json，gitignored）。
用法：面板「模板 → 🧩 自定义块」新建/保存/删除；插入画布时复制定义到 blocks。
"""
from __future__ import annotations
import os
import json
from typing import Dict, Optional

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLOCKS_FILE = os.path.join(_BASE, "config", "custom_blocks.json")


def _read() -> Dict[str, Dict]:
    try:
        with open(BLOCKS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write(data: Dict) -> None:
    os.makedirs(os.path.dirname(BLOCKS_FILE), exist_ok=True)
    with open(BLOCKS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_blocks() -> Dict[str, Dict]:
    """返回 {名称: 块定义}。"""
    return _read()


def save_block(name: str, definition: Dict) -> Dict:
    """保存/覆盖一个自定义块。"""
    blocks = _read()
    blocks[name.strip()] = definition
    _write(blocks)
    return blocks


def delete_block(name: str) -> Dict:
    blocks = _read()
    blocks.pop(name, None)
    _write(blocks)
    return blocks
