# -*- coding: utf-8 -*-
"""
PM Report Agent — FastAPI Web 操作面板后端。

启动： python -m uvicorn app:app --reload
访问： http://127.0.0.1:8000

API（主要）：
  数据：
    POST /api/workspace/load                上传文件 -> 归档 + AI 分析
    GET  /api/sources                       源文件列表
    GET  /api/sources/{sid}                 源文件详情（AI 提取条目）
    DELETE /api/sources/{sid}               删除源文件（留痕可撤销）
    GET  /api/source-groups                 源文件分组
  看板：
    GET /api/dashboard                      看板（只展示 AI 提取条目）
  规则：
    GET/POST /api/rules
    POST /api/rules/delete | batch-delete | converse/confirm | template-extract
  模板：
    GET/POST /api/template, POST /api/template/parse, GET/POST /api/template/lib
  生成：
    POST /api/generate/one                  一条线生成（模板原地更新）
    POST /api/export                        导出（word/text）
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
# ================= 工作区状态 =================
# sheets: {sid: {"name", "source", "project": {...}}}
# groups: {gid: {"name", "sheets": [sid,...]}}

ALLOWED_EXTS = {".xlsx", ".xlsm", ".csv", ".docx", ".pdf", ".txt", ".md", ".html", ".htm"}

app = FastAPI(title="PM Report Agent", version="0.11.0")

WORKSPACE: Dict = {"sheets": {}, "groups": {}, "package_id": str(uuid.uuid4())[:8]}
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
    generation_requirements: Optional[List[Dict]] = None


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
                # 数据集表格结构：保留每 sheet 的表头+行（给 AI 看的原始数据形态）
                try:
                    from pmo_report.dataset import parse_dataset_sheets
                    dataset_sections = parse_dataset_sheets(tmp)
                    datastore.save_dataset_sections(source_id, dataset_sections)
                except Exception:
                    dataset_sections = None
        except Exception as e:
            raise HTTPException(500, f"保存 {uf.filename} 失败: {e}")
        finally:
            os.path.exists(tmp) and os.remove(tmp)
        is_dataset = bool(dataset_sections)
        # 一条线：上传即 AI 分析（主体是 AI，注入处理约定+分析要求）
        # 无 Key / AI 失败时只保留原始文件（不产生规则假数据）
        ai_note = ""
        try:
            from pmo_report import datastore as _ds
            from pmo_report import prompts as _prompts_mod
            from pmo_report.ai_analysis import ai_assist_analysis
            src_rec = _ds.get_source(source_id)
            stored = src_rec.get("stored_path", "") if src_rec else ""
            if stored and os.path.exists(stored):
                r = ai_assist_analysis(source_id, stored, ext, ai_mod, _prompts_mod, rules_mod,
                                       existing_items=[], dataset_sections=dataset_sections)
                ai_note = r.get("note", "")
                if r.get("added"):
                    print(f"  [ai_analysis] {fname}: {ai_note}")
        except Exception as e:
            print(f"  [ai_analysis] 异常: {e}")
        created.append(source_id)
        log_note = f"上传 {fname}" + (f"；{ai_note}" if ai_note else "（未配置 AI Key，仅存原文）")
        from pmo_report import datastore as _ds2
        _ds2.log_source_op("add", source_id, fname, {"note": log_note})
    return {"ok": True, "created": created, "renamed": renamed,
            "is_dataset": is_dataset}


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


@app.get("/api/source-groups")
def list_source_groups():
    """源文件分组列表（周报生成可引用整组数据）。"""
    from pmo_report import datastore
    groups = datastore.source_groups()
    for g in groups:
        g["sources"] = datastore.sources_by_group(g["name"])
    return {"groups": groups}


class SourceGroupIn(BaseModel):
    group_name: str = ""


@app.post("/api/sources/{sid}/group")
def set_source_group_api(sid: str, payload: SourceGroupIn):
    """把源文件移入/移出分组（group_name 为空 = 移出）。"""
    from pmo_report import datastore
    if not datastore.get_source(sid):
        raise HTTPException(404, "源数据不存在")
    datastore.set_source_group(sid, (payload.group_name or "").strip())
    return {"ok": True, "note": f"已移入分组「{payload.group_name}」" if payload.group_name else "已移出分组"}


@app.post("/api/source-groups/delete")
def delete_source_group_api(payload: SourceGroupIn):
    """删除分组（组内源文件移出分组，不删除文件）。"""
    from pmo_report import datastore
    n = datastore.delete_source_group((payload.group_name or "").strip())
    return {"ok": True, "note": f"已删除分组（{n} 个源文件移出）"}


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
    # AI 提取为主：返回该源的分类仓条目（task/risk/issue/decision/milestone/metric/raw）
    try:
        ai_items = datastore.query_items(source_id=sid, limit=500)
    except Exception:
        ai_items = []
    # 数据集板块（若有）：源数据详情里可查看完整板块结构
    dataset_sections = None
    try:
        dataset_sections = datastore.get_dataset_sections(sid)
    except Exception:
        dataset_sections = None
    return {"source": src, "raw_rows": raw_rows, "text": text, "sheets": sheets,
            "ai_items": ai_items, "dataset_sections": dataset_sections}


# ================= 原始库操作（删除/批量/撤销/留痕） =================
def _delete_source_full(sid: str) -> Optional[str]:
    """删除源文件（留痕+快照可撤销），清理工作区关联 sheet。返回文件名。"""
    from pmo_report import datastore
    src = datastore.get_source(sid)
    if not src:
        return None
    datastore.delete_source(sid, keep_snapshot=True)
    for sk in [s for s, sh in WORKSPACE["sheets"].items() if sh.get("source_id") == sid]:
        del WORKSPACE["sheets"][sk]
        for g in WORKSPACE["groups"].values():
            if sk in g["sheets"]:
                g["sheets"].remove(sk)
    return src.get("filename", "")


@app.delete("/api/sources/{sid}")
def delete_source_api(sid: str):
    """删除源文件（留痕 + 快照，可撤销）。同时清理工作区中关联 sheet。"""
    fname = _delete_source_full(sid)
    if not fname:
        raise HTTPException(404, "源数据不存在")
    return {"ok": True, "note": f"已删除「{fname}」（留痕可撤销）", "filename": fname}


class SourceBatchDeleteIn(BaseModel):
    source_ids: List[str] = []


@app.post("/api/sources/batch-delete")
def batch_delete_sources(payload: SourceBatchDeleteIn):
    """批量删除源文件（每个都留痕+快照可撤销）。返回删除数量。"""
    ids = [s.strip() for s in payload.source_ids if s.strip()]
    if not ids:
        raise HTTPException(400, "source_ids 不能为空")
    removed = []
    for sid in ids:
        fname = _delete_source_full(sid)
        if fname:
            removed.append(fname)
    return {"ok": True, "removed": removed, "note": f"已批量删除 {len(removed)} 个源文件（留痕可撤销）"}


@app.post("/api/sources/undo")
def undo_source_op():
    """撤销最近一次原始库操作（删除→恢复源文件 + AI 重新分析）。"""
    from pmo_report import datastore
    r = datastore.restore_source()
    if not r:
        raise HTTPException(400, "没有可撤销的操作")
    if not r.get("ok"):
        raise HTTPException(400, r.get("reason", "撤销失败"))
    # 撤销后重新跑 AI 分析（主体是 AI；无 Key 则只保留原文）
    try:
        sid = r.get("source_id", "")
        src = datastore.get_source(sid)
        if src and os.path.exists(src.get("stored_path", "")):
            from pmo_report import prompts as _pp
            from pmo_report.ai_analysis import ai_assist_analysis
            ext = os.path.splitext(src["filename"] or "")[1].lower()
            ds = None
            if ext in (".xlsx", ".xlsm"):
                try:
                    from pmo_report.dataset import parse_dataset_sheets
                    ds = parse_dataset_sheets(src["stored_path"])
                    datastore.save_dataset_sections(sid, ds)
                except Exception:
                    ds = None
            try:
                ai_assist_analysis(sid, src["stored_path"], ext, ai_mod, _pp, rules_mod,
                                   existing_items=[], dataset_sections=ds)
            except Exception:
                pass
    except Exception:
        pass
    return {"ok": True, "note": f"已撤销删除，恢复「{r.get('filename')}」（并重新 AI 分析）", "filename": r.get("filename")}


@app.get("/api/sources/{sid}/download")
def source_download(sid: str):
    from pmo_report import datastore
    src = datastore.get_source(sid)
    if not src or not os.path.exists(src["stored_path"]):
        raise HTTPException(404, "源文件不存在")
    return StreamingResponse(open(src["stored_path"], "rb"),
                             media_type="application/octet-stream",
                             headers={"Content-Disposition": _content_disposition(src["filename"])})


@app.get("/api/dashboard")
def get_dashboard():
    """看板：只展示 AI 提取条目（分类仓 items 按 kind/板块分组）。
    无 AI 提取数据时提示「未配置/未分析」，不显示规则计算的假 KPI。"""
    from pmo_report import datastore
    items = datastore.query_items(limit=1000)
    kind_label = {"task": "任务", "risk": "风险", "issue": "依赖/问题", "decision": "决策",
                  "milestone": "里程碑", "metric": "指标", "raw": "原始"}
    by_kind = {}
    for it in items:
        k = it.get("kind") or "raw"
        by_kind.setdefault(k, []).append(it)
    kinds = []
    for k, arr in by_kind.items():
        by_sec = {}
        for it in arr:
            sec = (it.get("payload") or {}).get("section") or ""
            by_sec.setdefault(sec, []).append(it)
        kinds.append({
            "kind": k, "label": kind_label.get(k, k), "total": len(arr),
            "sections": [{"name": sec or "（未分板块）", "items": sec_items[:50]}
                         for sec, sec_items in by_sec.items()],
        })
    by_src = {}
    for it in items:
        by_src[it.get("source_id", "")] = by_src.get(it.get("source_id", ""), 0) + 1
    return {"kinds": kinds, "items_summary": datastore.items_summary(),
            "by_source": by_src, "has_ai_data": bool(items)}
@app.get("/api/rules")
def get_rules():
    return {"rules": rules_mod.load_rules(), "meta": rules_mod.RULE_META}


@app.post("/api/rules")
def save_rules(payload: RulesIn):
    rules = rules_mod.save_rules(payload.dict(exclude_none=True))
    # 规则变更后刷新已解析 sheet 的统计
    return {"ok": True, "rules": rules, "note": "已保存并写入历史"}


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
        # 无 key 时按描述启发式推断字段（AI 未给 key 的兜底）
        if not key:
            desc = str(draft.get("description") or "") + str(draft.get("title") or "")
            if any(k in desc for k in ("风险", "超期", "delay")):
                key = "delay_days_danger"
            elif any(k in desc for k in ("进度", "偏慢", "落后", "slow")):
                key = "slow_progress_pct"
            elif any(k in desc for k in ("临近", "预警", "near")):
                key = "risk_near_end_days"
        val = draft.get("value")
        if key and val is not None and key in rules_mod.DEFAULT_RULES:
            try:
                rules[key] = int(float(val))
                applied["detail"] = f"{key} = {rules[key]}"
            except (TypeError, ValueError):
                raise HTTPException(400, f"字段 {key} 的数值无效")
        elif not key:
            raise HTTPException(400, "无法识别该数值规则对应的字段，请换一种表达")
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
    elif typ in ("requirement", "文本要求", "analysis_requirement", "分析要求"):
        # 分析要求：作为给「分析 AI」的要求存 requirements（上传时 AI 辅助分析注入）
        reqs = rules.setdefault("requirements", [])
        title = str(draft.get("title") or "").strip()
        desc = str(draft.get("description") or "").strip()
        if title and desc and all(r.get("title") != title for r in reqs):
            reqs.append({"title": title, "description": desc,
                         "scope": draft.get("scope") or "全部",
                         "applies_to": draft.get("applies_to") or "全部"})
        applied["detail"] = f"分析要求「{title}」已加入（共 {len(reqs)} 条）"
    elif typ in ("generation_requirement", "生成要求"):
        # 生成要求：作为给「生成文稿 AI」的要求存 generation_requirements（所有生成方式注入）
        reqs = rules.setdefault("generation_requirements", [])
        title = str(draft.get("title") or "").strip()
        desc = str(draft.get("description") or "").strip()
        if title and desc and all(r.get("title") != title for r in reqs):
            reqs.append({"title": title, "description": desc,
                         "scope": draft.get("scope") or "全部",
                         "applies_to": draft.get("applies_to") or "全部"})
        applied["detail"] = f"生成要求「{title}」已加入（共 {len(reqs)} 条）"
    else:
        raise HTTPException(400, f"暂不支持该规则类型: {typ}")
    rules_mod.save_rules(rules)
    return {"ok": True, "rules": rules, "applied": applied,
            "note": "已确认并写入规则库"}


# ================= 通用 AI 对话弹窗（多轮往返 + 按用途配置） =================
class DialogueChatIn(BaseModel):
    purpose: str = "rule_dialogue"     # 用途：rule_dialogue / template_tune / rule_doc_import
    messages: List[Dict] = []          # [{role:user/assistant, content}...] 前端维护（窗口内记忆）


class DialogueFinalizeIn(BaseModel):
    purpose: str = "rule_dialogue"
    messages: List[Dict] = []
    extra_context: str = ""


class DialogueConfigIn(BaseModel):
    purpose: str
    model: str = ""
    temperature: Optional[float] = None
    system: str = ""


@app.post("/api/dialogue/chat")
def dialogue_chat(payload: DialogueChatIn):
    """多轮对话（弹窗窗口内记忆，前端传 messages）。"""
    from pmo_report.ai_dialogue import dialogue_chat as _chat
    return _chat(payload.purpose, payload.messages or [], ai_mod)


@app.post("/api/dialogue/finalize")
def dialogue_finalize(payload: DialogueFinalizeIn):
    """确认完成：AI 把完整对话总结为结构化结果（规则列表/HTML）。"""
    from pmo_report import prompts as prompts_mod
    from pmo_report.ai_dialogue import dialogue_finalize as _finalize
    return _finalize(payload.purpose, payload.messages or [], ai_mod, prompts_mod,
                     extra_context=payload.extra_context or "")


@app.post("/api/dialogue/config")
def dialogue_config_save(payload: DialogueConfigIn):
    """保存某用途的对话配置（模型/温度/系统提示词）。"""
    from pmo_report.ai_dialogue import save_dialogue_config
    return {"ok": True, "config": save_dialogue_config(
        payload.purpose, model=payload.model, temperature=payload.temperature,
        system=payload.system)}


@app.get("/api/dialogue/config")
def dialogue_config_get(purpose: str = "rule_dialogue"):
    """某用途的当前对话配置。"""
    from pmo_report.ai_dialogue import get_dialogue_config, DEFAULT_DIALOGUE_SYSTEM
    return {"purpose": purpose, "config": get_dialogue_config(purpose),
            "default_system": DEFAULT_DIALOGUE_SYSTEM,
            "models": ai_mod.SUPPORTED_MODELS}


class RuleDeleteIn(BaseModel):
    """删除规则：kind ∈ requirement/generation_requirement/ignore/status_word/column_alias；
    index 为列表下标（requirements 类），key 为映射/忽略词键名。"""
    kind: str
    index: Optional[int] = None
    key: str = ""


@app.post("/api/rules/delete")
def delete_rule(payload: RuleDeleteIn):
    """删除单条规则（分析要求/生成要求/忽略词/状态词/列名映射）。"""
    rules = rules_mod.load_rules()
    kind = payload.kind
    if kind in ("requirement", "generation_requirement"):
        field = "requirements" if kind == "requirement" else "generation_requirements"
        idx = payload.index
        items = rules.get(field) or []
        if idx is None or not (0 <= idx < len(items)):
            raise HTTPException(400, "下标越界")
        removed = items.pop(idx)
        rules[field] = items
        rules_mod.save_rules(rules)
        return {"ok": True, "removed": removed.get("title", ""), "note": f"已删除「{removed.get('title')}」"}
    if kind == "ignore":
        key = (payload.key or "").strip()
        ignore = rules.get("ignore_keywords") or []
        if key in ignore:
            ignore.remove(key)
            rules["ignore_keywords"] = ignore
            rules_mod.save_rules(rules)
            return {"ok": True, "removed": key, "note": f"已删除忽略词「{key}」"}
    if kind == "status_word":
        key = (payload.key or "").strip()
        sw = rules.get("status_words") or {}
        if key in sw:
            del sw[key]
            rules["status_words"] = sw
            rules_mod.save_rules(rules)
            return {"ok": True, "removed": key, "note": f"已删除状态词映射「{key}」"}
    if kind == "column_alias":
        key = (payload.key or "").strip()
        ca = rules.get("column_aliases") or {}
        if key in ca:
            del ca[key]
            rules["column_aliases"] = ca
            rules_mod.save_rules(rules)
            return {"ok": True, "removed": key, "note": f"已删除列名映射「{key}」"}
    raise HTTPException(400, "未找到要删除的规则")


class RuleBatchDeleteIn(BaseModel):
    """批量删除：kind + indices（requirements 类）或 keys（映射/忽略词）。"""
    kind: str
    indices: List[int] = []
    keys: List[str] = []


@app.post("/api/rules/batch-delete")
def batch_delete_rules(payload: RuleBatchDeleteIn):
    """批量删除规则（支持 requirements/generation_requirements 按下标；ignore/映射 按 key）。"""
    rules = rules_mod.load_rules()
    kind = payload.kind
    removed: List[str] = []
    if kind in ("requirement", "generation_requirement"):
        field = "requirements" if kind == "requirement" else "generation_requirements"
        items = rules.get(field) or []
        for idx in sorted(set(payload.indices), reverse=True):
            if 0 <= idx < len(items):
                removed.append(items.pop(idx).get("title", ""))
        rules[field] = items
    elif kind == "ignore":
        ignore = rules.get("ignore_keywords") or []
        for k in payload.keys:
            if k in ignore:
                ignore.remove(k)
                removed.append(k)
        rules["ignore_keywords"] = ignore
    elif kind == "status_word":
        sw = rules.get("status_words") or {}
        for k in payload.keys:
            if k in sw:
                removed.append(k)
                del sw[k]
        rules["status_words"] = sw
    elif kind == "column_alias":
        ca = rules.get("column_aliases") or {}
        for k in payload.keys:
            if k in ca:
                removed.append(k)
                del ca[k]
        rules["column_aliases"] = ca
    else:
        raise HTTPException(400, f"不支持批量删除类型: {kind}")
    rules_mod.save_rules(rules)
    return {"ok": True, "removed": removed, "note": f"已批量删除 {len(removed)} 条"}


@app.post("/api/rules/template-extract")
async def rules_template_extract(file: UploadFile = File(...)):
    """上传规则文档（txt/md/docx/pdf/html）→ 提取文本，供 import-text 使用。"""
    tmp = _save_upload_to_tmp(file)
    try:
        from pmo_report.parsers.text_parser import _extract_text_docx
        ext = os.path.splitext(file.filename or "")[1].lower()
        if ext in (".docx",):
            text = _extract_text_docx(tmp)
        elif ext in (".pdf",):
            from pmo_report.parsers.text_parser import _extract_text_pdf
            text = _extract_text_pdf(tmp, use_ocr=False)
        else:
            with open(tmp, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
    except Exception as e:
        raise HTTPException(500, f"规则文档提取失败: {e}")
    finally:
        os.path.exists(tmp) and os.remove(tmp)
    return {"ok": True, "filename": file.filename or "", "text": text[:20000]}
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


class AiConfigIn(BaseModel):
    model: str = ""
    base_url: str = ""


@app.get("/api/config-schema")
def config_schema():
    """动态表单 schema：规则库 + AI 配置 的分组字段定义（供 FormKit 渲染）。"""
    rules = rules_mod.load_rules()
    meta = rules_mod.RULE_META
    rule_fields = []
    for k, default in rules_mod.DEFAULT_RULES.items():
        if k in ("requirements", "generation_requirements"):
            continue
        m = meta.get(k) or {}
        ftype = "number"
        if m.get("type") == "color":
            ftype = "color"
        elif m.get("type") == "select":
            ftype = "select"
        elif m.get("type") == "keywords":
            ftype = "keywords"
        elif m.get("type") == "mapping":
            ftype = "mapping"
        rule_fields.append({
            "key": k, "label": m.get("label", k), "desc": m.get("desc", ""),
            "type": ftype, "unit": m.get("unit", ""), "min": m.get("min"), "max": m.get("max"),
            "options": m.get("options") or [],
        })
    ai_cfg = ai_mod.ai_config()
    schema = {
        "rules": {
            "groups": [
                {"title": "风险分级阈值与颜色", "fields": rule_fields},
            ]
        },
        "ai": {
            "groups": [
                {"title": "模型与接口", "fields": [
                    {"key": "model", "label": "模型", "type": "select",
                     "options": [{"value": m["value"], "label": m["label"]} for m in ai_mod.SUPPORTED_MODELS],
                     "desc": "deepseek-chat 通用性价比高；deepseek-reasoner 推理更强、更贵"},
                    {"key": "base_url", "label": "API 地址（Base URL）", "type": "text",
                     "placeholder": "https://api.deepseek.com", "desc": "兼容 OpenAI 格式；留空用官方地址"},
                ]},
            ]
        },
    }
    return {"schema": schema, "rules": rules, "ai_config": ai_cfg,
            "ai_models": ai_mod.SUPPORTED_MODELS}


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


class PromptsResetIn(BaseModel):
    keys: Optional[List[str]] = None


@app.post("/api/settings/prompts/reset")
def reset_prompts(payload: Optional[PromptsResetIn] = None):
    from pmo_report import prompts as prompts_mod
    keys = (payload.keys if payload and payload.keys else None)
    if keys:
        prompts_mod.reset_prompts(keys=keys)
        note = f"已恢复 {len(keys)} 项默认提示词"
    else:
        prompts_mod.reset_prompts()
        note = "已恢复全部默认提示词"


@app.get("/api/settings/prompts/history")
def prompts_history(key: str = "", date: str = ""):
    """提示词版本历史（可按用途key/日期筛选）。"""
    from pmo_report import datastore
    return {"items": datastore.list_prompt_versions(key=key, date=date)}


class PromptRollbackIn(BaseModel):
    key: str
    version_id: int


@app.post("/api/settings/prompts/rollback")
def prompts_rollback(payload: PromptRollbackIn):
    """回退某提示词到指定版本（只恢复该 key，不影响其他）。"""
    from pmo_report import prompts as prompts_mod
    try:
        prompts_mod.rollback_prompt(payload.key, payload.version_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "items": prompts_mod.prompts_status()["items"],
            "note": f"已回退「{payload.key}」到指定版本"}
    return {"ok": True, "items": prompts_mod.prompts_status()["items"], "note": note}


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


# ================= 模板库（命名入库 / 分类 / 按日期 / 生成时选用） =================
class TemplateLibIn(BaseModel):
    name: str = ""
    html: str = ""
    source_text: str = ""
    ttype: str = "week"      # week/day/other
    category: str = ""


@app.get("/api/template/lib")
def template_lib_list(ttype: str = "", category: str = ""):
    """模板库列表（新→旧；可按类型/分类过滤）。"""
    from pmo_report import datastore
    items = datastore.list_template_lib(ttype=ttype, category=category)
    cats = datastore.template_categories()
    latest = datastore.latest_template_lib(ttype=ttype)
    return {"items": items, "categories": cats,
            "latest_id": latest["id"] if latest else None}


@app.post("/api/template/lib")
def template_lib_save(payload: TemplateLibIn):
    """命名入库模板（人工确认后保存；ttype 区分周报/日报，category 可自定义分类）。"""
    from pmo_report import datastore
    if not payload.html.strip():
        raise HTTPException(400, "模板内容为空")
    tid = datastore.save_template_lib(
        name=(payload.name or "").strip() or f"模板{datetime.now().strftime('%m%d%H%M')}",
        html=payload.html, ttype=payload.ttype or "week", category=payload.category or "",
        source_text=payload.source_text or "")
    return {"ok": True, "id": tid, "note": "已命名入库"}


@app.delete("/api/template/lib/{tid}")
def template_lib_delete(tid: int):
    from pmo_report import datastore
    datastore.delete_template_lib(tid)
    return {"ok": True, "note": "已删除模板"}


@app.get("/api/template/lib/{tid}/apply")
def template_lib_apply(tid: int):
    """把库中某模板设为当前生效模板（生成默认用）。"""
    from pmo_report import datastore
    t = datastore.get_template_lib(tid)
    if not t:
        raise HTTPException(404, "模板不存在")
    ReportGenerator.save_custom_template(t["html"])
    _append_template_history(fmt="html", html=t["html"])
    return {"ok": True, "note": f"已应用模板「{t['name']}」"}


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
        # 解析成功后直接保存为当前 HTML 模板（模块化模板已移除，模板库走 HTML）
        ReportGenerator.save_custom_template(html_template)
        _append_template_history(fmt="html", html=html_template)
        return {"ok": True, "html": html_template, "source_text": text[:20000], "saved": True}
    except Exception as e:
        return {"ok": False, "html": "", "source_text": text[:5000],
                "error": f"AI 解析失败（{e}）。以下是抽取的原始文本，可手动整理。"}


# ================= 周报生成 =================
class GenerateOneIn(BaseModel):
    """一条线生成：选数据源/分组 + 模板（可选）→ 后端按数据形态自动选管线。"""
    source_id: str = ""
    source_ids: List[str] = []     # 分组生成：一次引用多个源文件（一个周报多次数据集）
    template_id: Optional[int] = None
    template_text: str = ""        # 未选库中模板时，可直传模板文本
    report_type: str = "week"
    project_name: str = ""
    period: str = ""


@app.post("/api/generate/one")
def generate_one(payload: GenerateOneIn):
    """一条线生成入口（取代三种方式分开调）。

    数据源：source_id（单选）或 source_ids（分组，一个周报引用多个数据集）。
    模板：优先 template_id 指定的库模板（用其原文 source_text 做「原地更新」），
          否则 template_text；都没有则 AI 自主编写骨架。
    生成语义：把模板原文原地更新为本周周报——找到模板中与数据对应的位置，
              用数据真实值替换，其余文字/风格/格式原样保留。
    """
    from pmo_report import datastore
    from pmo_report.dataset import sections_to_markdown
    from pmo_report import prompts as prompts_mod

    # 1) 确定源文件集合（单选或分组）
    ids = [s for s in (payload.source_ids or []) if s]
    if payload.source_id and payload.source_id not in ids:
        ids.insert(0, payload.source_id)
    ids = list(dict.fromkeys(ids))   # 去重保序
    if not ids:
        raise HTTPException(400, "请选择数据源（或分组）")
    srcs = [datastore.get_source(sid) for sid in ids]
    srcs = [s for s in srcs if s]
    if not srcs:
        raise HTTPException(404, "数据源不存在")

    # 2) 模板解析：库中模板（优先原文 source_text）> 直传文本 > 默认骨架
    template_text = (payload.template_text or "").strip()
    if payload.template_id:
        t = datastore.get_template_lib(payload.template_id)
        if t:
            # 「原地更新」用模板原文（保留论述风格/用词/结构）；没有原文时退回 HTML
            template_text = (t.get("source_text") or "").strip() or (t.get("html") or "").strip()

    gen_reqs = rules_mod.load_rules().get("generation_requirements") or []
    gen_reqs_text = "\n".join(f"- {r.get('title')}: {r.get('description')}" for r in gen_reqs) or "（无）"
    default_tpl = "一、总体进展\n二、关键数据\n三、风险与问题\n四、下周计划"
    if not template_text:
        template_text = default_tpl

    # 3) 汇总组内所有源的数据：数据集板块（优先）+ AI 提取条目
    all_sections = []
    for sid in ids:
        secs = datastore.get_dataset_sections(sid)
        if secs:
            all_sections.extend(secs)
    dataset_parts = []
    if all_sections:
        dataset_parts.append(sections_to_markdown(all_sections))
    all_items = []
    for sid in ids:
        all_items.extend(datastore.query_items(source_id=sid, limit=800))
    if all_items:
        from collections import defaultdict
        groups = defaultdict(list)
        for it in all_items:
            sec = (it.get("payload") or {}).get("section") or it.get("kind") or "其他"
            groups[sec].append(it)
        parts = []
        for sec, arr in groups.items():
            head = [f"### {sec}"]
            for it in arr[:50]:
                p = it.get("payload") or {}
                fields = p.get("fields") or {k: v for k, v in p.items() if k not in ("section", "source_note")}
                detail = " | ".join(f"{k}={v}" for k, v in list(fields.items())[:6]) if fields else ""
                head.append(f"- {it.get('name','')}" + (f"（{detail}）" if detail else ""))
            parts.append("\n".join(head))
        dataset_parts.append("\n\n".join(parts))

    if not dataset_parts:
        raise HTTPException(400, "所选数据还没有 AI 分析结果：请先配置 API Key 并重新上传（上传即自动分析），或检查源文件")

    dataset_text = "\n\n".join(dataset_parts)
    entry = prompts_mod.get_prompt("dataset_report")
    prompt = prompts_mod.render_user(entry, {
        "template_text": template_text[:8000],
        "dataset_text": dataset_text[:20000],
        "generation_requirements": gen_reqs_text,
    })
    out = ai_mod.call_with_cache(
        "dataset_report",
        [{"role": "system", "content": entry.get("system", "")},
         {"role": "user", "content": prompt}],
        temperature=0.3, max_tokens=4000,
    )
    html = _strip_code_block(out or "")
    if "<" not in html or ">" not in html:
        raise HTTPException(502, "AI 未产出有效 HTML")
    mode = "group" if len(ids) > 1 else ("dataset" if all_sections else "ai_items")
    return {"ok": True, "mode": mode, "html": html,
            "note": f"已按模板原地更新生成（{len(ids)} 个源，AI 条目 {len(all_items)} 条，板块 {len(all_sections)} 个）"}


def _strip_code_block(out: str) -> str:
    out = (out or "").strip()
    if out.startswith("```"):
        out = re.sub(r"^```(?:html)?\s*", "", out)
        out = re.sub(r"```\s*$", "", out)
    m = re.search(r"```(?:html)?\s*(.*?)```", out, re.S)
    if m:
        out = m.group(1).strip()
    return out


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
