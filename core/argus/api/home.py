"""The home page at /: a small live status page until Helios arrives (C6).

Status comes from /status every 3 s; events stream over /ws/events. If ARGUS_WORKER_TOKEN is set, open the
page as /?token=<token> so the live events can connect (the token stays in your browser).
"""

HOME_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Argus</title>
<style>
:root{--bg:#0B0D12;--panel:#11141B;--line:#1A1F2A;--text:#E6E8EE;--muted:#98A0B3;--ok:#3DD68C;--bad:#FF7A6B;
--warn:#F5A524;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 system-ui,sans-serif}
main{max-width:980px;margin:0 auto;padding:32px 16px}h1{font-size:21px;margin:0}h2{font-size:13px;font-weight:600;
color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin:28px 0 8px}.m{color:var(--muted)}
.top{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}.dot{display:inline-block;width:9px;height:9px;
border-radius:50%;background:var(--muted);margin-right:7px}.grid{display:grid;grid-template-columns:repeat(auto-fit,
minmax(200px,1fr));gap:10px;margin-top:18px}.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:12px 14px}.card b{display:block;font-size:12px;color:var(--muted);font-weight:500}.card span{font-family:var(--mono);
font-size:13px;word-break:break-word}table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:12.5px}
td{padding:5px 8px 5px 0;border-bottom:1px solid var(--line);vertical-align:top;white-space:nowrap}
td.k{color:var(--warn)}td.k.ok{color:var(--ok)}td.k.bad{color:var(--bad)}td.k.info{color:var(--muted)}td.w{white-space:normal;color:var(--muted)}tr.new td{animation:f 1.2s ease-out}
@keyframes f{from{background:#F5A52422}to{background:transparent}}.wrap{overflow-x:auto}
a{color:var(--warn)}.hint{color:var(--warn);font-size:13px}
</style></head><body><main>
<div class="top"><h1>Argus <span class="m" id="ver"></span></h1><span><span class="dot" id="dot"></span>
<span id="st">connecting…</span></span></div>
<div class="grid">
<div class="card"><b>Database</b><span id="db">–</span></div>
<div class="card"><b>Workers</b><span id="wk">–</span></div>
<div class="card"><b>Jobs</b><span id="jb">–</span></div>
<div class="card"><b>Map</b><span id="mp">–</span></div>
<div class="card"><b>Background</b><span id="bg">–</span></div>
<div class="card"><b>Uptime</b><span id="up">–</span></div>
</div>
<h2>Live events <span class="m" id="live" style="text-transform:none;letter-spacing:0"></span></h2>
<p class="hint" id="hint" hidden></p>
<div class="wrap"><table><tbody id="ev"><tr><td class="m">waiting for events…</td></tr></tbody></table></div>
<p class="m" style="margin-top:22px">Helios arrives in core step C6. Raw data: <a href="/status">/status</a> ·
<a href="/health">/health</a> · <a href="/docs">API docs</a></p>
</main><script>
const $=id=>document.getElementById(id);
const token=new URLSearchParams(location.search).get("token");
let lastSeq=null, rows=[], retry=1000, needToken=false;
const fmtUp=s=>s<120?Math.round(s)+" s":s<7200?Math.round(s/60)+" min":(s/3600).toFixed(1)+" h";
async function status(){
  try{
    const s=await (await fetch("/status")).json();
    $("ver").textContent=s.version; $("dot").style.background=s.status==="ok"?"var(--ok)":"var(--bad)";
    $("st").textContent=s.status+" · "+s.instance+" on "+s.host;
    $("db").textContent="schema v"+s.database.schema_version+", "+s.database.writer;
    const on=s.workers.filter(w=>w.state==="online").map(w=>w.id);
    $("wk").textContent=on.length?on.join(", "):"none connected";
    const j=Object.entries(s.jobs).map(([k,v])=>k+" "+v).join(", "); $("jb").textContent=j||"no jobs yet";
    $("mp").textContent=s.map.nodes+" boxes, "+s.map.edges+" lines";
    $("bg").textContent="watchdog "+(s.watchdog.alive?"on":"off")+", stream "+(s.events.alive?"on":"off")+
      " ("+s.events.subscribers+" viewing)";
    $("up").textContent=fmtUp(s.uptime_seconds);
    needToken=s.token_required&&!token;
    if(needToken){$("hint").hidden=false;$("hint").textContent="Live events need the worker token: open this page as /?token=<your ARGUS_WORKER_TOKEN>.";}
  }catch(e){$("dot").style.background="var(--bad)";$("st").textContent="argus not reachable";}
}
function time(t){const d=new Date(t*1000);return d.toLocaleTimeString([], {hour12:false})+"."+String(d.getMilliseconds()).padStart(3,"0");}
function detail(e){const d=e.data||{};const bits=[];for(const k of ["workflow","from","attempt","reason","idx","tier","error","host"]){if(d[k]!==undefined&&d[k]!==null)bits.push(k+"="+(typeof d[k]==="object"?JSON.stringify(d[k]):d[k]));}return bits.join(" ");}
function tone(k){return /succeeded|online|added/.test(k)?"ok":/dead|failed|offline/.test(k)?"bad":/^step\./.test(k)?"info":"";}
function add(events,fresh){
  for(const e of events){lastSeq=e.seq;rows.unshift({e,fresh});}
  rows=rows.slice(0,40);
  $("ev").innerHTML=rows.map(({e,fresh})=>`<tr class="${fresh?"new":""}"><td class="m">${time(e.at)}</td><td class="k ${tone(e.kind)}">${e.kind}</td>`+
    `<td>${e.from||""}${e.to?" → "+e.to:""}</td><td class="m">${e.job_id?e.job_id.slice(-6):""}${e.step?" · "+e.step:""}</td><td class="w">${detail(e)}</td></tr>`).join("")
    ||'<tr><td class="m">no events yet</td></tr>';
  rows.forEach(r=>r.fresh=false);
}
function connect(){
  const q=new URLSearchParams(); if(lastSeq!==null)q.set("since",lastSeq); if(token)q.set("token",token);
  const ws=new WebSocket((location.protocol==="https:"?"wss://":"ws://")+location.host+"/ws/events?"+q);
  ws.onopen=()=>{retry=1000;$("live").textContent="· live";};
  ws.onmessage=m=>{const msg=JSON.parse(m.data);
    if(msg.type==="hello"&&lastSeq===null){lastSeq=Math.max(0,msg.seq-30);ws.close(4100);} // first visit: show the last 30
    else if(msg.type==="events")add(msg.events,!msg.replay);};
  ws.onclose=ev=>{$("live").textContent=needToken?"· needs token":"· reconnecting";
    const wait=ev.code===4100?0:retry; retry=Math.min(retry*2,10000); setTimeout(connect,wait);};
}
status(); setInterval(status,3000); connect();
</script></body></html>"""
