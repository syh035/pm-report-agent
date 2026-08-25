/* FormKit —— 轻量 JSON Schema 驱动动态表单引擎
 * 输入：schema = { groups: [{title, icon?, fields: [{key, label, type, desc, options?, unit?, min?, max?, placeholder?}]}] }
 *       value = { key: value, ... }（当前配置值）
 * 能力：分组折叠、关键字搜索（跨 label/desc/key）、按 type 动态渲染控件
 *       number/text/select/color/textarea/keywords/mapping/bool/list
 * 用法：renderFormKit(containerId, schema, value, {onChange(key,val), onSave})
 */
function kitEsc(s){ return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

function renderFormKit(containerId, schema, value, opts){
  const box = document.getElementById(containerId);
  if(!box) return;
  opts = opts||{};
  const groups = schema.groups||[];
  const kw = (document.getElementById(containerId+'_search')?.value||'').toLowerCase();
  let html = `
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px;">
      <input type="text" id="${containerId}_search" placeholder="搜索配置项…"
        style="flex:1;max-width:320px;border:1px solid var(--border,#E4E7EC);border-radius:8px;padding:7px 12px;font-size:13px;"
        oninput="renderFormKit('${containerId}', window._kitSchema, window._kitValue, window._kitOpts)">
      <button class="btn btn-primary" style="border:none;border-radius:8px;padding:7px 16px;font-size:13px;cursor:pointer;background:var(--primary,#4A6CF7);color:#fff;" onclick="window._kitOpts&&window._kitOpts.onSave&&window._kitOpts.onSave()">保存修改</button>
    </div>`;
  const kwLower = kw;
  const filteredGroups = groups.map(g=>{
    const fields = (g.fields||[]).filter(f=>
      !kwLower || (f.label||'').toLowerCase().includes(kwLower)
             || (f.desc||'').toLowerCase().includes(kwLower)
             || (f.key||'').toLowerCase().includes(kwLower));
    return {...g, fields};
  }).filter(g=>!kwLower || g.fields.length);
  filteredGroups.forEach((g, gi)=>{
    const fid = containerId+'_g'+gi;
    html += `<div style="border:1px solid var(--border,#E4E7EC);border-radius:10px;margin-bottom:10px;background:#fff;overflow:hidden;">
      <div onclick="kitToggleGroup('${fid}')" style="display:flex;align-items:center;gap:8px;padding:10px 14px;cursor:pointer;background:#FAFBFC;font-size:13px;font-weight:600;">
        <span style="transition:.2s;" id="${fid}_caret">▸</span> ${kitEsc(g.title||'')}
        <span style="font-size:11px;color:var(--muted,#6B6B80);font-weight:400;">（${g.fields.length} 项）</span>
      </div>
      <div id="${fid}" style="display:${g.fields.length?'block':'none'};padding:6px 14px 12px;">
        ${g.fields.map(f=>kitFieldHtml(containerId, f, value)).join('')}
      </div>
    </div>`;
  });
  html += filteredGroups.length ? '' : '<div style="color:var(--muted);font-size:13px;padding:12px;">没有匹配的配置项</div>';
  box.innerHTML = html;
}
function kitToggleGroup(fid){
  const el = document.getElementById(fid);
  const carets = document.getElementById(fid+'_caret');
  if(!el) return;
  const show = el.style.display === 'none';
  el.style.display = show ? 'block' : 'none';
  if(carets) carets.textContent = show ? '▾' : '▸';
}
function kitFieldHtml(containerId, f, value){
  const v = value[f.key];
  const desc = f.desc ? `<div style="font-size:11px;color:var(--muted,#6B6B80);margin-top:2px;">${kitEsc(f.desc)}</div>` : '';
  const head = `<div style="font-size:12px;font-weight:600;margin:10px 0 4px;">${kitEsc(f.label||f.key)}${f.unit?` <span style="color:var(--muted,#6B6B80);font-weight:400;">(${kitEsc(f.unit)})</span>`:''}</div>`;
  const onchange = `kitSet('${containerId}','${f.key}',this.value)`;
  let ctrl = '';
  switch(f.type){
    case 'number':
      ctrl = `<input type="number" value="${kitEsc(v??'')}" min="${f.min??''}" max="${f.max??''}" style="width:140px;border:1px solid var(--border,#E4E7EC);border-radius:6px;padding:6px 10px;font-size:13px;" onchange="${onchange}">`;
      break;
    case 'select':
      ctrl = `<select style="border:1px solid var(--border,#E4E7EC);border-radius:6px;padding:6px 10px;font-size:13px;" onchange="${onchange}">
        ${(f.options||[]).map(o=>`<option value="${kitEsc(o.value)}" ${String(v)===String(o.value)?'selected':''}>${kitEsc(o.label)}</option>`).join('')}</select>`;
      break;
    case 'color':
      ctrl = `<input type="color" value="${kitEsc(v||'#000000')}" style="width:50px;height:30px;border:none;cursor:pointer;" onchange="${onchange}">`;
      break;
    case 'textarea':
      ctrl = `<textarea rows="${f.rows||4}" style="width:100%;border:1px solid var(--border,#E4E7EC);border-radius:6px;padding:8px;font-size:12px;font-family:inherit;" onchange="${onchange}">${kitEsc(v??'')}</textarea>`;
      break;
    case 'keywords': {   // 每行一个词
      const list = Array.isArray(v)?v:[];
      ctrl = `<textarea rows="${f.rows||4}" style="width:100%;border:1px solid var(--border,#E4E7EC);border-radius:6px;padding:8px;font-size:12px;font-family:inherit;" onchange="kitSetKeywords('${containerId}','${f.key}',this.value)">${kitEsc(list.join('\n'))}</textarea>`;
      break; }
    case 'mapping': {    // 每行 源=目标
      const map = v && typeof v==='object' ? v : {};
      const lines = Object.entries(map).map(([a,b])=>a+'='+b).join('\n');
      ctrl = `<textarea rows="${f.rows||4}" style="width:100%;border:1px solid var(--border,#E4E7EC);border-radius:6px;padding:8px;font-size:12px;font-family:inherit;" onchange="kitSetMapping('${containerId}','${f.key}',this.value)">${kitEsc(lines)}</textarea>`;
      break; }
    case 'bool':
      ctrl = `<input type="checkbox" ${v?'checked':''} style="width:18px;height:18px;accent-color:var(--primary,#4A6CF7);cursor:pointer;" onchange="kitSetBool('${containerId}','${f.key}',this.checked)">`;
      break;
    default:  // text
      ctrl = `<input type="text" value="${kitEsc(v??'')}" placeholder="${kitEsc(f.placeholder||'')}" style="width:100%;max-width:420px;border:1px solid var(--border,#E4E7EC);border-radius:6px;padding:7px 10px;font-size:13px;" onchange="${onchange}">`;
  }
  return `<div>${head}${ctrl}${desc}</div>`;
}
function kitSet(containerId, key, val){
  if(!window._kitValue) return;
  window._kitValue[key] = val;
  if(window._kitOpts && window._kitOpts.onChange) window._kitOpts.onChange(key, val);
}
function kitSetKeywords(containerId, key, text){
  if(!window._kitValue) return;
  window._kitValue[key] = String(text||'').split('\n').map(s=>s.trim()).filter(Boolean);
  if(window._kitOpts && window._kitOpts.onChange) window._kitOpts.onChange(key, window._kitValue[key]);
}
function kitSetMapping(containerId, key, text){
  if(!window._kitValue) return;
  const out = {};
  String(text||'').split('\n').forEach(line=>{
    const i = line.indexOf('=');
    if(i>0){ const a=line.slice(0,i).trim(), b=line.slice(i+1).trim(); if(a&&b) out[a]=b; }
  });
  window._kitValue[key] = out;
  if(window._kitOpts && window._kitOpts.onChange) window._kitOpts.onChange(key, out);
}
function kitSetBool(containerId, key, ch){
  if(!window._kitValue) return;
  window._kitValue[key] = ch;
  if(window._kitOpts && window._kitOpts.onChange) window._kitOpts.onChange(key, ch);
}
function kitInit(containerId, schema, value, opts){
  window._kitSchema = schema; window._kitValue = value; window._kitOpts = opts||{};
  renderFormKit(containerId, schema, value, opts||{});
}
