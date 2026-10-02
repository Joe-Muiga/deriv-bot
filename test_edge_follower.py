"""Offline tests for edge_gate / follower / scout_follower. No network.
Run: python test_edge_follower.py"""
import asyncio, json, os, random, sys, time, types
for _m in ("aiohttp", "requests", "websockets"):
    try:
        __import__(_m)
    except ImportError:
        sys.modules[_m] = types.ModuleType(_m)
for k in ("SF_GATE_STATE", "SF_FOLLOWER_STATE", "SF_FOLLOWER_GUARD", "SF_SCOUT_STATS"):
    os.environ.pop(k, None)
import config
config.SCOUT_FOLLOWER_ENABLED = True
import edge_gate as eg
from edge_gate import EdgeGate
import follower as fw
import scout_follower as sf
import balance_tiers as bt


class Clock:
    def __init__(self): self.t = 1_000_000.0
    def __call__(self): return self.t
    def adv(self, s): self.t += s


def res(won, cid, stake=100.0, confirmed=True, payout=212.0):
    return {"contract_id": str(cid), "stake": stake, "payout": payout,
            "pnl": (payout - stake) if won else -stake, "won": won,
            "confirmed": confirmed, "kind": "DIGIT", "symbol": "R_10",
            "contract_type": "DIFFER", "barrier": 3, "strategy": "DONKEY"}


# 1. break-even from payout
g = EdgeGate(clock=Clock())
assert abs(g.breakeven() - 100 / 212) < 1e-9
print("breakeven %.4f ok" % g.breakeven())

# 2. opens/closes with hysteresis on a synthetic stream
clk = Clock(); g = EdgeGate(clock=clk, min_open_secs=60, min_closed_secs=60)
clk.adv(100)
for i in range(12):                       # hot streak
    g.on_result(res(True, i)); clk.adv(10)
assert g.is_open(), g.state()
cyc = g.cycles
# a couple of losses must NOT flicker it shut while P still high
g.on_result(res(False, 100)); clk.adv(10)
assert g.is_open()
for i in range(5):                        # 5 losses in a row -> safety close
    g.on_result(res(False, 200 + i)); clk.adv(10)
assert not g.is_open() and g.state()["loss_run"] >= 5
# wins right away must not reopen before min_closed
g.on_result(res(True, 300)); assert not g.is_open()
print("hysteresis ok")

# 3. min_open respected for the SOFT close (P falls), not for the loss-run close
clk = Clock(); g = EdgeGate(clock=clk, min_open_secs=600, min_closed_secs=0, window=20)
clk.adv(10)
for i in range(10): g.on_result(res(True, i))
assert g.is_open()
for i in range(40):                       # alternating W/L: P decays, never 5 losses in a row
    g.on_result(res(i % 2 == 0, 50 + i))
assert g.state()["prob"] < 0.60 and g.is_open(), g.state()     # held by min_open
clk.adv(700); g.on_result(res(False, 999))
assert not g.is_open(), g.state()                              # now allowed to close
print("min_open ok")

# 4. unconfirmed / duplicate / flat never counted
g = EdgeGate(clock=Clock())
g.on_result(res(True, 1, confirmed=False)); assert g.state()["n"] == 0
g.on_result(res(True, 2)); g.on_result(res(True, 2)); assert g.state()["n"] == 1
f = res(True, 3); f["pnl"] = 0; g.on_result(f); assert g.state()["n"] == 1
g.on_result({**res(True, 4), "gate_eligible": False}); assert g.state()["n"] == 1
assert g.ignored_unconfirmed == 1
print("unconfirmed/dup/flat ok")

# 5. false-open rate on random 47% streams, many seeds (reported, not hidden)
def false_open_rate(p, seeds=400, trades=300, **kw):
    ever = 0; cyc = 0
    for sd in range(seeds):
        rnd = random.Random(sd); clk = Clock(); g = EdgeGate(clock=clk, **kw)
        opened = False
        for i in range(trades):
            clk.adv(18)
            g.on_result(res(rnd.random() < p, i))
            if g.is_open(): opened = True
        ever += opened; cyc += g.cycles
    return ever / seeds, cyc / seeds
fo, cy = false_open_rate(0.47)
fo_strict, cy_s = false_open_rate(0.47, min_trades=40, window=100)
print(f"FALSE-OPEN (47% stream, 300 trades, 400 seeds): "
      f"defaults (min10/win50/P0.97) -> {fo*100:.0f}% of runs ever opened, {cy:.1f} opens/run | "
      f"min40/win100/P0.97 -> {fo_strict*100:.0f}%, {cy_s:.1f} opens/run")
good, _ = false_open_rate(0.55, seeds=100)
print(f"OPEN rate on a real 55% stream: {good*100:.0f}% of runs")
assert good > 0.9

# 6. follower: tier stakes at several balances
for bal, st in [(10000, 500.0), (12000, 600.0), (500, 30.0), (30, 1.0)]:
    gg = EdgeGate(clock=Clock()); ff = fw.Follower(gg, mode="shadow", start_balance=bal)
    assert ff.stake() == st == bt.stake_for_balance(bal), (bal, ff.stake())
print("follower tier stakes ok")

def mk(mode="shadow", factory=None, bal=10000.0):
    clk = Clock(); gg = EdgeGate(clock=clk, min_open_secs=0, min_closed_secs=0)
    ff = fw.Follower(gg, mode=mode, start_balance=bal, clock=clk)
    return clk, gg, ff

def entry(cid, clk, kind="DIGIT"):
    return {"contract_id": str(cid), "symbol": "R_10", "kind": kind, "strategy": "DONKEY",
            "contract_type": "DIFFER", "barrier": 3, "direction": "SHORT", "ts": clk()}

def feed_open(gg, clk, n=12):
    for i in range(n): gg.on_result(res(True, f"w{i}")); clk.adv(1)
    assert gg.is_open()

# 7. shadow places nothing; mirrored result settles virtual balance + baseline
class FakeClient:
    def __init__(self): self.buys = []; self.subs = {}; self.balance = 10000.0
    async def buy_digit_contract(self, **kw): self.buys.append(kw); return {"contract_id": f"F{len(self.buys)}"}
    async def buy_contract(self, **kw): self.buys.append(kw); return {"contract_id": f"F{len(self.buys)}"}
    async def subscribe_contract(self, cid, cb, symbol=""): self.subs[cid] = cb
    def stop_tracking(self, cid): pass
    async def force_check_contract(self, cid): return {}

clk, gg, ff = mk("shadow"); ff.client = FakeClient(); feed_open(gg, clk)
asyncio.run(ff.on_entry(entry("S1", clk)))
assert ff.client.buys == [] and ff.c["taken"] == 1 and ff.open_count() == 0
ff.on_scout_result({**res(True, "S1", stake=500, payout=1060), "ts": clk()})
assert abs(ff.balance - (10000 + 560)) < 1e-6, ff.balance
assert abs(ff.baseline_balance - 10560) < 1e-6
print("shadow ok (nothing placed, balance %.2f)" % ff.balance)

# 8. gate closed -> no trade, but baseline still accounted
clk, gg, ff = mk("shadow")
asyncio.run(ff.on_entry(entry("B1", clk)))
assert ff.c["taken"] == 0 and ff.c["skipped_gate"] == 1
ff.on_scout_result({**res(False, "B1", stake=500, payout=1060), "ts": clk()})
assert ff.balance == 10000 and abs(ff.baseline_balance - 9500) < 1e-6
assert ff.c["gate_off_n"] == 1 and ff.c["gate_on_n"] == 0
print("baseline vs gated accounting ok")

# 9. demo: places on its own client, settles only on CONFIRMED close
clk, gg, ff = mk("demo"); ff.client = FakeClient(); feed_open(gg, clk)
asyncio.run(ff.on_entry(entry("D1", clk)))
assert len(ff.client.buys) == 1 and ff.client.buys[0]["stake"] == 500.0
assert ff.client.buys[0]["digit"] == 3 and ff.client.buys[0]["match_type"] == "DIFFER"
cb = ff.client.subs["F1"]
async def _drive():
    # unconfirmed: expired but status open, profit == -stake -> must NOT count
    cb({"proposal_open_contract": {"is_expired": 1, "status": "open", "profit": -500}})
    await asyncio.sleep(0)
    assert ff.balance == 10000 and ff.open_count() == 1 and ff.c["losses"] == 0
    cb({"proposal_open_contract": {"is_sold": 1, "status": "won", "profit": 560, "payout": 1060}})
    await asyncio.sleep(0.01)
asyncio.run(_drive())
assert abs(ff.balance - 10560) < 1e-6 and ff.c["wins"] == 1 and ff.open_count() == 0
print("demo placement + confirmed-only settle ok")

# 10. exposure ceiling / open cap
clk, gg, ff = mk("demo"); ff.client = FakeClient(); feed_open(gg, clk)
async def _many():
    for i in range(5): await ff.on_entry(entry(f"E{i}", clk))
asyncio.run(_many())
assert len(ff.client.buys) == 2, len(ff.client.buys)    # 10% of 10k / $500 stake
print("exposure ceiling ok (2 of 5 placed)")

# 11. stale entry skipped; unsupported kind skipped
clk, gg, ff = mk("demo"); ff.client = FakeClient(); feed_open(gg, clk)
e = entry("X1", clk); e["ts"] = clk() - 60
asyncio.run(ff.on_entry(e)); asyncio.run(ff.on_entry(entry("X2", clk, kind="MULT")))
assert ff.client.buys == [] and ff.c["skipped_stale"] == 1 and ff.c["skipped_unsupported"] == 1
print("stale/unsupported ok")

# 12. live refuses without confirmation
config.FOLLOWER_LIVE_CONFIRM = ""
try:
    fw.Follower(EdgeGate(clock=Clock()), mode="live"); raise SystemExit("live should refuse")
except ValueError:
    pass
config.FOLLOWER_LIVE_CONFIRM = fw.LIVE_CONFIRM_PHRASE
fw.Follower(EdgeGate(clock=Clock()), mode="live")      # allowed only with phrase
config.FOLLOWER_LIVE_CONFIRM = ""
print("live refusal ok")

# 13. restart restores state (gate + follower + guard + stats via env)
config.FOLLOWER_MODE = "shadow"
clk = Clock()
h = sf.Hub(clock=clk, events_path="/tmp/sf_test_events.jsonl")
for i in range(12): sf.bus.publish("result", {**res(True, f"r{i}"), "ts": clk()}); clk.adv(20)
sf.bus.publish("entry", entry("Z1", clk)); asyncio.run(asyncio.sleep(0)) if False else None
h.follower.balance = 10321.5; h.follower.c["taken"] = 7
env = h.export_env()
assert set(env) == {"SF_GATE_STATE", "SF_SCOUT_STATS", "SF_FOLLOWER_STATE"}
assert all(len(v) < 8000 for v in env.values()), {k: len(v) for k, v in env.items()}
for k, v in env.items(): os.environ[k] = v
h2 = sf.Hub(clock=clk, events_path="/tmp/sf_test_events.jsonl")
assert h2.gate.state()["n"] == 12 and h2.gate.is_open()
assert h2.follower.balance == 10321.5 and h2.follower.c["taken"] == 7
assert h2.scout_total["n"] == 12
print("restart restore ok (env sizes %s)" % {k: len(v) for k, v in env.items()})
for k in env: os.environ.pop(k, None)

# 14. hub never counts unconfirmed Scout results
h3 = sf.Hub(clock=Clock(), events_path="/tmp/sf_test_events.jsonl")
sf.bus.publish("result", {**res(False, "u1"), "confirmed": False})
assert h3.gate.state()["n"] == 0 and h3.scout_total["n"] == 0
print("hub unconfirmed ignored ok")

# 15. profit pause + guard on the follower (shadow, stream of wins)
clk, gg, ff = mk("shadow"); feed_open(gg, clk)
paused = False
for i in range(40):
    asyncio.run(ff.on_entry(entry(f"P{i}", clk)))
    ff.on_scout_result({**res(True, f"P{i}", stake=500, payout=1060), "ts": clk()})
    if ff.pause.paused(clk()): paused = True; break
    clk.adv(1)
assert paused and 11*60 <= ff.pause.seconds_left(clk()) <= 18*60 + 1
asyncio.run(ff.on_entry(entry("PX", clk)))
assert ff.c["skipped_pause"] >= 1
print("profit pause ok (balance %.0f)" % ff.balance)

# 16. report renders
print("\n".join(h.report_lines()))
print("\nALL EDGE/FOLLOWER TESTS PASSED")
