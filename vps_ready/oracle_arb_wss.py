"""
arb2 — Chainlink <-> Polymarket Oracle Arbitrage (WebSocket edition)

Monitors Chainlink BTC/USD and ETH/USD price feeds via Alchemy WebSocket.
For each configured Polymarket "price target" market, computes the fair
probability implied by the current Chainlink price and flags/executes
when Polymarket diverges by more than ARB_THRESHOLD.

This is a SKELETON. You MUST:
  1. Fill MARKETS with real Polymarket market slugs & strike targets you want
     to arbitrage (e.g. "Will BTC reach $X by Y?").
  2. Verify Chainlink feed addresses against https://docs.chain.link/data-feeds
  3. Paper-trade for days before flipping to live.

Run (paper):
    ALCHEMY_WSS=wss://... python3 oracle_arb_wss.py --paper

Run (live):
    ALCHEMY_WSS=wss://... POLY_PRIVATE_KEY=0x... POLY_FUNDER=0x... \\
        python3 oracle_arb_wss.py

Environment:
    ALCHEMY_WSS        wss://polygon-mainnet.g.alchemy.com/v2/KEY   (required)
    POLY_PRIVATE_KEY   live only
    POLY_FUNDER        live only
    POLY_SIG_TYPE      default 0 (EOA)
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID

Dependencies:
    pip install aiohttp websockets web3 py-clob-client
"""

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from telegram_notify import notify, notify_critical  # noqa: E402

# ═══════════════════════════════════════════════════════
# CONFIG — VERIFY THESE ADDRESSES AGAINST docs.chain.link
# ═══════════════════════════════════════════════════════

ALCHEMY_WSS = os.getenv("ALCHEMY_WSS", "")

# Polygon mainnet Chainlink aggregators (user-verified)
CHAINLINK_FEEDS = {
    "BTC": {
        "address":  "0xc907E116054Ad103354f2D350FD2514433D57F6f",
        "decimals": 8,
    },
    "ETH": {
        "address":  "0xF9680D99D6C9589e2a93a78A04A279e509205945",
        "decimals": 8,
    },
}

# latestRoundData() selector: 0xfeaf968c
LATEST_ROUND_SELECTOR = "0xfeaf968c"

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID_POLYGON = 137

# Per-bot wallet (with fallback to shared POLY_* for backwards compat)
PRIVATE_KEY = os.getenv("ARB2_PRIVATE_KEY") or os.getenv("POLY_PRIVATE_KEY", "")
FUNDER      = os.getenv("ARB2_FUNDER")      or os.getenv("POLY_FUNDER", "")
SIG_TYPE    = int(os.getenv("ARB2_SIG_TYPE") or os.getenv("POLY_SIG_TYPE", "0"))

# ═══════════════════════════════════════════════════════
# MARKETS — configure the price-target markets you want to arbitrage
# ═══════════════════════════════════════════════════════
# Each entry:
#   slug:    polymarket market slug
#   asset:   "BTC" or "ETH"
#   strike:  target price in USD
#   side:    "above" (YES if price > strike) or "below"
#   deadline: ISO date when the question resolves (for time decay)
#
# Leave empty on first run; fill after vetting specific markets.

MARKETS: list[dict] = [
    # {
    #     "slug":     "will-btc-hit-100k-by-dec-31",
    #     "asset":    "BTC",
    #     "strike":   100_000,
    #     "side":     "above",
    #     "deadline": "2026-12-31T23:59:59Z",
    # },
]

# ═══════════════════════════════════════════════════════
# STRATEGY PARAMS
# ═══════════════════════════════════════════════════════

ARB_THRESHOLD      = 0.08          # flag if |fair - market| > 8%
COOLDOWN_PER_MARKET = 30 * 60       # 30 min per market
DAILY_TRADE_CAP    = 5
DAILY_LOSS_USDC    = 30.0
TRADE_USDC         = 25.0
POLL_INTERVAL      = 30             # seconds between Polymarket price polls
RECONNECT_WAIT     = 10             # seconds between WSS reconnects

# ═══════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════

def _f(d, *keys, default=0.0):
    for k in keys:
        if k in d and d[k] is not None:
            try:
                return float(d[k])
            except (TypeError, ValueError):
                continue
    return default


def _s(d, *keys, default=""):
    for k in keys:
        if k in d and d[k] is not None:
            return str(d[k])
    return default


def _parse_token_ids(raw) -> tuple[str, str]:
    if not raw:
        return "", ""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return "", ""
    if isinstance(raw, list) and len(raw) >= 2:
        return str(raw[0]), str(raw[1])
    return "", ""


def fair_probability(current: float, strike: float, deadline_iso: str,
                     side: str) -> float:
    """Simplified log-normal fair probability estimate.

    NOT an exact options pricing model. Rough heuristic to flag obvious
    mispricings. Assumes 60% annualized vol; tune per asset if needed.
    """
    try:
        t_end = datetime.fromisoformat(deadline_iso.replace("Z", "+00:00"))
    except ValueError:
        return 0.5
    now = datetime.now(timezone.utc)
    years = max((t_end - now).total_seconds() / (365 * 86400), 1 / 365)

    import math
    sigma = 0.60
    # log-normal: P(S_T > K) = N(d2) with d2 = (ln(S/K) - sigma^2*T/2) / (sigma*sqrt(T))
    if current <= 0 or strike <= 0:
        return 0.5
    d2 = (math.log(current / strike) - (sigma ** 2) * years / 2) / (
        sigma * math.sqrt(years)
    )
    # Normal CDF approximation (Abramowitz & Stegun 26.2.17)
    p_above = 0.5 * (1 + math.erf(d2 / math.sqrt(2)))
    return p_above if side == "above" else 1 - p_above


# ═══════════════════════════════════════════════════════
# WEB3 / WSS
# ═══════════════════════════════════════════════════════

async def read_chainlink_price(w3, feed: dict) -> Optional[float]:
    """Call latestRoundData() synchronously via the w3 instance."""
    try:
        # Build the raw call
        address = w3.to_checksum_address(feed["address"])
        result = await asyncio.to_thread(
            w3.eth.call,
            {"to": address, "data": LATEST_ROUND_SELECTOR},
        )
        # latestRoundData returns: (roundId, answer, startedAt, updatedAt, answeredInRound)
        # answer is at bytes 32-64 as int256
        if len(result) < 64:
            return None
        answer = int.from_bytes(result[32:64], "big", signed=True)
        return answer / (10 ** feed["decimals"])
    except Exception as e:
        print(f"[w3] read failed: {e}")
        return None


# ═══════════════════════════════════════════════════════
# POLYMARKET
# ═══════════════════════════════════════════════════════

async def fetch_json(session, url, params=None):
    try:
        async with session.get(
            url, params=params or {},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status == 200:
                return await resp.json()
            return None
    except Exception as e:
        print(f"[http] {url}: {e}")
        return None


async def get_market_by_slug(session, slug: str) -> Optional[dict]:
    data = await fetch_json(session, f"{GAMMA_API}/markets", {"slug": slug, "limit": 1})
    if isinstance(data, list) and data:
        return data[0]
    return None


# ═══════════════════════════════════════════════════════
# STATE
# ═══════════════════════════════════════════════════════

@dataclass
class State:
    day: str = ""
    trades: int = 0
    realized: float = 0.0
    cooldowns: dict = None  # slug -> timestamp
    prices: dict = None     # asset -> usd price

    def __post_init__(self):
        if self.cooldowns is None:
            self.cooldowns = {}
        if self.prices is None:
            self.prices = {}

    def maybe_reset(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.day != today:
            print(f"[DAY] {self.day or '-'} -> {today}")
            self.day = today
            self.trades = 0
            self.realized = 0.0


# ═══════════════════════════════════════════════════════
# ARBITRAGE LOOP
# ═══════════════════════════════════════════════════════

async def check_market(session, market: dict, state: State, paper: bool):
    slug = market["slug"]
    asset = market["asset"]
    strike = market["strike"]
    side = market["side"]
    deadline = market["deadline"]

    if asset not in state.prices:
        return

    last_trade = state.cooldowns.get(slug, 0)
    if time.time() - last_trade < COOLDOWN_PER_MARKET:
        return

    if state.trades >= DAILY_TRADE_CAP:
        return
    if state.realized <= -DAILY_LOSS_USDC:
        return

    pm = await get_market_by_slug(session, slug)
    if not pm:
        print(f"[skip] market not found: {slug}")
        return

    poly_price = _f(pm, "bestAsk", "best_ask", "lastTradePrice")
    if not (0 < poly_price < 1):
        return

    current = state.prices[asset]
    fair = fair_probability(current, strike, deadline, side)
    edge = fair - poly_price   # positive = polymarket underpriced YES

    print(f"[{slug[:30]}] {asset}=${current:,.0f} strike=${strike:,} "
          f"fair={fair:.3f} poly={poly_price:.3f} edge={edge:+.3f}")

    if abs(edge) < ARB_THRESHOLD:
        return

    direction = "YES" if edge > 0 else "NO"
    yes_id, no_id = _parse_token_ids(pm.get("clobTokenIds"))
    token_id = yes_id if direction == "YES" else no_id
    if not token_id:
        return

    trade_price = poly_price if direction == "YES" else (1 - poly_price)
    shares = round(TRADE_USDC / trade_price, 2)

    msg = (
        f"ARB {slug[:40]}\n"
        f"{asset}=${current:,.0f} strike=${strike:,}\n"
        f"fair={fair:.3f} poly={poly_price:.3f} edge={edge:+.3f}\n"
        f"trade {direction} {shares} @ {trade_price:.4f}"
    )
    print(msg)
    notify(msg, tag="arb2")

    state.cooldowns[slug] = time.time()
    state.trades += 1

    if paper:
        return

    # ─── LIVE ───
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY
    except ImportError:
        print("[err] pip install py-clob-client")
        return

    try:
        client = ClobClient(
            host     = CLOB_HOST,
            key      = PRIVATE_KEY,
            chain_id = CHAIN_ID_POLYGON,
            funder   = FUNDER,
            signature_type = SIG_TYPE,
        )
        client.set_api_creds(client.create_or_derive_api_creds())
        order = client.create_order(OrderArgs(
            token_id = token_id,
            price    = trade_price,
            size     = shares,
            side     = BUY,
        ))
        resp = client.post_order(order, OrderType.GTC)
        if not resp or not resp.get("success", True):
            notify_critical(f"order rejected: {resp}", tag="arb2")
            return
        notify(f"OK {resp.get('orderID', '?')}", tag="arb2")
    except Exception as e:
        notify_critical(f"exec failed: {e}", tag="arb2")


async def price_poll_loop(w3, state: State):
    while True:
        for asset, feed in CHAINLINK_FEEDS.items():
            px = await read_chainlink_price(w3, feed)
            if px is not None:
                state.prices[asset] = px
        await asyncio.sleep(POLL_INTERVAL)


async def arb_loop(session, state: State, paper: bool):
    while True:
        try:
            state.maybe_reset()
            for m in MARKETS:
                await check_market(session, m, state, paper)
                await asyncio.sleep(1)
        except Exception as e:
            print(f"[arb] loop error: {e}")
            notify_critical(f"loop error: {e}", tag="arb2")
        await asyncio.sleep(POLL_INTERVAL)


# ═══════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════

def preflight(paper: bool) -> bool:
    if not ALCHEMY_WSS.startswith("wss://"):
        print("[PREFLIGHT] ALCHEMY_WSS missing or invalid")
        return False
    if not MARKETS:
        print("[PREFLIGHT] MARKETS is empty — fill it before running")
        return False
    if paper:
        return True
    if not PRIVATE_KEY.startswith("0x"):
        print("[PREFLIGHT] ARB2_PRIVATE_KEY (or POLY_PRIVATE_KEY) missing")
        return False
    if not FUNDER.startswith("0x") or len(FUNDER) != 42:
        print(f"[PREFLIGHT] ARB2_FUNDER invalid: {FUNDER}")
        return False
    print(f"[PREFLIGHT OK] arb2 funder={FUNDER}")
    return True


async def main(paper: bool):
    try:
        from web3 import Web3
        from web3.providers.persistent import WebSocketProvider
    except ImportError:
        print("[err] pip install web3")
        sys.exit(1)

    print(f"[arb2] mode={'PAPER' if paper else 'LIVE'}")
    notify(f"started ({'PAPER' if paper else 'LIVE'})", tag="arb2")

    state = State()

    # Connect web3 via WSS with reconnect loop
    while True:
        try:
            w3 = Web3(WebSocketProvider(ALCHEMY_WSS))
            if not w3.is_connected():
                raise RuntimeError("web3 not connected")
            print("[w3] connected")
            break
        except Exception as e:
            print(f"[w3] connect failed: {e}, retrying in {RECONNECT_WAIT}s")
            await asyncio.sleep(RECONNECT_WAIT)

    async with aiohttp.ClientSession() as session:
        await asyncio.gather(
            price_poll_loop(w3, state),
            arb_loop(session, state, paper),
        )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper", action="store_true")
    args = ap.parse_args()

    if not preflight(args.paper):
        sys.exit(1)

    try:
        asyncio.run(main(args.paper))
    except KeyboardInterrupt:
        notify("stopped (SIGINT)", tag="arb2")
        print("\n[arb2] stopped")
