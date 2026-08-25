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
    c.execute("""CREATE TABLE IF NOT EXISTS source_ops(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        op TEXT, source_id TEXT, filename TEXT, payload TEXT,
        created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS templates(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, ttype TEXT, category TEXT, html TEXT,
        created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS prompt_versions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        key TEXT, system TEXT, user TEXT, examples TEXT,
        created_at TEXT)""")
    # 迁移：旧库补充新列（templates.source_text 模板原文，供「原地更新」生成）
    try:
        c.execute("ALTER TABLE templates ADD COLUMN source_text TEXT")
    except sqlite3.OperationalError:
        pass  # 列已存在
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


def delete_source(sid: str, keep_snapshot: bool = True) -> None:
    """删除源文件（含其板块/条目）。keep_snapshot=True 时把原文件移入 .trash/ 并记操作日志（可撤销）。"""
    src = get_source(sid)
    snapshot = None
    if src and os.path.exists(src.get("stored_path", "")):
        if keep_snapshot:
            # 快照：把文件复制到 .trash/ 供撤销
            trash_dir = os.path.join(_data_dir(), "files", ".trash")
            os.makedirs(trash_dir, exist_ok=True)
            trash_path = os.path.join(trash_dir, f"{sid}_{os.path.basename(src['stored_path'])}")
            try:
                shutil.copyfile(src["stored_path"], trash_path)
                snapshot = {"stored_path": src["stored_path"], "trash_path": trash_path}
            except Exception:
                snapshot = None
        try:
            if os.path.exists(src["stored_path"]):
                os.remove(src["stored_path"])
        except Exception:
            pass
    with _conn() as c:
        c.execute("DELETE FROM sources WHERE id=?", (sid,))
        c.execute("DELETE FROM items WHERE source_id=?", (sid,))
        c.execute("DELETE FROM dataset_sections WHERE source_id=?", (sid,))
    if src and keep_snapshot:
        log_source_op("delete", sid, src.get("filename", ""),
                      {"snapshot": snapshot,
                       "record": {k: src.get(k) for k in ("filename", "ext", "uploaded_at", "group_name")}})


def restore_source(op_id: Optional[int] = None) -> Optional[Dict]:
    """撤销最近一次原始库操作（删除→恢复）。成功返回恢复的信息。"""
    op = None
    if op_id is not None:
        for o in list_source_ops(limit=500):
            if o["id"] == op_id:
                op = o
                break
    else:
        op = latest_source_op()
    if not op:
        return None
    payload = op.get("payload") or {}
    if op["op"] == "delete":
        # 撤销删除：把快照文件恢复为源记录
        snap = payload.get("snapshot") or {}
        rec = payload.get("record") or {}
        trash_path = snap.get("trash_path")
        if not trash_path or not os.path.exists(trash_path):
            return {"ok": False, "reason": "快照文件已不存在（可能已被清理）"}
        sid = op.get("source_id", "")
        ext = rec.get("ext") or os.path.splitext(op.get("filename", ""))[1].lower()
        month = datetime.now().strftime("%Y-%m")
        dest_dir = os.path.join(_data_dir(), "files", month)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, f"{sid}_{os.path.basename(trash_path)}")
        shutil.copyfile(trash_path, dest)
        with _conn() as c:
            c.execute(
                "INSERT INTO sources(id,filename,stored_path,uploaded_at,ext,group_name) VALUES(?,?,?,?,?,?)",
                (sid, op.get("filename", ""), dest,
                 rec.get("uploaded_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 ext, rec.get("group_name") or ""))
        try:
            if os.path.exists(trash_path):
                os.remove(trash_path)
        except Exception:
            pass
        log_source_op("restore", sid, op.get("filename", ""), {"from_op": op.get("id")})
        return {"ok": True, "op": "restore", "filename": op.get("filename", ""), "source_id": sid}
    if op["op"] == "restore":
        return {"ok": False, "reason": "该操作已是撤销结果，不能再撤销（如需删除请手动操作）"}
    return {"ok": False, "reason": f"不支持撤销操作类型 {op.get('op')}"}


# ---------------- 原始库操作留痕（删除/增添/撤销，可回溯） ----------------
def log_source_op(op: str, source_id: str, filename: str = "", payload: Optional[Dict] = None) -> int:
    """记录一次原始库操作。op ∈ add/delete/restore。payload 存撤销所需快照。"""
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO source_ops(op,source_id,filename,payload,created_at) VALUES(?,?,?,?,?)",
            (op, source_id or "", filename or "",
             json.dumps(payload or {}, ensure_ascii=False),
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        return int(cur.lastrowid)


def list_source_ops(limit: int = 100) -> List[Dict]:
    """操作留痕（新→旧）。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM source_ops ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["payload"] = json.loads(d["payload"] or "{}")
        except Exception:
            d["payload"] = {}
        out.append(d)
    return out


def latest_source_op() -> Optional[Dict]:
    """最近一次操作（供撤销）。"""
    rows = list_source_ops(limit=1)
    return rows[0] if rows else None


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


# ---------------- 模板库（命名入库 / 分类 / 按日期） ----------------
def save_template_lib(name: str, html: str, ttype: str = "week",
                      category: str = "", source_text: str = "") -> int:
    """命名入库一个模板（按类型 week/day + 自定义分类 + 日期）。返回 id。"""
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO templates(name,ttype,category,html,source_text,created_at) VALUES(?,?,?,?,?,?)",
            (name or "未命名模板", ttype or "week", category or "",
             html or "", source_text or "", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        return int(cur.lastrowid)


def list_template_lib(ttype: str = "", category: str = "", limit: int = 200) -> List[Dict]:
    """模板库列表（可按类型/分类过滤，新→旧）。"""
    sql = "SELECT * FROM templates WHERE 1=1"
    args: List = []
    if ttype:
        sql += " AND ttype=?"
        args.append(ttype)
    if category:
        sql += " AND category=?"
        args.append(category)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        rows = c.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def get_template_lib(tid: int) -> Optional[Dict]:
    with _conn() as c:
        r = c.execute("SELECT * FROM templates WHERE id=?", (tid,)).fetchone()
    return dict(r) if r else None


def delete_template_lib(tid: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM templates WHERE id=?", (tid,))


def latest_template_lib(ttype: str = "") -> Optional[Dict]:
    """该类型最新模板（生成时默认用）。"""
    rows = list_template_lib(ttype=ttype, limit=1)
    return rows[0] if rows else None


def template_categories() -> List[str]:
    """模板库已有分类（供下拉选择）。"""
    with _conn() as c:
        rows = c.execute("SELECT DISTINCT category FROM templates WHERE category<>''").fetchall()
    return [r["category"] for r in rows]


# ---------------- 提示词版本历史（每次保存记一版，可回退） ----------------
def save_prompt_version(key: str, system: str, user: str, examples: str) -> int:
    """记录一次提示词保存为版本。返回版本 id。"""
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO prompt_versions(key,system,user,examples,created_at) VALUES(?,?,?,?,?)",
            (key, system or "", user or "", examples or "",
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        return int(cur.lastrowid)


def list_prompt_versions(key: str = "", date: str = "", limit: int = 200) -> List[Dict]:
    """提示词版本历史（可按 key / 日期筛选，新→旧）。date 格式 YYYY-MM-DD。"""
    sql = "SELECT * FROM prompt_versions WHERE 1=1"
    args: List = []
    if key:
        sql += " AND key=?"
        args.append(key)
    if date:
        sql += " AND substr(created_at,1,10)=?"
        args.append(date)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        rows = c.execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["examples_list"] = json.loads(d["examples"] or "[]")
        except Exception:
            d["examples_list"] = []
        out.append(d)
    return out


def get_prompt_version(vid: int) -> Optional[Dict]:
    with _conn() as c:
        r = c.execute("SELECT * FROM prompt_versions WHERE id=?", (vid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    try:
        d["examples_list"] = json.loads(d["examples"] or "[]")
    except Exception:
        d["examples_list"] = []
    return d


# ---------------- 源文件分组（周报可引用整组数据） ----------------
def set_source_group(sid: str, group_name: str) -> None:
    """把源文件移入/移出分组（group_name 为空 = 移出）。"""
    with _conn() as c:
        c.execute("UPDATE sources SET group_name=? WHERE id=?", (group_name or "", sid))


def source_groups() -> List[Dict]:
    """分组列表（含成员数），按名称排序。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT group_name AS name, COUNT(*) AS count FROM sources "
            "WHERE group_name<>'' GROUP BY group_name ORDER BY group_name").fetchall()
    return [dict(r) for r in rows]


def sources_by_group(group_name: str) -> List[Dict]:
    """某分组下的所有源文件。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, filename, uploaded_at, ext, group_name FROM sources WHERE group_name=? ORDER BY uploaded_at",
            (group_name,)).fetchall()
    return [dict(r) for r in rows]


def delete_source_group(group_name: str) -> int:
    """删除分组（组内源文件移出分组，不删除文件）。返回移出数量。"""
    with _conn() as c:
        cur = c.execute("UPDATE sources SET group_name='' WHERE group_name=?", (group_name,))
        return cur.rowcount
