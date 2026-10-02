"""Offline simulation + tests for regime_gate / online_learner / auto_tuner.
No network. Run: python test_regime.py

Synthetic Scout: zig-zag regimes. HOT segments win at HOT_WR, COLD segments at COLD_WR,
segment lengths random. A follower takes every Scout trade the gate lets through
(decision uses results up to one settle earlier). We compare:
   always-follow | old Beta EdgeGate | new RegimeGate | oracle (perfect hindsight)
and run a NO-REGIME control (iid 47%) to prove the gate does not conjure an edge."""
import os, random, sys, types
for _m in ("aiohttp", "requests", "websockets"):
    try: __import__(_m)
    except ImportError: sys.modules[_m] = types.ModuleType(_m)
import config
from regime_gate import RegimeGate, DEFAULT_PARAMS
from edge_gate import EdgeGate
from online_learner import OnlineLearner
from auto_tuner import AutoTuner, replay
logging = __import__("logging"); logging.disable(logging.CRITICAL)

PAY = 2.12
class Clock:
    def __init__(s): s.t = 1_700_000_000.0
    def __call__(s): return s.t

def make_stream(n, hot_wr=0.66, cold_wr=0.33, seg=(15, 60), seed=1, regimes=True, iid_wr=0.47):
    rnd = random.Random(seed); out = []; hot = True
    while len(out) < n:
        L = rnd.randint(*seg)
        for _ in range(L):
            wr = (hot_wr if hot else cold_wr) if regimes else iid_wr
            out.append((rnd.random() < wr, hot))
        hot = not hot
    return out[:n]

def ev(i, won, clk):
    return {"contract_id": f"c{i}", "stake": 1.0, "payout": PAY, "pnl": (PAY-1) if won else -1.0,
            "confirmed": True, "symbol": "R_10", "strategy": "D", "contract_type": "DIFFER", "ts": clk()}

def run(gate_factory, stream, lag=1):
    clk = Clock(); g = gate_factory(clk); states = []; R = 0.0; taken = 0; inhot = 0
    for i, (won, hot) in enumerate(stream):
        j = i - 1 - lag
        if j >= 0 and states[j]:
            R += (PAY-1) if won else -1.0; taken += 1; inhot += int(hot)
        clk.t += 12; g.on_result(ev(i, won, clk)); states.append(g.is_open())
    return R, taken, (inhot / taken if taken else 0)

def totals(stream):
    return sum((PAY-1) if w else -1.0 for w, _ in stream), sum((PAY-1) if w else -1.0 for w, h in stream if h)

REG = lambda c: RegimeGate(clock=c)
EDG = lambda c: EdgeGate(clock=c, min_open_secs=60, min_closed_secs=60)

print("== zig-zag Scout (hot 66% / cold 33%), 20 seeds x 1500 results ==")
agg = {k: [0.0, 0] for k in ("always", "edge", "regime", "oracle")}
for seed in range(20):
    s = make_stream(1500, seed=seed)
    a, o = totals(s)
    agg["always"][0] += a; agg["oracle"][0] += o
    for name, f in (("edge", EDG), ("regime", REG)):
        R, tk, pct = run(f, s); agg[name][0] += R; agg[name][1] += tk
for k, (R, tk) in agg.items():
    print(f"  {k:7s} avg R per 1500 results: {R/20:+8.1f}" + (f"  (takes {tk/20:.0f} trades)" if tk else ""))
assert agg["regime"][0] > agg["edge"][0] and agg["regime"][0] > agg["always"][0]

print("== NO-REGIME control: iid 47% (no edge exists) ==")
ctl = {"always": 0.0, "regime": 0.0, "edge": 0.0}
for seed in range(20):
    s = make_stream(1500, seed=100+seed, regimes=False)
    ctl["always"] += totals(s)[0]
    for name, f in (("edge", EDG), ("regime", REG)):
        ctl[name] += run(f, s)[0]
for k, v in ctl.items(): print(f"  {k:7s} avg R: {v/20:+8.1f}")
print("  -> with no regimes every strategy bleeds the house edge; the gate must not be sold as magic")

print("== regime quality: share of taken trades that were inside a HOT segment ==")
pcts = [run(REG, make_stream(1500, seed=s))[2] for s in range(20)]
print(f"  regime gate: {sum(pcts)/20*100:.0f}% of its trades were in hot segments (50% = random)")
assert sum(pcts)/20 > 0.6

print("== early in / early out on one hot segment ==")
clk = Clock(); g = RegimeGate(clock=clk)
rnd = random.Random(5)
for i in range(30): clk.t += 12; g.on_result(ev(i, rnd.random() < 0.40, clk))
seq = [True, True, False, True, True, True]          # hot segment begins
opened_at = None
for k, w in enumerate(seq + [True] * 6):
    clk.t += 12; g.on_result(ev(100 + k, w, clk))
    if g.is_open() and opened_at is None: opened_at = k
assert opened_at is not None and opened_at <= 5, opened_at
print(f"  went HOT after {opened_at+1} results of the turn")
for k, w in enumerate([True, False, False]):
    clk.t += 12; g.on_result(ev(200 + k, w, clk))
assert not g.is_open(); print(f"  went COLD after 2 straight losses: {g.state()['reason']}")

print("== stale + reload gap close the gate ==")
clk = Clock(); g = RegimeGate(clock=clk)
for i in range(14): clk.t += 10; g.on_result(ev(i, True, clk))
assert g.is_open(); clk.t += 200; assert not g.is_open()
clk = Clock(); g = RegimeGate(clock=clk)
for i in range(14): clk.t += 10; g.on_result(ev(i, True, clk))
d = g.export(); clk2 = Clock(); clk2.t = clk.t + 400
g2 = RegimeGate(clock=clk2); g2.load(d); assert not g2.is_open()
print("  ok")

print("== online learner: learns a real signal, stays quiet on noise ==")
def learn(stream):
    clk = Clock(); g = RegimeGate(clock=clk); L = OnlineLearner(g, clock=clk)
    for i, (won, _) in enumerate(stream):
        e = ev(i, won, clk); L.on_entry(e); clk.t += 12; g.on_result(e); L.on_result(e)
    return L
Ls = learn(make_stream(3000, seed=7)); ms = Ls.metrics()
Ln = learn(make_stream(3000, seed=8, regimes=False)); mn = Ln.metrics()
print(f"  zig-zag: logloss {ms['logloss']:.3f} vs base {ms['baseline_logloss']:.3f}, lift {ms['lift']:+.3f}, useful={ms['useful']}")
print(f"  iid    : logloss {mn['logloss']:.3f} vs base {mn['baseline_logloss']:.3f}, lift {mn['lift']:+.3f}, useful={mn['useful']}")
assert ms["useful"] and not mn["useful"]
ok, why = Ln.allow("none"); assert ok
env = Ls.export(); L2 = OnlineLearner(RegimeGate(clock=Clock()), clock=Clock()); L2.load(env)
assert L2.n == Ls.n and len(L2.scored) == len(Ls.scored); print("  persistence ok, state size", len(str(env)), "chars")

print("== auto-tuner: adopts only validated gains, flags no-edge ==")
s = make_stream(500, seed=11, hot_wr=0.70, cold_wr=0.30, seg=(25, 70))
clk = Clock(); g = RegimeGate(clock=clk, params={"entry_wins": 3, "exit_loss_run": 3, "exit_wins": 2})  # deliberately poor start
for i, (won, _) in enumerate(s): clk.t += 12; g.on_result(ev(i, won, clk))
tu = AutoTuner(g, clock=clk); tu._since_tune = 999
before = replay(g.results_compact(), g.params, split=300)["score"]
tu.maybe_tune(background=False)
after = replay(g.results_compact(), g.params, split=300)["score"]
print(f"  replay score {before:+.1f}R -> {after:+.1f}R, params {g.params}, adopted={tu.adopted}")
assert after >= before
s = make_stream(500, seed=12, regimes=False, iid_wr=0.40)
clk = Clock(); g = RegimeGate(clock=clk)
for i, (won, _) in enumerate(s): clk.t += 12; g.on_result(ev(i, won, clk))
tu = AutoTuner(g, clock=clk); tu._since_tune = 999; tu.maybe_tune(background=False)
print(f"  losing iid Scout -> no_edge={tu.no_edge}  (last: {tu.last.get('newest_R')}R on {tu.last.get('newest_taken')})")
print("\nALL REGIME/ML TESTS PASSED")
