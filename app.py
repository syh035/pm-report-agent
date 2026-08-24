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


BASE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE, "web")
HISTORY_DIR = os.path.join(BASE, "history")
UPLOAD_TMP = os.path.join(BASE, "tmp_uploads")
os.makedirs(HISTORY_DIR, exist_ok=True)
os.makedirs(UPLOAD_TMP, exist_ok=True)

ALLOWED_EXTS = {".xlsx", ".xlsm", ".csv", ".docx", ".pdf"}

app = FastAPI(title="PM Report Agent", version="0.3.0")


# ================= 工作区状态 =================
# sheets: {sid: {"name", "source", "project": {...}}}
# groups: {gid: {"name", "sheets": [sid,...]}}
WORKSPACE: Dict = {"sheets": {}, "groups": {}, "package_id": str(uuid.uuid4())[:8]}


def _next_sid():
    return "S" + str(len(WORKSPACE["sheets"]) + 1)


def _summarize_sheet(sheets: list) -> Dict:
    """把一组 sheet 的 ProjectStats 合并成汇总。"""
    all_tasks = []
    total_avg_target = []
    total_completion = []
    total_risk = 0
    for sh in sheets:
        stats = sh.get("_stats")
        if not stats:
            continue
        all_tasks.extend(stats["tasks"])
        total_completion.append(stats["completion_rate"])
        total_risk += stats["risk"]
        if stats.get("avg_target_progress") is not None:
            total_avg_target.append(stats["avg_target_progress"])
    total = len(all_tasks)
    done = sum(1 for t in all_tasks if (t.get("progress") or 0) >= 100)
    return {
        "total": total,
        "done": done,
        "in_progress": sum(1 for t in all_tasks if 0 < (t.get("progress") or 0) < 100),
        "not_started": sum(1 for t in all_tasks if (t.get("progress") or 0) <= 0),
        "completion_rate": round(sum(total_completion) / len(total_completion), 1) if total_completion else 0,
        "avg_progress": round(sum(t.get("progress") or 0 for t in all_tasks) / total, 1) if total else 0,
        "avg_target_progress": round(sum(total_avg_target) / len(total_avg_target), 1) if total_avg_target else None,
        "risk": total_risk,
        "tasks": all_tasks,
    }


def _workspace_view() -> Dict:
    """返回前端可用的工作区结构 + 各分组汇总。"""
    sheets_view = {}
    for sid, sh in WORKSPACE["sheets"].items():
        sheets_view[sid] = {
            "id": sid,
            "name": sh["name"],
            "source": sh["source"],
            "stats": sh["_stats"],
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
    color_risk: Optional[str] = "#C0504D"
    color_warning: Optional[str] = "#E65100"
    color_normal: Optional[str] = "#2E7D32"


class RenameIn(BaseModel):
    name: str


class GroupIn(BaseModel):
    name: str


class GroupSheetIn(BaseModel):
    sheet_id: str


class TemplateIn(BaseModel):
    content: str = ""
    format: Optional[str] = "html"   # html | blocks
    blocks: Optional[List] = None


class SaveReportIn(BaseModel):
    filename: str
    content: str


class ExporterIn(BaseModel):
    report_html: str
    format: str = "text"   # word | text
    filename: str = "周报"


# ================= 页面 =================
@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(WEB_DIR, "index.html"), "r", encoding="utf-8") as f:
        return f.read()


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
async def workspace_load(files: List[UploadFile] = File(...)):
    created = []
    for uf in files:
        tmp = _save_upload_to_tmp(uf)
        try:
            proj = parse_file(tmp, period="")
        except Exception as e:
            raise HTTPException(500, f"解析 {uf.filename} 失败: {e}")
        finally:
            os.path.exists(tmp) and os.remove(tmp)
        sid = _next_sid()
        stats = analyze(proj, rules=load_rules())
        WORKSPACE["sheets"][sid] = {
            "name": proj.name or os.path.splitext(uf.filename or "")[0],
            "source": uf.filename or "",
            "project": proj,
            "_stats": stats.to_dict(),
        }
        created.append(sid)
    return {"ok": True, "created": created, "workspace": _workspace_view()}


@app.post("/api/workspace/sheet/{sid}/delete")
def workspace_sheet_delete(sid: str):
    if sid not in WORKSPACE["sheets"]:
        raise HTTPException(404, "sheet 不存在")
    del WORKSPACE["sheets"][sid]
    for g in WORKSPACE["groups"].values():
        if sid in g["sheets"]:
            g["sheets"].remove(sid)
    return {"ok": True, "workspace": _workspace_view()}


@app.post("/api/workspace/sheet/{sid}/rename")
def workspace_sheet_rename(sid: str, payload: RenameIn):
    if sid not in WORKSPACE["sheets"]:
        raise HTTPException(404, "sheet 不存在")
    WORKSPACE["sheets"][sid]["name"] = payload.name.strip() or WORKSPACE["sheets"][sid]["name"]
    return {"ok": True, "workspace": _workspace_view()}


@app.post("/api/workspace/groups")
def workspace_group_create(payload: GroupIn):
    gid = "G" + str(len(WORKSPACE["groups"]) + 1)
    WORKSPACE["groups"][gid] = {"name": payload.name or "新分组", "sheets": []}
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
    return {"ok": True, "workspace": _workspace_view()}


@app.post("/api/workspace/groups/{gid}/delete")
def workspace_group_delete(gid: str):
    if gid in WORKSPACE["groups"]:
        del WORKSPACE["groups"][gid]
    return {"ok": True, "workspace": _workspace_view()}


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


@app.get("/api/rules/history")
def rules_history():
    return {"history": rules_mod.history()}


def _recompute_all():
    """按当前规则重算所有 sheet 的统计。"""
    rules = load_rules()
    for sh in WORKSPACE["sheets"].values():
        if "project" in sh:
            sh["_stats"] = analyze(sh["project"], rules=rules).to_dict()


# ================= 模板 =================
@app.get("/api/template")
def get_template():
    from pmo_report import report as report_mod
    from pmo_report.template_schema import DEFAULT_BLOCKS, MODULE_LIBRARY, KPI_OPTIONS
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
            "html": "",
            "is_custom": True,
        }
    return {
        "format": "html",
        "blocks": [],
        "default_blocks": DEFAULT_BLOCKS,
        "modules": MODULE_LIBRARY,
        "kpi_options": KPI_OPTIONS,
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
        return {"ok": True, "path": path, "format": "blocks"}
    if not payload.content.strip():
        raise HTTPException(400, "模板内容不能为空")
    path = ReportGenerator.save_custom_template(payload.content)
    return {"ok": True, "path": path, "format": "html"}


@app.post("/api/template/reset")
def reset_template():
    ReportGenerator.reset_template()
    return {"ok": True}


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
        return {"ok": True, "html": html_template, "source_text": text[:2000]}
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

    # 多 sheet：生成一组合并统计
    merged = _merge_stats(stats_list, name=project_name or "项目汇总", period=period)
    result = gen.render(merged, use_ai=use_ai, rules=rules)
    generated.append({
        "project": merged.project.name,
        "report": result["report_html"],
        "report_text": result["report_text"],
        "ai_used": result.get("ai_used", False),
        "ai_error": result.get("error"),
        "frame": frame,
        "n_sheets": len(stats_list),
    })
    return {"generated": generated}


def _merge_stats(stats_list, name="项目汇总", period=""):
    """把多个 ProjectStats 合并为一个（用于分组/全工作区汇总周报）。"""
    from pmo_report.engine import ProjectStats
    from pmo_report.models import Project
    total_tasks = sum(s.total_tasks for s in stats_list)
    done = sum(s.done_count for s in stats_list)
    in_prog = sum(s.in_progress_count for s in stats_list)
    not_started = sum(s.not_started_count for s in stats_list)
    delayed = sum(s.delayed_count for s in stats_list)
    rates = [s.completion_rate for s in stats_list if s.total_tasks]
    progs = [s.avg_progress for s in stats_list if s.total_tasks]
    targets = [s.avg_target_progress for s in stats_list if s.avg_target_progress is not None]
    risk = sum(s.risk_count for s in stats_list)

    stats = ProjectStats(project=Project(name=name, period=period))
    stats.total_tasks = total_tasks
    stats.done_count = done
    stats.in_progress_count = in_prog
    stats.not_started_count = not_started
    stats.delayed_count = delayed
    stats.risk_count = risk
    stats.completion_rate = round(sum(rates) / len(rates), 1) if rates else 0
    stats.avg_progress = round(sum(progs) / len(progs), 1) if progs else 0
    stats.avg_target_progress = round(sum(targets) / len(targets), 1) if targets else None
    # 收集所有 task_stat
    all_ts = []
    for s in stats_list:
        all_ts.extend(s.task_stats)
    stats.task_stats = all_ts
    return stats


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
@app.get("/api/history")
def list_history():
    if not os.path.isdir(HISTORY_DIR):
        return {"items": []}
    items = []
    for fn in sorted(os.listdir(HISTORY_DIR)):
        if fn.endswith((".md", ".html", ".txt")):
            full = os.path.join(HISTORY_DIR, fn)
            items.append({
                "name": fn,
                "size": os.path.getsize(full),
                "mtime": datetime.fromtimestamp(os.path.getmtime(full)).strftime("%Y-%m-%d %H:%M"),
            })
    items.reverse()
    return {"items": items}


@app.get("/api/history/{name}")
def view_history(name: str):
    safe = os.path.basename(name)
    full = os.path.join(HISTORY_DIR, safe)
    if not os.path.exists(full):
        raise HTTPException(404, "历史周报不存在")
    with open(full, "r", encoding="utf-8") as f:
        return PlainTextResponse(f.read())


@app.post("/api/save_report")
def api_save_report(payload: SaveReportIn):
    if not payload.content.strip():
        raise HTTPException(400, "周报内容为空")
    safe = os.path.basename(payload.filename)
    with open(os.path.join(HISTORY_DIR, safe), "w", encoding="utf-8") as f:
        f.write(payload.content)
    return {"ok": True, "name": safe}
