# Polymarket Whale Bot — Full Setup Guide

## Files

| File | Purpose |
|---|---|
| `polymarket_bot.py` | Main bot — scans whales, generates signals, executes trades |
| `whale_scorer.py` | Standalone tool to vet whale candidates before adding them |

---

## ⚠️ Read This First

Before putting real money in this bot:

1. **The bot has NO exit logic.** It opens positions but never sells. You must close positions manually (on polymarket.com) when you want to take profit or cut losses. The daily loss limit uses unrealized PnL (mark-to-market) to protect you.
2. **Paper-trade for at least 1-2 weeks.** Polymarket's data API is not formally documented; field names can change. Watch the logs carefully.
3. **Whale selection is more important than the code.** A bad whale list = guaranteed losses. Use `whale_scorer.py` before trusting any address.
4. **Start with a tiny budget.** First live run: `MAX_TRADE_USDC = 5`.

---

## Step 1 — Install

```bash
pip install aiohttp py-clob-client
```

Python 3.10+ required (uses `|` type hints and PEP 604 syntax).

---

## Step 2 — Find Whale Candidates

Sources:
- **Polymarket Leaderboard**: https://polymarket.com/leaderboard — but DO NOT just copy the top 10. Those are absolute-profit leaders, often massive funds not retail-friendly.
- **Dune Analytics**: search `polymarket whales` on https://dune.com — filter by ROI (not absolute profit), minimum 50 trades, active in last 30 days.
- **On-chain reverse engineering**: find people who entered early in markets that later resolved favorably, across multiple categories.

Collect **10-15 candidate addresses** minimum.

---

## Step 3 — Vet Whales with `whale_scorer.py`

1. Open `whale_scorer.py`
2. Edit the `CANDIDATES` list at the top:
   ```python
   CANDIDATES = [
       "0xaad03b403c831d0a8b484abe59ac25188b49f61d",
       "0x...another...",
       "0x...another...",
   ]
   ```
3. Run:
   ```bash
   python whale_scorer.py
   ```
4. The tool checks each candidate against 7 criteria:

   | # | Criterion | Default Threshold |
   |---|---|---|
   | 1 | **ROI** — total return on cost basis | ≥ 20% |
   | 2 | **TRADES** — distinct markets touched | ≥ 50 |
   | 3 | **WIN%** — win rate on closed positions | ≥ 55% |
   | 4 | **DD** — max drawdown from peak PnL | ≤ 40% |
   | 5 | **DIV** — category diversity | ≥ 5 tags |
   | 6 | **ACTV** — active in recent days | ≤ 7 days |
   | 7 | **SIZE** — median trade size | $1K–$200K |

5. Only keep whales that score **5/7 or better**. The script prints a copy-paste block at the end.

   If a criterion shows `NA`, the API didn't return that field — treat as fail conservatively.

---

## Step 4 — Configure `polymarket_bot.py`

Open `polymarket_bot.py` and edit the `CONFIG` section:

```python
WHALES = [
    # paste addresses from whale_scorer output
    "0x...",
    "0x...",
    "0x...",
]

MAX_TRADE_USDC      = 5.0    # START SMALL for first live run
MAX_DAILY_TRADES    = 5      # conservative
MAX_DAILY_LOSS      = 20.0   # hard stop at $20 loss/day
MAX_OPEN_POSITIONS  = 3
MIN_CONSENSUS_EXEC  = 0.75   # stricter = fewer but stronger signals

PAPER_TRADING       = True   # LEAVE TRUE FOR AT LEAST 1-2 WEEKS
```

---

## Step 5 — Paper Trading (MANDATORY)

```bash
python polymarket_bot.py
```

Let it run for **at least 1-2 weeks**. During this time:

- Watch the logs: are signals being generated? Are they being skipped for sensible reasons?
- Spot-check a few signals by hand on polymarket.com — does the whale really hold that position? Is the price shown correct?
- Look for any `[WARN]` or `[ERROR]` lines — these usually mean the Polymarket API returned an unexpected field.
- Track hypothetical PnL: would you have made money following these signals?

If anything looks off, **do not go live**. Debug first.

---

## Step 6 — Going Live (only after paper trading succeeds)

### Required environment variables

```bash
export POLY_PRIVATE_KEY="0x..."      # private key of the wallet that will sign orders
export POLY_FUNDER="0x..."            # your Polymarket proxy wallet address
                                      # (found in polymarket.com → account → deposit address)
export POLY_SIG_TYPE="2"              # 2 = browser/Gnosis Safe proxy (most users)
                                      # 0 = EOA, 1 = email/magic login
```

### Get POLY_FUNDER

On polymarket.com:
1. Log in
2. Click your account → Deposit
3. Copy the address shown (that's your proxy wallet / funder)

### Flip the switch

Edit `polymarket_bot.py`:
```python
PAPER_TRADING = False
```

### Preflight checks

The bot will refuse to go live if:
- `POLY_PRIVATE_KEY` is missing or not `0x...`
- `POLY_FUNDER` is missing
- Fewer than 3 whales configured
- `MAX_TRADE_USDC > 100` (on first live run keep it tiny)

### Run

```bash
python polymarket_bot.py
```

You'll see a **10-second abort window** before the scan loop starts. Press Ctrl+C if anything looks wrong.

---

## Monitoring Live Operations

### What the bot does each cycle (every 5 minutes)

1. Reset daily counters if UTC date changed
2. Poll midpoint prices for all open positions → update unrealized PnL
3. Fetch top 100 active markets from Gamma API
4. Fetch whale positions from Data API
5. Compute signals (consensus, agreement, slippage)
6. For each valid signal, run risk checks and place orders
7. Print cycle summary and sleep

### Kill switches built in

- Daily trade cap (stop trading after N trades/day)
- Daily loss cap (stop trading if unrealized+realized PnL ≤ -$N)
- Max open positions (stop opening new once N are open)
- Per-market cooldown (no re-entry within 24h)
- Pre-trade risk checks (slippage, price sanity, weak consensus)

### You still need to

- **Manually close positions** — the bot doesn't sell
- **Monitor whale behavior** — if they exit a position, you probably should too
- **Check logs daily** for API warnings
- **Withdraw profits** periodically

---

## Tuning Knobs

| Config | Default | Meaning |
|---|---|---|
| `CONSENSUS_THRESHOLD` | 0.60 | Min volume-weighted agreement to generate a signal |
| `AGREEMENT_THRESHOLD` | 0.50 | Min fraction of whales that must agree |
| `MIN_WHALES_FOR_EXEC` | 2 | Min count of whales agreeing (absolute) |
| `MIN_CONSENSUS_EXEC` | 0.70 | Stricter consensus required before actually trading |
| `MAX_SLIPPAGE` | 0.03 | Reject if market price has moved >3% from whale entry |
| `MAX_MARKET_PRICE` | 0.90 | Reject near-resolved markets (bad R/R) |
| `RECENCY_HOURS` | 24 | Only consider positions traded in last 24h |
| `MIN_WHALE_VOLUME` | 5000 | Ignore positions smaller than $5K |

Tighter thresholds = fewer trades, higher quality. Start tight, loosen only after a long successful paper phase.

---

## Troubleshooting

**`HTTP 404` on /positions** — address has no public positions yet, or the data API moved. Check https://data-api.polymarket.com/positions?user=YOUR_ADDR in a browser.

**`HTTP 400` with `invalid token_id`** — `clobTokenIds` wasn't parsed. The Gamma API may have changed its response shape. Add a print statement in `_parse_token_ids()` to debug.

**Bot runs but never executes** — probably all signals fail `MIN_CONSENSUS_EXEC = 0.70`. Normal. Real consensus signals are rare.

**Orders rejected with `not_enough_balance`** — ensure your Polymarket proxy wallet has USDC.e (bridged USDC on Polygon), not native USDC.

**`ImportError: py_clob_client`** — `pip install py-clob-client`.

---

## Safety Checklist Before Going Live

```
[ ] Paper-traded successfully for >= 1 week
[ ] >= 5 whales configured, each scored >= 5/7 by whale_scorer.py
[ ] MAX_TRADE_USDC = 5 (first run)
[ ] MAX_DAILY_LOSS set to an amount you can afford to lose
[ ] POLY_FUNDER verified (copied from polymarket.com, not guessed)
[ ] Proxy wallet funded with USDC.e on Polygon
[ ] You understand the bot does NOT exit positions
[ ] You have a plan for manually closing losing positions
[ ] You've read this entire README
```

Once all boxes are checked: good luck, trade small, trust the process.
