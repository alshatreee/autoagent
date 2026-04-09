"""
Signal Copier — Telegram Channel → Binance / Gate.io (Spot Only)

Monitors a Telegram channel (Naif_Alert by default) for trading signals,
parses entry/TP/SL from the message, and executes spot trades.

Also runs in EVAL mode: logs every signal + tracks its P&L outcome after
24h so you can objectively score the channel BEFORE risking real money.

Architecture:
    1. Telethon (userbot) listens for new messages in the target channel
    2. Parser extracts: coin, entry price, TP targets, SL
    3. Risk manager checks daily caps and position limits
    4. ccxt executes spot market buy (paper or live)
    5. Background loop monitors open positions for TP/SL exits

First-time setup:
    1. Get api_id + api_hash from https://my.telegram.org
    2. Run: python3 signal_copier.py --login
       (interactive: enter phone → receive code → enter code → done)
       This creates a .session file. After that, the bot auto-connects.
    3. Run: python3 signal_copier.py --paper

Dependencies:
    pip install telethon ccxt aiohttp

Environment (in .env):
    TG_API_ID          from my.telegram.org
    TG_API_HASH        from my.telegram.org
    TG_CHANNEL         channel username (default: Naif_Alert)
    EXCHANGE           "binance" or "gateio" or "both" (default: binance)
    BINANCE_API_KEY    Binance API key (live only)
    BINANCE_SECRET     Binance API secret (live only)
    GATE_API_KEY       Gate.io API key (live only)
    GATE_SECRET        Gate.io API secret (live only)
    TRADE_USDT         USDT per trade (default: 20)
    MAX_DAILY_TRADES   daily trade cap (default: 5)
    MAX_DAILY_LOSS     daily loss cap in USDT (default: 30)
    MAX_OPEN           max simultaneous positions (default: 3)
    TELEGRAM_BOT_TOKEN for notifications (reuses existing)
    TELEGRAM_CHAT_ID   for notifications
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from telegram_notify import notify, notify_critical  # noqa: E402

# ═══════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════

TG_API_ID   = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_CHANNEL  = os.getenv("TG_CHANNEL", "Naif_Alert")
SESSION     = os.getenv("TG_SESSION", "signal_copier")

EXCHANGE       = os.getenv("EXCHANGE", "binance").lower()  # binance | gateio | both
TRADE_USDT     = float(os.getenv("TRADE_USDT", "20"))
MAX_DAILY_TRADES = int(os.getenv("MAX_DAILY_TRADES", "5"))
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "30"))
MAX_OPEN       = int(os.getenv("MAX_OPEN", "3"))

STATE_FILE  = os.getenv("SIGNAL_STATE", "signal_state.json")
LOG_FILE    = os.getenv("SIGNAL_LOG", "signal_log.jsonl")

MONITOR_INTERVAL = 10   # seconds between TP/SL checks
MIN_VOLUME_24H   = 50_000  # skip coins with < $50K 24h volume
MIN_SELL_USDT    = 11.0    # Binance min notional ~$10; use $11 for safety

# Timeframe-based auto TP/SL (since Naif_Alert signals have no TP/SL)
TIMEFRAME_PARAMS = {
    "15m": {"tp_pct": 0.02, "sl_pct": 0.01, "hold_h": 2},
    "1h":  {"tp_pct": 0.03, "sl_pct": 0.015, "hold_h": 6},
    "4h":  {"tp_pct": 0.05, "sl_pct": 0.025, "hold_h": 24},
    "1d":  {"tp_pct": 0.08, "sl_pct": 0.04, "hold_h": 72},
}
DEFAULT_TF_PARAMS = {"tp_pct": 0.03, "sl_pct": 0.015, "hold_h": 6}

# Trend strength filter — only trade strong signals
# 🔥 قوي جداً = always, 💪 قوي = if volume > 50%, ⚖️ متوسط = if volume > 100%
MIN_TREND_STRENGTH = 60     # minimum trend % to consider at all
MIN_VOLUME_STRENGTH = 30.0  # minimum volume strength %

# ═══════════════════════════════════════════════════════
# SIGNAL PARSER — tuned for Naif_Alert format
# ═══════════════════════════════════════════════════════

@dataclass
class Signal:
    coin:           str            # e.g. "FET"
    pair:           str            # e.g. "FET/USDT"
    side:           str            # "BUY" only (skip SELL for spot)
    entry:          float          # entry price
    targets:        list[float]    # auto-calculated TP
    stop:           float          # auto-calculated SL
    timeframe:      str = "15m"
    trend_strength: float = 0.0    # 0-100
    volume_strength: float = 0.0   # percentage
    max_hold_hours: int = 6
    raw_text:       str = ""


# ─── Regex patterns for Naif_Alert ───
# Direction: 🟢 BUY or LONG / 🔴 SELL or SHORT
_DIRECTION_RE = re.compile(r'(BUY|LONG|SELL|SHORT)', re.IGNORECASE)

# Coin: from TradingView link BINANCE:XXXUSDT or العملة: XXXUSDT
_TV_COIN_RE = re.compile(r'BINANCE[:\s]+([A-Z0-9]{2,10})USDT', re.IGNORECASE)
_COIN_LABEL_RE = re.compile(
    r'(?:العملة|عملة)[:\s]*([A-Z0-9]{2,10})USDT', re.IGNORECASE
)
_COIN_USDT_RE = re.compile(r'\b([A-Z]{2,10})USDT\b', re.IGNORECASE)

# Price: 💵 السعر: 0.235
_PRICE_RE = re.compile(
    r'(?:السعر|سعر|price)[:\s]*(\d+[\.,]?\d*)', re.IGNORECASE
)

# Timeframe: ⏱️ الفريم: 15m
_TF_RE = re.compile(r'(?:الفريم|فريم|frame)[:\s]*(\d+[mhd])', re.IGNORECASE)

# Volume strength: قوة الحجم: 56.4%
_VOL_STR_RE = re.compile(r'قوة الحجم[:\s]*(\d+[\.,]?\d*)%')

# Trend strength: قوة الاتجاه: 59%
_TREND_STR_RE = re.compile(r'قوة الاتجاه[:\s]*(\d+[\.,]?\d*)%')


def _parse_num(s: str) -> float:
    return float(s.replace(",", ".").replace("٫", "."))


def parse_signal(text: str) -> Optional[Signal]:
    """Parse Naif_Alert signal format."""
    if not text or len(text) < 30:
        return None

    # ─── 1. Direction (skip SELL/SHORT — spot only) ───
    dir_match = _DIRECTION_RE.search(text)
    if not dir_match:
        return None
    direction = dir_match.group(1).upper()
    if direction in ("SELL", "SHORT"):
        return None  # spot only — can't short

    # ─── 2. Coin ───
    coin = None
    for pat in [_TV_COIN_RE, _COIN_LABEL_RE, _COIN_USDT_RE]:
        m = pat.search(text)
        if m:
            coin = m.group(1).upper()
            break
    if not coin:
        return None
    if coin in ("USDT", "USD", "BUSD", "USDC"):
        return None
    pair = f"{coin}/USDT"

    # ─── 3. Entry price ───
    entry = 0.0
    m = _PRICE_RE.search(text)
    if m:
        entry = _parse_num(m.group(1))
    if entry <= 0:
        return None

    # ─── 4. Timeframe ───
    timeframe = "15m"
    m = _TF_RE.search(text)
    if m:
        timeframe = m.group(1).lower()

    # ─── 5. Volume strength ───
    vol_str = 0.0
    m = _VOL_STR_RE.search(text)
    if m:
        vol_str = _parse_num(m.group(1))

    # ─── 6. Trend strength ───
    trend_str = 0.0
    m = _TREND_STR_RE.search(text)
    if m:
        trend_str = _parse_num(m.group(1))

    # ─── 7. Quality filter (based on channel guide) ───
    # ضعيف ⚠️ (<60%) = always skip
    if trend_str < MIN_TREND_STRENGTH:
        return None
    # متوسط ⚖️ (60-69%) = only if volume is very strong (>100%)
    if trend_str < 70 and vol_str < 100:
        return None
    # قوي 💪 (70-89%) = trade if volume decent (>50%)
    if 70 <= trend_str < 90 and vol_str < 50:
        return None
    # قوي جداً 🔥 (90%+) = always trade

    # ─── 8. Auto TP/SL based on timeframe ───
    params = TIMEFRAME_PARAMS.get(timeframe, DEFAULT_TF_PARAMS)
    tp = round(entry * (1 + params["tp_pct"]), 8)
    sl = round(entry * (1 - params["sl_pct"]), 8)
    hold_h = params["hold_h"]

    return Signal(
        coin=coin, pair=pair, side="BUY",
        entry=entry, targets=[tp], stop=sl,
        timeframe=timeframe,
        trend_strength=trend_str,
        volume_strength=vol_str,
        max_hold_hours=hold_h,
        raw_text=text[:500],
    )


# ═══════════════════════════════════════════════════════
# EXCHANGE (ccxt)
# ═══════════════════════════════════════════════════════

def _create_exchange(name: str, paper: bool):
    import ccxt
    opts = {"enableRateLimit": True}

    if name == "binance":
        opts["apiKey"] = os.getenv("BINANCE_API_KEY", "")
        opts["secret"] = os.getenv("BINANCE_SECRET", "")
        ex = ccxt.binance(opts)
    elif name == "gateio":
        opts["apiKey"] = os.getenv("GATE_API_KEY", "")
        opts["secret"] = os.getenv("GATE_SECRET", "")
        ex = ccxt.gateio(opts)
    else:
        raise ValueError(f"unknown exchange: {name}")

    # NOTE: do NOT set sandbox mode even for paper. Paper mode uses real
    # market data (prices, pairs, volume) but skips order execution.
    # Sandbox has different pairs/prices which makes paper results useless.

    return ex


async def ensure_markets_loaded(exchange) -> bool:
    """Load markets once (ccxt caches internally after first call)."""
    if exchange.markets:
        return True
    try:
        await asyncio.to_thread(exchange.load_markets)
        return True
    except Exception as e:
        print(f"[exchange] load_markets failed: {e}")
        return False


async def check_pair_exists(exchange, pair: str) -> bool:
    if not await ensure_markets_loaded(exchange):
        return False
    return pair in exchange.markets


async def check_volume(exchange, pair: str) -> float:
    try:
        ticker = await asyncio.to_thread(exchange.fetch_ticker, pair)
        vol = ticker.get("quoteVolume", 0) or 0
        return float(vol)
    except Exception:
        return 0.0


async def get_price(exchange, pair: str) -> float:
    try:
        ticker = await asyncio.to_thread(exchange.fetch_ticker, pair)
        return float(ticker.get("last", 0) or 0)
    except Exception:
        return 0.0


async def market_buy(exchange, pair: str, usdt_amount: float) -> Optional[dict]:
    """Market buy using quoteOrderQty (buy $X worth of coin)."""
    try:
        price = await get_price(exchange, pair)
        if price <= 0:
            return None
        amount = usdt_amount / price
        order = await asyncio.to_thread(
            exchange.create_market_buy_order, pair, amount,
        )
        return order
    except Exception as e:
        print(f"[exec] buy failed: {e}")
        return None


async def market_sell(exchange, pair: str, coin_amount: float) -> Optional[dict]:
    try:
        order = await asyncio.to_thread(
            exchange.create_market_sell_order, pair, coin_amount,
        )
        return order
    except Exception as e:
        print(f"[exec] sell failed: {e}")
        return None


# ═══════════════════════════════════════════════════════
# STATE
# ═══════════════════════════════════════════════════════

@dataclass
class OpenPosition:
    pair:           str
    coin:           str
    exchange:       str
    amount:         float     # coin quantity
    entry:          float     # price
    cost:           float     # USDT spent
    targets:        list[float]
    stop:           float
    opened_at:      float
    targets_hit:    int = 0
    max_hold_hours: int = 6


@dataclass
class State:
    day:       str = ""
    trades:    int = 0
    realized:  float = 0.0
    positions: dict = field(default_factory=dict)  # pair -> OpenPosition

    def maybe_reset(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.day != today:
            print(f"[DAY] {self.day or '-'} -> {today}")
            self.day = today
            self.trades = 0
            self.realized = 0.0

    def save(self, path: str):
        try:
            with open(path, "w") as f:
                json.dump({
                    "day": self.day,
                    "trades": self.trades,
                    "realized": self.realized,
                    "positions": {
                        k: {
                            "pair": v.pair, "coin": v.coin,
                            "exchange": v.exchange, "amount": v.amount,
                            "entry": v.entry, "cost": v.cost,
                            "targets": v.targets, "stop": v.stop,
                            "opened_at": v.opened_at,
                            "targets_hit": v.targets_hit,
                            "max_hold_hours": v.max_hold_hours,
                        }
                        for k, v in self.positions.items()
                    },
                }, f, indent=2)
        except Exception as e:
            print(f"[state] save err: {e}")

    @classmethod
    def load(cls, path: str) -> "State":
        s = cls()
        try:
            with open(path) as f:
                data = json.load(f)
            s.day = data.get("day", "")
            s.trades = int(data.get("trades", 0))
            s.realized = float(data.get("realized", 0.0))
            for k, v in data.get("positions", {}).items():
                s.positions[k] = OpenPosition(**v)
            print(f"[state] loaded {len(s.positions)} open")
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[state] load err: {e}")
        return s


def log_signal(signal: Signal, action: str, result: dict = None):
    """Append signal to JSONL log for channel evaluation."""
    entry = {
        "ts": time.time(),
        "iso": datetime.now(timezone.utc).isoformat(),
        "coin": signal.coin,
        "pair": signal.pair,
        "entry": signal.entry,
        "targets": signal.targets,
        "stop": signal.stop,
        "action": action,
        "result": result,
        "text": signal.raw_text[:200],
    }
    try:
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════
# RISK
# ═══════════════════════════════════════════════════════

def passes_risk(signal: Signal, state: State) -> tuple[bool, str]:
    if state.trades >= MAX_DAILY_TRADES:
        return False, f"daily cap ({MAX_DAILY_TRADES})"
    if state.realized <= -MAX_DAILY_LOSS:
        return False, f"daily loss (${MAX_DAILY_LOSS})"
    if len(state.positions) >= MAX_OPEN:
        return False, f"max open ({MAX_OPEN})"
    if signal.pair in state.positions:
        return False, "already open"
    return True, "OK"


# ═══════════════════════════════════════════════════════
# ENTRY HANDLER
# ═══════════════════════════════════════════════════════

async def handle_signal(signal: Signal, exchanges: dict,
                        state: State, paper: bool):
    ok, reason = passes_risk(signal, state)
    if not ok:
        print(f"[skip] {signal.pair} — {reason}")
        log_signal(signal, f"skip:{reason}")
        return

    # Pick exchange
    ex_names = list(exchanges.keys())
    chosen_ex_name = ex_names[0]  # primary
    exchange = exchanges[chosen_ex_name]

    # Validate pair exists
    exists = await check_pair_exists(exchange, signal.pair)
    if not exists:
        # Try secondary exchange
        if len(ex_names) > 1:
            chosen_ex_name = ex_names[1]
            exchange = exchanges[chosen_ex_name]
            exists = await check_pair_exists(exchange, signal.pair)
        if not exists:
            print(f"[skip] {signal.pair} not on any exchange")
            log_signal(signal, "skip:not_listed")
            return

    # Volume check
    vol = await check_volume(exchange, signal.pair)
    if vol < MIN_VOLUME_24H:
        print(f"[skip] {signal.pair} low volume ${vol:,.0f}")
        log_signal(signal, f"skip:low_vol_{vol:.0f}")
        return

    price = await get_price(exchange, signal.pair)
    if price <= 0:
        return

    # If signal has entry price, check we're not too far from it
    if signal.entry > 0:
        dev = abs(price - signal.entry) / signal.entry
        if dev > 0.03:
            print(f"[skip] {signal.pair} price ${price} too far from entry ${signal.entry} ({dev:.1%})")
            log_signal(signal, f"skip:price_dev_{dev:.1%}")
            return

    # SL is always set by parser (auto-calculated from timeframe)
    effective_stop = signal.stop if signal.stop > 0 else round(price * 0.985, 8)

    coin_amount = round(TRADE_USDT / price, 8)

    tag = "PAPER" if paper else "LIVE"
    msg = (f"[{tag}] BUY {signal.pair} on {chosen_ex_name}\n"
           f"price=${price} tf={signal.timeframe} "
           f"trend={signal.trend_strength:.0f}% vol={signal.volume_strength:.0f}%\n"
           f"TP={signal.targets} SL={effective_stop} hold={signal.max_hold_hours}h")
    print(msg)
    notify(msg, tag="signal")

    if paper:
        state.positions[signal.pair] = OpenPosition(
            pair=signal.pair, coin=signal.coin,
            exchange=chosen_ex_name,
            amount=coin_amount, entry=price,
            cost=TRADE_USDT, targets=signal.targets,
            stop=effective_stop, opened_at=time.time(),
            max_hold_hours=signal.max_hold_hours,
        )
        state.trades += 1
        log_signal(signal, "paper_buy", {"price": price})
        state.save(STATE_FILE)
        return

    # LIVE
    order = await market_buy(exchange, signal.pair, TRADE_USDT)
    if not order:
        notify_critical(f"buy failed {signal.pair}", tag="signal")
        log_signal(signal, "buy_failed")
        return

    fill_price = float(order.get("average", price) or price)
    fill_amount = float(order.get("filled", coin_amount) or coin_amount)

    state.positions[signal.pair] = OpenPosition(
        pair=signal.pair, coin=signal.coin,
        exchange=chosen_ex_name,
        amount=fill_amount, entry=fill_price,
        cost=TRADE_USDT, targets=signal.targets,
        stop=effective_stop, opened_at=time.time(),
        max_hold_hours=signal.max_hold_hours,
    )
    state.trades += 1
    log_signal(signal, "live_buy", {
        "price": fill_price, "amount": fill_amount,
        "order_id": order.get("id"),
    })
    notify(f"BUY OK {signal.pair} @ {fill_price}", tag="signal")
    state.save(STATE_FILE)


# ═══════════════════════════════════════════════════════
# POSITION MONITOR (TP / SL / TIME EXIT)
# ═══════════════════════════════════════════════════════

async def monitor_positions(exchanges: dict, state: State, paper: bool):
    """Check every open position for TP/SL exit."""
    to_close: list[tuple[str, str, float]] = []  # (pair, reason, exit_price)

    for pair, pos in list(state.positions.items()):
        exchange = exchanges.get(pos.exchange)
        if not exchange:
            continue
        cur = await get_price(exchange, pair)
        if cur <= 0:
            continue

        # TP — partial: sell portion at each target hit
        if pos.targets and pos.targets_hit < len(pos.targets):
            next_tp = pos.targets[pos.targets_hit]
            if cur >= next_tp:
                pos.targets_hit += 1
                if pos.targets_hit >= len(pos.targets):
                    # All targets hit — full exit
                    to_close.append((pair, f"TP_ALL @{cur:.6g}", cur))
                else:
                    # Partial exit — sell 1/N of remaining
                    portion = pos.amount / (len(pos.targets) - pos.targets_hit + 1)
                    portion_value = portion * cur
                    # Skip partial if below exchange minimum notional
                    if portion_value < MIN_SELL_USDT:
                        print(f"[skip] partial TP too small ${portion_value:.2f} < ${MIN_SELL_USDT}")
                        continue
                    pnl = (cur - pos.entry) * portion
                    msg = (f"TP{pos.targets_hit} {pair} @{cur:.6g} "
                           f"(partial sell {portion:.6g} ~${portion_value:.1f})")
                    print(msg)
                    notify(msg, tag="signal")
                    if not paper:
                        await market_sell(exchange, pair, portion)
                    pos.amount -= portion
                    state.realized += pnl

        # SL
        if pos.stop > 0 and cur <= pos.stop:
            to_close.append((pair, f"SL @{cur:.6g}", cur))

        # Time stop — per-signal hold duration based on timeframe
        max_hold = pos.max_hold_hours * 3600
        if time.time() - pos.opened_at > max_hold:
            to_close.append((pair, f"TIME_24H @{cur:.6g}", cur))

    for pair, reason, exit_price in to_close:
        pos = state.positions.get(pair)
        if not pos:
            continue
        pnl = (exit_price - pos.entry) * pos.amount
        msg = f"EXIT [{reason}] {pair} pnl=${pnl:+.2f}"
        print(msg)
        notify(msg, tag="signal")

        if not paper:
            exchange = exchanges.get(pos.exchange)
            if exchange:
                await market_sell(exchange, pair, pos.amount)

        state.realized += pnl
        del state.positions[pair]
        log_signal(
            Signal(coin=pos.coin, pair=pair, side="SELL",
                   entry=pos.entry, targets=[], stop=0),
            f"exit:{reason}",
            {"exit_price": exit_price, "pnl": pnl},
        )

    state.save(STATE_FILE)


# ═══════════════════════════════════════════════════════
# CHANNEL EVAL — score signals after 24h
# ═══════════════════════════════════════════════════════

async def eval_channel(exchanges: dict):
    """Read signal_log.jsonl and compute channel stats."""
    try:
        with open(LOG_FILE) as f:
            lines = f.readlines()
    except FileNotFoundError:
        print("no signal log found — run in paper mode first")
        return

    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    buys = [e for e in entries if e.get("action") in ("paper_buy", "live_buy")]
    exits = [e for e in entries if e.get("action", "").startswith("exit:")]

    print(f"\n{'='*50}")
    print(f"Channel Evaluation: {TG_CHANNEL}")
    print(f"{'='*50}")
    print(f"Total signals logged:  {len(entries)}")
    print(f"Trades entered:        {len(buys)}")
    print(f"Trades exited:         {len(exits)}")

    skipped = [e for e in entries if e.get("action", "").startswith("skip:")]
    skip_reasons = {}
    for s in skipped:
        reason = s.get("action", "skip:?").split(":", 1)[1]
        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

    if exits:
        pnls = [e.get("result", {}).get("pnl", 0) for e in exits if e.get("result")]
        wins = sum(1 for p in pnls if p > 0)
        total_pnl = sum(pnls)
        print(f"Win rate:              {wins}/{len(pnls)} = {wins/len(pnls):.1%}")
        print(f"Total PnL:             ${total_pnl:+.2f}")
        print(f"Avg PnL per trade:     ${total_pnl/len(pnls):+.2f}")
        if wins > 0:
            avg_win = sum(p for p in pnls if p > 0) / wins
            avg_loss = sum(p for p in pnls if p <= 0) / max(len(pnls) - wins, 1)
            print(f"Avg win:               ${avg_win:+.2f}")
            print(f"Avg loss:              ${avg_loss:+.2f}")

    still_open = len(buys) - len(exits)
    if still_open > 0:
        print(f"Still open:            {still_open} (unrealized PnL not counted)")

    if skip_reasons:
        print(f"\nSkip reasons:")
        for reason, count in sorted(skip_reasons.items(), key=lambda x: -x[1]):
            print(f"  {reason:30s} {count}")

    verdict = "UNKNOWN"
    if exits and len(exits) >= 10:
        wr = wins / len(pnls)
        if total_pnl > 0 and wr >= 0.50:
            verdict = "PROMISING — consider small live test"
        elif total_pnl > 0 and wr < 0.50:
            verdict = "RISKY — few big wins carry it, could reverse"
        else:
            verdict = "UNPROFITABLE — do NOT go live"
    elif exits:
        verdict = "TOO FEW TRADES — need 10+ exits to evaluate"
    print(f"\nVerdict: {verdict}")
    print(f"{'='*50}\n")


# ═══════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════

def build_exchanges(paper: bool) -> dict:
    """Create exchange instances based on EXCHANGE config."""
    import ccxt  # noqa: F811
    exs = {}
    names = [EXCHANGE] if EXCHANGE != "both" else ["binance", "gateio"]
    for name in names:
        try:
            exs[name] = _create_exchange(name, paper)
            print(f"[exchange] {name} {'(sandbox)' if paper else '(live)'}")
        except Exception as e:
            print(f"[exchange] {name} failed: {e}")
    return exs


async def main(paper: bool, login_only: bool = False):
    try:
        from telethon import TelegramClient, events
    except ImportError:
        print("[err] pip install telethon")
        sys.exit(1)

    if not TG_API_ID or not TG_API_HASH:
        print("[err] set TG_API_ID and TG_API_HASH in .env")
        print("      get them from https://my.telegram.org")
        sys.exit(1)

    client = TelegramClient(SESSION, TG_API_ID, TG_API_HASH)
    await client.start()

    if login_only:
        me = await client.get_me()
        print(f"[login] OK — logged in as {me.first_name} ({me.phone})")
        print(f"[login] session saved to {SESSION}.session")
        print(f"[login] IMPORTANT: chmod 600 {SESSION}.session")
        await client.disconnect()
        return

    label = "PAPER" if paper else "LIVE"
    print(f"[signal] mode={label} channel={TG_CHANNEL} exchange={EXCHANGE}")
    notify(f"signal copier started ({label})", tag="signal")

    state = State.load(STATE_FILE)
    exchanges = build_exchanges(paper)
    if not exchanges:
        print("[err] no exchanges configured")
        return

    # Pre-load markets once at startup (faster signal handling later)
    for name, ex in exchanges.items():
        ok = await ensure_markets_loaded(ex)
        if ok:
            print(f"[exchange] {name}: {len(ex.markets)} pairs loaded")
        else:
            print(f"[exchange] {name}: market load failed — will retry per signal")

    # ─── Telethon event handler ───
    @client.on(events.NewMessage(chats=TG_CHANNEL))
    async def on_message(event):
        text = event.raw_text
        if not text:
            return

        signal = parse_signal(text)
        if not signal:
            return  # not a signal message

        print(f"\n[SIGNAL] {signal.pair} @{signal.entry} "
              f"tf={signal.timeframe} trend={signal.trend_strength:.0f}% "
              f"vol={signal.volume_strength:.0f}%")

        state.maybe_reset()
        await handle_signal(signal, exchanges, state, paper)

    # ─── Background position monitor ───
    async def monitor_loop():
        while True:
            try:
                if state.positions:
                    await monitor_positions(exchanges, state, paper)
            except Exception as e:
                print(f"[monitor] err: {e}")
            await asyncio.sleep(MONITOR_INTERVAL)

    # Start monitor in background
    asyncio.create_task(monitor_loop())

    print(f"[signal] listening to {TG_CHANNEL}...")
    await client.run_until_disconnected()


def preflight(paper: bool) -> bool:
    if not TG_API_ID or not TG_API_HASH:
        print("[PREFLIGHT] TG_API_ID / TG_API_HASH missing")
        return False
    if paper:
        return True
    # Live checks
    if EXCHANGE in ("binance", "both"):
        if not os.getenv("BINANCE_API_KEY") or not os.getenv("BINANCE_SECRET"):
            print("[PREFLIGHT] BINANCE_API_KEY/SECRET missing")
            return False
    if EXCHANGE in ("gateio", "both"):
        if not os.getenv("GATE_API_KEY") or not os.getenv("GATE_SECRET"):
            print("[PREFLIGHT] GATE_API_KEY/SECRET missing")
            return False
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Telegram Signal Copier")
    ap.add_argument("--paper", action="store_true", help="paper mode")
    ap.add_argument("--login", action="store_true",
                    help="login to Telegram (first-time setup)")
    ap.add_argument("--eval", action="store_true",
                    help="evaluate channel from signal log")
    args = ap.parse_args()

    if args.eval:
        import ccxt  # noqa: F811
        exs = build_exchanges(True)
        asyncio.run(eval_channel(exs))
        sys.exit(0)

    if not preflight(args.paper or args.login):
        sys.exit(1)

    try:
        asyncio.run(main(args.paper, args.login))
    except KeyboardInterrupt:
        notify("signal copier stopped", tag="signal")
        print("\n[signal] stopped")
