'use strict';
let token;
const result=document.getElementById('result'),button=document.getElementById('connect');
function show(s){result.textContent=s.configured?(s.enabled?'JEV 已启用 · ':'已有密钥，尚未启用 · ')+s.model+(s.verified?' · 模型列表连接验证通过':' · 尚未验证连接'):'等待填写 TypeSafe API key，目前尚未连接 JEV。';}
(async()=>{try{const r=await fetch('/api/bootstrap');if(!r.ok)throw Error('无法连接本机服务');const b=await r.json();token=b.token;show(b.jev);}catch(e){result.textContent=e.message;}})();
document.getElementById('configure').addEventListener('submit',async e=>{e.preventDefault();if(!token){result.textContent='本机连接尚未就绪，请稍后刷新此配置页。';return;}button.disabled=true;result.textContent='正在验证 TypeSafe 连接…';try{const r=await fetch('/api/models/connect',{method:'POST',headers:{'Content-Type':'application/json','X-Subtasks-Token':token},body:JSON.stringify({key:document.getElementById('key').value})});const b=await r.json();if(!r.ok)throw Error(b.error||'连接失败');document.getElementById('key').value='';show(b);}catch(e){result.textContent=e.message;}finally{button.disabled=false;}});
