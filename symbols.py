"""
symbols.py – Every Deriv tradeable market organised by asset class.
Synthetic indices are always available 24/7; others follow market hours.
"""

FOREX = [
    "frxEURUSD", "frxGBPUSD", "frxUSDJPY", "frxAUDUSD", "frxUSDCAD",
    "frxUSDCHF", "frxNZDUSD", "frxEURGBP", "frxEURJPY", "frxGBPJPY",
    "frxAUDJPY", "frxCADJPY", "frxCHFJPY", "frxEURCAD", "frxEURCHF",
    "frxGBPCAD", "frxGBPCHF", "frxAUDCAD", "frxAUDCHF", "frxNZDJPY",
    "frxEURAUD", "frxGBPAUD", "frxEURNZD", "frxGBPNZD",
]

METALS = [
    "frxXAUUSD",   # Gold
    "frxXAGUSD",   # Silver
    "frxXPDUSD",   # Palladium
    "frxXPTUSD",   # Platinum
]

CRYPTO = [
    "cryBTCUSD",    # Bitcoin
    "cryETHUSD",    # Ethereum
    "cryLTCUSD",    # Litecoin
    "cryBCHUSD",    # Bitcoin Cash
    "cryXRPUSD",    # Ripple / XRP
    "cryDOGEUSD",   # Dogecoin
    "cryADAUSD",    # Cardano
    "crySOLUSD",    # Solana
    "cryDOTUSD",    # Polkadot
    "cryMATICUSD",  # Polygon
    "cryBNBUSD",    # BNB
    "cryAVAXUSD",   # Avalanche
]

INDICES = [
    "OTC_DJI",      # Dow Jones 30
    "OTC_SPC",      # S&P 500
    "OTC_NASDAQ",   # NASDAQ Composite
    "OTC_N225",     # Nikkei 225
    "OTC_FTSE",     # FTSE 100
    "OTC_DAX",      # DAX 40
    "OTC_STOXX50E", # Euro Stoxx 50
    "OTC_AUS200",   # ASX 200
    "OTC_HSI",      # Hang Seng
    "OTC_AS51",     # ASX 200 (alt)
]

COMMODITIES = [
    "frxUSOIL",    # WTI Crude Oil
    "frxUKOIL",    # Brent Crude Oil
    "frxNGAS",     # Natural Gas
    "frxXCUUSD",   # Copper
    "frxXALUSD",   # Aluminium
    "frxXNIUSD",   # Nickel
]

# Synthetic indices – available 24 / 7 (great for out-of-hours trading)
SYNTHETIC = [
    "R_10",   "R_25",   "R_50",   "R_75",   "R_100",   # Volatility indices (2s tick)
    "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V", # Volatility (1 Hz)
    "1HZ150V","1HZ200V","1HZ250V",                        # Volatility (1 Hz), higher tiers
    "stpRNG",                                             # Step index
    "BOOM300N", "BOOM500",  "BOOM1000",                   # Boom indices
    "CRASH300N","CRASH500", "CRASH1000",                  # Crash indices
    "JD10",   "JD25",   "JD50",   "JD75",   "JD100",    # Jump indices
    "RDBEAR", "RDBULL",                                   # Bear/Bull ("Daily Reset") indices
                                                           # NOTE: these were mislabeled as
                                                           # "Range-break indices" previously —
                                                           # they are not. True Range Break
                                                           # symbol codes are not yet confirmed
                                                           # for this account and are not listed
                                                           # here; see config.py's RANGE_BREAK note.
]

# ── ICT / SMC TRADING UNIVERSE (Sep 2026 pivot) ──────────────
# Gold, major commodities, and major Forex pairs only — this is the ONLY
# universe the bot scans/trades now (see config.ALL_TRADE_SYMBOLS, which
# is pointed at ICT_TRADING_UNIVERSE below). Synthetic indices, crypto,
# stock indices, and the minor/cross Forex pairs above are kept in this
# file (nothing is deleted) but are no longer part of the active scan
# list — see config.py's "ICT TRADING UNIVERSE" section for the switch.

# The 7 USD majors — the most liquid, tightest-spread Forex pairs, and the
# pairs ICT/SMC methodology is most commonly taught and validated against.
MAJOR_FOREX = [
    "frxEURUSD", "frxGBPUSD", "frxUSDJPY", "frxUSDCHF",
    "frxAUDUSD", "frxUSDCAD", "frxNZDUSD",
]

# Gold, called out on its own per user request — the single most heavily
# ICT/SMC-traded instrument (deep liquidity, clean structure, tight London/
# NY killzone behaviour).
GOLD = ["frxXAUUSD"]

# Major commodities beyond gold: Silver (the other precious metal majors
# trade), and WTI/Brent crude (the two major oil benchmarks).
MAJOR_COMMODITIES = ["frxXAGUSD", "frxUSOIL", "frxUKOIL"]

# The bot's actual active trading universe.
ICT_TRADING_UNIVERSE = list(dict.fromkeys(GOLD + MAJOR_COMMODITIES + MAJOR_FOREX))

def get_ict_asset_class(sym: str) -> str:
    """Coarser classification used by ict_engine.py / config.py's per-class
    tuning (killzone weighting, liquidity tolerance, etc.) — distinct from
    get_symbol_class() below, which still returns 'forex'/'commodity' for
    everything, ICT universe or not."""
    if sym in GOLD:               return "gold"
    if sym in MAJOR_COMMODITIES:  return "commodity"
    if sym in MAJOR_FOREX:        return "forex_major"
    return get_symbol_class(sym)

# Ordered by priority (most liquid / best spreads first)
PRIORITY_ORDER = (
    SYNTHETIC[:5]          # synthetics always available
    + ["frxEURUSD", "frxGBPUSD", "frxUSDJPY"]
    + ["frxXAUUSD", "frxXAGUSD"]
    + ["cryBTCUSD", "cryETHUSD"]
    + ["frxUSOIL", "frxUKOIL"]
    + ["OTC_SPC", "OTC_DJI", "OTC_NASDAQ"]
    + FOREX[3:]
    + METALS[2:]
    + CRYPTO[2:]
    + INDICES[3:]
    + COMMODITIES[2:]
)

ALL_SYMBOLS = list(dict.fromkeys(
    PRIORITY_ORDER + FOREX + METALS + CRYPTO + INDICES + COMMODITIES + SYNTHETIC
))

ALWAYS_AVAILABLE = SYNTHETIC  # tradeable 24/7 regardless of market hours

def get_symbol_class(sym: str) -> str:
    if sym in FOREX:        return "forex"
    if sym in METALS:       return "metal"
    if sym in CRYPTO:       return "crypto"
    if sym in INDICES:      return "index"
    if sym in COMMODITIES:  return "commodity"
    if sym in SYNTHETIC:    return "synthetic"
    return "unknown"
