"""
sf_dashboard.py — Scout + Follower panel (Oct 2026).

Shown ON the existing home dashboard ("/"): inject() puts the panel after the
paused banner, above the balance cards, with the first paint embedded so a
page refresh doesn't flash. Also available alone at /sf/view; raw data at /sf.

Read-only. No tokens are ever in the data. Works during the cooldown from the
state persisted in the Render env vars. Inert unless SCOUT_FOLLOWER_ENABLED.
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


def widget_html() -> str:
    """The panel: markup + scoped CSS + script, with the first paint embedded."""
    try:
        data = json.dumps(_summary(), default=str).replace("</", "<\\/")
    except Exception as exc:                      # never break the host page
        logger.warning(f"sf_dashboard: summary failed: {exc}")
        data = "null"
    return ("<style>" + WIDGET_CSS + "</style>" + WIDGET_MARKUP
            + "<script>window.__SF=" + data + ";" + WIDGET_JS + "</script>")


def inject(html: str) -> str:
    """Insert the panel into the existing dashboard page. No-op if disabled
    or on any error, so the original dashboard can never be broken by this."""
    try:
        import config
        if not getattr(config, "SCOUT_FOLLOWER_ENABLED", False):
            return html
        w = widget_html()
        marker = '<div class="grid">'
        i = html.find(marker)
        if i != -1:
            return html[:i] + w + html[i:]
        j = html.rfind("</body>")
        return (html[:j] + w + html[j:]) if j != -1 else html + w
    except Exception as exc:
        logger.warning(f"sf_dashboard.inject failed: {exc}")
        return html


def register(app) -> None:
    def _json():
        return (json.dumps(_summary(), default=str),
                200, {"Content-Type": "application/json", "Cache-Control": "no-store"})

    def _view():
        page = ("<!doctype html><html><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>Scout + Follower</title></head>"
                "<body style='background:#0a0e1a;margin:0;padding:12px;max-width:680px;margin:auto'>"
                + widget_html() + "</body></html>")
        return page, 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}

    app.add_url_rule("/sf", "sf_json", _json)
    app.add_url_rule("/sf/view", "sf_view", _view)


WIDGET_CSS = r"""
#sf-root{color:#c9d1d9;font:14px/1.35 'Segoe UI',system-ui,sans-serif;margin:0 0 24px}
#sf-root .sf-head{display:flex;justify-content:space-between;align-items:baseline;margin:0 2px 10px;padding-bottom:8px;border-bottom:1px solid #21262d}
#sf-root .sf-head b{font-size:1rem;color:#e6edf3;letter-spacing:.04em;text-transform:uppercase}
#sf-root .sfc{background:#161b22;border:1px solid #21262d;border-radius:10px;padding:14px 16px;margin-bottom:12px}
#sf-root .sfc h2{font-size:.75rem;text-transform:uppercase;letter-spacing:.06em;color:#8b949e;margin:0 0 8px}
#sf-root .sfb{font-size:1.7rem;font-weight:700}
#sf-root .sfr{display:flex;justify-content:space-between;gap:8px;padding:4px 0;border-bottom:1px solid #21262d}
#sf-root .sfr:last-child{border:0}
#sf-root .sfm{color:#8b949e}#sf-root .sfok{color:#3fb950}#sf-root .sfbad{color:#f85149}#sf-root .sfwarn{color:#d29922}
#sf-root .sfp{display:inline-block;border-radius:999px;padding:2px 10px;font-size:.75rem;font-weight:600;border:1px solid currentColor}
#sf-root .sfg{display:grid;grid-template-columns:1fr 1fr;gap:8px}
#sf-root .sfx{background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:8px 10px}
#sf-root .sfx b{display:block;font-size:1.1rem}
#sf-root .sfbar{position:relative;height:12px;background:#0d1117;border-radius:6px;margin:8px 0;border:1px solid #21262d}
#sf-root .sfbar i{position:absolute;top:0;bottom:0;left:0;border-radius:6px;background:#58a6ff}
#sf-root .sfbar u{position:absolute;top:-3px;bottom:-3px;width:2px;background:#e6edf3;opacity:.7}
#sf-root table{width:100%;border-collapse:collapse;font-size:.78rem}
#sf-root td,#sf-root th{padding:4px 3px;text-align:left;border-bottom:1px solid #21262d}
#sf-root th{color:#8b949e;font-weight:600}#sf-root tr:hover td{background:transparent}
#sf-root .sfs{overflow-x:auto}#sf-root svg{width:100%;height:150px;display:block}
#sf-root .sfv{padding:8px;border-radius:8px;margin-top:8px;font-weight:600;background:#0d1117}
"""

WIDGET_MARKUP = r"""<div id="sf-root"><div class="sf-head"><b>Scout + Follower</b><span class="sf-upd" style="color:#8b949e;font-size:.75rem">…</span></div><div class="sf-body"><div class="sfc">Loading…</div></div></div>"""

WIDGET_JS = r"""
const $=s=>document.querySelector(s);const ROOT='#sf-root';
const money=v=>v==null?'–':(v<0?'-':'')+'$'+Math.abs(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
const sgn=v=>v==null?'–':(v>=0?'+':'-')+'$'+Math.abs(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
const pc=v=>v==null?'n/a':(v*100).toFixed(1)+'%';
const cl=v=>v>0?'sfok':v<0?'sfbad':'';
const tm=t=>new Date(t*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'});
const dur=s=>{s=Math.max(0,Math.round(s));return s>=3600?(s/3600).toFixed(1)+'h':s>=60?Math.floor(s/60)+'m '+(s%60)+'s':s+'s'};
function chart(eq){
  if(!eq||eq.length<2)return '<div class="sfm">Not enough points yet (needs a few settled trades).</div>';
  const W=600,H=150,P=6,xs=eq.map(p=>p[0]),a=eq.map(p=>p[1]),b=eq.map(p=>p[2]);
  const all=a.concat(b),lo=Math.min(...all),hi=Math.max(...all),rg=(hi-lo)||1,x0=xs[0],xr=(xs[xs.length-1]-x0)||1;
  const pt=(t,v)=>(P+(t-x0)/xr*(W-2*P)).toFixed(1)+','+(H-P-(v-lo)/rg*(H-2*P)).toFixed(1);
  const line=(arr,c)=>'<polyline fill="none" stroke="'+c+'" stroke-width="2" points="'+arr.map((v,i)=>pt(xs[i],v)).join(' ')+'"/>';
  return '<svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none">'+line(b,'#9aa0a6')+line(a,'#2563eb')+'</svg>'
   +'<div class="sfm" style="font-size:12px"><span style="color:#2563eb">■</span> Follower (gated) &nbsp; <span style="color:#9aa0a6">■</span> Always-follow &nbsp; range '+money(lo)+' – '+money(hi)+'</div>';
}
function render(d){
  if(d.status==='off'){$(ROOT+' .sf-body').innerHTML='<div class="sfc">'+d.msg+'</div>';return}
  const g=d.gate,f=d.follower||{},now=d.ts,cfg=d.config||{};
  const cool=d.phase==='cooldown'&&d.cooldown_until>now;
  const phase=cool?'<span class="sfp sfwarn">COOLDOWN '+dur(d.cooldown_until-now)+'</span>':'<span class="sfp sfok">SCOUT TRADING</span>';
  const open=g.open;
  let h='<div class="sfc"><div style="display:flex;justify-content:space-between;align-items:center">'
   +'<div><div class="sfm">Edge gate</div><div class="sfb '+(open?'sfok':'sfbad')+'">'+(open?'OPEN ✅':'CLOSED ⛔')+'</div>'
   +'<div class="sfm">for '+dur(g.held_secs)+' · '+g.reason+'</div></div><div style="text-align:right">'+phase
   +'<div style="margin-top:6px"><span class="sfp">'+(f.mode||'no follower').toUpperCase()+'</span></div></div></div></div>';
  const be=g.breakeven,thr=g.threshold;
  h+='<div class="sfc"><h2>Is there an edge right now?</h2>'
   +'<div class="sfr"><span>Scout win rate (last '+g.n+')</span><b class="'+(g.win_rate>=thr?'sfok':'sfbad')+'">'+pc(g.win_rate)+'</b></div>'
   +'<div class="sfr"><span class="sfm">Break-even / needed</span><span>'+pc(be)+' / '+pc(thr)+'</span></div>'
   +'<div class="sfr"><span>Probability of edge</span><b>'+(g.prob*100).toFixed(0)+'%</b></div>'
   +'<div class="sfbar"><i style="width:'+(g.prob*100)+'%"></i><u style="left:'+(cfg.EDGE_GATE_CLOSE_PROB*100)+'%"></u><u style="left:'+(cfg.EDGE_GATE_OPEN_PROB*100)+'%"></u></div>'
   +'<div class="sfm" style="font-size:12px">Lines: closes below '+(cfg.EDGE_GATE_CLOSE_PROB*100)+'% · opens above '+(cfg.EDGE_GATE_OPEN_PROB*100)+'%</div>'
   +'<div class="sfr"><span class="sfm">Current loss run</span><span class="'+(g.loss_run>=cfg.EDGE_GATE_CLOSE_LOSS_RUN-1?'sfbad':'')+'">'+g.loss_run+' / '+cfg.EDGE_GATE_CLOSE_LOSS_RUN+'</span></div>'
   +'<div class="sfr"><span class="sfm">Open/close cycles</span><span>'+g.cycles+'</span></div></div>';
  if(g.engine==='hmm'){
   h+='<div class="sfc"><h2>Regime tracker (HMM)</h2>'
    +'<div class="sfr"><span>P(hot) right now</span><b>'+(g.belief*100).toFixed(0)+'%</b></div>'
    +'<div class="sfbar"><i style="width:'+(g.belief*100)+'%"></i></div>'
    +'<div class="sfr"><span>Forecast next-trade win</span><b class="'+(g.edge>=0?'sfok':'sfbad')+'">'+pc(g.q)+'</b></div>'
    +'<div class="sfr"><span class="sfm">vs break-even · edge</span><span>'+pc(g.breakeven)+' · '+(g.edge*100>=0?'+':'')+(g.edge*100).toFixed(1)+'pt</span></div>'
    +'<div class="sfr"><span class="sfm">Learned hot / cold win rate</span><span>'+pc(g.p_hot)+' / '+pc(g.p_cold)+'</span></div>'
    +'<div class="sfr"><span class="sfm">Typical session</span><span>~'+g.session_secs+'s</span></div>'
    +'<div class="sfr"><span class="sfm">Zig-zag statistically real?</span><b class="'+(g.regimes_real?'sfok':'sfbad')+'">'+(g.regimes_real?'YES':'NO')+' (LR '+g.fit_lr+')</b></div>'
    +'<div class="sfm" style="font-size:12px">Opens when edge ≥ '+((g.params.enter_margin||0)*100).toFixed(0)+'pt · closes below '+((g.params.exit_margin||0)*100).toFixed(0)+'pt (auto-tuned)</div></div>';
  }
  if(f.disabled!==undefined){h+='<div class="sfc sfbad">Follower disabled: '+f.disabled+'</div>'}
  else{
   const adv=f.pnl-f.baseline_pnl;
   h+='<div class="sfc"><h2>Follower ('+f.mode+') vs always-follow</h2><div class="sfg">'
    +'<div class="sfx"><span class="sfm">Gated PnL</span><b class="'+cl(f.pnl)+'">'+sgn(f.pnl)+'</b></div>'
    +'<div class="sfx"><span class="sfm">Always-follow PnL</span><b class="'+cl(f.baseline_pnl)+'">'+sgn(f.baseline_pnl)+'</b></div>'
    +'<div class="sfx"><span class="sfm">Balance</span><b>'+money(f.balance)+'</b></div>'
    +'<div class="sfx"><span class="sfm">Stake now</span><b>'+money(f.stake)+'</b></div></div>'
    +'<div class="sfv" style="background:var(--bg)">'+(f.taken<20?'<span class="sfwarn">Too few trades ('+f.taken+') — not enough to judge the gate yet.</span>':adv>0?'<span class="sfok">Gate is ahead by '+sgn(adv)+'</span>':'<span class="sfbad">Gate is behind by '+sgn(adv)+' — not adding value yet</span>')+'</div></div>';
   h+='<div class="sfc"><h2>Equity</h2>'+chart(d.equity)+'</div>';
   const q=(f.gate_on_wr!=null&&f.gate_off_wr!=null)?(f.gate_on_wr-f.gate_off_wr):null;
   h+='<div class="sfc"><h2>Are there high/low sessions?</h2><div class="sfg">'
    +'<div class="sfx"><span class="sfm">Scout win rate, gate ON</span><b>'+pc(f.gate_on_wr)+'</b><span class="sfm">n='+f.gate_on_n+'</span></div>'
    +'<div class="sfx"><span class="sfm">Scout win rate, gate OFF</span><b>'+pc(f.gate_off_wr)+'</b><span class="sfm">n='+f.gate_off_n+'</span></div></div>'
    +'<div class="sfm" style="margin-top:6px;font-size:12px">'+(q==null?'Needs trades in both states.':q>0.03?'ON is clearly better than OFF: the gate is catching good stretches.':'ON ≈ OFF: no evidence of catchable sessions yet.')+'</div></div>';
   h+='<div class="sfc"><h2>Follower details</h2>'
    +'<div class="sfr"><span>Taken</span><span>'+f.taken+' ('+f.wins+'W / '+f.losses+'L, '+pc(f.win_rate)+')</span></div>'
    +'<div class="sfr"><span>Open now</span><span>'+f.open+' (max '+cfg.FOLLOWER_MAX_OPEN+')</span></div>'
    +'<div class="sfr"><span>Profit pause</span><span class="'+(f.paused_secs>0?'sfwarn':'')+'">'+(f.paused_secs>0?dur(f.paused_secs)+' left':'none')+'</span></div>'
    +'<div class="sfr"><span>Donkey guard</span><span class="'+(f.guard==='ok'?'sfok':'sfbad')+'">'+f.guard+'</span></div>'
    +'<div class="sfr"><span>Placement failures</span><span class="'+(f.place_failed?'sfbad':'')+'">'+f.place_failed+'</span></div>'
    +'<div class="sfr"><span class="sfm">Skipped</span><span class="sfm">'+Object.entries(f.skipped||{}).map(([k,v])=>k+' '+v).join(' · ')+'</span></div></div>';
  }
  const s1=d.scout_1h,s24=d.scout_24h,t=d.scout_total;
  h+='<div class="sfc"><h2>Scout (demo, trades continuously)</h2>'
   +'<div class="sfr"><span>Last 1h</span><span>'+s1.wins+'W / '+s1.losses+'L · <b class="'+cl(s1.pnl)+'">'+sgn(s1.pnl)+'</b></span></div>'
   +'<div class="sfr"><span>Last 24h</span><span>'+s24.wins+'W / '+s24.losses+'L · <b class="'+cl(s24.pnl)+'">'+sgn(s24.pnl)+'</b></span></div>'
   +'<div class="sfr"><span>Since start</span><span>'+t.n+' trades · '+pc(t.n?t.wins/t.n:null)+' · '+sgn(t.pnl)+'</span></div></div>';
  h+='<div class="sfc"><h2>Gate history</h2>'+((d.gate_history||[]).length?'<div class="sfs"><table>'+d.gate_history.slice(0,12).map(r=>'<tr><td>'+tm(r[0])+'</td><td class="'+(r[1]==='OPEN'?'sfok':'sfbad')+'">'+r[1]+'</td><td class="sfm">'+r[2]+'</td></tr>').join('')+'</table></div>':'<div class="sfm">No changes yet.</div>')+'</div>';
  h+='<div class="sfc"><h2>Recent settled trades</h2>'+((d.recent||[]).length?'<div class="sfs"><table><tr><th>Time</th><th></th><th>Symbol</th><th>Type</th><th>Stake</th><th>PnL</th></tr>'
   +d.recent.slice(0,20).map(r=>'<tr><td>'+tm(r[0])+'</td><td>'+(r[1]==='f'?'F':'S')+'</td><td>'+r[2]+'</td><td>'+(r[3]||'')+(r[4]!=null?' '+r[4]:'')+'</td><td>'+(r[5]!=null?(+r[5]).toFixed(2):'')+'</td><td class="'+cl(r[6])+'">'+(r[6]!=null?(+r[6]).toFixed(2):'')+'</td></tr>').join('')
   +'</table></div><div class="sfm" style="font-size:11px">S = Scout, F = Follower</div>':'<div class="sfm">None yet.</div>')+'</div>';
  h+='<div class="sfc sfm" style="font-size:12px"><h2>Settings in use</h2>window '+cfg.EDGE_GATE_WINDOW+' · min trades '+cfg.EDGE_GATE_MIN_TRADES+' · margin +'+(cfg.EDGE_GATE_MARGIN*100)+'pt · payout x'+cfg.EDGE_PAYOUT_MULTIPLE+' · exposure cap '+(cfg.FOLLOWER_MAX_EXPOSURE_PCT*100)+'% · max '+cfg.FOLLOWER_MAX_TRADES_PER_HOUR+'/h · leg '+cfg.FIXED_CYCLE_LEG_MINUTES+'min</div>';
  $(ROOT+' .sf-body').innerHTML=h;$(ROOT+' .sf-upd').textContent='updated '+tm(d.ts);
}
async function tick(){try{const r=await fetch('/sf',{cache:'no-store'});render(await r.json())}catch(e){$(ROOT+' .sf-upd').textContent='offline…'}}
if(window.__SF){render(window.__SF)}tick();setInterval(tick,5000);
"""
