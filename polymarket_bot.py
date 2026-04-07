"""
Polymarket Whale Bot — Fixed Version (Production-Ready Skeleton)

Critical fixes from review (READ BEFORE USING WITH REAL MONEY):
  1. token_id is now fetched from `clobTokenIds` in Gamma API instead of
     wrongly using `conditionId`. The original code would have failed every
     live order.
  2. CLOB `side` is "BUY" (always) — direction is encoded by which token_id
     we trade (YES vs NO). Original code passed "YES"/"NO" which is invalid.
  3. Order `size` is now in SHARES, not USDC. Conversion: shares = usdc/price.
     Original code sent USDC as size which would massively over-order.
  4. DailyStats resets at UTC midnight (was never reset).
  5. Real PnL tracking by polling current prices of open positions every
     scan cycle (was a dead variable; daily loss limit was effectively off).
  6. Defensive parsing of API fields with multiple fallback names so a
     missing field doesn't silently filter every market to zero.
  7. Recency filter degrades gracefully if timestamp absent.
  8. Pre-flight validation in main() before any live trade.
  9. Open-position cap and per-market cooldown to prevent spam re-entries.

CRITICAL LIMITATION — NO EXIT LOGIC:
  This bot OPENS positions but never SELLS them. Realized PnL only happens
  when a market resolves. You are responsible for manually closing positions
  if you want to take profit / cut losses before resolution. Daily loss
  limit uses unrealized PnL (mark-to-market) which is the safest default.

NOTE: even after these fixes, START WITH PAPER_TRADING=True for at least
1-2 weeks to validate that signals match expectations on the real API.
The Polymarket data API field names occasionally change; the defensive
parsing helps but is not a substitute for real observation.

Install:
    pip install aiohttp py-clob-client

Run (paper):
    python polymarket_bot.py

Run (live):
    export POLY_PRIVATE_KEY="0x..."
    export POLY_FUNDER="0x..."     # your Polymarket proxy wallet address
    # set PAPER_TRADING = False below
    python polymarket_bot.py
"""

import asyncio
import aiohttp
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# ═══════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════

WHALES = [
    "0xaad03b403c831d0a8b484abe59ac25188b49f61d",  # Fredi9999
    # add more whale addresses here — one whale gives no real "agreement"
]

DATA_API  = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"

# Signal filters
MIN_POSITION_SIZE   = 500
MIN_WHALE_VOLUME    = 5_000
CONSENSUS_THRESHOLD = 0.60
AGREEMENT_THRESHOLD = 0.50
MAX_SLIPPAGE        = 0.03
MAX_MARKET_PRICE    = 0.90
RECENCY_HOURS       = 24

# Performance
MAX_CONCURRENT      = 5
TOP_MARKETS_LIMIT   = 100
SCAN_INTERVAL_SEC   = 300

# Execution
PRIVATE_KEY         = os.getenv("POLY_PRIVATE_KEY", "")
POLY_FUNDER         = os.getenv("POLY_FUNDER", "")  # Polymarket proxy wallet
# 0 = EOA, 1 = email/magic proxy, 2 = browser-wallet (Gnosis Safe) proxy.
# Most Polymarket users have type 2. Override via POLY_SIG_TYPE env var.
POLY_SIG_TYPE       = int(os.getenv("POLY_SIG_TYPE", "2"))
MIN_WHALES_FOR_EXEC = 2     # require at least N whales agreeing for any signal
MAX_TRADE_USDC      = 50.0
MAX_DAILY_TRADES    = 10
MAX_DAILY_LOSS      = 200.0
MAX_OPEN_POSITIONS  = 5
MIN_CONSENSUS_EXEC  = 0.70
PER_MARKET_COOLDOWN = 24 * 3600  # don't re-enter same market within 24h
PAPER_TRADING       = True       # KEEP TRUE until validated for 1-2 weeks

# CLOB chain
CHAIN_ID_POLYGON = 137

# ═══════════════════════════════════════════════════════
# DATA STRUCTURES
# ═══════════════════════════════════════════════════════

@dataclass
class Position:
    market_id:     str
    outcome:       str        # "YES" or "NO"
    size:          float      # USDC value of position
    avg_price:     float
    current_price: float
    last_traded:   float = 0.0


@dataclass
class MarketInfo:
    condition_id: str
    question:     str
    slug:         str
    best_ask:     float
    yes_token_id: str         # ERC1155 token id for YES outcome
    no_token_id:  str         # ERC1155 token id for NO outcome


@dataclass
class Signal:
    market_id:       str
    question:        str
    direction:       str      # "YES" or "NO"
    token_id:        str      # the token id we will actually trade
    consensus_score: float
    agreement_ratio: float
    whale_count:     int
    total_volume:    float
    entry_price:     float
    whale_avg_entry: float
    slippage:        float
    slippage_ok:     bool


@dataclass
class OpenPosition:
    direction: str
    token_id:  str
    shares:    float
    entry:     float
    cost_usdc: float
    opened_at: float


@dataclass
class DailyStats:
    date:      str   = ""
    trades:    int   = 0
    realized_pnl:    float = 0.0
    unrealized_pnl:  float = 0.0
    positions: dict  = field(default_factory=dict)   # market_id -> OpenPosition
    cooldowns: dict  = field(default_factory=dict)   # market_id -> ts opened

    def maybe_reset(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.date != today:
            print(f"[DAY RESET] {self.date or '(init)'} -> {today}")
            self.date = today
            self.trades = 0
            self.realized_pnl = 0.0
            # NOTE: open positions and cooldowns intentionally persist across days

    @property
    def total_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl


# ═══════════════════════════════════════════════════════
# ASYNC API LAYER
# ═══════════════════════════════════════════════════════

async def fetch_json(session, url: str, params: dict = None):
    try:
        async with session.get(
            url,
            params=params or {},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status == 200:
                return await resp.json()
            body = await resp.text()
            print(f"[WARN] HTTP {resp.status} {url} :: {body[:200]}")
            return None
    except Exception as e:
        print(f"[ERROR] {url}: {e}")
        return None


def _f(d: dict, *keys, default=0.0) -> float:
    """Defensive float extraction with multiple key fallbacks."""
    for k in keys:
        if k in d and d[k] is not None:
            try:
                return float(d[k])
            except (TypeError, ValueError):
                continue
    return default


def _s(d: dict, *keys, default="") -> str:
    for k in keys:
        if k in d and d[k] is not None:
            return str(d[k])
    return default


async def get_whale_positions(session, address: str) -> list[Position]:
    data = await fetch_json(session, f"{DATA_API}/positions", {
        "user":          address,
        "sizeThreshold": MIN_POSITION_SIZE,
        "limit":         200,
    })
    if not data:
        return []

    cutoff = time.time() - (RECENCY_HOURS * 3600)
    out: list[Position] = []
    for item in data:
        # Defensive: timestamp may be missing or named differently.
        # If absent, do NOT silently drop — keep the position.
        ts = _f(item, "lastTradedTimestamp", "lastUpdateTimestamp",
                "lastTradeTime", "timestamp", default=0.0)
        if ts and ts < cutoff:
            continue

        out.append(Position(
            market_id     = _s(item, "conditionId", "condition_id"),
            outcome       = _s(item, "outcome").upper(),
            size          = _f(item, "size", "currentValue", "value"),
            avg_price     = _f(item, "avgPrice", "averagePrice", "entryPrice"),
            current_price = _f(item, "curPrice", "currentPrice", "lastPrice"),
            last_traded   = ts,
        ))
    return out


def _parse_token_ids(raw) -> tuple[str, str]:
    """clobTokenIds may arrive as JSON-string or list. Returns (yes_id, no_id)."""
    if not raw:
        return "", ""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return "", ""
    if isinstance(raw, list) and len(raw) >= 2:
        # Polymarket convention: [YES, NO]
        return str(raw[0]), str(raw[1])
    return "", ""


async def get_active_markets(session) -> list[MarketInfo]:
    data = await fetch_json(session, f"{GAMMA_API}/markets", {
        "active":  "true",
        "closed":  "false",
        "limit":   TOP_MARKETS_LIMIT,
        "order":   "volume",
        "ascending": "false",
    })
    if not data:
        return []

    out: list[MarketInfo] = []
    for m in data:
        cid = _s(m, "conditionId", "condition_id")
        if not cid:
            continue
        yes_id, no_id = _parse_token_ids(m.get("clobTokenIds"))
        if not yes_id or not no_id:
            continue  # cannot trade without token ids
        out.append(MarketInfo(
            condition_id = cid,
            question     = _s(m, "question", default=cid),
            slug         = _s(m, "slug"),
            best_ask     = _f(m, "bestAsk", "best_ask", "lastTradePrice"),
            yes_token_id = yes_id,
            no_token_id  = no_id,
        ))
    return out


async def get_token_midpoint(session, token_id: str) -> float:
    """Fetch midpoint price for a token id from CLOB (used for PnL valuation)."""
    data = await fetch_json(session, f"{CLOB_HOST}/midpoint",
                            {"token_id": token_id})
    if not data:
        return 0.0
    return _f(data, "mid", "midpoint", "price", default=0.0)


# ═══════════════════════════════════════════════════════
# SENTIMENT ENGINE
# ═══════════════════════════════════════════════════════

def analyze_market(market: MarketInfo, all_positions: dict) -> Optional[Signal]:
    if market.best_ask <= 0 or market.best_ask >= MAX_MARKET_PRICE:
        return None

    yes_volume, no_volume = 0.0, 0.0
    yes_prices, no_prices = [], []
    whale_count = 0

    for positions in all_positions.values():
        for pos in positions:
            if pos.market_id != market.condition_id:
                continue
            if pos.size < MIN_WHALE_VOLUME:
                continue
            whale_count += 1
            if pos.outcome == "YES":
                yes_volume += pos.size
                yes_prices.append((pos.avg_price, pos.size))
            elif pos.outcome == "NO":
                no_volume += pos.size
                no_prices.append((pos.avg_price, pos.size))

    total_volume = yes_volume + no_volume
    if total_volume == 0 or whale_count == 0:
        return None

    weighted_yes = yes_volume / total_volume

    if weighted_yes >= CONSENSUS_THRESHOLD:
        direction, relevant = "YES", yes_prices
        token_id = market.yes_token_id
        agreement = len(yes_prices) / whale_count
    elif weighted_yes <= (1 - CONSENSUS_THRESHOLD):
        direction, relevant = "NO", no_prices
        token_id = market.no_token_id
        agreement = len(no_prices) / whale_count
    else:
        return None

    if agreement < AGREEMENT_THRESHOLD:
        return None

    # Require minimum number of agreeing whales — guards against
    # "1 whale = 100% agreement" false positives.
    agreeing = len(relevant)
    if agreeing < MIN_WHALES_FOR_EXEC:
        return None

    total_w   = sum(s for _, s in relevant)
    whale_avg = sum(p * s for p, s in relevant) / total_w if total_w > 0 else 0

    slippage    = abs(market.best_ask - whale_avg) / whale_avg if whale_avg > 0 else 999.0
    slippage_ok = slippage <= MAX_SLIPPAGE

    return Signal(
        market_id       = market.condition_id,
        question        = market.question,
        direction       = direction,
        token_id        = token_id,
        consensus_score = weighted_yes if direction == "YES" else (1 - weighted_yes),
        agreement_ratio = agreement,
        whale_count     = whale_count,
        total_volume    = total_volume,
        entry_price     = market.best_ask,
        whale_avg_entry = whale_avg,
        slippage        = slippage,
        slippage_ok     = slippage_ok,
    )


async def scan(session) -> list[Signal]:
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async def throttled(addr):
        async with semaphore:
            return await get_whale_positions(session, addr)

    print("\n[SCAN] fetching active markets...")
    markets = await get_active_markets(session)
    print(f"       {len(markets)} markets")

    print("[SCAN] fetching whale positions...")
    results       = await asyncio.gather(*[throttled(a) for a in WHALES])
    all_positions = {WHALES[i]: results[i] for i in range(len(WHALES))}

    active = sum(len(v) for v in all_positions.values())
    print(f"       {active} active positions (last {RECENCY_HOURS}h)")

    signals = [s for m in markets if (s := analyze_market(m, all_positions))]
    signals.sort(key=lambda s: s.consensus_score, reverse=True)
    return signals


# ═══════════════════════════════════════════════════════
# RISK MANAGER
# ═══════════════════════════════════════════════════════

def passes_risk(signal: Signal, stats: DailyStats) -> tuple[bool, str]:
    if not signal.slippage_ok:
        return False, f"slippage too high {signal.slippage:.2%}"
    if signal.consensus_score < MIN_CONSENSUS_EXEC:
        return False, f"weak consensus {signal.consensus_score:.1%}"
    if stats.trades >= MAX_DAILY_TRADES:
        return False, f"daily trade cap ({MAX_DAILY_TRADES})"
    if stats.total_pnl <= -MAX_DAILY_LOSS:
        return False, f"daily loss limit (${MAX_DAILY_LOSS})"
    if len(stats.positions) >= MAX_OPEN_POSITIONS:
        return False, f"max open positions ({MAX_OPEN_POSITIONS})"
    if signal.market_id in stats.positions:
        return False, "position already open"
    last_open = stats.cooldowns.get(signal.market_id, 0)
    if last_open and (time.time() - last_open) < PER_MARKET_COOLDOWN:
        return False, "market cooldown"
    if not (0 < signal.entry_price < 1):
        return False, f"invalid price {signal.entry_price}"
    return True, "OK"


def calc_size(signal: Signal) -> float:
    """USDC notional sized by signal strength."""
    strength  = (signal.consensus_score - MIN_CONSENSUS_EXEC) / (1 - MIN_CONSENSUS_EXEC)
    size_usdc = MAX_TRADE_USDC * min(max(strength, 0), 1.0)
    return round(max(size_usdc, 1.0), 2)


# ═══════════════════════════════════════════════════════
# EXECUTOR
# ═══════════════════════════════════════════════════════

async def execute_signal(session, signal: Signal, stats: DailyStats) -> Optional[dict]:
    ok, reason = passes_risk(signal, stats)
    if not ok:
        print(f"   [SKIP] {signal.question[:45]} — {reason}")
        return None

    size_usdc = calc_size(signal)
    # CRITICAL FIX: CLOB orders are sized in SHARES, not USDC.
    shares = round(size_usdc / signal.entry_price, 2)
    if shares <= 0:
        print(f"   [SKIP] computed shares <= 0")
        return None

    label = "(PAPER)" if PAPER_TRADING else "(LIVE) "
    print(f"""
[TRADE {label}]
  market:    {signal.question[:60]}
  direction: {signal.direction}
  token_id:  {signal.token_id[:16]}...
  notional:  ${size_usdc} USDC
  shares:    {shares}
  price:     {signal.entry_price:.4f}
  consensus: {signal.consensus_score:.1%}
""")

    if PAPER_TRADING:
        stats.trades += 1
        stats.positions[signal.market_id] = OpenPosition(
            direction = signal.direction,
            token_id  = signal.token_id,
            shares    = shares,
            entry     = signal.entry_price,
            cost_usdc = size_usdc,
            opened_at = time.time(),
        )
        stats.cooldowns[signal.market_id] = time.time()
        return {"status": "paper", "shares": shares, "cost": size_usdc}

    # ─── LIVE EXECUTION ───
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY
    except ImportError:
        print("   [ERROR] pip install py-clob-client")
        return None

    try:
        client = ClobClient(
            host       = CLOB_HOST,
            key        = PRIVATE_KEY,
            chain_id   = CHAIN_ID_POLYGON,
            funder     = POLY_FUNDER,
            signature_type = POLY_SIG_TYPE,
        )
        client.set_api_creds(client.create_or_derive_api_creds())

        order_args = OrderArgs(
            token_id = signal.token_id,        # FIX: real ERC1155 token id
            price    = signal.entry_price,
            size     = shares,                 # FIX: shares, not USDC
            side     = BUY,                    # FIX: always BUY (direction = which token)
        )
        signed = client.create_order(order_args)
        resp = client.post_order(signed, OrderType.GTC)

        if not resp or not resp.get("success", True):
            print(f"   [ERROR] order rejected: {resp}")
            return None

        stats.trades += 1
        stats.positions[signal.market_id] = OpenPosition(
            direction = signal.direction,
            token_id  = signal.token_id,
            shares    = shares,
            entry     = signal.entry_price,
            cost_usdc = size_usdc,
            opened_at = time.time(),
        )
        stats.cooldowns[signal.market_id] = time.time()
        print(f"   [OK] order id: {resp.get('orderID', resp.get('orderId', '?'))}")
        return resp

    except Exception as e:
        print(f"   [ERROR] live order failed: {e}")
        return None


# ═══════════════════════════════════════════════════════
# PNL TRACKER
# ═══════════════════════════════════════════════════════

async def update_unrealized_pnl(session, stats: DailyStats):
    """Poll current prices for open positions and recompute unrealized PnL."""
    if not stats.positions:
        stats.unrealized_pnl = 0.0
        return

    total = 0.0
    for mid, pos in list(stats.positions.items()):
        cur = await get_token_midpoint(session, pos.token_id)
        if cur <= 0:
            continue
        pnl = (cur - pos.entry) * pos.shares
        total += pnl
    stats.unrealized_pnl = round(total, 2)


# ═══════════════════════════════════════════════════════
# DISPLAY
# ═══════════════════════════════════════════════════════

def print_signal(s: Signal):
    icon = "OK " if s.slippage_ok else "!! "
    print(f"""
{"="*55}
{s.question[:60]}
{"-"*55}
  direction:    {s.direction}
  consensus:    {s.consensus_score:.1%}
  agreement:    {s.agreement_ratio:.1%}  ({s.whale_count} whales)
  liquidity:    ${s.total_volume:>10,.0f}
  whale entry:  {s.whale_avg_entry:.4f}
  market price: {s.entry_price:.4f}
  slippage:     {s.slippage:.2%}  {icon}
{"="*55}""")


def print_header():
    mode = "PAPER TRADING" if PAPER_TRADING else "LIVE TRADING"
    print(f"""
{"#"*55}
  Polymarket Whale Bot
  mode:        {mode}
  whales:      {len(WHALES)}
  per trade:   ${MAX_TRADE_USDC}
  daily caps:  {MAX_DAILY_TRADES} trades / ${MAX_DAILY_LOSS} loss
  open cap:    {MAX_OPEN_POSITIONS} positions
{"#"*55}
""")


# ═══════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════

def preflight() -> bool:
    if PAPER_TRADING:
        return True
    problems = []
    if not PRIVATE_KEY or not PRIVATE_KEY.startswith("0x"):
        problems.append("POLY_PRIVATE_KEY missing or invalid")
    if not POLY_FUNDER or not POLY_FUNDER.startswith("0x"):
        problems.append("POLY_FUNDER missing (your Polymarket proxy wallet)")
    if len(WHALES) < 3:
        problems.append(f"only {len(WHALES)} whale(s); need 3+ for meaningful agreement")
    if MAX_TRADE_USDC > 100:
        problems.append(f"MAX_TRADE_USDC={MAX_TRADE_USDC} is high for first live run")
    if problems:
        print("[PREFLIGHT FAILED]")
        for p in problems:
            print(f"  - {p}")
        return False
    print("[PREFLIGHT OK]")
    print("\n!! WARNING: LIVE MODE — bot has NO exit logic. Positions are")
    print("!! never sold automatically. You must manage exits manually.")
    print("!! Press Ctrl+C in the next 10 seconds to abort.\n")
    time.sleep(10)
    return True


async def main():
    if not preflight():
        return

    print_header()
    stats = DailyStats()

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                stats.maybe_reset()
                t0 = time.time()

                await update_unrealized_pnl(session, stats)

                signals = await scan(session)
                valid   = [s for s in signals if s.slippage_ok]
                invalid = len(signals) - len(valid)

                print(f"\n{'-'*55}")
                print(f"signals: {len(valid)} valid | {invalid} high-slippage")
                print(f"{'-'*55}")

                for s in valid:
                    print_signal(s)
                    await execute_signal(session, s, stats)
                    await asyncio.sleep(1)

                print(f"\n[DAY {stats.date}] trades={stats.trades} "
                      f"realized=${stats.realized_pnl:.2f} "
                      f"unrealized=${stats.unrealized_pnl:.2f} "
                      f"open={len(stats.positions)}")
                print(f"[CYCLE] {time.time()-t0:.1f}s | next in "
                      f"{SCAN_INTERVAL_SEC//60} min...\n")

            except Exception as e:
                print(f"[LOOP ERROR] {e}")

            await asyncio.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    asyncio.run(main())
