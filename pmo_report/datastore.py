# -*- coding: utf-8 -*-
"""分类数据仓 + 源数据仓库（SQLite，零新依赖）。

sources 表：原始上传文件（永久保留、按时间归档、可下载）—— 解决"解析后源数据丢失"
items 表：分类数据（task/risk/issue/decision/milestone/metric/raw）—— 一份资料可拆多类，
          按「分类 + 时间」查询，供看板/周报/自定义块取数。

数据目录可用环境变量 PM_DATA_DIR 覆盖（测试用）。
"""
from __future__ import annotations
import os
import json
import sqlite3
import shutil
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Tuple

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 数据目录动态解析：每次操作时读取 PM_DATA_DIR（测试可在 import 后覆盖，互不污染）
KINDS = ("task", "risk", "issue", "decision", "milestone", "metric", "raw")


def _data_dir() -> str:
    return os.environ.get("PM_DATA_DIR") or os.path.join(_BASE, "data_sources")


def _conn() -> sqlite3.Connection:
    data_dir = _data_dir()
    db_path = os.path.join(data_dir, "catalog.sqlite")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(os.path.join(data_dir, "files"), exist_ok=True)
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS sources(
        id TEXT PRIMARY KEY, filename TEXT, stored_path TEXT,
        uploaded_at TEXT, ext TEXT, group_name TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS items(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id TEXT, kind TEXT, name TEXT, payload TEXT,
        period TEXT, created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS dataset_sections(
        source_id TEXT PRIMARY KEY, sections TEXT, updated_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS workspace(
        id INTEGER PRIMARY KEY CHECK(id=1), state TEXT, updated_at TEXT)""")
    c.commit()
    return c


def save_source(filename: str, tmp_path: str, group_name: str = "") -> str:
    """把上传的原始文件永久归档，返回 source_id。"""
    sid = uuid.uuid4().hex[:12]
    ext = os.path.splitext(filename)[1].lower()
    month = datetime.now().strftime("%Y-%m")
    dest_dir = os.path.join(_data_dir(), "files", month)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"{sid}_{os.path.basename(filename)}")
    shutil.copyfile(tmp_path, dest)
    with _conn() as c:
        c.execute(
            "INSERT INTO sources(id,filename,stored_path,uploaded_at,ext,group_name) VALUES(?,?,?,?,?,?)",
            (sid, filename, dest, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ext, group_name),
        )
    return sid


def add_items(source_id: str, items: List[Tuple[str, str, Dict, str]]) -> int:
    """批量写入分类数据：items = [(kind, name, payload_dict, period), ...]。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n = 0
    with _conn() as c:
        for kind, name, payload, period in items:
            if not name or kind not in KINDS:
                continue
            c.execute(
                "INSERT INTO items(source_id,kind,name,payload,period,created_at) VALUES(?,?,?,?,?,?)",
                (source_id, kind, name, json.dumps(payload or {}, ensure_ascii=False), period or "", now),
            )
            n += 1
    return n


def list_sources() -> List[Dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM items i WHERE i.source_id=s.id) n_items "
            "FROM sources s ORDER BY uploaded_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_source(sid: str) -> Optional[Dict]:
    with _conn() as c:
        r = c.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
    return dict(r) if r else None


def find_sources_by_name(filename: str) -> List[Dict]:
    """按文件名精确查找（上传重名检测用）。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, filename, uploaded_at FROM sources WHERE filename=? ORDER BY uploaded_at DESC",
            (filename,)).fetchall()
    return [dict(r) for r in rows]


def delete_source(sid: str) -> None:
    src = get_source(sid)
    if src:
        try:
            if os.path.exists(src["stored_path"]):
                os.remove(src["stored_path"])
        except Exception:
            pass
    with _conn() as c:
        c.execute("DELETE FROM sources WHERE id=?", (sid,))
        c.execute("DELETE FROM items WHERE source_id=?", (sid,))
        c.execute("DELETE FROM dataset_sections WHERE source_id=?", (sid,))


def query_items(kind: Optional[str] = None, period: Optional[str] = None,
                source_id: Optional[str] = None, limit: int = 500) -> List[Dict]:
    sql = "SELECT * FROM items WHERE 1=1"
    args: List = []
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    if period:
        sql += " AND period=?"
        args.append(period)
    if source_id:
        sql += " AND source_id=?"
        args.append(source_id)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        rows = c.execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["payload"] = json.loads(d["payload"] or "{}")
        except Exception:
            d["payload"] = {}
        out.append(d)
    return out


def items_summary() -> Dict[str, int]:
    """各分类条目数（看板/源数据区展示）。"""
    with _conn() as c:
        rows = c.execute("SELECT kind, COUNT(*) n FROM items GROUP BY kind").fetchall()
    return {r["kind"]: r["n"] for r in rows}


# ---------------- 数据集板块（多 sheet 周报数据集，保留完整字段） ----------------
def save_dataset_sections(source_id: str, sections: List[Dict]) -> int:
    """把一个源文件的板块结构落盘（替换同源旧数据），返回板块数。"""
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO dataset_sections(source_id, sections, updated_at) VALUES(?,?,?)",
            (source_id, json.dumps(sections or [], ensure_ascii=False),
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
    return len(sections or [])


def get_dataset_sections(source_id: str) -> Optional[List[Dict]]:
    """读取某源文件的板块结构；无记录返回 None。"""
    with _conn() as c:
        r = c.execute("SELECT sections FROM dataset_sections WHERE source_id=?", (source_id,)).fetchone()
    if not r:
        return None
    try:
        return json.loads(r["sections"])
    except Exception:
        return None


def delete_dataset_sections(source_id: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM dataset_sections WHERE source_id=?", (source_id,))


# ---------------- 工作区持久化（分组 + sheet 元数据，重启不丢） ----------------
def save_workspace(sheets: Dict, groups: Dict) -> None:
    """把工作区状态（sheet 元数据 + 分组结构）落盘。
    sheets: {sid: {"name","source","source_id","project"(Project),"parse_stats","rule_tasks"}}
    只存可序列化字段；project 用 to_dict 落盘，恢复时 from_dict 还原。"""
    from .models import Project
    payload = {
        "sheets": {
            sid: {
                "name": sh.get("name", ""),
                "source": sh.get("source", ""),
                "source_id": sh.get("source_id", ""),
                "project": (sh.get("project") or Project()).to_dict(),
                "parse_stats": sh.get("parse_stats") or {},
                "rule_tasks": sh.get("rule_tasks") or [],
            }
            for sid, sh in (sheets or {}).items()
        },
        "groups": {
            gid: {"name": g.get("name", ""), "sheets": list(g.get("sheets") or [])}
            for gid, g in (groups or {}).items()
        },
    }
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO workspace(id, state, updated_at) VALUES(1,?,?)",
            (json.dumps(payload, ensure_ascii=False),
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )


def load_workspace() -> Optional[Dict]:
    """恢复工作区状态；无记录返回 None。"""
    from .models import Project
    with _conn() as c:
        r = c.execute("SELECT state FROM workspace WHERE id=1").fetchone()
    if not r:
        return None
    try:
        data = json.loads(r["state"])
    except Exception:
        return None
    sheets = {}
    for sid, meta in (data.get("sheets") or {}).items():
        proj = Project.from_dict(meta.get("project") or {})
        sheets[sid] = {
            "name": meta.get("name", proj.name),
            "source": meta.get("source", ""),
            "source_id": meta.get("source_id", ""),
            "project": proj,
            "parse_stats": meta.get("parse_stats") or {},
            "rule_tasks": meta.get("rule_tasks") or [],
        }
    groups = {
        gid: {"name": g.get("name", ""), "sheets": list(g.get("sheets") or [])}
        for gid, g in (data.get("groups") or {}).items()
    }
    return {"sheets": sheets, "groups": groups}
