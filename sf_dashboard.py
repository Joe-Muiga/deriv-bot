"""
sf_dashboard.py — phone-friendly Scout + Follower dashboard (Oct 2026).

  GET /sf/view   the dashboard page (auto-refreshes every 5 s)
  GET /sf        the same data as JSON

Read-only. Never exposes tokens (none are in the summary). Works during the
cooldown too: it rebuilds from the state persisted in the Render env vars.
Registered from main.py via register(app); inert unless SCOUT_FOLLOWER_ENABLED.
"""
import json
import logging

logger = logging.getLogger("sf_dashboard")


def _summary() -> dict:
    import scout_follower as sf
    hub = sf.get_hub()
    if hub is None:
        return {"status": "off", "msg": "SCOUT_FOLLOWER_ENABLED is not set"}
    return hub.summary()


def register(app) -> None:
    def _json():
        return (json.dumps(_summary(), default=str),
                200, {"Content-Type": "application/json", "Cache-Control": "no-store"})

    def _view():
        return PAGE, 200, {"Content-Type": "text/html; charset=utf-8",
                           "Cache-Control": "no-store"}

    app.add_url_rule("/sf", "sf_json", _json)
    app.add_url_rule("/sf/view", "sf_view", _view)


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Scout + Follower</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--tx:#14171a;--mu:#6b7280;--ok:#0f9d58;--bad:#d93025;--warn:#e8a200;--ln:#e5e7eb;--ac:#2563eb}
@media (prefers-color-scheme:dark){:root{--bg:#0e1116;--card:#171b22;--tx:#e8eaed;--mu:#9aa0a6;--ln:#272c35;--ac:#6ea0ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.35 system-ui,sans-serif;padding:10px;max-width:640px;margin:auto}
h1{font-size:16px;margin:4px 2px 8px;display:flex;justify-content:space-between;align-items:center}
.card{background:var(--card);border:1px solid var(--ln);border-radius:12px;padding:12px;margin-bottom:10px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--mu);margin:0 0 8px}
.big{font-size:26px;font-weight:700}.row{display:flex;justify-content:space-between;gap:8px;padding:3px 0;border-bottom:1px solid var(--ln)}
.row:last-child{border:0}.mu{color:var(--mu)}.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
.chip{display:inline-block;border-radius:999px;padding:2px 10px;font-size:12px;font-weight:600;border:1px solid currentColor}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.cell{background:var(--bg);border-radius:10px;padding:8px}
.cell b{display:block;font-size:18px}.bar{position:relative;height:12px;background:var(--bg);border-radius:6px;margin:6px 0}
.bar i{position:absolute;top:0;bottom:0;left:0;border-radius:6px;background:var(--ac)}
.bar u{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--tx);opacity:.6}
table{width:100%;border-collapse:collapse;font-size:12px}td,th{padding:4px 3px;text-align:left;border-bottom:1px solid var(--ln)}
th{color:var(--mu);font-weight:600}.sc{overflow-x:auto}svg{width:100%;height:150px;display:block}
.verdict{padding:8px;border-radius:10px;margin-top:8px;font-weight:600}
</style></head><body>
<h1><span>Scout + Follower</span><span id="upd" class="mu" style="font-size:12px">…</span></h1>
<div id="app"><div class="card">Loading…</div></div>
<script>
const $=s=>document.querySelector(s);
const money=v=>v==null?'–':(v<0?'-':'')+'$'+Math.abs(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
const sgn=v=>v==null?'–':(v>=0?'+':'-')+'$'+Math.abs(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
const pc=v=>v==null?'n/a':(v*100).toFixed(1)+'%';
const cl=v=>v>0?'ok':v<0?'bad':'';
const tm=t=>new Date(t*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'});
const dur=s=>{s=Math.max(0,Math.round(s));return s>=3600?(s/3600).toFixed(1)+'h':s>=60?Math.floor(s/60)+'m '+(s%60)+'s':s+'s'};
function chart(eq){
  if(!eq||eq.length<2)return '<div class="mu">Not enough points yet (needs a few settled trades).</div>';
  const W=600,H=150,P=6,xs=eq.map(p=>p[0]),a=eq.map(p=>p[1]),b=eq.map(p=>p[2]);
  const all=a.concat(b),lo=Math.min(...all),hi=Math.max(...all),rg=(hi-lo)||1,x0=xs[0],xr=(xs[xs.length-1]-x0)||1;
  const pt=(t,v)=>(P+(t-x0)/xr*(W-2*P)).toFixed(1)+','+(H-P-(v-lo)/rg*(H-2*P)).toFixed(1);
  const line=(arr,c)=>'<polyline fill="none" stroke="'+c+'" stroke-width="2" points="'+arr.map((v,i)=>pt(xs[i],v)).join(' ')+'"/>';
  return '<svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none">'+line(b,'#9aa0a6')+line(a,'#2563eb')+'</svg>'
   +'<div class="mu" style="font-size:12px"><span style="color:#2563eb">■</span> Follower (gated) &nbsp; <span style="color:#9aa0a6">■</span> Always-follow &nbsp; range '+money(lo)+' – '+money(hi)+'</div>';
}
function render(d){
  if(d.status==='off'){$('#app').innerHTML='<div class="card">'+d.msg+'</div>';return}
  const g=d.gate,f=d.follower||{},now=d.ts,cfg=d.config||{};
  const cool=d.phase==='cooldown'&&d.cooldown_until>now;
  const phase=cool?'<span class="chip warn">COOLDOWN '+dur(d.cooldown_until-now)+'</span>':'<span class="chip ok">SCOUT TRADING</span>';
  const open=g.open;
  let h='<div class="card"><div style="display:flex;justify-content:space-between;align-items:center">'
   +'<div><div class="mu">Edge gate</div><div class="big '+(open?'ok':'bad')+'">'+(open?'OPEN ✅':'CLOSED ⛔')+'</div>'
   +'<div class="mu">for '+dur(g.held_secs)+' · '+g.reason+'</div></div><div style="text-align:right">'+phase
   +'<div style="margin-top:6px"><span class="chip">'+(f.mode||'no follower').toUpperCase()+'</span></div></div></div></div>';
  const be=g.breakeven,thr=g.threshold;
  h+='<div class="card"><h2>Is there an edge right now?</h2>'
   +'<div class="row"><span>Scout win rate (last '+g.n+')</span><b class="'+(g.win_rate>=thr?'ok':'bad')+'">'+pc(g.win_rate)+'</b></div>'
   +'<div class="row"><span class="mu">Break-even / needed</span><span>'+pc(be)+' / '+pc(thr)+'</span></div>'
   +'<div class="row"><span>Probability of edge</span><b>'+(g.prob*100).toFixed(0)+'%</b></div>'
   +'<div class="bar"><i style="width:'+(g.prob*100)+'%"></i><u style="left:'+(cfg.EDGE_GATE_CLOSE_PROB*100)+'%"></u><u style="left:'+(cfg.EDGE_GATE_OPEN_PROB*100)+'%"></u></div>'
   +'<div class="mu" style="font-size:12px">Lines: closes below '+(cfg.EDGE_GATE_CLOSE_PROB*100)+'% · opens above '+(cfg.EDGE_GATE_OPEN_PROB*100)+'%</div>'
   +'<div class="row"><span class="mu">Current loss run</span><span class="'+(g.loss_run>=cfg.EDGE_GATE_CLOSE_LOSS_RUN-1?'bad':'')+'">'+g.loss_run+' / '+cfg.EDGE_GATE_CLOSE_LOSS_RUN+'</span></div>'
   +'<div class="row"><span class="mu">Open/close cycles</span><span>'+g.cycles+'</span></div></div>';
  if(f.disabled!==undefined){h+='<div class="card bad">Follower disabled: '+f.disabled+'</div>'}
  else{
   const adv=f.pnl-f.baseline_pnl;
   h+='<div class="card"><h2>Follower ('+f.mode+') vs always-follow</h2><div class="grid">'
    +'<div class="cell"><span class="mu">Gated PnL</span><b class="'+cl(f.pnl)+'">'+sgn(f.pnl)+'</b></div>'
    +'<div class="cell"><span class="mu">Always-follow PnL</span><b class="'+cl(f.baseline_pnl)+'">'+sgn(f.baseline_pnl)+'</b></div>'
    +'<div class="cell"><span class="mu">Balance</span><b>'+money(f.balance)+'</b></div>'
    +'<div class="cell"><span class="mu">Stake now</span><b>'+money(f.stake)+'</b></div></div>'
    +'<div class="verdict" style="background:var(--bg)">'+(f.taken<20?'<span class="warn">Too few trades ('+f.taken+') — not enough to judge the gate yet.</span>':adv>0?'<span class="ok">Gate is ahead by '+sgn(adv)+'</span>':'<span class="bad">Gate is behind by '+sgn(adv)+' — not adding value yet</span>')+'</div></div>';
   h+='<div class="card"><h2>Equity</h2>'+chart(d.equity)+'</div>';
   const q=(f.gate_on_wr!=null&&f.gate_off_wr!=null)?(f.gate_on_wr-f.gate_off_wr):null;
   h+='<div class="card"><h2>Are there high/low sessions?</h2><div class="grid">'
    +'<div class="cell"><span class="mu">Scout win rate, gate ON</span><b>'+pc(f.gate_on_wr)+'</b><span class="mu">n='+f.gate_on_n+'</span></div>'
    +'<div class="cell"><span class="mu">Scout win rate, gate OFF</span><b>'+pc(f.gate_off_wr)+'</b><span class="mu">n='+f.gate_off_n+'</span></div></div>'
    +'<div class="mu" style="margin-top:6px;font-size:12px">'+(q==null?'Needs trades in both states.':q>0.03?'ON is clearly better than OFF: the gate is catching good stretches.':'ON ≈ OFF: no evidence of catchable sessions yet.')+'</div></div>';
   h+='<div class="card"><h2>Follower details</h2>'
    +'<div class="row"><span>Taken</span><span>'+f.taken+' ('+f.wins+'W / '+f.losses+'L, '+pc(f.win_rate)+')</span></div>'
    +'<div class="row"><span>Open now</span><span>'+f.open+' (max '+cfg.FOLLOWER_MAX_OPEN+')</span></div>'
    +'<div class="row"><span>Profit pause</span><span class="'+(f.paused_secs>0?'warn':'')+'">'+(f.paused_secs>0?dur(f.paused_secs)+' left':'none')+'</span></div>'
    +'<div class="row"><span>Donkey guard</span><span class="'+(f.guard==='ok'?'ok':'bad')+'">'+f.guard+'</span></div>'
    +'<div class="row"><span>Placement failures</span><span class="'+(f.place_failed?'bad':'')+'">'+f.place_failed+'</span></div>'
    +'<div class="row"><span class="mu">Skipped</span><span class="mu">'+Object.entries(f.skipped||{}).map(([k,v])=>k+' '+v).join(' · ')+'</span></div></div>';
  }
  const s1=d.scout_1h,s24=d.scout_24h,t=d.scout_total;
  h+='<div class="card"><h2>Scout (demo, trades continuously)</h2>'
   +'<div class="row"><span>Last 1h</span><span>'+s1.wins+'W / '+s1.losses+'L · <b class="'+cl(s1.pnl)+'">'+sgn(s1.pnl)+'</b></span></div>'
   +'<div class="row"><span>Last 24h</span><span>'+s24.wins+'W / '+s24.losses+'L · <b class="'+cl(s24.pnl)+'">'+sgn(s24.pnl)+'</b></span></div>'
   +'<div class="row"><span>Since start</span><span>'+t.n+' trades · '+pc(t.n?t.wins/t.n:null)+' · '+sgn(t.pnl)+'</span></div></div>';
  h+='<div class="card"><h2>Gate history</h2>'+((d.gate_history||[]).length?'<div class="sc"><table>'+d.gate_history.slice(0,12).map(r=>'<tr><td>'+tm(r[0])+'</td><td class="'+(r[1]==='OPEN'?'ok':'bad')+'">'+r[1]+'</td><td class="mu">'+r[2]+'</td></tr>').join('')+'</table></div>':'<div class="mu">No changes yet.</div>')+'</div>';
  h+='<div class="card"><h2>Recent settled trades</h2>'+((d.recent||[]).length?'<div class="sc"><table><tr><th>Time</th><th></th><th>Symbol</th><th>Type</th><th>Stake</th><th>PnL</th></tr>'
   +d.recent.slice(0,20).map(r=>'<tr><td>'+tm(r[0])+'</td><td>'+(r[1]==='f'?'F':'S')+'</td><td>'+r[2]+'</td><td>'+(r[3]||'')+(r[4]!=null?' '+r[4]:'')+'</td><td>'+(r[5]!=null?(+r[5]).toFixed(2):'')+'</td><td class="'+cl(r[6])+'">'+(r[6]!=null?(+r[6]).toFixed(2):'')+'</td></tr>').join('')
   +'</table></div><div class="mu" style="font-size:11px">S = Scout, F = Follower</div>':'<div class="mu">None yet.</div>')+'</div>';
  h+='<div class="card mu" style="font-size:12px"><h2>Settings in use</h2>window '+cfg.EDGE_GATE_WINDOW+' · min trades '+cfg.EDGE_GATE_MIN_TRADES+' · margin +'+(cfg.EDGE_GATE_MARGIN*100)+'pt · payout x'+cfg.EDGE_PAYOUT_MULTIPLE+' · exposure cap '+(cfg.FOLLOWER_MAX_EXPOSURE_PCT*100)+'% · max '+cfg.FOLLOWER_MAX_TRADES_PER_HOUR+'/h · leg '+cfg.FIXED_CYCLE_LEG_MINUTES+'min</div>';
  $('#app').innerHTML=h;$('#upd').textContent='updated '+tm(d.ts);
}
async function tick(){try{const r=await fetch('/sf',{cache:'no-store'});render(await r.json())}catch(e){$('#upd').textContent='offline…'}}
tick();setInterval(tick,5000);
</script></body></html>"""
