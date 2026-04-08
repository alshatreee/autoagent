"""
arb1 — Copy Trader

Copies a single smart wallet's new Polymarket positions with fixed size.
Detects new positions by diffing the target's position list each cycle
against a persistent state file.

Run (paper — default, safe):
    python3 copy_trader.py --paper

Run (live):
    POLY_PRIVATE_KEY=0x... POLY_FUNDER=0x... python3 copy_trader.py

Environment:
    POLY_PRIVATE_KEY   signer private key (live only)
    POLY_FUNDER        proxy/funder address (live only)
    POLY_SIG_TYPE      0=EOA, 1=email, 2=browser (default: 0 per our wallets)
    TARGET_WALLET      wallet to copy (override default)
    STATE_FILE         path to state file (default: ./copy_trader_state.json)
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
"""

import argparse
import asyncio
import aiohttp
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from telegram_notify import notify, notify_critical  # noqa: E402

# ═══════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════

DATA_API  = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"

# sharky6999 (fill in the exact address)
TARGET_WALLET = os.getenv(
    "TARGET_WALLET",
    "0x751a2b860000000000000000000000000000000000",  # REPLACE with real address
).lower()

TRADE_USDC      = 25.0      # fixed size per copied trade
SCAN_INTERVAL   = 120       # 2 minutes
PRICE_MIN       = 0.05
PRICE_MAX       = 0.95
MIN_TARGET_SIZE = 50.0      # ignore target positions smaller than $50
MAX_DAILY_COPY  = 20        # daily copy cap
STATE_FILE      = os.getenv("STATE_FILE", "copy_trader_state.json")

CHAIN_ID_POLYGON = 137

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


async def fetch_json(session, url, params=None):
    try:
        async with session.get(
            url, params=params or {},
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


# ═══════════════════════════════════════════════════════
# STATE
# ═══════════════════════════════════════════════════════

@dataclass
class State:
    seen_positions: set = field(default_factory=set)   # {f"{cid}:{outcome}"}
    day:            str = ""
    copies_today:   int = 0

    def maybe_reset(self):
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.day != today:
            print(f"[DAY] {self.day or '-'} -> {today}")
            self.day = today
            self.copies_today = 0

    def save(self, path: str):
        try:
            with open(path, "w") as f:
                json.dump({
                    "seen_positions": sorted(self.seen_positions),
                    "day": self.day,
                    "copies_today": self.copies_today,
                }, f, indent=2)
        except Exception as e:
            print(f"[STATE] save failed: {e}")

    @classmethod
    def load(cls, path: str) -> "State":
        s = cls()
        try:
            with open(path) as f:
                data = json.load(f)
                s.seen_positions = set(data.get("seen_positions", []))
                s.day = data.get("day", "")
                s.copies_today = int(data.get("copies_today", 0))
            print(f"[STATE] loaded {len(s.seen_positions)} seen positions")
        except FileNotFoundError:
            print("[STATE] fresh state")
        except Exception as e:
            print(f"[STATE] load failed: {e}")
        return s


# ═══════════════════════════════════════════════════════
# DATA FETCHERS
# ═══════════════════════════════════════════════════════

async def get_target_positions(session) -> list[dict]:
    data = await fetch_json(session, f"{DATA_API}/positions", {
        "user":  TARGET_WALLET,
        "limit": 200,
    })
    return data if isinstance(data, list) else []


async def get_market_info(session, condition_id: str) -> Optional[dict]:
    """Fetch market by conditionId to get clobTokenIds and current price."""
    data = await fetch_json(session, f"{GAMMA_API}/markets", {
        "condition_ids": condition_id,
        "limit": 1,
    })
    if isinstance(data, list) and data:
        return data[0]
    return None


# ═══════════════════════════════════════════════════════
# EXECUTOR
# ═══════════════════════════════════════════════════════

async def copy_trade(session, target_pos: dict, state: State, paper: bool) -> bool:
    cid = _s(target_pos, "conditionId", "condition_id")
    outcome = _s(target_pos, "outcome").upper()
    target_size = _f(target_pos, "size", "currentValue", "value")

    key = f"{cid}:{outcome}"
    if key in state.seen_positions:
        return False
    if target_size < MIN_TARGET_SIZE:
        state.seen_positions.add(key)
        return False
    if state.copies_today >= MAX_DAILY_COPY:
        print(f"[CAP] daily copy limit hit ({MAX_DAILY_COPY})")
        return False

    market = await get_market_info(session, cid)
    if not market:
        print(f"[SKIP] could not fetch market {cid}")
        return False

    yes_id, no_id = _parse_token_ids(market.get("clobTokenIds"))
    if not yes_id or not no_id:
        print(f"[SKIP] no clobTokenIds for {cid}")
        return False

    token_id = yes_id if outcome == "YES" else no_id
    price = _f(market, "bestAsk", "best_ask", "lastTradePrice")
    if not (PRICE_MIN <= price <= PRICE_MAX):
        print(f"[SKIP] price {price} out of range")
        state.seen_positions.add(key)
        return False

    shares = round(TRADE_USDC / price, 2)
    question = _s(market, "question", default=cid)[:60]

    tag = "PAPER" if paper else "LIVE"
    msg = (
        f"[{tag}] copy {outcome} {question}\n"
        f"price={price:.4f} shares={shares} cost=${TRADE_USDC}"
    )
    print(msg)
    notify(msg, tag="arb1")

    if paper:
        state.seen_positions.add(key)
        state.copies_today += 1
        return True

    # ─── LIVE ───
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY
    except ImportError:
        print("[ERROR] pip install py-clob-client")
        return False

    try:
        client = ClobClient(
            host     = CLOB_HOST,
            key      = os.getenv("POLY_PRIVATE_KEY", ""),
            chain_id = CHAIN_ID_POLYGON,
            funder   = os.getenv("POLY_FUNDER", ""),
            signature_type = int(os.getenv("POLY_SIG_TYPE", "0")),
        )
        client.set_api_creds(client.create_or_derive_api_creds())

        order = client.create_order(OrderArgs(
            token_id = token_id,
            price    = price,
            size     = shares,
            side     = BUY,
        ))
        resp = client.post_order(order, OrderType.GTC)

        if not resp or not resp.get("success", True):
            notify_critical(f"order rejected: {resp}", tag="arb1")
            return False

        state.seen_positions.add(key)
        state.copies_today += 1
        notify(f"OK order={resp.get('orderID', resp.get('orderId', '?'))}", tag="arb1")
        return True

    except Exception as e:
        notify_critical(f"exec failed: {e}", tag="arb1")
        return False


# ═══════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════

async def main_loop(paper: bool):
    print(f"[arb1] mode={'PAPER' if paper else 'LIVE'} target={TARGET_WALLET}")
    notify(f"started ({'PAPER' if paper else 'LIVE'})", tag="arb1")

    state = State.load(STATE_FILE)

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                state.maybe_reset()
                t0 = time.time()

                positions = await get_target_positions(session)
                print(f"[SCAN] {len(positions)} target positions")

                new_count = 0
                for pos in positions:
                    if await copy_trade(session, pos, state, paper):
                        new_count += 1
                        await asyncio.sleep(2)

                state.save(STATE_FILE)
                print(f"[CYCLE] {time.time()-t0:.1f}s new={new_count} "
                      f"copies_today={state.copies_today}")

            except Exception as e:
                print(f"[LOOP] error: {e}")
                notify_critical(f"loop error: {e}", tag="arb1")

            await asyncio.sleep(SCAN_INTERVAL)


def preflight(paper: bool) -> bool:
    if paper:
        return True
    problems = []
    if not os.getenv("POLY_PRIVATE_KEY", "").startswith("0x"):
        problems.append("POLY_PRIVATE_KEY missing")
    if not os.getenv("POLY_FUNDER", "").startswith("0x"):
        problems.append("POLY_FUNDER missing")
    if not TARGET_WALLET.startswith("0x") or len(TARGET_WALLET) != 42:
        problems.append(f"TARGET_WALLET invalid: {TARGET_WALLET}")
    if problems:
        print("[PREFLIGHT FAILED]")
        for p in problems:
            print(f"  - {p}")
        return False
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper", action="store_true",
                    help="paper mode (no real orders)")
    args = ap.parse_args()

    if not preflight(args.paper):
        sys.exit(1)

    try:
        asyncio.run(main_loop(args.paper))
    except KeyboardInterrupt:
        print("\n[arb1] stopped")
        notify("stopped (SIGINT)", tag="arb1")
