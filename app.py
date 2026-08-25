# -*- coding: utf-8 -*-
"""
PM Report Agent — FastAPI Web 操作面板后端。

启动： python -m uvicorn app:app --reload
访问： http://127.0.0.1:8000

API：
  数据/工作区：
    POST /api/workspace/load                上传文件->生成 sheet
    GET  /api/workspace                     获取完整工作区（含分组汇总）
    POST /api/workspace/sheet/{sid}/delete  删除 sheet
    POST /api/workspace/sheet/{sid}/rename  重命名 sheet
    POST /api/workspace/groups              新建分组
    POST /api/workspace/groups/{gid}/sheet  把 sheet 移入分组
    POST /api/workspace/groups/{gid}/delete 删除分组
  规则：
    GET/POST /api/rules, POST /api/rules/reset, GET /api/rules/history
  模板：
    GET/POST /api/template, POST /api/template/reset, POST /api/template/parse
  周报：
    POST /api/generate
    POST /api/export   导出（word/text）
  历史：
    GET /api/history, GET /api/history/{name}, POST /api/save_report
"""
from __future__ import annotations
import os
import shutil
import re
import uuid
import io
import json
from datetime import datetime
from typing import List, Optional, Dict

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pmo_report.parsers import parse_file
from pmo_report.engine import analyze
from pmo_report.report import ReportGenerator
from pmo_report.rules import load_rules
from pmo_report import rules as rules_mod
from pmo_report import ai as ai_mod
from pmo_report.export import html_to_text, html_to_docx_bytes
from pmo_report.models import Task, Project
from pmo_report.parsers._date_util import parse_date


BASE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE, "web")
HISTORY_DIR = os.path.join(BASE, "history")
UPLOAD_TMP = os.path.join(BASE, "tmp_uploads")
STATS_HISTORY_FILE = os.path.join(HISTORY_DIR, "stats_history.json")
os.makedirs(HISTORY_DIR, exist_ok=True)
os.makedirs(UPLOAD_TMP, exist_ok=True)


def _load_stats_history() -> List[Dict]:
    """读取历史统计（供周报环比）。"""
    try:
        with open(STATS_HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _append_stats_history(project_name: str, file_name: str, stats_dict: Dict,
                          rtype: str = "", period: str = "") -> None:
    """把一次周报/日报统计写入历史（最多 50 条）。
    project 用于环比匹配（按项目名），file 用于与历史文件对应；
    rtype=day/week 供文稿库分类，period 供归档。"""
    h = _load_stats_history()
    h.insert(0, {"time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                 "project": project_name, "file": file_name, "stats": stats_dict,
                 "rtype": rtype, "period": period})
    h = h[:50]
    try:
        with open(STATS_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(h, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


_SUMMARY_KEYS = ("total", "done", "in_progress", "not_started", "delayed", "risk",
                 "completion_rate", "avg_progress", "avg_target_progress")


def _compact_stats(stats_dict: Dict) -> Dict:
    """只保留汇总数字 + 每任务进度快照，供环比使用（不存完整任务明细）。
    tasks_progress 用于「连续两周无进展」与「同任务集合完成率」对比。"""
    out = {k: stats_dict.get(k) for k in _SUMMARY_KEYS if k in stats_dict}
    tp = {}
    for t in stats_dict.get("tasks") or []:
        if t.get("progress") is not None:
            tp[t.get("name", "")] = t.get("progress")
    if tp:
        out["tasks_progress"] = tp
    return out


def _latest_prev_stats(name: str = "") -> Optional[Dict]:
    """取最近一期统计作为环比基准。
    只与同名项目对比（避免拿别的项目的上一期来比，环比数字才可信）；无同名记录返回 None。"""
    if not name:
        return None
    h = _load_stats_history()
    for e in h:
        if e.get("project") == name:
            return e.get("stats")
    return None

ALLOWED_EXTS = {".xlsx", ".xlsm", ".csv", ".docx", ".pdf"}

app = FastAPI(title="PM Report Agent", version="0.11.0")


# ================= 工作区状态 =================
# sheets: {sid: {"name", "source", "project": {...}}}
# groups: {gid: {"name", "sheets": [sid,...]}}
WORKSPACE: Dict = {"sheets": {}, "groups": {}, "package_id": str(uuid.uuid4())[:8]}


def _persist_workspace() -> None:
    """把当前工作区（分组 + sheet 元数据）落盘，重启自动恢复。"""
    try:
        from pmo_report import datastore
        datastore.save_workspace(WORKSPACE["sheets"], WORKSPACE["groups"])
    except Exception:
        pass


def _restore_workspace() -> None:
    """启动时恢复上次的工作区（分组 + sheet 元数据）。"""
    try:
        from pmo_report import datastore
        st = datastore.load_workspace()
        if not st:
            return
        # 只恢复分组结构与 sheet 元数据；project 对象在 _workspace_view 需要时再解析？
        # —— 直接整体恢复：project 已由 from_dict 还原，stats 需要重算
        for sid, meta in (st.get("sheets") or {}).items():
            proj = meta.get("project")
            if proj is None:
                continue
            try:
                stats = analyze(proj, rules=load_rules())
            except Exception:
                stats = None
            WORKSPACE["sheets"][sid] = {
                "name": meta.get("name") or proj.name,
                "source": meta.get("source", ""),
                "source_id": meta.get("source_id", ""),
                "project": proj,
                "_stats": stats.to_dict() if stats else {},
                "parse_stats": meta.get("parse_stats") or {},
                "rule_tasks": meta.get("rule_tasks") or [],
            }
        WORKSPACE["groups"] = dict(st.get("groups") or {})
    except Exception:
        pass


_restore_workspace()


def _next_sid():
    return "S" + str(len(WORKSPACE["sheets"]) + 1)


def _summarize_sheet(sheets: list) -> Dict:
    """把一组 sheet 合并为分组汇总。
    与全工作区汇总同口径：任务去重（名称+负责人）、完成率/进度按任务数或权重计算，
    并重新计算 风险/关键路径/健康体检（而非简单累加）。"""
    all_tasks: List[Task] = []
    seen_keys = set()
    for sh in sheets:
        for t in (sh.get("project") or {}).tasks:
            key = (t.name, t.owner)
            if key not in seen_keys:
                seen_keys.add(key)
                all_tasks.append(t)
    all_tasks = _dedupe_tasks(all_tasks)
    merged_proj = Project(name="分组汇总", tasks=all_tasks)
    stats = analyze(merged_proj, rules=load_rules())
    return stats.to_dict()


def _workspace_view() -> Dict:
    """返回前端可用的工作区结构 + 各分组汇总。"""
    sheets_view = {}
    for sid, sh in WORKSPACE["sheets"].items():
        sheets_view[sid] = {
            "id": sid,
            "name": sh["name"],
            "source": sh["source"],
            "source_id": sh.get("source_id", ""),
            "stats": sh["_stats"],
            "tasks_full": [t.to_dict() for t in sh["project"].tasks],  # 供任务校对编辑
            "parse_stats": sh.get("parse_stats") or {},                # 解析质量度量
            "rule_tasks": sh.get("rule_tasks") or [],                  # AI 提炼前的规则快照（diff 视图）
        }
    groups_view = {}
    for gid, g in WORKSPACE["groups"].items():
        member_sheets = [WORKSPACE["sheets"][sid] for sid in g["sheets"] if sid in WORKSPACE["sheets"]]
        groups_view[gid] = {
            "id": gid,
            "name": g["name"],
            "sheets": g["sheets"],
            "summary": _summarize_sheet(member_sheets) if member_sheets else None,
        }
    return {"sheets": sheets_view, "groups": groups_view}


# ================= 数据模型 =================
class RulesIn(BaseModel):
    delay_days_danger: int
    risk_near_end_days: int
    slow_progress_pct: int
    progress_curve: Optional[str] = "linear"
    color_risk: Optional[str] = "#C0504D"
    color_warning: Optional[str] = "#E65100"
    color_normal: Optional[str] = "#2E7D32"
    # 路线C：可编辑规则库
    ignore_keywords: Optional[List[str]] = None
    column_aliases: Optional[Dict[str, str]] = None
    status_words: Optional[Dict[str, str]] = None
    # 对话式管理：AI 的工作要求（规则引擎之外，注入 AI 生成的指令）
    requirements: Optional[List[Dict]] = None


class RenameIn(BaseModel):
    name: str


class GroupIn(BaseModel):
    name: str


class GroupSheetIn(BaseModel):
    sheet_id: str


class BatchIn(BaseModel):
    """批量操作：action ∈ move | ungroup | delete；move 时 gid 为目标分组。"""
    action: str
    sheet_ids: List[str] = []
    gid: Optional[str] = None


class TemplateIn(BaseModel):
    content: str = ""
    format: Optional[str] = "html"   # html | blocks
    blocks: Optional[List] = None


class SaveReportIn(BaseModel):
    filename: str
    content: str
    stats: Optional[Dict] = None   # 统计汇总（写入环比历史）
    report_type: Optional[str] = ""   # day=日报 / week=周报 / other
    period: Optional[str] = ""        # 统计周期/日期（文稿库归档用）


class TaskUpdateIn(BaseModel):
    tasks: List[Dict]              # 校对后的完整任务列表


class ExportTasksIn(BaseModel):
    sheet_ids: str = ""            # 逗号分隔；空则导出全部


class ExporterIn(BaseModel):
    report_html: str
    format: str = "text"   # word | text
    filename: str = "周报"


class ApiKeyIn(BaseModel):
    api_key: str = ""


class KeyTestIn(BaseModel):
    api_key: Optional[str] = None  # 为空则用已保存/环境变量的 Key


# ================= 页面 =================
@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(WEB_DIR, "index.html"), "r", encoding="utf-8") as f:
        return f.read()


@app.get("/libraries.html", response_class=HTMLResponse)
def libraries_page():
    """数据库中心：所有库一页列出，点击弹新窗口。"""
    with open(os.path.join(WEB_DIR, "libraries.html"), "r", encoding="utf-8") as f:
        return f.read()


# 独立库页面（/lib/*.html，浏览器新标签页打开）
if os.path.isdir(os.path.join(WEB_DIR, "lib")):
    app.mount("/lib", StaticFiles(directory=os.path.join(WEB_DIR, "lib")), name="lib")


if os.path.isdir(os.path.join(WEB_DIR, "static")):
    app.mount("/static", StaticFiles(directory=os.path.join(WEB_DIR, "static")), name="static")


def _save_upload_to_tmp(uf: UploadFile) -> str:
    ext = os.path.splitext(uf.filename or "")[1].lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(400, f"不支持的文件类型: {ext}")
    tmp_path = os.path.join(UPLOAD_TMP, (uf.filename or "temp"))
    with open(tmp_path, "wb") as fo:
        shutil.copyfileobj(uf.file, fo)
    return tmp_path


# ================= 工作区 =================
@app.get("/api/workspace")
def workspace_view():
    return _workspace_view()


@app.post("/api/workspace/load")
async def workspace_load(files: List[UploadFile] = File(...),
                         overwrite: str = Form("false"),
                         rename: str = Form("false")):
    created = []
    renamed: List[Dict] = []
    for uf in files:
        tmp = _save_upload_to_tmp(uf)
        ext = os.path.splitext(uf.filename or "")[1].lower()
        fname = uf.filename or "未命名"
        try:
            # 源数据仓库：原始文件永久归档（不再删除）
            from pmo_report import datastore
            # 重名处理：
            #  - overwrite=true：删除同名旧源及其板块/条目，用原名覆盖
            #  - rename=true：自动改名为「原名(2).ext」，保留旧文件
            if rename.strip().lower() == "true":
                base, dot = os.path.splitext(fname)
                n = 2
                while datastore.find_sources_by_name(f"{base}({n}){dot}"):
                    n += 1
                new_name = f"{base}({n}){dot}"
                renamed.append({"old": fname, "new": new_name})
                fname = new_name
            elif overwrite.strip().lower() == "true":
                for old in datastore.find_sources_by_name(fname):
                    datastore.delete_source(old["id"])
                    # 工作区中引用该 source 的 sheet 一并移除
                    for sid in [s for s, sh in WORKSPACE["sheets"].items()
                                if sh.get("source_id") == old["id"]]:
                        del WORKSPACE["sheets"][sid]
                        for g in WORKSPACE["groups"].values():
                            if sid in g["sheets"]:
                                g["sheets"].remove(sid)
            source_id = datastore.save_source(fname, tmp)
            dataset_sections = None
            if ext in (".xlsx", ".xlsm"):
                # Excel 多 sheet：每个工作表生成一个独立 sheet 对象
                from pmo_report.parsers import tabular_parser
                sheet_projects = tabular_parser.parse_excel_all(tmp)
                # 数据集形态：保留每 sheet 的完整板块结构（指标/里程碑/风险/依赖/资源/预算/计划/RFA/附录），
                # 供「模板 + 数据集 → AI 严格按模板抓数生成周报」使用；同时分类入仓。
                try:
                    from pmo_report.dataset import parse_dataset_sheets
                    dataset_sections = parse_dataset_sheets(tmp)
                    datastore.save_dataset_sections(source_id, dataset_sections)
                except Exception:
                    dataset_sections = None
            else:
                sheet_projects = [(os.path.splitext(uf.filename or "")[0], parse_file(tmp, period=""))]
        except Exception as e:
            raise HTTPException(500, f"解析 {uf.filename} 失败: {e}")
        finally:
            os.path.exists(tmp) and os.remove(tmp)
        is_dataset = bool(dataset_sections)
        for name, proj in sheet_projects:
            sid = _next_sid()
            stats = analyze(proj, rules=load_rules())
            WORKSPACE["sheets"][sid] = {
                "name": proj.name or name,
                "source": uf.filename or "",
                "source_id": source_id,
                "project": proj,
                "_stats": stats.to_dict(),
                "parse_stats": proj.parse_stats,
                "rule_tasks": proj.rule_snapshot,
                "sheet_type": "dataset" if is_dataset else "task",
            }
            created.append(sid)
        # 分类入仓：任务 → task；风险项 → risk；未识别 → raw；
        # 数据集板块 → milestone/metric/decision/issue 等（板块化，不再强压成 task）
        if sheet_projects:
            items = []
            for _, proj in sheet_projects:
                for t in proj.tasks:
                    items.append(("task", t.name, {
                        "owner": t.owner, "progress": t.progress, "status": t.status,
                        "plan_end": t.plan_end.isoformat() if t.plan_end else None,
                        "note": t.note, "source_line": t.source_line,
                    }, ""))
            st_all = [analyze(p, rules=load_rules()) for _, p in sheet_projects]
            for st in st_all:
                for ts in st.task_stats:
                    if ts.risk_level != "正常":
                        items.append(("risk", ts.task.name, {
                            "level": ts.risk_level, "reason": ts.risk_reason, "source_line": ts.task.source_line,
                        }, ""))
            # 数据集板块分类入仓（保留字段，供看板/周报/自定义块取数）
            if dataset_sections:
                from pmo_report.dataset import _section_kind_hint
                for sec in dataset_sections:
                    kind = _section_kind_hint(sec.get("section", ""))
                    for r in sec.get("rows", []):
                        name = r.get(sec["headers"][0]) if sec.get("headers") else ""
                        name = str(name or "").strip()
                        if not name:
                            continue
                        items.append((kind, name, {"section": sec["section"], "fields": r}, ""))
            datastore.add_items(source_id, items)
            # 留痕：增添操作（上传成功记录）
            datastore.log_source_op("add", source_id, fname, {"sheets": len(sheet_projects)})
    _persist_workspace()
    return {"ok": True, "created": created, "renamed": renamed,
            "is_dataset": is_dataset, "workspace": _workspace_view()}


@app.post("/api/workspace/sheet/{sid}/delete")
def workspace_sheet_delete(sid: str):
    if sid not in WORKSPACE["sheets"]:
        raise HTTPException(404, "sheet 不存在")
    del WORKSPACE["sheets"][sid]
    for g in WORKSPACE["groups"].values():
        if sid in g["sheets"]:
            g["sheets"].remove(sid)
    _persist_workspace()
    return {"ok": True, "workspace": _workspace_view()}


@app.post("/api/workspace/sheet/{sid}/rename")
def workspace_sheet_rename(sid: str, payload: RenameIn):
    if sid not in WORKSPACE["sheets"]:
        raise HTTPException(404, "sheet 不存在")
    WORKSPACE["sheets"][sid]["name"] = payload.name.strip() or WORKSPACE["sheets"][sid]["name"]
    _persist_workspace()
    return {"ok": True, "workspace": _workspace_view()}


@app.post("/api/workspace/groups")
def workspace_group_create(payload: GroupIn):
    gid = "G" + str(len(WORKSPACE["groups"]) + 1)
    WORKSPACE["groups"][gid] = {"name": payload.name or "新分组", "sheets": []}
    _persist_workspace()
    return {"ok": True, "workspace": _workspace_view()}


@app.post("/api/workspace/groups/{gid}/sheet")
def workspace_group_add_sheet(gid: str, payload: GroupSheetIn):
    if gid not in WORKSPACE["groups"]:
        raise HTTPException(404, "分组不存在")
    sid = payload.sheet_id
    if sid in WORKSPACE["groups"][gid]["sheets"]:
        return {"ok": True, "workspace": _workspace_view()}
    # 先从其它组移除
    for g in WORKSPACE["groups"].values():
        if sid in g["sheets"]:
            g["sheets"].remove(sid)
    WORKSPACE["groups"][gid]["sheets"].append(sid)
    _persist_workspace()
    return {"ok": True, "workspace": _workspace_view()}


@app.post("/api/workspace/groups/{gid}/delete")
def workspace_group_delete(gid: str):
    if gid in WORKSPACE["groups"]:
        del WORKSPACE["groups"][gid]
    _persist_workspace()
    return {"ok": True, "workspace": _workspace_view()}


@app.post("/api/workspace/sheet/{sid}/ungroup")
def workspace_sheet_ungroup(sid: str):
    """把 sheet 移出所有分组（回到未分组区）。"""
    if sid not in WORKSPACE["sheets"]:
        raise HTTPException(404, "sheet 不存在")
    for g in WORKSPACE["groups"].values():
        if sid in g["sheets"]:
            g["sheets"].remove(sid)
    _persist_workspace()
    return {"ok": True, "workspace": _workspace_view()}


@app.post("/api/workspace/batch")
def workspace_batch(payload: BatchIn):
    """批量操作：move（移入/移出分组）、ungroup（全部移出）、delete（删除）。

    返回 {ok, done, missing, workspace}。部分失败不整体回滚，缺失的 sheet 计入 missing。
    """
    action = (payload.action or "").strip()
    if action not in {"move", "ungroup", "delete"}:
        raise HTTPException(400, "action 必须是 move / ungroup / delete")
    if not payload.sheet_ids:
        raise HTTPException(400, "sheet_ids 不能为空")
    if action == "move" and payload.gid not in WORKSPACE["groups"]:
        raise HTTPException(404, "目标分组不存在")
    done, missing = [], []
    for sid in payload.sheet_ids:
        if sid not in WORKSPACE["sheets"]:
            missing.append(sid)
            continue
        if action == "delete":
            del WORKSPACE["sheets"][sid]
            for g in WORKSPACE["groups"].values():
                if sid in g["sheets"]:
                    g["sheets"].remove(sid)
        elif action == "ungroup":
            for g in WORKSPACE["groups"].values():
                if sid in g["sheets"]:
                    g["sheets"].remove(sid)
        else:  # move
            if sid in WORKSPACE["groups"][payload.gid]["sheets"]:
                done.append(sid)
                continue
            for g in WORKSPACE["groups"].values():
                if sid in g["sheets"]:
                    g["sheets"].remove(sid)
            WORKSPACE["groups"][payload.gid]["sheets"].append(sid)
        done.append(sid)
    _persist_workspace()
    return {"ok": True, "done": done, "missing": missing, "workspace": _workspace_view()}


# ================= 源数据仓库（原始文件保留 + 行级对照 + 分类数据仓） =================
@app.get("/api/sources/duplicate-check")
def check_source_duplicate(name: str = ""):
    """上传前重名检测：返回已有同名源（前端弹窗让用户选 覆盖/改名/取消）。"""
    from pmo_report import datastore
    dup = datastore.find_sources_by_name((name or "").strip())
    return {"duplicate": bool(dup), "count": len(dup),
            "items": [{"id": d["id"], "filename": d["filename"], "uploaded_at": d["uploaded_at"]} for d in dup]}


@app.get("/api/sources")
def list_sources():
    from pmo_report import datastore
    items = datastore.list_sources()
    by_src: Dict[str, int] = {}
    for sh in WORKSPACE["sheets"].values():
        by_src[sh.get("source_id", "")] = by_src.get(sh.get("source_id", ""), 0) + 1
    for it in items:
        it["n_sheets"] = by_src.get(it["id"], 0)
        it["has_dataset"] = datastore.get_dataset_sections(it["id"]) is not None
    return {"items": items, "summary": datastore.items_summary()}


@app.get("/api/sources/ops")
def list_source_ops_api(limit: int = 100):
    """原始库操作留痕（删除/恢复记录，供查看修改历史）。"""
    from pmo_report import datastore
    return {"items": datastore.list_source_ops(limit=limit)}


@app.get("/api/sources/{sid}")
def source_detail(sid: str):
    """源数据详情：原文内容 + 解析任务（含 source_line 行级对照）。"""
    from pmo_report import datastore
    src = datastore.get_source(sid)
    if not src:
        raise HTTPException(404, "源数据不存在")
    ext = src["ext"]
    raw_rows, text = [], ""
    try:
        if ext in (".csv", ".txt"):
            from pmo_report.parsers.tabular_parser import _read_csv_rows
            raw_rows = _read_csv_rows(src["stored_path"])
        elif ext in (".docx",):
            from pmo_report.parsers.text_parser import _extract_text_docx
            text = _extract_text_docx(src["stored_path"])
        elif ext in (".pdf",):
            from pmo_report.parsers.text_parser import _extract_text_pdf
            text = _extract_text_pdf(src["stored_path"], use_ocr=False)
        else:
            with open(src["stored_path"], "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
    except Exception as e:
        text = f"[读取原文失败: {e}]"
    sheets = []
    for sid_key, sh in WORKSPACE["sheets"].items():
        if sh.get("source_id") == sid:
            sheets.append({"sid": sid_key, "name": sh["name"],
                           "tasks": [t.to_dict() for t in sh["project"].tasks],
                           "parse_stats": sh.get("parse_stats") or {}})
    # 数据集板块（若有）：源数据详情里可查看完整板块结构
    dataset_sections = None
    try:
        dataset_sections = datastore.get_dataset_sections(sid)
    except Exception:
        dataset_sections = None
    return {"source": src, "raw_rows": raw_rows, "text": text, "sheets": sheets,
            "dataset_sections": dataset_sections}


@app.get("/api/dataset/{sid}")
def dataset_detail(sid: str):
    """数据集板块详情（供前端展示 / AI 抓数预览）。"""
    from pmo_report import datastore
    src = datastore.get_source(sid)
    if not src:
        raise HTTPException(404, "源数据不存在")
    sections = datastore.get_dataset_sections(sid)
    if sections is None:
        raise HTTPException(404, "该文件没有数据集板块（仅 Excel 多 sheet 数据集支持）")
    return {"source": src, "sections": sections}


# ================= 原始库操作（删除/撤销/留痕） =================
@app.delete("/api/sources/{sid}")
def delete_source_api(sid: str):
    """删除源文件（留痕 + 快照，可撤销）。同时清理工作区中关联 sheet。"""
    from pmo_report import datastore
    src = datastore.get_source(sid)
    if not src:
        raise HTTPException(404, "源数据不存在")
    datastore.delete_source(sid, keep_snapshot=True)
    # 工作区中引用该 source 的 sheet 一并移除
    for sk in [s for s, sh in WORKSPACE["sheets"].items() if sh.get("source_id") == sid]:
        del WORKSPACE["sheets"][sk]
        for g in WORKSPACE["groups"].values():
            if sk in g["sheets"]:
                g["sheets"].remove(sk)
    _persist_workspace()
    return {"ok": True, "note": f"已删除「{src['filename']}」（留痕可撤销）", "filename": src["filename"]}


@app.post("/api/sources/undo")
def undo_source_op():
    """撤销最近一次原始库操作（删除→恢复）。"""
    from pmo_report import datastore
    r = datastore.restore_source()
    if not r:
        raise HTTPException(400, "没有可撤销的操作")
    if not r.get("ok"):
        raise HTTPException(400, r.get("reason", "撤销失败"))
    # 撤销删除恢复 sheet 需要重新解析源文件 → 重新加载工作区
    try:
        sid = r.get("source_id", "")
        src = datastore.get_source(sid)
        if src and os.path.exists(src.get("stored_path", "")):
            tmp = src["stored_path"]
            ext = os.path.splitext(src["filename"] or "")[1].lower()
            if ext in (".xlsx", ".xlsm"):
                from pmo_report.parsers import tabular_parser
                sheet_projects = tabular_parser.parse_excel_all(tmp)
            else:
                sheet_projects = [(os.path.splitext(src["filename"] or "")[0], parse_file(tmp, period=""))]
            from pmo_report.dataset import parse_dataset_sheets
            try:
                ds = parse_dataset_sheets(tmp)
                datastore.save_dataset_sections(sid, ds)
            except Exception:
                ds = None
            for name, proj in sheet_projects:
                nk = _next_sid()
                stats = analyze(proj, rules=load_rules())
                WORKSPACE["sheets"][nk] = {
                    "name": proj.name or name, "source": src.get("filename", ""),
                    "source_id": sid, "project": proj, "_stats": stats.to_dict(),
                    "parse_stats": proj.parse_stats, "rule_tasks": proj.rule_snapshot,
                    "sheet_type": "dataset" if ds else "task",
                }
            _persist_workspace()
    except Exception:
        pass
    return {"ok": True, "note": f"已撤销删除，恢复「{r.get('filename')}」", "filename": r.get("filename")}


@app.get("/api/sources/{sid}/download")
def source_download(sid: str):
    from pmo_report import datastore
    src = datastore.get_source(sid)
    if not src or not os.path.exists(src["stored_path"]):
        raise HTTPException(404, "源文件不存在")
    return StreamingResponse(open(src["stored_path"], "rb"),
                             media_type="application/octet-stream",
                             headers={"Content-Disposition": _content_disposition(src["filename"])})


@app.get("/api/items")
def list_items(kind: str = "", period: str = ""):
    """分类数据仓查询（kind：task/risk/issue/decision/milestone/metric/raw）。"""
    from pmo_report import datastore
    return {"items": datastore.query_items(kind=kind or None, period=period or None)}


@app.get("/api/dashboard")
def get_dashboard():
    """分组看板数据：全局汇总 + 各分组独立汇总 + 趋势序列 + 分类仓摘要。
    设计语言遵循看板 skill：KPI 卡带涨跌/迷你趋势、升即坏反色、分组各自成区不混在一起。"""
    rules = load_rules()
    all_sheets = [sh for sh in WORKSPACE["sheets"].values() if "project" in sh]
    global_stats = _summarize_sheet(all_sheets) if all_sheets else None
    groups = []
    for gid, g in WORKSPACE["groups"].items():
        member = [WORKSPACE["sheets"][s] for s in g["sheets"] if s in WORKSPACE["sheets"] and "project" in WORKSPACE["sheets"][s]]
        groups.append({"gid": gid, "name": g["name"], "summary": _summarize_sheet(member) if member else None})
    # 趋势序列（来自已保存的历史统计，按项目/分组名聚合出迷你趋势）
    h = _load_stats_history()
    series = [{"time": e.get("time", ""), "name": e.get("project") or e.get("file", ""),
               "completion_rate": (e.get("stats") or {}).get("completion_rate"),
               "risk": (e.get("stats") or {}).get("risk")} for e in h[:30]]
    from pmo_report import datastore
    return {"global": global_stats, "groups": groups, "trend": series,
            "items_summary": datastore.items_summary()}


@app.post("/api/workspace/sheet/{sid}/ai-review")
def ai_review_sheet(sid: str):
    """AI 深度解析：把规则解析结果交给 AI 校验/修正字段（复杂数据增强理解）。
    返回修正后的任务 + 原始规则快照（前端 diff 展示，人工确认后保存）。"""
    from pmo_report import prompts as prompts_mod
    if sid not in WORKSPACE["sheets"]:
        raise HTTPException(404, "sheet 不存在")
    sh = WORKSPACE["sheets"][sid]
    tasks = sh["project"].tasks
    if not tasks:
        raise HTTPException(400, "该 sheet 无任务可校验")
    payload = [{"name": t.name, "owner": t.owner, "progress": t.progress,
                "status": t.status, "plan_end": t.plan_end.isoformat() if t.plan_end else None,
                "note": t.note, "source_line": t.source_line} for t in tasks]
    entry = prompts_mod.get_prompt("ai_review")
    prompt = prompts_mod.render_user(entry, {"tasks_json": json.dumps(payload, ensure_ascii=False)})
    try:
        out = ai_mod.call_with_cache(
            "ai_review",
            [{"role": "system", "content": entry.get("system", "")},
             {"role": "user", "content": prompt}],
            temperature=0.2, max_tokens=1500,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}
    # 解析 + schema 校验（复用提炼校验口径）
    import re as _re
    m = _re.search(r"\[.*\]", out, _re.S)
    if not m:
        return {"ok": False, "error": "AI 未返回有效 JSON"}
    try:
        data = json.loads(m.group(0))
    except Exception:
        return {"ok": False, "error": "AI 返回 JSON 解析失败"}
    _VALID = {"已完成", "进行中", "未开始", "已滞后", "有风险"}
    corrected = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            continue
        t = Task(name=str(item.get("name") or "").strip(),
                 owner=str(item.get("owner") or "").strip(),
                 status=str(item.get("status") or "").strip() if str(item.get("status") or "").strip() in _VALID else "",
                 note=str(item.get("note") or "").strip(),
                 depends_on=str(item.get("depends_on") or "").strip())
        p = item.get("progress")
        try:
            t.progress = None if p in (None, "") else max(0.0, min(float(p), 100.0))
        except (TypeError, ValueError):
            t.progress = None
        pe = item.get("plan_end")
        if pe:
            t.plan_end = parse_date(str(pe))
        corrected.append(t.to_dict())
    if not corrected:
        return {"ok": False, "error": "AI 未产出有效任务"}
    return {"ok": True, "tasks": corrected,
            "rule_tasks": [t.to_dict() for t in tasks],
            "note": "AI 深度解析完成，请在编辑页核对（AI改 标注）后保存"}


# ================= Agent 一句话入口（受限 agent：意图识别 → 固定动作序列 → 执行日志） =================
AGENT_INTENTS = {"generate_report", "dashboard", "risks", "help"}


def _parse_agent_intent(instr: str, log: list):
    """意图识别：优先 AI（可编辑提示词），无 Key/失败时关键词回退。返回 (intent, params, ai_used)。"""
    from pmo_report import prompts as prompts_mod
    entry = prompts_mod.get_prompt("agent_intent")
    try:
        prompt = prompts_mod.render_user(entry, {"instruction": instr[:500]})
        out = ai_mod.call_with_cache(
            "agent_intent",
            [{"role": "system", "content": entry.get("system", "")},
             {"role": "user", "content": prompt}],
            temperature=0, max_tokens=120,
        )
        m = re.search(r"\{.*\}", out, re.S)
        data = json.loads(m.group(0)) if m else {}
        intent = str(data.get("intent") or "")
        if intent in AGENT_INTENTS:
            params = data.get("params") or {}
            log.append({"step": "意图识别", "tool": "LLM", "detail": f"{intent} {json.dumps(params, ensure_ascii=False)}"})
            return intent, params, True
    except Exception:
        pass
    # 关键词回退
    intent, params = "help", {}
    if "日报" in instr or "日" in instr.strip()[:6]:
        intent, params = "generate_report", {"report_type": "day"}
    elif "周报" in instr or "周" in instr.strip()[:6] or "生成" in instr or "做周报" in instr:
        intent, params = "generate_report", {"report_type": "week"}
    elif "风险" in instr or "预警" in instr:
        intent = "risks"
    elif "看板" in instr or "概览" in instr or "总体" in instr:
        intent = "dashboard"
    if "项目" in instr:
        m2 = re.search(r"项目[:：]?\s*([\u4e00-\u9fa5A-Za-z0-9]{2,20})", instr)
        if m2:
            params["project_name"] = m2.group(1)
    log.append({"step": "意图识别", "tool": "规则回退", "detail": f"{intent} {json.dumps(params, ensure_ascii=False)}"})
    return intent, params, False


def _agent_generate_report(params: dict, log: list):
    rules = load_rules()
    sheets = [sh for sh in WORKSPACE["sheets"].values() if "project" in sh]
    pname = (params.get("project_name") or "").strip()
    if pname:
        sheets = [sh for sh in sheets if pname in (sh.get("name") or "") or pname in (sh["project"].name or "")]
    log.append({"step": "收集数据", "tool": "工作区", "detail": f"选中 {len(sheets)} 个 sheet"})
    if not sheets:
        return {"report": "<p>（工作区暂无数据，请先上传）</p>", "text": "工作区暂无数据", "log_done": True}
    stats_list = [analyze(sh["project"], rules=rules) for sh in sheets]
    merged = _merge_stats(stats_list, name=params.get("project_name") or "Agent 生成", period=params.get("period") or "", rules=rules)
    log.append({"step": "统计", "tool": "规则引擎", "detail": f"任务 {merged.total_tasks} 完成率 {merged.completion_rate}%"})
    gen = ReportGenerator()
    result = gen.render(merged, use_ai=False, rules=rules)
    log.append({"step": "渲染", "tool": "模板", "detail": "周报 HTML 生成"})
    return {"report": result["report_html"], "text": result["report_text"]}


def _agent_risks(log: list):
    rows = []
    seen = set()
    for sh in WORKSPACE["sheets"].values():
        if "project" not in sh:
            continue
        stats = analyze(sh["project"], rules=load_rules())
        for ts in stats.task_stats:
            if ts.risk_level != "正常" and ts.task.name not in seen:
                seen.add(ts.task.name)
                rows.append(f"{ts.task.name}（{ts.risk_level}）：{ts.risk_reason or '需关注'}")
    log.append({"step": "查询", "tool": "规则引擎", "detail": f"风险 {len(rows)} 条"})
    if not rows:
        return {"report": "<p>无风险/预警项</p>", "text": "无风险/预警项"}
    html = '<ul style="margin:10px 0 10px 18px;line-height:1.8;">' + "".join(f"<li>{esc_(r)}</li>" for r in rows[:30]) + "</ul>"
    return {"report": html, "text": "\n".join(rows)}


def _agent_dashboard(log: list):
    from pmo_report import datastore
    rules = load_rules()
    all_sheets = [sh for sh in WORKSPACE["sheets"].values() if "project" in sh]
    g = _summarize_sheet(all_sheets) if all_sheets else None
    lines = []
    if g:
        lines.append(f"总任务 {g.get('total',0)}，完成率 {g.get('completion_rate')}%，平均实际 {g.get('avg_progress')}%，风险 {g.get('risk')}，滞后 {g.get('delayed')}，关键任务 {sum(1 for t in (g.get('tasks') or []) if t.get('is_critical'))}")
    for gid, gr in WORKSPACE["groups"].items():
        member = [WORKSPACE["sheets"][s] for s in gr["sheets"] if s in WORKSPACE["sheets"] and "project" in WORKSPACE["sheets"][s]]
        sg = _summarize_sheet(member) if member else None
        if sg:
            lines.append(f"分组「{gr['name']}」：任务 {sg.get('total',0)}，完成率 {sg.get('completion_rate')}%，风险 {sg.get('risk')}")
    lines.append("分类仓：" + "、".join(f"{k} {v}" for k, v in (datastore.items_summary() or {}).items()))
    log.append({"step": "查询", "tool": "看板", "detail": f"{len(lines)} 行概览"})
    text = "\n".join(lines) or "工作区暂无数据"
    html = "<p>" + "<br>".join(esc_(l) for l in lines) + "</p>"
    return {"report": html, "text": text}


def _agent_help(log: list):
    help_text = ("支持指令示例：\n"
                 "· 生成周报 / 生成日报（可加：项目：xxx）\n"
                 "· 列出风险 / 风险预警\n"
                 "· 看板概览 / 总体情况\n"
                 "· 帮助")
    log.append({"step": "帮助", "tool": "-", "detail": "返回帮助"})
    return {"report": f"<pre>{help_text}</pre>", "text": help_text}


def esc_(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class AgentRunIn(BaseModel):
    instruction: str = ""


@app.post("/api/agent/run")
def agent_run(payload: AgentRunIn):
    """受限 agent：意图识别（AI 或关键词回退）→ 固定动作序列（走现有工具）→ 执行日志。
    所有数字/统计仍由规则引擎与工具层产出，AI 只做意图理解，不参与计算。"""
    instr = (payload.instruction or "").strip()
    if not instr:
        raise HTTPException(400, "指令为空")
    log = []
    intent, params, ai_used = _parse_agent_intent(instr, log)
    if intent == "generate_report":
        result = _agent_generate_report(params, log)
    elif intent == "risks":
        result = _agent_risks(log)
    elif intent == "dashboard":
        result = _agent_dashboard(log)
    else:
        result = _agent_help(log)
    return {"ok": True, "intent": intent, "ai_used": ai_used, "log": log, **result}


@app.post("/api/workspace/sheet/{sid}/tasks")
def update_sheet_tasks(sid: str, payload: TaskUpdateIn):
    """保存 sheet 的任务校对结果（编辑/增删），并立即重算统计。"""
    if sid not in WORKSPACE["sheets"]:
        raise HTTPException(404, "sheet 不存在")
    proj = WORKSPACE["sheets"][sid]["project"]
    new_tasks = []
    for item in payload.tasks or []:
        t = Task(
            name=str(item.get("name") or "").strip(),
            owner=str(item.get("owner") or "").strip(),
            status=str(item.get("status") or "").strip(),
            note=str(item.get("note") or "").strip(),
            depends_on=str(item.get("depends_on") or "").strip(),
            slow_ok=bool(item.get("slow_ok")),
            critical=bool(item.get("critical")),
        )
        t.plan_start = parse_date(item.get("plan_start"))
        t.plan_end = parse_date(item.get("plan_end"))
        t.actual_end = parse_date(item.get("actual_end"))
        p = item.get("progress")
        try:
            t.progress = None if p in (None, "") else float(p)
        except (TypeError, ValueError):
            t.progress = None
        w = item.get("weight")
        try:
            t.weight = float(w) if w not in (None, "") else 1.0
        except (TypeError, ValueError):
            t.weight = 1.0
        if t.name:
            new_tasks.append(t)
    proj.tasks = new_tasks
    WORKSPACE["sheets"][sid]["_stats"] = analyze(proj, rules=load_rules()).to_dict()
    _persist_workspace()
    return {"ok": True, "n_tasks": len(new_tasks), "workspace": _workspace_view()}


def _extract_document_text(path: str) -> str:
    """从 Word/PDF/HTML/文本 中提取模板原文（供 AI 严格照抄结构）。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".docx",):
        from pmo_report.parsers.text_parser import _extract_text_docx
        return _extract_text_docx(path)
    if ext in (".pdf",):
        from pmo_report.parsers.text_parser import _extract_text_pdf
        return _extract_text_pdf(path, use_ocr=False)
    if ext in (".html", ".htm"):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


class DatasetReportIn(BaseModel):
    """模板+数据集生成：source_id 为数据集文件，template_text 可直接传文本。"""
    source_id: str
    template_text: str = ""


@app.post("/api/dataset/template-extract")
async def dataset_template_extract(file: UploadFile = File(...)):
    """上传周报模板文档（Word/PDF/HTML/txt），提取原文文本返回，供 dataset_report 使用。"""
    tmp = _save_upload_to_tmp(file)
    try:
        text = _extract_document_text(tmp)
    except Exception as e:
        raise HTTPException(500, f"模板提取失败: {e}")
    finally:
        os.path.exists(tmp) and os.remove(tmp)
    return {"ok": True, "filename": file.filename or "", "text": text[:20000]}


@app.post("/api/dataset/report")
def dataset_report(payload: DatasetReportIn):
    """核心能力：上传周报模板 + 数据集 → AI 严格按模板结构、从数据集板块抓数生成周报。

    数据一致性：模板中的示例数字是格式示意，AI 必须替换为数据集真实值；
    生成后返回报告 HTML，并在 detail 中提示数据集里的真实指标供核对。
    """
    from pmo_report import datastore
    from pmo_report.dataset import sections_to_markdown
    from pmo_report import prompts as prompts_mod
    src = datastore.get_source(payload.source_id)
    if not src:
        raise HTTPException(404, "源数据不存在")
    sections = datastore.get_dataset_sections(payload.source_id)
    if not sections:
        raise HTTPException(400, "该文件没有数据集板块（仅支持多 sheet 数据集 Excel）")
    template_text = (payload.template_text or "").strip()
    if not template_text:
        raise HTTPException(400, "请提供周报模板（文本，或在前端上传模板文档）")
    dataset_text = sections_to_markdown(sections)
    entry = prompts_mod.get_prompt("dataset_report")
    prompt = prompts_mod.render_user(entry, {"template_text": template_text[:8000],
                                             "dataset_text": dataset_text[:20000]})
    out = ai_mod.call_with_cache(
        "dataset_report",
        [{"role": "system", "content": entry.get("system", "")},
         {"role": "user", "content": prompt}],
        temperature=0.3, max_tokens=4000,
    )
    out = (out or "").strip()
    # 去掉可能的代码块包裹（含未闭合的 ```html 前缀）
    if out.startswith("```"):
        out = re.sub(r"^```(?:html)?\s*", "", out)
        out = re.sub(r"```\s*$", "", out)
    m = re.search(r"```(?:html)?\s*(.*?)```", out, re.S)
    if m:
        out = m.group(1).strip()
    if "<" not in out or ">" not in out:
        raise HTTPException(502, "AI 未产出有效 HTML 报告")
    # 提供数据集关键指标供人工核对（AI 生成内容不代写数字正确性）
    metrics = []
    for sec in sections:
        if "指标" in sec.get("section", "") or "kpi" in sec.get("section", "").lower():
            metrics = sec.get("rows", [])[:12]
            break
    return {"ok": True, "html": out, "source": src["filename"],
            "sections": [s["section"] for s in sections], "metrics": metrics}


class AiPipelineIn(BaseModel):
    """AI 主导主链路输入：上传的源文件 + 可选模板文本 + 报告类型。"""
    source_id: str
    template_text: str = ""
    report_type: str = "week"


@app.post("/api/ai-pipeline/run")
def ai_pipeline_run(payload: AiPipelineIn):
    """AI 主导主链路：markitdown 转 markdown → AI 筛选 → AI 分析 → AI 呈现。
    无 API Key 或任一环节失败 → 回退规则引擎（用现有数据集/模板路径生成）。
    返回 {ok, used_ai, markdown_len, filtered, analysis, html, fallback}。"""
    from pmo_report import datastore, prompts as prompts_mod
    from pmo_report.ai_pipeline import doc_to_markdown, ai_filter_data, ai_analyze, ai_present
    src = datastore.get_source(payload.source_id)
    if not src or not os.path.exists(src.get("stored_path", "")):
        raise HTTPException(404, "源数据不存在")
    # 1) 解析层：markitdown 转 markdown
    try:
        md_text = doc_to_markdown(src["stored_path"])
    except Exception as e:
        raise HTTPException(500, f"文档转 markdown 失败: {e}")
    if not md_text.strip():
        raise HTTPException(400, "文档内容为空（无法转换）")
    # 2~4) AI 三环节；失败回退规则引擎
    try:
        structured = ai_filter_data(md_text, ai_mod, prompts_mod)
        reqs = rules_mod.load_rules().get("requirements") or []
        analysis = ai_analyze(structured, reqs, ai_mod, prompts_mod)
        template = (payload.template_text or "").strip()
        if not template:
            try:
                from pmo_report.report import ReportGenerator
                template = ReportGenerator.get_template_text()
            except Exception:
                template = ""
        html = ai_present(analysis, structured, template, ai_mod, prompts_mod,
                          report_type=payload.report_type)
        if "<" not in html or ">" not in html:
            raise RuntimeError("AI 未产出有效 HTML")
        return {"ok": True, "used_ai": True, "markdown_len": len(md_text),
                "filtered": structured, "analysis": analysis, "html": html, "fallback": False}
    except Exception as e:
        # 回退：无 Key / AI 失败 → 用数据集/规则引擎路径
        try:
            sections = datastore.get_dataset_sections(payload.source_id)
            if sections:
                from pmo_report.dataset import sections_to_markdown
                ds_text = sections_to_markdown(sections)
            else:
                ds_text = md_text
            template = (payload.template_text or "").strip()
            if not template:
                from pmo_report.report import ReportGenerator
                template = ReportGenerator.get_template_text()
            entry = prompts_mod.get_prompt("dataset_report")
            prompt = prompts_mod.render_user(entry, {"template_text": template[:8000],
                                                     "dataset_text": ds_text[:20000]})
            out = ai_mod.call_with_cache(
                "dataset_report",
                [{"role": "system", "content": entry.get("system", "")},
                 {"role": "user", "content": prompt}],
                temperature=0.3, max_tokens=4000,
            )
            out = (out or "").strip()
            if out.startswith("```"):
                out = re.sub(r"^```(?:html)?\s*", "", out)
                out = re.sub(r"```\s*$", "", out)
            if "<" not in out or ">" not in out:
                raise RuntimeError("fallback 也未产出 HTML")
            return {"ok": True, "used_ai": True, "markdown_len": len(md_text),
                    "filtered": None, "analysis": "", "html": out, "fallback": True,
                    "note": f"AI 主链路失败（{str(e)[:150]}），已回退数据集生成"}
        except Exception as e2:
            raise HTTPException(502, f"AI 生成失败且回退失败: {e2}")


@app.post("/api/export/tasks")
def export_tasks(payload: ExportTasksIn):
    """导出任务明细为 CSV（utf-8-sig，Excel 打开不乱码）。"""
    import pandas as pd
    ids = [s.strip() for s in payload.sheet_ids.split(",") if s.strip()]
    sheets = [WORKSPACE["sheets"][sid] for sid in ids if sid in WORKSPACE["sheets"]]
    if not sheets:
        sheets = list(WORKSPACE["sheets"].values())
    if not sheets:
        raise HTTPException(400, "工作区暂无任务可导出")
    rows = []
    for sh in sheets:
        for t in sh["project"].tasks:
            d = t.to_dict()
            rows.append({
                "任务名称": d.get("name", ""),
                "负责人": d.get("owner", ""),
                "进度%": d.get("progress"),
                "状态": d.get("status", ""),
                "计划开始": d.get("plan_start", ""),
                "计划完成": d.get("plan_end", ""),
                "实际完成": d.get("actual_end", ""),
                "依赖任务": d.get("depends_on", ""),
                "权重": d.get("weight", 1),
                "偏慢豁免": "是" if d.get("slow_ok") else "",
                "备注": d.get("note", ""),
            })
    df = pd.DataFrame(rows)
    bio = io.BytesIO()
    df.to_csv(bio, index=False, encoding="utf-8-sig")
    bio.seek(0)
    fname = f"任务明细_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    return StreamingResponse(bio, media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": _content_disposition(fname)})


# ================= 规则 =================
@app.get("/api/rules")
def get_rules():
    return {"rules": rules_mod.load_rules(), "meta": rules_mod.RULE_META}


@app.post("/api/rules")
def save_rules(payload: RulesIn):
    rules = rules_mod.save_rules(payload.dict(exclude_none=True))
    # 规则变更后刷新已解析 sheet 的统计
    _recompute_all()
    return {"ok": True, "rules": rules, "note": "已保存并写入历史"}


@app.post("/api/rules/reset")
def reset_rules():
    rules = rules_mod.reset_rules()
    _recompute_all()
    return {"ok": True, "rules": rules, "note": "已恢复默认（旧规则已归档到历史）"}


class RulesLearnIn(BaseModel):
    ignore_names: List[str] = []      # 校对删除的任务名 → 忽略词
    status_maps: Dict[str, str] = {}  # 校对修正的状态变体 → 标准状态


@app.post("/api/rules/learn")
def learn_rules(payload: RulesLearnIn):
    """校对回写（路线C）：把用户在校对页的修正学习进规则库，本地持久化，越用越准。"""
    rules = rules_mod.load_rules()
    learned = {"ignore": [], "status": []}
    ignore = rules.setdefault("ignore_keywords", [])
    for name in payload.ignore_names:
        n = (name or "").strip()
        if n and n not in ignore:
            ignore.append(n)
            learned["ignore"].append(n)
    status = rules.setdefault("status_words", {})
    for src, dst in (payload.status_maps or {}).items():
        s, d = (src or "").strip(), (dst or "").strip()
        if s and d and d in rules_mod.STATUS_VALUES and status.get(s) != d:
            status[s] = d
            learned["status"].append(f"{s}→{d}")
    if learned["ignore"] or learned["status"]:
        rules_mod.save_rules(rules)
    return {"ok": True, "learned": learned,
            "note": f"已学习忽略词 {len(learned['ignore'])} 条、状态词 {len(learned['status'])} 条（本地生效）"}


class RulesConverseIn(BaseModel):
    instruction: str = ""


@app.post("/api/rules/converse")
def rules_converse(payload: RulesConverseIn):
    """规则对话式管理（第一步）：用户用自然语言描述规则/工作要求 →
    AI 理解并转成结构化规则 JSON（待人工确认，不直接写入）。"""
    from pmo_report import prompts as prompts_mod
    instr = (payload.instruction or "").strip()
    if not instr:
        raise HTTPException(400, "请描述您想要的规则或工作要求")
    entry = prompts_mod.get_prompt("rule_intent")
    prompt = prompts_mod.render_user(entry, {"instruction": instr[:1000]})
    try:
        out = ai_mod.call_with_cache(
            "rule_intent",
            [{"role": "system", "content": entry.get("system", "")},
             {"role": "user", "content": prompt}],
            temperature=0, max_tokens=400,
        )
    except Exception as e:
        raise HTTPException(502, f"AI 理解失败（可检查 API Key）：{e}")
    m = re.search(r"\{.*\}", out or "", re.S)
    data = json.loads(m.group(0)) if m else {}
    if not data.get("type") or data.get("type") == "other":
        return {"ok": True, "understood": False,
                "note": "AI 未能识别为规则需求，请换一种表达（如：超期超过10天标记高风险）",
                "draft": None}
    return {"ok": True, "understood": True, "draft": data,
            "note": "请确认下面 AI 理解的规则，确认后将写入规则库"}


class RulesConfirmIn(BaseModel):
    draft: Dict = {}


@app.post("/api/rules/converse/confirm")
def rules_converse_confirm(payload: RulesConfirmIn):
    """规则对话式管理（第二步）：人工确认后把 AI 理解的规则写入规则库。
    支持：数值阈值（key/value）、忽略词、状态词映射、列名映射、文本要求（存 requirements 列表）。"""
    draft = payload.draft or {}
    typ = str(draft.get("type") or "")
    rules = rules_mod.load_rules()
    applied = {"type": typ, "title": draft.get("title", ""), "detail": ""}
    if typ == "number" or (typ in ("rule", "数值阈值") and draft.get("key")):
        key = str(draft.get("key") or "")
        val = draft.get("value")
        if key and val is not None and key in rules_mod.DEFAULT_RULES:
            try:
                rules[key] = int(float(val))
                applied["detail"] = f"{key} = {rules[key]}"
            except (TypeError, ValueError):
                raise HTTPException(400, f"字段 {key} 的数值无效")
    elif typ in ("status_map", "状态词映射"):
        for src, dst in (draft.get("value") or {}).items():
            s, d = str(src).strip(), str(dst).strip()
            if s and d and d in rules_mod.STATUS_VALUES:
                rules.setdefault("status_words", {})[s] = d
        applied["detail"] = f"状态词映射 {json.dumps(draft.get('value') or {}, ensure_ascii=False)}"
    elif typ in ("column_map", "列名映射"):
        for src, dst in (draft.get("value") or {}).items():
            s, d = str(src).strip(), str(dst).strip()
            if s and d and d in rules_mod.FIELD_NAMES:
                rules.setdefault("column_aliases", {})[s] = d
        applied["detail"] = f"列名映射 {json.dumps(draft.get('value') or {}, ensure_ascii=False)}"
    elif typ in ("ignore", "忽略词"):
        for w in (draft.get("value") or []):
            w = str(w).strip()
            if w and w not in rules.setdefault("ignore_keywords", []):
                rules["ignore_keywords"].append(w)
        applied["detail"] = f"忽略词 {rules['ignore_keywords']}"
    elif typ in ("requirement", "文本要求"):
        # 文本要求：作为给 AI 的「工作要求」存 requirements（后续 AI 生成时注入）
        reqs = rules.setdefault("requirements", [])
        title = str(draft.get("title") or "").strip()
        desc = str(draft.get("description") or "").strip()
        if title and desc and all(r.get("title") != title for r in reqs):
            reqs.append({"title": title, "description": desc,
                         "scope": draft.get("scope") or "全部",
                         "applies_to": draft.get("applies_to") or "全部"})
        applied["detail"] = f"工作要求「{title}」已加入（共 {len(reqs)} 条）"
    else:
        raise HTTPException(400, f"暂不支持该规则类型: {typ}")
    rules_mod.save_rules(rules)
    _recompute_all()
    return {"ok": True, "rules": rules, "applied": applied,
            "note": "已确认并写入规则库"}


@app.get("/api/rules/export")
def export_rules():
    """导出规则库为 JSON 文件（备份/迁移用）。"""
    rules = rules_mod.load_rules()
    bio = io.BytesIO(json.dumps(rules, ensure_ascii=False, indent=2).encode("utf-8"))
    bio.seek(0)
    return StreamingResponse(bio, media_type="application/json; charset=utf-8",
                             headers={"Content-Disposition": _content_disposition("规则库导出.json")})


class RulesImportIn(BaseModel):
    rules: Dict


@app.post("/api/rules/import")
def import_rules(payload: RulesImportIn):
    """导入规则库（合并：导入值覆盖同名键，其余保留）。"""
    rules = rules_mod.load_rules()
    for k, v in (payload.rules or {}).items():
        if k in rules_mod.DEFAULT_RULES and v is not None:
            rules[k] = v
    normed = rules_mod.save_rules(rules)
    _recompute_all()
    return {"ok": True, "rules": normed, "note": "已导入规则库并重新统计"}


@app.get("/api/rules/history")
def rules_history():
    return {"history": rules_mod.history()}


def _recompute_all():
    """按当前规则重算所有 sheet 的统计。"""
    rules = load_rules()
    for sh in WORKSPACE["sheets"].values():
        if "project" in sh:
            sh["_stats"] = analyze(sh["project"], rules=rules).to_dict()


# ================= 设置（API Key 本地管理） =================
# 安全设计：Key 仅写入本机 config/keys.json（已被 .gitignore 排除），
# 所有读取接口只返回脱敏信息，绝不返回完整 Key。
@app.get("/api/settings/key")
def get_key_setting():
    return {"status": ai_mod.key_status()}


@app.post("/api/settings/key")
def save_key_setting(payload: ApiKeyIn):
    try:
        st = ai_mod.save_api_key(payload.api_key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "status": st,
            "note": "已保存到本地 config/keys.json（仅本机可见，不会进入 git / 不会上传）"}


@app.post("/api/settings/key/delete")
def delete_key_setting():
    st = ai_mod.clear_api_key()
    return {"ok": True, "status": st, "note": "已清除本地保存的 API Key"}


@app.post("/api/settings/key/test")
def test_key_setting(payload: KeyTestIn):
    return ai_mod.test_api_key(payload.api_key or None)


@app.get("/api/settings/tokens")
def get_token_usage():
    """Token 用量统计（累计 + 最近明细 + 缓存命中）。"""
    return ai_mod.usage_summary()


@app.post("/api/settings/tokens/clear")
def clear_token_usage():
    ai_mod.clear_usage()
    return {"ok": True, "note": "已清空 Token 用量统计"}


class AiConfigIn(BaseModel):
    model: str = ""
    base_url: str = ""


@app.get("/api/settings/ai")
def get_ai_config():
    """当前 AI 模型与接口配置（只回脱敏信息，本地持久化）。"""
    return {"config": ai_mod.ai_config(), "models": ai_mod.SUPPORTED_MODELS}


@app.post("/api/settings/ai")
def save_ai_config(payload: AiConfigIn):
    cfg = ai_mod.save_ai_config(payload.model, payload.base_url)
    return {"ok": True, "config": cfg,
            "note": "已保存模型与接口配置（仅本地 config/keys.json）"}


class PromptsSaveIn(BaseModel):
    overrides: Dict


@app.get("/api/settings/prompts")
def get_prompts():
    """提示词管理：默认值 + 用户覆盖 + 占位符说明 + 示例。"""
    from pmo_report import prompts as prompts_mod
    return prompts_mod.prompts_status()


@app.post("/api/settings/prompts")
def save_prompts(payload: PromptsSaveIn):
    """保存提示词覆盖（面板可编辑调试，本地 config/prompts.json）。"""
    from pmo_report import prompts as prompts_mod
    prompts_mod.save_prompts(payload.overrides or {})
    return {"ok": True, "items": prompts_mod.prompts_status()["items"],
            "note": "已保存提示词（本地 config/prompts.json，之后生成/提炼/模板解析立即生效）"}


@app.post("/api/settings/prompts/reset")
def reset_prompts():
    from pmo_report import prompts as prompts_mod
    prompts_mod.reset_prompts()
    return {"ok": True, "items": prompts_mod.prompts_status()["items"], "note": "已恢复全部默认提示词"}


# ================= 模板 =================
@app.get("/api/template")
def get_template():
    from pmo_report import report as report_mod
    from pmo_report.template_schema import (DEFAULT_BLOCKS, MODULE_LIBRARY, KPI_OPTIONS,
                                            PRESET_TEMPLATES)
    gen = ReportGenerator()
    custom = gen.load_custom_template()
    blocks = gen.load_blocks_template()
    if blocks is not None:
        return {
            "format": "blocks",
            "blocks": blocks,
            "default_blocks": DEFAULT_BLOCKS,
            "modules": MODULE_LIBRARY,
            "kpi_options": KPI_OPTIONS,
            "presets": PRESET_TEMPLATES,
            "placeholder_docs": report_mod.PLACEHOLDER_DOC,
            "ai_scopes": report_mod.AI_SCOPE_OPTIONS,
            "html": "",
            "is_custom": True,
        }
    return {
        "format": "html",
        "blocks": [],
        "default_blocks": DEFAULT_BLOCKS,
        "modules": MODULE_LIBRARY,
        "kpi_options": KPI_OPTIONS,
        "presets": PRESET_TEMPLATES,
        "placeholder_docs": report_mod.PLACEHOLDER_DOC,
        "html": custom or report_mod.DEFAULT_TEMPLATE,
        "is_custom": bool(custom),
    }


@app.post("/api/template")
def save_template(payload: TemplateIn):
    fmt = payload.format or "html"
    if fmt == "blocks":
        if not payload.blocks:
            raise HTTPException(400, "模板 blocks 不能为空")
        path = ReportGenerator.save_blocks_template(payload.blocks)
        _append_template_history(fmt="blocks", blocks=payload.blocks)
        return {"ok": True, "path": path, "format": "blocks"}
    if not payload.content.strip():
        raise HTTPException(400, "模板内容不能为空")
    path = ReportGenerator.save_custom_template(payload.content)
    _append_template_history(fmt="html", html=payload.content)
    return {"ok": True, "path": path, "format": "html"}


TEMPLATE_HISTORY_FILE = os.path.join(BASE, "config", "template_history.json")


def _append_template_history(fmt: str, html: str = "", blocks: Optional[list] = None) -> None:
    """模板保存时归档版本快照（按日期），供模板库日历浏览历史版本。"""
    try:
        hist = []
        if os.path.exists(TEMPLATE_HISTORY_FILE):
            with open(TEMPLATE_HISTORY_FILE, "r", encoding="utf-8") as f:
                hist = json.load(f) or []
        hist.append({
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "fmt": fmt,
            "html": (html or "")[:50000],
            "blocks": blocks,
        })
        # 只保留最近 100 个版本
        hist = hist[-100:]
        os.makedirs(os.path.dirname(TEMPLATE_HISTORY_FILE), exist_ok=True)
        with open(TEMPLATE_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


@app.get("/api/template/history")
def template_history():
    """模板版本历史（模板库日历浏览）。"""
    if not os.path.exists(TEMPLATE_HISTORY_FILE):
        return {"items": []}
    try:
        with open(TEMPLATE_HISTORY_FILE, "r", encoding="utf-8") as f:
            hist = json.load(f) or []
    except Exception:
        hist = []
    items = [{"ts": h.get("ts", ""), "fmt": h.get("fmt", ""),
              "preview": (h.get("html") or "")[:200]} for h in reversed(hist)]
    return {"items": items}


@app.post("/api/template/reset")
def reset_template():
    ReportGenerator.reset_template()
    return {"ok": True}


class CustomBlockIn(BaseModel):
    name: str
    definition: Dict


@app.get("/api/custom_blocks")
def get_custom_blocks():
    """自定义块库（AI 提示词块/公式计算块，本地保存复用）。"""
    from pmo_report import custom_blocks
    return {"blocks": custom_blocks.load_blocks()}


@app.post("/api/custom_blocks")
def save_custom_block(payload: CustomBlockIn):
    from pmo_report import custom_blocks
    blocks = custom_blocks.save_block(payload.name, payload.definition)
    return {"ok": True, "blocks": blocks, "note": f"已保存自定义块「{payload.name}」"}


@app.post("/api/custom_blocks/delete")
def delete_custom_block(payload: RenameIn):
    from pmo_report import custom_blocks
    blocks = custom_blocks.delete_block(payload.name)
    return {"ok": True, "blocks": blocks, "note": "已删除"}


@app.post("/api/template/parse")
async def template_parse(file: UploadFile = File(...)):
    """上传模板文件（docx/pdf/html/md/txt）-> 抽取文本 -> AI 转 HTML 模板。"""
    tmp = _save_upload_to_tmp(file)
    ext = os.path.splitext(file.filename or "")[1].lower()
    text = ""
    try:
        if ext in (".docx", ".pdf"):
            from pmo_report.parsers import text_parser
            if ext == ".docx":
                text = text_parser._extract_text_docx(tmp)
            else:
                text = text_parser._extract_text_pdf(tmp, use_ocr=True)
        else:
            with open(tmp, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
    finally:
        os.path.exists(tmp) and os.remove(tmp)
    if not text.strip():
        raise HTTPException(400, "无法从该文件抽取文本")
    try:
        html_template = ai_mod.parse_template_to_html(text)
        # 解析成功后直接保存为当前 HTML 模板（模块化模板已移除，模板库走 HTML）
        ReportGenerator.save_custom_template(html_template)
        _append_template_history(fmt="html", html=html_template)
        return {"ok": True, "html": html_template, "source_text": text[:2000], "saved": True}
    except Exception as e:
        return {"ok": False, "html": "", "source_text": text[:5000],
                "error": f"AI 解析失败（{e}）。以下是抽取的原始文本，可手动整理。"}


# ================= 周报生成 =================
@app.post("/api/generate")
async def generate(
    sheet_ids: str = Form(""),        # 逗号分隔；空则用所有 sheet 或上传文件
    project_name: str = Form(""),
    period: str = Form(""),
    use_ai: bool = Form(True),
    template_blocks: Optional[str] = Form(None),  # JSON blocks（可视化编辑器画布）
    files: List[UploadFile] = File(None),
):
    gen_template = None
    if template_blocks and template_blocks.strip():
        try:
            gen_template = json.loads(template_blocks)
        except Exception:
            gen_template = None

    # 收集要统计的 sheet project
    sheet_objs = []
    if sheet_ids.strip():
        for sid in [s.strip() for s in sheet_ids.split(",") if s.strip()]:
            if sid in WORKSPACE["sheets"]:
                sheet_objs.append(WORKSPACE["sheets"][sid])
    elif files and any(f.filename for f in files):
        for uf in files:
            if not uf.filename:
                continue
            try:
                tmp = _save_upload_to_tmp(uf)
                proj = parse_file(tmp, project_name=project_name, period=period)
                os.path.exists(tmp) and os.remove(tmp)
            except Exception:
                continue
            sheet_objs.append({"name": proj.name, "project": proj})
    else:
        # 没有指定 -> 用当前工作区所有 sheet（连同分组归属标识）
        for sh in WORKSPACE["sheets"].values():
            sheet_objs.append(sh)

    frame = "分组汇总" if sheet_ids.strip() else "全工作区汇总"
    gen = ReportGenerator(template=gen_template) if gen_template is not None else ReportGenerator()
    rules = load_rules()
    generated = []

    # 按分组聚合生成汇总周报（若 sheet_ids 指定的 sheet 同属一个分组，可按组）
    stats_list = []
    for sh in sheet_objs:
        proj = sh["project"]
        stats_list.append(analyze(proj, rules=rules))

    # 多 sheet：生成一组合并统计（含任务去重）
    merged = _merge_stats(stats_list, name=project_name or "项目汇总", period=period, rules=rules)
    prev = _latest_prev_stats(merged.project.name)   # 环比基准（上一期统计）
    if prev:
        # 环比增强①：连续两周无进展的任务（进度未变且未完成）
        prev_tp = prev.get("tasks_progress") or {}
        no_progress = [
            ts.task.name for ts in merged.task_stats
            if (ts.task.progress is not None and prev_tp.get(ts.task.name) is not None
                and abs(prev_tp.get(ts.task.name, 0) - ts.task.progress) < 0.5
                and ts.task.progress < 100)
        ]
        # 环比增强②：只对比两周都存在的任务集合，剔除集合变化影响
        cur_tp = {ts.task.name: ts.task.progress for ts in merged.task_stats}
        overlap = [n for n in cur_tp if n in prev_tp]
        prev = dict(prev)
        if no_progress:
            prev["no_progress"] = no_progress[:10]
        if len(overlap) >= 3:
            prev_done = sum(1 for n in overlap if (prev_tp[n] or 0) >= 100)
            cur_done = sum(1 for n in overlap if (cur_tp[n] or 0) >= 100)
            prev["overlap_rate"] = {
                "prev": round(prev_done / len(overlap) * 100, 1),
                "cur": round(cur_done / len(overlap) * 100, 1),
                "n": len(overlap),
            }
    tokens_before = ai_mod.usage_summary()
    # 分项目数据（供「重点项目表/分项目区块」等精细模块）
    projects_data = [
        {"name": (sh.get("name") or sh["project"].name or f"项目{i+1}"), "stats": st.to_dict()}
        for i, (sh, st) in enumerate(zip(sheet_objs, stats_list))
    ]
    result = gen.render(merged, use_ai=use_ai, rules=rules, prev_stats=prev, projects_data=projects_data)
    tokens_after = ai_mod.usage_summary()
    ignored_total = sum((sh.get("parse_stats") or {}).get("ignored", 0) for sh in sheet_objs)
    generated.append({
        "project": merged.project.name,
        "report": result["report_html"],
        "report_text": result["report_text"],
        "ai_used": result.get("ai_used", False),
        "ai_error": result.get("error"),
        "numbers_corrected": result.get("numbers_corrected", 0),
        "has_prev": bool(prev),
        "delta": ReportGenerator._delta_parts(merged, prev),
        "stats": merged.to_dict(),
        "frame": frame,
        "n_sheets": len(stats_list),
        "ignored": ignored_total,
        "tokens": {
            "in": tokens_after["total_in"] - tokens_before["total_in"],
            "out": tokens_after["total_out"] - tokens_before["total_out"],
            "cache_hits": tokens_after["cache_hits"] - tokens_before["cache_hits"],
        },
    })
    return {"generated": generated}


def _task_completeness(t: Task) -> int:
    """任务信息完整度（去重时保留信息更全的一份）。"""
    return sum(1 for v in (t.name, t.owner, t.progress, t.plan_start, t.plan_end, t.note) if v)


def _dedupe_tasks(tasks: List[Task]) -> List[Task]:
    """跨资料去重：同一任务（名称+负责人相同）只保留一份（信息最全者）。
    解决同一任务出现在「进度表 + 周会纪要」等多份资料中被重复计数的问题。"""
    best: Dict = {}
    for t in tasks:
        key = (t.name, t.owner)
        if key not in best or _task_completeness(t) > _task_completeness(best[key]):
            best[key] = t
    return list(best.values())


def _merge_stats(stats_list, name="项目汇总", period="", rules: Optional[Dict] = None):
    """把多个 ProjectStats 合并为一个（用于分组/全工作区汇总周报）。
    - 任务先去重（名称+负责人），再统一按 analyze 重新统计
    - 完成率/平均进度支持任务权重，避免大小表权重失衡与重复计数"""
    from pmo_report.engine import analyze as _analyze
    from pmo_report.models import Project
    from copy import deepcopy
    all_tasks: List[Task] = []
    seen_keys = set()
    for s in stats_list:
        for t in s.project.tasks:
            key = (t.name, t.owner)
            if key not in seen_keys:
                seen_keys.add(key)
                all_tasks.append(t)
    all_tasks = _dedupe_tasks(all_tasks)
    merged_proj = Project(name=name, period=period, tasks=all_tasks)
    return _analyze(merged_proj, rules=rules)


# ================= 导出 =================
def _content_disposition(filename: str):
    """构造 Content-Disposition，支持中文文件名（用 RFC5987 filename* 编码）。"""
    from urllib.parse import quote
    ascii_name = filename.encode("ascii", "replace").decode("ascii").replace("?", "_")
    encoded = quote(filename)
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"


@app.post("/api/export")
def export(payload: ExporterIn):
    text = html_to_text(payload.report_html)
    if payload.format == "word":
        bio = html_to_docx_bytes(payload.report_html)
        bio.seek(0)
        fname = f"{payload.filename or '周报'}.docx"
        return StreamingResponse(bio, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                 headers={"Content-Disposition": _content_disposition(fname)})
    # 纯文本
    bio = io.BytesIO(text.encode("utf-8"))
    bio.seek(0)
    fname = f"{payload.filename or '周报'}.txt"
    return StreamingResponse(bio, media_type="text/plain; charset=utf-8",
                             headers={"Content-Disposition": _content_disposition(fname)})


# ================= 历史 =================
def _history_stats_names() -> set:
    """有统计快照（可环比）的历史文件名集合。"""
    return {e.get("file", "") for e in _load_stats_history()}


@app.get("/api/history")
def list_history():
    if not os.path.isdir(HISTORY_DIR):
        return {"items": []}
    stats_names = _history_stats_names()
    meta = {e.get("file", ""): e for e in _load_stats_history()}
    items = []
    for fn in sorted(os.listdir(HISTORY_DIR)):
        if fn.endswith((".md", ".html", ".txt")):
            full = os.path.join(HISTORY_DIR, fn)
            m = meta.get(fn) or {}
            items.append({
                "name": fn,
                "size": os.path.getsize(full),
                "mtime": datetime.fromtimestamp(os.path.getmtime(full)).strftime("%Y-%m-%d %H:%M"),
                "has_stats": fn in stats_names,          # 环比徽标
                "project": m.get("project", ""),         # 所属项目（供前端分组）
                "rtype": m.get("rtype", ""),             # day/week：文稿库分类
                "period": m.get("period", ""),           # 周期/日期（归档）
            })
    items.reverse()
    return {"items": items}


class SummarizeIn(BaseModel):
    names: List[str] = []


@app.post("/api/documents/summarize")
def summarize_documents(payload: SummarizeIn):
    """文稿库多选汇总：把多份已保存的日报/周报统计合并生成一份汇总稿（确定性，不调 AI）。"""
    from pmo_report.engine import ProjectStats
    from pmo_report.models import Project
    wanted = set(payload.names)
    entries = [e for e in _load_stats_history() if e.get("file") in wanted]
    if not entries:
        raise HTTPException(400, "未找到可汇总的文稿（需先保存且带统计快照）")
    def s(e): return e.get("stats") or {}
    total = sum(s(e).get("total", 0) for e in entries)
    done = sum(s(e).get("done", 0) for e in entries)
    in_prog = sum(s(e).get("in_progress", 0) for e in entries)
    not_started = sum(s(e).get("not_started", 0) for e in entries)
    delayed = sum(s(e).get("delayed", 0) for e in entries)
    risk = sum(s(e).get("risk", 0) for e in entries)
    weights = [s(e).get("total", 0) for e in entries]
    wsum = sum(weights)
    st = ProjectStats(project=Project(name=f"多文稿汇总（{len(entries)} 份）", period=""))
    st.total_tasks = total
    st.done_count = done
    st.in_progress_count = in_prog
    st.not_started_count = not_started
    st.delayed_count = delayed
    st.risk_count = risk
    st.completion_rate = round(sum(s(e).get("completion_rate", 0) * w for e, w in zip(entries, weights)) / wsum, 1) if wsum else 0
    st.avg_progress = round(sum(s(e).get("avg_progress", 0) * w for e, w in zip(entries, weights)) / wsum, 1) if wsum else 0
    gen = ReportGenerator()
    result = gen.render(st, use_ai=False, rules=load_rules())
    return {"ok": True, "report": result["report_html"], "report_text": result["report_text"],
            "project": st.project.name, "n": len(entries),
            "names": [e.get("file", "") for e in entries]}


@app.get("/api/history/{name}")
def view_history(name: str):
    safe = os.path.basename(name)
    full = os.path.join(HISTORY_DIR, safe)
    if not os.path.exists(full):
        raise HTTPException(404, "历史周报不存在")
    with open(full, "r", encoding="utf-8") as f:
        return PlainTextResponse(f.read())


@app.post("/api/history/{name}/delete")
def delete_history(name: str):
    """删除历史周报（连同其环比统计快照）。"""
    safe = os.path.basename(name)
    full = os.path.join(HISTORY_DIR, safe)
    if os.path.exists(full):
        os.remove(full)
    h = _load_stats_history()
    h = [e for e in h if e.get("file") != safe]
    try:
        with open(STATS_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(h, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return {"ok": True}


class RenameHistoryIn(BaseModel):
    new_name: str


@app.post("/api/history/{name}/rename")
def rename_history(name: str, payload: RenameHistoryIn):
    """重命名历史周报（同步更新环比快照的 file 标识）。"""
    safe = os.path.basename(name)
    new_name = os.path.basename((payload.new_name or "").strip())
    if not new_name or new_name == safe:
        raise HTTPException(400, "新名称无效")
    old_full = os.path.join(HISTORY_DIR, safe)
    if not os.path.exists(old_full):
        raise HTTPException(404, "历史周报不存在")
    new_full = os.path.join(HISTORY_DIR, new_name)
    os.rename(old_full, new_full)
    h = _load_stats_history()
    for e in h:
        if e.get("file") == safe:
            e["file"] = new_name
    try:
        with open(STATS_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(h, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return {"ok": True, "name": new_name}


@app.get("/api/settings/trend")
def get_trend():
    """历史统计趋势（供面板画完成率/风险折线）。"""
    h = _load_stats_history()
    series = []
    for e in h[:20]:
        s = e.get("stats") or {}
        series.append({
            "time": e.get("time", ""),
            "name": e.get("project") or e.get("file", ""),
            "completion_rate": s.get("completion_rate"),
            "avg_progress": s.get("avg_progress"),
            "risk": s.get("risk"),
            "delayed": s.get("delayed"),
        })
    return {"series": series}


@app.post("/api/save_report")
def api_save_report(payload: SaveReportIn):
    if not payload.content.strip():
        raise HTTPException(400, "周报内容为空")
    safe = os.path.basename(payload.filename)
    with open(os.path.join(HISTORY_DIR, safe), "w", encoding="utf-8") as f:
        f.write(payload.content)
    # 写入环比历史（只存汇总数字+任务进度快照；project 供环比匹配；rtype/period 供文稿库分类）
    if payload.stats:
        proj = (payload.stats.get("project_name") or safe).strip()
        rtype = (payload.report_type or "").strip()
        if rtype not in ("day", "week", "other"):
            rtype = ""
        _append_stats_history(proj, safe, _compact_stats(payload.stats),
                              rtype=rtype, period=(payload.period or "").strip())
    return {"ok": True, "name": safe}
