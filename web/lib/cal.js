/* 通用日历组件：年月可点击展开选择（年份列表 + 月份网格）。
 * 用法：页面需有 <span class="ym" id="calYM">；调用 bindCalPicker(ymEl, onPick) 后点击标题弹出选择器。
 * onPick(year, month) 由页面实现（切月后重渲染日历）。 */
function bindCalPicker(ymEl, onPick){
  if(!ymEl || ymEl._bound) return;
  ymEl._bound = true;
  ymEl.style.cursor = 'pointer';
  ymEl.title = '点击选择年份/月份';
  ymEl.onclick = function(e){
    e.stopPropagation();
    openCalPicker(ymEl, onPick);
  };
}
function openCalPicker(ymEl, onPick){
  const old = document.getElementById('calPicker');
  if(old) old.remove();
  // 从标题文本解析当前年月（格式：2026 年 8 月）
  const m = (ymEl.textContent||'').match(/(\d{4})\s*年\s*(\d{1,2})\s*月/);
  let curY = m ? +m[1] : new Date().getFullYear();
  let curM = m ? +m[2] : new Date().getMonth()+1;
  const box = document.createElement('div');
  box.id = 'calPicker';
  box.style.cssText = 'position:fixed;z-index:200;background:var(--card,#fff);border:1px solid var(--border,#E4E7EC);border-radius:10px;box-shadow:0 8px 30px rgba(0,0,0,.18);padding:14px;min-width:280px;';
  const r = ymEl.getBoundingClientRect();
  box.style.left = Math.min(r.left, window.innerWidth-300)+'px';
  box.style.top = (r.bottom+6)+'px';
  // 年份导航 + 列表
  const years = [];
  for(let i=-6;i<=4;i++) years.push(curY+i);
  box.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
      <button style="border:none;background:#EEF2FF;border-radius:6px;padding:2px 10px;cursor:pointer;" onclick="calYears(-12)">‹‹</button>
      <b style="font-size:14px;" id="calPickerTitle">${curY} 年</b>
      <button style="border:none;background:#EEF2FF;border-radius:6px;padding:2px 10px;cursor:pointer;" onclick="calYears(12)">››</button>
    </div>
    <div id="calPickerYears" style="display:grid;grid-template-columns:repeat(4,1fr);gap:4px;margin-bottom:10px;max-height:150px;overflow-y:auto;"></div>
    <div id="calPickerMonths" style="display:grid;grid-template-columns:repeat(3,1fr);gap:4px;"></div>`;
  document.body.appendChild(box);
  window._calPicker = {curY, curM, onPick, ymEl};
  renderCalYears();
  renderCalMonths();
  // 点击外部关闭
  setTimeout(()=>{ document.addEventListener('click', closeCalPickerOutside, {once:true}); }, 10);
}
function closeCalPickerOutside(e){
  const box = document.getElementById('calPicker');
  if(box && !box.contains(e.target)) box.remove();
}
function calYears(delta){
  const p = window._calPicker; if(!p) return;
  p.curY += delta;
  document.getElementById('calPickerTitle').textContent = p.curY + ' 年';
  renderCalYears();
}
function renderCalYears(){
  const p = window._calPicker; if(!p) return;
  const el = document.getElementById('calPickerYears');
  const cur = p.curY;
  let html = '';
  for(let i=-6;i<=4;i++){
    const y = cur+i;
    html += `<div style="text-align:center;padding:5px 0;border-radius:6px;cursor:pointer;font-size:12px;${y===p.curY?'background:var(--primary,#4A6CF7);color:#fff;font-weight:600;':'color:var(--text,#2D3142);'}" onclick="calPickYear(${y})">${y}</div>`;
  }
  el.innerHTML = html;
}
function renderCalMonths(){
  const p = window._calPicker; if(!p) return;
  const el = document.getElementById('calPickerMonths');
  el.innerHTML = Array.from({length:12}, (_,i)=>i+1).map(md=>
    `<div style="text-align:center;padding:6px 0;border-radius:6px;cursor:pointer;font-size:13px;${md===p.curM?'background:var(--primary,#4A6CF7);color:#fff;font-weight:600;':'color:var(--text,#2D3142);'}" onclick="calPickMonth(${md})">${md}月</div>`
  ).join('');
}
function calPickYear(y){
  const p = window._calPicker; if(!p) return;
  p.curY = y;
  document.getElementById('calPickerTitle').textContent = y + ' 年';
  renderCalYears(); renderCalMonths();
}
function calPickMonth(md){
  const p = window._calPicker; if(!p) return;
  p.curM = md;
  const box = document.getElementById('calPicker'); if(box) box.remove();
  if(p.onPick) p.onPick(p.curY, p.curM);
  p.ymEl.textContent = `${p.curY} 年 ${p.curM} 月`;
  window._calPicker = null;
}
