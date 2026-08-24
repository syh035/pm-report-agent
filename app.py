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


def _append_stats_history(project_name: str, file_name: str, stats_dict: Dict) -> None:
    """把一次周报统计写入历史（最多 50 条）。
    project 用于环比匹配（按项目名），file 用于与历史文件对应。"""
    h = _load_stats_history()
    h.insert(0, {"time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                 "project": project_name, "file": file_name, "stats": stats_dict})
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
    stats: Optional[Dict] = None   # 统计汇总（写入环比历史）


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
        ext = os.path.splitext(uf.filename or "")[1].lower()
        try:
            if ext in (".xlsx", ".xlsm"):
                # Excel 多 sheet：每个工作表生成一个独立 sheet 对象
                from pmo_report.parsers import tabular_parser
                sheet_projects = tabular_parser.parse_excel_all(tmp)
            else:
                sheet_projects = [(os.path.splitext(uf.filename or "")[0], parse_file(tmp, period=""))]
        except Exception as e:
            raise HTTPException(500, f"解析 {uf.filename} 失败: {e}")
        finally:
            os.path.exists(tmp) and os.remove(tmp)
        for name, proj in sheet_projects:
            sid = _next_sid()
            stats = analyze(proj, rules=load_rules())
            WORKSPACE["sheets"][sid] = {
                "name": proj.name or name,
                "source": uf.filename or "",
                "project": proj,
                "_stats": stats.to_dict(),
                "parse_stats": proj.parse_stats,
                "rule_tasks": proj.rule_snapshot,
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


@app.post("/api/workspace/sheet/{sid}/ungroup")
def workspace_sheet_ungroup(sid: str):
    """把 sheet 移出所有分组（回到未分组区）。"""
    if sid not in WORKSPACE["sheets"]:
        raise HTTPException(404, "sheet 不存在")
    for g in WORKSPACE["groups"].values():
        if sid in g["sheets"]:
            g["sheets"].remove(sid)
    return {"ok": True, "workspace": _workspace_view()}


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
    return {"ok": True, "n_tasks": len(new_tasks), "workspace": _workspace_view()}


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
    file_to_project = {e.get("file", ""): e.get("project", "") for e in _load_stats_history()}
    items = []
    for fn in sorted(os.listdir(HISTORY_DIR)):
        if fn.endswith((".md", ".html", ".txt")):
            full = os.path.join(HISTORY_DIR, fn)
            items.append({
                "name": fn,
                "size": os.path.getsize(full),
                "mtime": datetime.fromtimestamp(os.path.getmtime(full)).strftime("%Y-%m-%d %H:%M"),
                "has_stats": fn in stats_names,          # 环比徽标
                "project": file_to_project.get(fn, ""),  # 所属项目（供前端分组）
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
    # 写入环比历史（只存汇总数字+任务进度快照；project 供环比匹配）
    if payload.stats:
        proj = (payload.stats.get("project_name") or safe).strip()
        _append_stats_history(proj, safe, _compact_stats(payload.stats))
    return {"ok": True, "name": safe}
