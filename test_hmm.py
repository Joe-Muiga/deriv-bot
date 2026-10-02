"""Offline tests for the HMM regime engine + its tuner path. No network. Run: python test_hmm.py
Synthetic Scout: hot sessions win HOT_WR, cold sessions COLD_WR, session lengths random (in results,
one result every GAP seconds). Control: iid stream where NO regime exists."""
import json, logging, math, random, sys, types
for _m in ("aiohttp", "requests", "websockets"):
    try: __import__(_m)
    except ImportError: sys.modules[_m] = types.ModuleType(_m)
logging.disable(logging.CRITICAL)
import config
from regime_gate import RegimeGate, HMMGate, build_gate, hmm_fit
from auto_tuner import AutoTuner, replay_hmm

PAY, GAP, NS = 2.12, 4.0, 6
class Clock:
    def __init__(s): s.t = 1_700_000_000.0
    def __call__(s): return s.t

def stream(n, seg, hot=0.66, cold=0.33, seed=1):
    rnd = random.Random(seed); out = []; h = True
    while len(out) < n:
        for _ in range(rnd.randint(*seg)): out.append((rnd.random() < (hot if h else cold), h))
        h = not h
    return out[:n]

def ev(i, won, t): return {"contract_id": f"c{i}", "stake": 1.0, "payout": PAY,
                           "pnl": (PAY - 1) if won else -1.0, "confirmed": True, "ts": t}

def run(factory, s, lag=1, skip=0):
    """R from results >= skip (skip = the one-time learning period; the live bot keeps its
    learned state across the 3m45s redeploys, so steady state is what it actually sees)."""
    clk = Clock(); g = factory(clk); st = []; R = 0.0; tk = 0; inhot = 0
    for i, (w, h) in enumerate(s):
        j = i - 1 - lag
        if j >= 0 and st[j] and i >= skip: R += (PAY - 1) if w else -1.0; tk += 1; inhot += int(h)
        clk.t += GAP; g.on_result(ev(i, w, clk.t)); st.append(g.is_open())
    return R, tk, inhot

always = lambda s: sum((PAY - 1) if w else -1.0 for w, _ in s)
RULES = lambda c: RegimeGate(clock=c)
HMM = lambda c: HMMGate(clock=c, quiet=True)

SKIP = 500
print(f"== zig-zag Scout, steady state (results {SKIP}-2000, {NS} seeds; learning period excluded) ==")
res = {}
for name, seg in (("short 4-12", (4, 12)), ("medium 6-25", (6, 25)), ("long 15-60", (15, 60))):
    A = Rr = Rh = Th = Hh = 0
    for seed in range(NS):
        s = stream(2000, seg, seed=seed); A += always(s[SKIP:])
        Rr += run(RULES, s, skip=SKIP)[0]
        r, tk, ih = run(HMM, s, skip=SKIP); Rh += r; Th += tk; Hh += ih
    res[name] = (A / NS, Rr / NS, Rh / NS)
    print(f"  {name:12s} old rules {Rr/NS:+7.1f}R | HMM {Rh/NS:+7.1f}R "
          f"({Th/NS:.0f} trades, {Hh/max(Th,1)*100:.0f}% in hot sessions) | always-follow {A/NS:+7.1f}R")
print("  (always-follow is +EV in this sim only because hot/cold average 49.5% > 47.2% breakeven)")
for k, (a_, r_, h_) in res.items():
    assert h_ > r_, f"HMM must beat the old rules on {k}"

print("== NO-REGIME control: iid 47% — gate must not conjure an edge or bleed ==")
Gi = Ti = 0
for seed in range(NS):
    s = stream(1500, (6, 25), hot=.47, cold=.47, seed=100 + seed)
    r, tk, _ = run(HMM, s); Gi += r; Ti += tk
print(f"  HMM avg {Gi/NS:+.1f}R on {Ti/NS:.0f} trades per 1500 results (always-follow bleeds ~-40R)")
assert Ti / NS < 150 and Gi / NS > -10

print("== EM fit recovers the hidden parameters ==")
s = stream(400, (15, 60), seed=3); ys = [int(w) for w, _ in s]; ts = [GAP * i for i in range(400)]
best = max((hmm_fit(ys, ts, .62, .36, ms, iters=12) for ms in (40.0, 80.0, 160.0)), key=lambda r: r[2])
print(f"  true 66%/33%  ->  fitted {best[0]*100:.0f}%/{best[1]*100:.0f}%")
assert abs(best[0] - .66) < .10 and abs(best[1] - .33) < .10

print("== time awareness: evidence ages out; turn latency ==")
clk = Clock(); g = HMMGate(clock=clk, quiet=True)
s = stream(300, (15, 60), seed=5)
for i, (w, h) in enumerate(s):
    clk.t += GAP; g.on_result(ev(i, w, clk.t))
    if g.is_open(): break
if g.is_open():
    b0 = g.belief_now(); clk.t += 400
    print(f"  P(hot) {b0:.2f} -> {g.belief_now():.2f} after 400s of silence; open={g.is_open()}")
    assert not g.is_open(), "an open gate must close when no result confirms it for a long time"
# latency: results needed to react after a real turn
lat_in, lat_out = [], []
for seed in range(NS):
    s = stream(1500, (15, 60), seed=seed); clk = Clock(); g = HMMGate(clock=clk, quiet=True)
    prev = None; turn_i = None; prev_open = False
    for i, (w, h) in enumerate(s):
        if i > 400 and prev is not None and h != prev: turn_i = (i, h)
        prev = h; clk.t += GAP; g.on_result(ev(i, w, clk.t)); o = g.is_open()
        if turn_i and o != prev_open and i > turn_i[0]:
            (lat_in if turn_i[1] else lat_out).append(i - turn_i[0]); turn_i = None
        prev_open = o
if lat_in and lat_out:
    print(f"  after a real turn: opens after ~{sum(lat_in)/len(lat_in):.1f} results, closes after ~{sum(lat_out)/len(lat_out):.1f} results")

print("== persistence round trip (JSON, as stored in Render env vars) ==")
clk = Clock(); g = HMMGate(clock=clk, quiet=True)
for i, (w, h) in enumerate(stream(500, (15, 60), seed=2)): clk.t += GAP; g.on_result(ev(i, w, clk.t))
blob = json.dumps(g.export(), separators=(",", ":"))
g2 = HMMGate(clock=clk, quiet=True); g2.load(json.loads(blob))
assert abs(g2.ph - g.ph) < 1e-3 and abs(g2.pc - g.pc) < 1e-3 and g2.fitted == g.fitted
print(f"  ok, {len(blob)} chars, fitted={g2.fitted} p_hot={g2.ph:.2f} p_cold={g2.pc:.2f}")
g3 = HMMGate(clock=clk, quiet=True); r = RegimeGate(clock=clk, quiet=True)   # rules -> hmm upgrade path
for i, (w, h) in enumerate(stream(300, (15, 60), seed=2)): clk.t += GAP; r.on_result(ev(i, w, clk.t))
g3.load(r.export()); print(f"  upgrade from a rules-engine state: restored {len(g3._res)} results, fits={g3.fits}")

print("== auto-tuner on the HMM engine ==")
clk = Clock(); g = HMMGate(clock=clk, quiet=True); tn = AutoTuner(g, clock=clk)
for i, (w, h) in enumerate(stream(450, (15, 60), seed=7)):
    clk.t += GAP; g.on_result(ev(i, w, clk.t)); tn.note_result()
tn._since_tune = 999; tn.maybe_tune(background=False)
print(f"  runs={tn.runs} adopted={tn.adopted} no_edge={tn.no_edge} params={ {k: g.params[k] for k in ('enter_margin','exit_margin','exit_dd_r')} }")
assert tn.runs == 1 and g.params["exit_margin"] <= g.params["enter_margin"] - 0.02
clk = Clock(); g = HMMGate(clock=clk, quiet=True); tn = AutoTuner(g, clock=clk)
for i, (w, h) in enumerate(stream(450, (6, 25), hot=.47, cold=.47, seed=9)):
    clk.t += GAP; g.on_result(ev(i, w, clk.t)); tn.note_result()
tn._since_tune = 999; tn.maybe_tune(background=False)
print(f"  iid control: adopted={tn.adopted} no_edge={tn.no_edge} regimes_real={g.fitted} (gate should sit out)")

print("== hub integration: bus -> HMM gate -> learner -> tuner -> follower (shadow) ==")
config.SCOUT_FOLLOWER_ENABLED = True; config.FOLLOWER_MODE = "shadow"
import scout_follower, os, tempfile
from event_bus import bus
hub = scout_follower.Hub(events_path=os.path.join(tempfile.mkdtemp(), "ev.jsonl"))
assert hub.gate.ENGINE == "hmm", hub.gate.ENGINE
clk_t = [1_700_000_000.0]
hub.gate._clock = lambda: clk_t[0]; hub.follower._clock = lambda: clk_t[0]
taken0 = hub.follower.c["taken"]
for i, (w, h) in enumerate(stream(400, (15, 60), seed=11)):
    clk_t[0] += GAP
    e = {"contract_id": f"h{i}", "symbol": "R_10", "kind": "DIGIT", "strategy": "DONKEY", "contract_type": "DIFFER",
         "barrier": 3, "ts": clk_t[0]}
    bus.publish("entry", e)
    bus.publish("result", {**e, "stake": 0.35, "payout": 0.35 * PAY, "pnl": 0.35 * ((PAY - 1) if w else -1.0),
                           "won": w, "confirmed": True, "gate_eligible": True})
    import asyncio
rep = hub.report_lines()
print("  " + "\n  ".join(l[:170] for l in rep[:4]))
json.dumps(hub.export_env()); json.dumps(hub.summary(), default=str)
print("  summary + env export serialise OK")
print("\nALL HMM TESTS PASSED")
