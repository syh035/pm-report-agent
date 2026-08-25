/* ChatBox —— 通用 AI 对话弹窗组件
 * 弹出窗口式 + 有来有回的上下文对话（窗口内记忆，不持久化）
 * 用法：
 *   openChatBox({
 *     purpose: 'rule_dialogue',        // 对话用途（决定配置与 finalize 格式）
 *     title: '规则对话',                // 弹窗标题
 *     initial: [{role:'user', content:'...'}],  // 可选：预置首条消息
 *     extraContext: '',                // 可选：finalize 时附加的上下文（如文档文本）
 *     onFinalize: async (resultText) => {...}   // 用户点「确认完成」后收到结构化结果
 *   });
 */
let _chatbox = null;
function openChatBox(opts){
  closeChatBox();
  const messages = (opts.initial||[]).map(m=>({role:m.role, content:m.content}));
  const m = document.createElement('div');
  m.id = 'chatBox';
  m.style.cssText = 'position:fixed;left:0;top:0;right:0;bottom:0;background:rgba(0,0,0,.35);z-index:400;overflow:auto;';
  m.innerHTML = `
    <div style="background:var(--card,#fff);max-width:640px;margin:60px auto;border-radius:14px;box-shadow:0 10px 40px rgba(0,0,0,.2);display:flex;flex-direction:column;max-height:80vh;">
      <div style="display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-bottom:1px solid var(--border,#E4E7EC);">
        <b style="font-size:15px;">${esc(opts.title||'AI 对话')}</b>
        <div style="display:flex;gap:8px;align-items:center;">
          <span id="chatCfg" style="font-size:11px;color:var(--muted,#6B6B80);cursor:pointer;" title="配置该对话的模型/温度" onclick="chatCfgOpen()">⚙ 配置</span>
          <button style="border:none;background:#F0F2F5;border-radius:8px;padding:3px 12px;cursor:pointer;font-size:13px;" onclick="closeChatBox()">✕</button>
        </div>
      </div>
      <div id="chatMsgs" style="flex:1;overflow-y:auto;padding:14px 18px;min-height:220px;max-height:46vh;background:var(--bg,#F7F8FA);"></div>
      <div style="padding:12px 18px;border-top:1px solid var(--border,#E4E7EC);">
        <div style="display:flex;gap:8px;">
          <input id="chatInput" type="text" placeholder="输入你的需求或修正…" style="flex:1;border:1px solid var(--border,#E4E7EC);border-radius:8px;padding:9px 12px;font-size:13px;"
            onkeydown="if(event.key==='Enter')chatSend()">
          <button class="chat-btn" style="background:var(--primary,#4A6CF7);color:#fff;" onclick="chatSend()">发送</button>
          <button class="chat-btn" style="background:#166534;color:#fff;" onclick="chatFinalize()">确认完成</button>
        </div>
        <div id="chatStatus" style="font-size:11px;color:var(--muted,#6B6B80);margin-top:6px;"></div>
      </div>
    </div>`;
  document.body.appendChild(m);
  _chatbox = { m, opts, messages, busy:false };
  renderChatMsgs();
  // 预置首条消息自动发送
  if(messages.length){
    const last = messages[messages.length-1];
    if(last.role==='user'){ renderChatMsgs(); chatSend(); }
  }
}
function closeChatBox(){ if(_chatbox && _chatbox.m){ _chatbox.m.remove(); } _chatbox=null; }
function renderChatMsgs(){
  if(!_chatbox) return;
  const box = document.getElementById('chatMsgs');
  if(!box) return;
  box.innerHTML = _chatbox.messages.map((msg,i)=>{
    const isUser = msg.role==='user';
    return `<div style="display:flex;justify-content:${isUser?'flex-end':'flex-start'};margin-bottom:10px;">
      <div style="max-width:80%;padding:8px 12px;border-radius:10px;font-size:13px;white-space:pre-wrap;
        ${isUser?'background:var(--primary,#4A6CF7);color:#fff;':'background:#fff;border:1px solid var(--border,#E4E7EC);'}">${esc(msg.content)}</div>
    </div>`;
  }).join('') || '<div style="color:var(--muted,#6B6B80);font-size:12px;text-align:center;padding:40px 0;">开始对话吧</div>';
  box.scrollTop = box.scrollHeight;
}
async function chatSend(){
  if(!_chatbox || _chatbox.busy) return;
  const inp = document.getElementById('chatInput');
  const text = (inp.value||'').trim();
  if(!text) return;
  inp.value='';
  _chatbox.messages.push({role:'user', content:text});
  renderChatMsgs();
  _chatbox.busy = true;
  document.getElementById('chatStatus').textContent='AI 思考中…';
  try{
    const res = await fetch('/api/dialogue/chat',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({purpose:_chatbox.opts.purpose, messages:_chatbox.messages})});
    const d = await res.json();
    if(!res.ok) throw new Error(d.detail||JSON.stringify(d));
    _chatbox.messages.push({role:'assistant', content:d.reply});
    document.getElementById('chatStatus').textContent='';
  }catch(e){
    document.getElementById('chatStatus').textContent='对话失败: '+e.message;
  }
  _chatbox.busy=false;
  renderChatMsgs();
}
async function chatFinalize(){
  if(!_chatbox || _chatbox.busy) return;
  if(!_chatbox.messages.length){ alert('还没有对话内容'); return; }
  if(!confirm('确认完成？AI 将根据对话生成最终结果。')) return;
  _chatbox.busy = true;
  document.getElementById('chatStatus').textContent='正在生成结果…';
  try{
    const res = await fetch('/api/dialogue/finalize',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({purpose:_chatbox.opts.purpose, messages:_chatbox.messages,
        extra_context:_chatbox.opts.extraContext||''})});
    const d = await res.json();
    if(!res.ok) throw new Error(d.detail||JSON.stringify(d));
    document.getElementById('chatStatus').textContent='';
    const onFinalize = _chatbox.opts.onFinalize;
    closeChatBox();
    if(onFinalize) await onFinalize(d.result);
  }catch(e){
    document.getElementById('chatStatus').textContent='生成失败: '+e.message;
    _chatbox.busy=false;
  }
}
// —— 对话配置弹窗（每用途 model/temperature/system） ——
function chatCfgOpen(){
  if(!_chatbox) return;
  const purpose = _chatbox.opts.purpose;
  fetch('/api/dialogue/config?purpose='+purpose).then(r=>r.json()).then(d=>{
    const cfg = d.config||{};
    const models = (d.models||[]).map(m=>`<option value="${m.value}" ${cfg.model===m.value?'selected':''}>${m.label}</option>`).join('');
    const html = `
      <div style="position:fixed;left:0;top:0;right:0;bottom:0;background:rgba(0,0,0,.3);z-index:450;display:flex;align-items:center;justify-content:center;" id="chatCfgModal" onclick="if(event.target===this)this.remove()">
        <div style="background:var(--card,#fff);border-radius:12px;padding:18px;width:420px;">
          <b style="font-size:14px;">对话配置（${esc(purpose)}）</b>
          <div style="margin-top:12px;">
            <div style="font-size:12px;color:var(--muted,#6B6B80);margin-bottom:4px;">模型</div>
            <select id="cfgModel" style="width:100%;padding:7px;border:1px solid var(--border,#E4E7EC);border-radius:6px;font-size:13px;">${models}</select>
          </div>
          <div style="margin-top:10px;">
            <div style="font-size:12px;color:var(--muted,#6B6B80);margin-bottom:4px;">温度（0~1，低=严谨确认）</div>
            <input id="cfgTemp" type="number" step="0.1" min="0" max="1" value="${cfg.temperature??0.4}" style="width:100%;padding:7px;border:1px solid var(--border,#E4E7EC);border-radius:6px;font-size:13px;">
          </div>
          <div style="margin-top:10px;">
            <div style="font-size:12px;color:var(--muted,#6B6B80);margin-bottom:4px;">对话系统提示词（可选，默认通用）</div>
            <textarea id="cfgSystem" rows="4" style="width:100%;padding:7px;border:1px solid var(--border,#E4E7EC);border-radius:6px;font-size:12px;">${esc(cfg.system||'')}</textarea>
          </div>
          <div style="display:flex;gap:8px;margin-top:14px;justify-content:flex-end;">
            <button style="border:none;border-radius:8px;padding:7px 14px;background:var(--primary,#4A6CF7);color:#fff;cursor:pointer;" onclick="chatCfgSave()">保存</button>
            <button style="border:none;border-radius:8px;padding:7px 14px;background:#F0F2F5;cursor:pointer;" onclick="document.getElementById('chatCfgModal').remove()">取消</button>
          </div>
        </div>
      </div>`;
    document.body.insertAdjacentHTML('beforeend', html);
  });
}
async function chatCfgSave(){
  const purpose = _chatbox.opts.purpose;
  const body = {purpose,
    model: document.getElementById('cfgModel').value,
    temperature: parseFloat(document.getElementById('cfgTemp').value),
    system: document.getElementById('cfgSystem').value};
  const res = await fetch('/api/dialogue/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d = await res.json();
  const modal = document.getElementById('chatCfgModal'); if(modal) modal.remove();
  if(!res.ok){ alert('保存失败: '+(d.detail||'')); return; }
  alert('已保存该对话的配置');
}
function esc(s){ return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
