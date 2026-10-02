"""Offline checks for balance_tiers / profit_pause / tiered donkey guard."""
import os, sys, time
os.environ.pop("PROFIT_PAUSE_START_BALANCE", None)
import types
for _m in ("aiohttp", "requests"):   # offline sandbox: stub if not installed
    try:
        __import__(_m)
    except ImportError:
        sys.modules[_m] = types.ModuleType(_m)
import balance_tiers as bt

# --- user's stake bands, exact boundaries ---
cases = [(0,.35),(8,.35),(8.01,.5),(15,.5),(15.01,.7),(25,.7),(25.01,1),(36,1),
         (36.01,2),(50,2),(50.01,3),(64,3),(64.01,4),(80,4),(80.01,5),(94,5),
         (94.01,6),(106,6),(106.01,7),(120,7),(120.01,8),(132,8),(132.01,9),
         (142,9),(142.01,10),(160,10),(160.01,12),(36500,1500),(36500.01,2000),
         (50000,2000),(75000,2000)]
for bal, st in cases:
    assert bt.stake_for_balance(bal) == st, (bal, bt.stake_for_balance(bal), st)
# --- user's profit targets ---
for st, tgt in [(.35,3),(.5,4.5),(.7,6),(1,10),(2,15),(4,22),(5,30),(6,36),
                (7,44),(8,56),(9,60),(10,70)]:
    assert bt.tier_for_stake(st).profit_target == tgt, st
# --- ladder sanity: contiguous, monotone, max stake 2000, top 50k ---
T = bt.TIERS
assert T[-1].stake == 2000 and T[-1].bal_high == 50000
for a, b in zip(T, T[1:]):
    assert abs(b.bal_low - (a.bal_high + .01)) < 1e-6 and b.stake > a.stake
    assert b.profit_target > a.profit_target
assert max(t.stake for t in T) == 2000
print("tiers ok:", len(T), "rows")

# --- profit pause flow ---
import fixed_cycle, profit_pause as pp, config
config.FIXED_CYCLE_ENABLED = True   # this test exercises the (now optional) cooldown path
fixed_cycle._persist_env_vars = lambda m: os.environ.update({k: str(v) for k, v in m.items()})
assert pp.on_boot(10.00) == 10.0           # tier $0.5 -> target 4.5
assert not pp.check(14.40)                 # +4.40 < 4.5
assert not fixed_cycle.is_cooldown_requested()
assert pp.check(14.50)                     # +4.50 -> fires
assert fixed_cycle.is_cooldown_requested()
assert not pp.check(20.0)                  # fires once only
assert 11 <= fixed_cycle._pending_cooldown_mins <= 18
pend = fixed_cycle._pending_cooldown_mins
until = None
fixed_cycle.start_cooldown_supervisor = lambda u: None
until = fixed_cycle.enter_cooldown_now()
assert abs((until - time.time())/60 - pend) < 0.1, until
assert os.environ["PROFIT_PAUSE_START_BALANCE"] == "0"
assert not fixed_cycle.is_cooldown_requested()
# next deploy starts fresh session at resumed balance
assert pp.on_boot(14.5) == 14.5
# basis=start: climbing into higher band does not move the goalpost
config.PROFIT_PAUSE_TARGET_BASIS = "start"
pp._fired = False; pp._start_balance = 7.0   # tier $0.35 -> target 3
assert pp.check(10.0)                        # now in $0.5 band, still pauses at +3
# basis=current: target follows live tier
config.PROFIT_PAUSE_TARGET_BASIS = "current"
pp._fired = False; pp._start_balance = 7.0
assert not pp.check(10.0)                    # $0.5 tier needs +4.5
assert pp.check(11.6)
# leg-end cooldown pending, then profit pause upgrades it to 45
fixed_cycle._cooldown_requested = False; fixed_cycle._pending_cooldown_mins = None
fixed_cycle.request_cooldown("leg 1 complete")
fixed_cycle.request_cooldown("profit", fixed_mins=45)
assert fixed_cycle._pending_cooldown_mins == 45
u = fixed_cycle.enter_cooldown_now(); assert abs((u-time.time())/60-45) < 0.1
# plain leg-end cooldown still random 1-3 min
fixed_cycle.request_cooldown("leg 1 complete")
u = fixed_cycle.enter_cooldown_now()
assert 0.9 <= (u - time.time())/60 <= 3.1
# random length stays inside 11-18 and actually varies
seen = []
for _ in range(200):
    pp._fired = False; pp._start_balance = 10.0
    fixed_cycle._cooldown_requested = False; fixed_cycle._pending_cooldown_mins = None
    assert pp.check(20.0)
    seen.append(fixed_cycle._pending_cooldown_mins)
assert min(seen) >= 11 and max(seen) <= 18 and max(seen) - min(seen) > 5, (min(seen), max(seen))
fixed_cycle._cooldown_requested = False; fixed_cycle._pending_cooldown_mins = None
print("profit pause ok")

# --- tiered donkey guard ---
import donkey_guard as dg
g = dg.DonkeyGuard(edge_log_path="/tmp/edge.csv")
def lose(n, stake):
    for i in range(n):
        g.record(symbol="X", signal_kind="freq", contract="DIGITUNDER", barrier=3,
                 stake=stake, payout=stake*2.4, won=False, pnl=-stake, now=1000+i)
# stake 2 tier: loss streak limit 6, stop-loss 7 stakes -> streak pause on 6th loss
lose(5, 2.0); assert g.can_enter(1010)[0]
lose(1, 2.0); ok, why = g.can_enter(1010); assert not ok and "loss-streak" in why, why
# stake 10 tier: stop-loss is 5 stakes -> halts on 5th straight loss (before streak 6)
g = dg.DonkeyGuard(edge_log_path="/tmp/edge.csv")
lose(4, 10.0); assert g.can_enter(1010)[0]
lose(1, 10.0); ok, why = g.can_enter(1010); assert not ok and "stop-loss" in why, why
# stake 10 tier: stop-loss 5 stakes -> halt. use wins in between to avoid streak pause
g = dg.DonkeyGuard(edge_log_path="/tmp/edge.csv")
for i in range(5):
    g.record(symbol="X", signal_kind="freq", contract="D", barrier=3, stake=10,
             payout=24, won=False, pnl=-10, now=2000+i)
    if i < 4:
        g.record(symbol="X", signal_kind="freq", contract="D", barrier=3, stake=10,
                 payout=24, won=True, pnl=0, now=2000+i+.5)
ok, why = g.can_enter(2010); assert not ok and "stop-loss" in why, why
# take-profit inactive while PROFIT_PAUSE_ENABLED
g = dg.DonkeyGuard(edge_log_path="/tmp/edge.csv")
g.record(symbol="X", signal_kind="freq", contract="D", barrier=3, stake=1,
         payout=2.4, won=True, pnl=15, now=3000)
assert g.can_enter(3001)[0]
g.reset_session(); assert g._session_pnl == 0
# guard pause lengths are random within the configured ranges
for _ in range(100):
    g = dg.DonkeyGuard(edge_log_path="/tmp/edge.csv")
    for i in range(6):
        g.record(symbol="X", signal_kind="freq", contract="D", barrier=3, stake=2,
                 payout=4.8, won=False, pnl=-2, now=5000+i)
    m = (g._pause_until - 5005) / 60
    assert 11 - 0.1 <= m <= 18 + 0.1, m
    g = dg.DonkeyGuard(edge_log_path="/tmp/edge.csv")
    for i in range(5):
        g.record(symbol="X", signal_kind="freq", contract="D", barrier=3, stake=10,
                 payout=24, won=False, pnl=-10, now=7000+i)
        g.record(symbol="X", signal_kind="freq", contract="D", barrier=3, stake=10,
                 payout=24, won=True, pnl=0, now=7000+i+.5) if i < 4 else None
    h = (g._halt_until - 7004) / 60
    assert 30 - 0.1 <= h <= 45 + 0.1, h
print("guard ok")
# --- confirmed-loss-only behaviour ---
import re as _re, textwrap
_src = open("deriv_client.py").read()
_m = _re.search(r"def is_confirmed_close.*?\n    return str\(poc.get\(\"status\", \"\"\)\)\.lower\(\) in \(\"won\", \"lost\", \"sold\"\)\n", _src, _re.S)
_ns = {}; exec(_m.group(0), _ns); icc = _ns["is_confirmed_close"]
assert not icc({"is_expired": 1, "status": "open", "profit": -1.0})   # stake deducted, unsettled
assert not icc({"status": "open", "profit": -1.0})
assert not icc({}) and not icc(None)
assert icc({"is_sold": 1, "profit": 1.4}) and icc({"status": "won"}) and icc({"status": "lost"})

g = dg.DonkeyGuard(edge_log_path="/tmp/edge.csv")
# 3 unsettled "losses" at a $10 stake (profit == -stake) must NOT trip anything
for i in range(8):
    g.record(symbol="X", signal_kind="freq", contract="D", barrier=3, stake=10, payout=0,
             won=False, pnl=-10, now=9000+i, contract_id=f"u{i}", confirmed=False)
assert g.can_enter(9010)[0] and g._session_pnl == 0 and g._consec_losses == 0
# 3 confirmed wins keep it open
for i in range(3):
    g.record(symbol="X", signal_kind="freq", contract="D", barrier=3, stake=10, payout=24,
             won=True, pnl=14, now=9100+i, contract_id=f"w{i}", confirmed=True)
assert g.can_enter(9110)[0] and g._session_pnl == 42
# same contract delivered twice is counted once
g.record(symbol="X", signal_kind="freq", contract="D", barrier=3, stake=10, payout=0,
         won=False, pnl=-10, now=9200, contract_id="dup", confirmed=True)
g.record(symbol="X", signal_kind="freq", contract="D", barrier=3, stake=10, payout=0,
         won=False, pnl=-10, now=9201, contract_id="dup", confirmed=True)
assert g._consec_losses == 1
# break-even / cancelled (pnl == 0) is not a loss
g.record(symbol="X", signal_kind="freq", contract="D", barrier=3, stake=10, payout=10,
         won=False, pnl=0, now=9202, contract_id="flat", confirmed=True)
assert g._consec_losses == 1
print("confirmed-only ok")
print("ALL PASS")
