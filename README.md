# polymarket-bot

A Polymarket trading bot. `data/` (live order book streaming + market
metadata, persisted to SQLite), `signals/` (three strategies: stat-arb,
news-driven, market-making), `execution/` (Kelly-sized, risk-gated order
placement), `backtest/` (replays stored history through signals/ and
execution/ with no live API calls), and `main.py` + `scripts/cli.py` (runs
the full pipeline live in dry-run mode by default, with a CLI to inspect
positions/signals/P&L) are all implemented.

## Architecture

The pipeline flows in one direction: **data → signals → execution**, with
**backtest** replaying the same `signals` code against historical data instead
of live markets.

```
data/        Market data ingestion. Implemented:
               - ws_client.py    CLOB WebSocket client (market channel):
                                 subscribes to order book updates for a list
                                 of token ids, reconnects with backoff, and
                                 normalizes book/price_change events into
                                 OrderBook snapshots.
               - gamma_client.py Gamma API client: market metadata (question,
                                 end date, resolution criteria, category,
                                 outcomes, CLOB token ids), with retry/backoff
                                 on 429/5xx.
               - store.py        SQLite persistence for order book snapshots
                                 and market metadata (for backtest/ to replay).
               - ingest.py       Wires the above together: resolves
                                 condition_ids -> token_ids via Gamma, streams
                                 + persists book updates, refreshes metadata
                                 periodically.
               - models.py       OrderBook / PriceLevel / MarketMetadata.
               - market_screener.py
                                 Screens all active Gamma markets down to a
                                 ranked shortlist (by 24h volume) for picking
                                 an initial test set by hand - see below.
             Run standalone: `uv run python scripts/run_data_layer.py`.

signals/     Edge-detection strategies. Pure functions/classes that take
             market data in and return a score or trade idea out. No
             knowledge of orders, credentials, or risk limits. Implemented:
               - base.py         Signal (edge_estimate, confidence, side,
                                 optional token_id, metadata), SignalContext
                                 (order_books for every outcome of a market),
                                 and the SignalStrategy ABC:
                                 evaluate(market, order_book, context) -> Signal | None.
               - complementary_outcomes.py
                                 Stat-arb: sums best-ask (and separately
                                 best-bid) prices across every outcome of a
                                 market, which should sum to $1. Flags a BUY
                                 signal when buying one of every outcome costs
                                 under $1 net of taker fees, or a SELL signal
                                 when selling one of every outcome (e.g. after
                                 minting a complete set) nets over $1. Requires
                                 order books for *every* outcome in
                                 context.order_books to fire.
               - news/           News-driven signal (see below).
               - market_making/  Stateful two-sided quoting strategy (see
                                 below) - does NOT implement SignalStrategy;
                                 it has its own small interface instead.

execution/   Order management and risk gating (see below for detail).
             order_manager.py's OrderManager is the single entry point every
             strategy's output routes through - it sizes single-token
             Signal-based trades with fractional Kelly, sizes multi-leg
             (arb) signals at equal share counts across legs, sizes
             market-making quotes via the strategy's own position caps, and
             runs every order through the same hard exposure caps (risk.py)
             regardless of which path it came from. Places orders via
             py-clob-client with retry + idempotency (client.py, orders.py).
             Has a dry-run mode (default on) that logs intended trades
             without submitting them. journal.py persists every
             signal/quote and the decision it produced, so scripts/cli.py
             can inspect them from a separate process.

backtest/    Historical simulation (see below for detail). Replays stored
             order book data through the real signals/ and execution/ code
             (OrderManager in dry_run mode, so nothing reaches
             execution/orders.py or the real API), adding its own simulated
             fill model, P&L/calibration/risk metrics, and reporting on top.

main.py      Runs the full pipeline live (see below for detail): the data
             layer feeds signals/, signals/ routes through execution/'s risk
             gate, every decision is logged via execution/journal.py.
             Dry-run by default; going live requires two explicit opt-ins.

scripts/cli.py
             Inspect what the bot has done: `positions`, `signals`, `pnl`
             subcommands, reading from the same journal + market data
             main.py writes. Works whether main.py is still running or not.

config.py    Single source of truth for API keys and risk limits, loaded
             from environment variables / .env via python-dotenv. Import
             `settings` from here rather than reading os.environ elsewhere.

logging_config.py
             One place to configure logging (level, format). Call
             configure_logging() once at process start.
```

Why the split: `signals/` should never need to know how orders get placed,
and `execution/` should never need to know why a trade was chosen. That
separation is what lets `backtest/` reuse both `signals/` and `execution/`
unmodified — it runs the same `OrderManager` risk gate live trading would,
just in `dry_run` mode, and simulates fills on top instead of ever calling
`execution/orders.py`.

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync                       # installs dependencies into .venv
cp .env.example .env          # then fill in your credentials
```

Environment variables (see `.env.example`):

| Variable                  | Purpose                                   |
|----------------------------|--------------------------------------------|
| `CLOB_API_URL`             | Polymarket CLOB API base URL              |
| `GAMMA_API_URL`            | Polymarket Gamma API base URL             |
| `CLOB_WS_MARKET_URL`       | CLOB WebSocket market-channel URL         |
| `POLYMARKET_PRIVATE_KEY`   | Wallet private key used to sign orders    |
| `CLOB_API_KEY` / `_SECRET` / `_PASSPHRASE` | CLOB API credentials       |
| `MAX_POSITION_USD`         | Risk limit: max position size per market  |
| `MAX_ORDER_USD`            | Risk limit: max size of a single order    |
| `MAX_DAILY_LOSS_USD`       | Risk limit: max daily drawdown before halting |
| `TRACKED_MARKETS_PATH`     | JSON file of `{condition_id, question}` to stream/persist/trade (default `tracked_markets.json`) - if it exists, wins over `MARKET_CONDITION_IDS` |
| `MARKET_CONDITION_IDS`     | Fallback: comma-separated condition_ids, used only if `TRACKED_MARKETS_PATH` doesn't exist |
| `DB_PATH`                  | SQLite file for order book snapshots + metadata |
| `GAMMA_REFRESH_INTERVAL_SECONDS` | How often to re-fetch market metadata (default 300s) |
| `ANTHROPIC_API_KEY`        | Claude API key, used by `ClaudeNewsAssessor` |
| `CLAUDE_MODEL`             | Model for news assessment (default `claude-opus-4-8`) |
| `NEWS_EMBEDDING_MODEL`     | Local embedding model name (default `BAAI/bge-small-en-v1.5`) |
| `NEWS_SIMILARITY_THRESHOLD` | Min cosine similarity to consider a headline relevant (default 0.5) |
| `NEWS_MIN_PROBABILITY_SHIFT` | Min `abs(probability_shift)` to emit a Signal (default 0.03) |
| `CLOB_SIGNATURE_TYPE`      | 0 = EOA/MetaMask (default), 1 = email/Magic wallet, 2 = browser proxy wallet |
| `CLOB_FUNDER_ADDRESS`      | Funder address for signature types 1/2 (proxy wallets) |
| `MAX_PORTFOLIO_EXPOSURE_USD` | Risk limit: max total notional committed across all markets |
| `KELLY_FRACTION`           | Fraction of full Kelly actually staked (default 0.25 = quarter-Kelly) |
| `LOG_LEVEL`                | Logging verbosity (default `INFO`)        |
| `DRY_RUN`                  | When `true` (default), `OrderManager` logs intended trades instead of submitting them |
| `LIVE_TRADING_CONFIRMED`   | Must also be `true` (default `false`) for `DRY_RUN=false` to take effect - see "Running the live pipeline" |

`config.settings` is a frozen dataclass populated from these at import time.
Trading credentials are optional at import (so `data/` and `backtest/` can
run without them); code that actually places live orders should call
`settings.require_trading_credentials()` first.

## Running the data layer

```bash
# Populate tracked_markets.json (or MARKET_CONDITION_IDS in .env) first -
# see "Screening markets for an initial test set" below, or look up one
# market you already know about:
uv run python scripts/find_market.py "fed interest rate"
uv run python scripts/find_market.py https://polymarket.com/event/some-event-slug

uv run python scripts/run_data_layer.py
```

This resolves each condition_id to its outcome token_ids via the Gamma API,
subscribes to CLOB WebSocket order book updates for those tokens, logs every
book update (best bid/ask, level counts) so you can verify it's receiving
real data, and persists every snapshot plus the market metadata to the
SQLite file at `DB_PATH`. Stop with Ctrl+C.

Notes:
- The CLOB market channel subscribes by **token_id** (one per outcome), not
  condition_id — `data/ingest.py` does the condition_id → token_id resolution
  via Gamma before subscribing.
- `data/ws_client.py` reconnects with exponential backoff + jitter on any
  connection error and resubscribes from scratch; `data/gamma_client.py`
  retries on HTTP 429/5xx with backoff (honoring `Retry-After` when present).
- `py-clob-client` is in `pyproject.toml` for later use by `execution/`, but
  the data layer talks to the WebSocket and Gamma API directly (via
  `websockets`/`httpx`) rather than through it — Polymarket has archived that
  package upstream in favor of a newer SDK, and the parts used here (public
  WS + Gamma reads) don't need it.

## Screening markets for an initial test set

`scripts/find_market.py` looks up one market you already know about;
`data/market_screener.py` goes the other direction - it pulls **every**
active market and narrows thousands down to a ranked shortlist you can
actually look at:

```bash
uv run python data/market_screener.py \
  --min-volume-24h 5000 --min-liquidity 1000 \
  --min-days 1 --max-days 30 \
  --max-outcomes 2 \
  --category Politics \
  --limit 20 \
  --output shortlist.json
```

It walks every active, non-closed event via `GammaClient.get_active_events()`
(paginated, same retry/backoff as everything else in `gamma_client.py`),
filters each of that event's markets by 24h volume, liquidity, days until
`endDate`, and outcome count (`--max-outcomes 2` for binary-only,
`--min-outcomes 3` for multi-outcome-only), and ranks survivors by 24h volume
descending. Output is JSON: `question`, `condition_id`, `category`,
`volume_24h`, `liquidity`, `days_to_resolution`, `outcome_count`.

Review `shortlist.json` (or the terminal output, without `--output`), then
commit the ones you actually want with `--write-tracked-markets`:

```bash
uv run python data/market_screener.py --min-volume-24h 5000 --category Politics \
  --limit 10 --write-tracked-markets
```

This **merges** `{condition_id, question}` pairs into
`config.settings.tracked_markets_path` (`tracked_markets.json` by default,
gitignored) - existing entries are kept, new ones are appended, nothing is
ever removed automatically. `config.py` reads `MARKET_CONDITION_IDS` from
that file when it exists (falling back to the `.env` variable of the same
name otherwise), so `main.py` / `scripts/run_data_layer.py` pick up your
picks without hand-copying condition_ids into `.env`. Run the screener again
with different filters (e.g. a different `--category`) and it'll add to the
same file rather than starting over. Since it's a small, human-readable JSON
file and the script only ever adds to it, open it afterwards and delete any
entries you don't actually want before running the bot - that's the "review
by hand" step.

Three things worth knowing:
- **"category" is really tags.** Gamma's per-market `category` field is
  empty on live data now (confirmed while building this) - the real
  taxonomy is each market's *event's* free-form tags (`"Politics"`,
  `"Sports"`, `"NBA"`, ...). `--category` matches case-insensitively against
  that tag list, and the full list is included in the output so you can see
  what's actually there and adjust.
- **`--max-days` has no implicit floor at 0.** Some markets stay `active` in
  Gamma past their nominal `endDate` (pending resolution) and show a
  *negative* `days_to_resolution` - `--max-days 30` alone won't exclude
  them. Add `--min-days 0` if you only want markets that haven't reached
  their end date yet.
- **`--write-tracked-markets` only ever adds.** It dedupes by `condition_id`
  and keeps the existing entry on conflict (so re-running with a typo'd
  filter can't silently rename something you already picked) - it never
  removes a market you previously tracked, even if a later run's filters
  wouldn't have matched it. Trim `tracked_markets.json` by hand when you
  want something gone.

## News edge signal

`signals/news/` implements `NewsEdgeSignal`: it turns a stream of headlines
into `Signal`s by comparing Claude's read on a headline to the current book
price.

```
signals/news/
  feed.py             NewsHeadline; NewsFeed (ABC); MockNewsFeed (fixed list,
                       for tests/dev); RssNewsFeed (real integration point -
                       raises NotImplementedError until you wire up an RSS
                       parser or news API client).
  embeddings.py        Embedder protocol + FastEmbedEmbedder, a local
                       sentence-embedding model (fastembed, ONNX - no torch,
                       default BAAI/bge-small-en-v1.5, ~130MB, downloaded from
                       Hugging Face on first use then cached). cosine_similarity().
  claude_assessor.py   ClaudeNewsAssessor: sends the market question + headline
                       + current book price to Claude (claude-opus-4-8, via the
                       `anthropic` SDK) with a fixed JSON schema
                       (output_config.format) asking for probability_shift and
                       confidence. Raises NewsAssessmentRefused on
                       stop_reason == "refusal".
  signal.py            NewsEdgeSignal(SignalStrategy).
```

The flow is two-phase, because embedding similarity is cheap/local and the
Claude call isn't:

1. **Pre-filter (local, no API call):** for each incoming headline, call
   `signal.relevant_markets(headline, tracked_markets)` — it embeds the
   headline and every market's question with `Embedder` and keeps markets
   above `similarity_threshold` (cosine similarity, default 0.5).
2. **Assess (Claude API call, only for markets that passed step 1):** call
   `signal.evaluate(market, order_book, SignalContext(metadata={"headline": headline}))`.
   This asks Claude for a `probability_shift` calibrated against the book's
   current midpoint price, and returns a `Signal` (`side=BUY` if the shift is
   positive, `SELL` if negative) only when `abs(probability_shift)` clears
   `min_probability_shift` (default 0.03) - a plain relevance match with no
   real price effect doesn't fire.

```python
from signals.news.embeddings import FastEmbedEmbedder
from signals.news.claude_assessor import ClaudeNewsAssessor
from signals.news.signal import NewsEdgeSignal
from signals.news.feed import MockNewsFeed, NewsHeadline

news_signal = NewsEdgeSignal(embedder=FastEmbedEmbedder(), assessor=ClaudeNewsAssessor())
```

Swap `MockNewsFeed` (a fixed list of `NewsHeadline`s, useful for tests and
local runs) for `RssNewsFeed` once you have real feed URLs — it's the
documented integration point in `feed.py` and currently raises
`NotImplementedError` pending an RSS parser or news API client.

Requires `ANTHROPIC_API_KEY` in `.env` for `ClaudeNewsAssessor` (the
`anthropic` SDK reads it directly from the environment).

## Market-making strategy

`signals/market_making/` quotes both sides of a token's book and tracks
inventory across calls. It doesn't implement `signals.base.SignalStrategy` —
that interface is a stateless `evaluate() -> Signal | None` modeling one
directional idea; market making has to remember inventory between calls (a
fill changes the next quote) and returns a two-sided quote pair, not a single
`Signal`. It has its own small interface instead:

```
signals/market_making/
  spread.py       Pure, stateless spread-widening logic - no market/order-book
                   objects involved, which is what makes it independently
                   testable. compute_half_spread_bps() combines three
                   multiplicative factors (each >= 1.0) onto a base
                   half-spread, clipped to a hard ceiling:
                     - time_to_resolution_factor: 1.0 far from resolution,
                       ramping up to time_widen_max_multiplier as
                       market.end_date approaches.
                     - inventory_skew_factor: 1.0 at flat inventory, ramping
                       up to inventory_widen_max_multiplier at a full
                       position (position / max_position == ±1).
                     - volatility_factor: 1.0 at zero volatility, growing
                       linearly with a normalized rolling volatility estimate.
  volatility.py    RollingVolatility: stdev of recent mid-price returns per
                   token, feeding volatility_factor.
  models.py        Quote, QuotePair, Inventory (position per token_id, mutated
                   via apply_fill), PositionLimits (max_position,
                   max_order_size - hard caps; PositionLimits.from_settings()
                   reuses config.settings' existing max_position_usd /
                   max_order_usd rather than adding new config).
  strategy.py      MarketMakingStrategy: the stateful class. quote(market,
                   order_book, now) -> QuotePair centers on the book midpoint,
                   widens per spread.py, and hard-caps each side's size so a
                   fill can never push |position| past max_position (a side
                   is quoted at size 0, i.e. not quoted, once there's no room
                   left). record_fill(token_id, side, size) updates the
                   tracked position - the strategy has no market connection
                   of its own, so execution/ is responsible for calling this
                   when an order actually fills.
```

```python
from signals.market_making.models import PositionLimits
from signals.market_making.strategy import MarketMakingStrategy
from config import settings

mm = MarketMakingStrategy(position_limits=PositionLimits.from_settings(settings))
quote_pair = mm.quote(market, order_book)   # -> QuotePair(bid=Quote(...), ask=Quote(...))
mm.record_fill(order_book.token_id, Side.BUY, filled_size)  # after execution/ reports a fill
```

## Order execution

`execution/` is the single risk-gated path from a strategy's output to an
actual order. `OrderManager` is the entry point:

```
execution/
  order_manager.py   OrderManager - the shared entry point. Three methods,
                      because strategy outputs don't all produce the same
                      shape of decision, but all three converge on the same
                      private _submit() (idempotency check, then submit-or-log
                      and book exposure) after a shared check_order() risk
                      check:
                        - handle_signal(signal, market, order_book): for
                          single-token Signal strategies (news) - Kelly-sizes
                          the trade (sizing.py) using signal.edge_estimate
                          and signal.confidence, then risk-gates it.
                        - handle_multi_leg_signal(signal, market): for
                          multi-leg arb signals (complementary_outcomes) -
                          signal.metadata["legs"] has no single token_id, and
                          a complete-set arb needs equal SHARE counts across
                          every leg (not equal dollar notional, since leg
                          prices differ), so this sizes the whole basket
                          against the risk gate once, then submits one
                          same-share-count leg per token.
                        - handle_quote(quote_pair, market): for
                          MarketMakingStrategy's two-sided quotes, which are
                          already sized by its own position caps (no single
                          edge estimate to Kelly-size against) - each side
                          still passes through the identical risk gate.
                      Tracks exposure per market and in total (notional
                      committed at submission time, including dry-run
                      decisions) and dedupes identical order intents seen
                      within idempotency_ttl_seconds (default 60s).
  sizing.py           Fractional Kelly, pure functions (mirrors spread.py's
                      isolation pattern): implied_fair_price() backs out a
                      fair-value probability from edge_estimate; for a BUY,
                      full Kelly is (fair - price) / (1 - price) (and the
                      mirror image for SELL); kelly_position_size_usd()
                      scales that by confidence and KELLY_FRACTION, then by
                      a bankroll (MAX_PORTFOLIO_EXPOSURE_USD).
  risk.py             The shared hard-cap gate, also pure: check_order()
                      takes a requested notional plus current per-market and
                      total exposure, and approves it, resizes it down (to
                      max_order_usd, then remaining per-market room, then
                      remaining portfolio room - tightest constraint wins),
                      or rejects it if no room is left. RiskLimits.from_settings()
                      reads MAX_POSITION_USD / MAX_ORDER_USD / MAX_PORTFOLIO_EXPOSURE_USD.
  client.py           get_client() builds and caches a Level 2 (fully
                      authenticated) py_clob_client.ClobClient from
                      config.settings (private key + API creds + signature
                      type/funder for proxy wallets). reset_client() clears
                      the cache, mainly for tests.
  orders.py           place_order() signs an order once via
                      client.create_order() and retries only the POST
                      (client.post_order()) on transient failures (network
                      errors, 429, 5xx) - never re-signs, since py-clob-client
                      embeds a fresh random salt per signed order and
                      resubmitting a re-signed "retry" could leave two valid
                      orders live if the first attempt actually went through
                      despite a client-side error. A non-retryable 4xx or an
                      exchange-level rejection (HTTP 200, `success: false`)
                      raises OrderPlacementError immediately.
  journal.py          DecisionJournal: persists every signal/quote
                      (activity_log) and every OrderDecision (decisions_log)
                      to SQLite at DB_PATH, so a separate process
                      (scripts/cli.py) can inspect them later. See
                      "Running the live pipeline" below.
```

```python
from execution.order_manager import OrderManager
from execution.risk import RiskLimits
from config import settings

# dry_run defaults to True - nothing is submitted until you pass dry_run=False
# and a real client (execution.client.get_client()).
manager = OrderManager(risk_limits=RiskLimits.from_settings(settings), dry_run=settings.dry_run)

decision = manager.handle_signal(signal, market, order_book)         # NewsEdgeSignal
decisions = manager.handle_multi_leg_signal(signal, market)          # ComplementaryOutcomesSignal
decisions = manager.handle_quote(quote_pair, market)                 # MarketMakingStrategy, one per side
```

`decision.status` is one of `SUBMITTED`, `DRY_RUN`, `REJECTED` (no room under
the caps, no edge, missing price, or a duplicate of a recent intent), or
`FAILED` (sent but placement failed after retries). `decision.reasons`
explains any resizing or rejection.

Note: exposure tracking books notional at *submission* time (a resting limit
order locks collateral on Polymarket, so this is a reasonable proxy for
capital at risk) and does not currently decrease when an order is later
cancelled - see Status below.

## Backtesting

`backtest/` replays order book history already collected by
`scripts/run_data_layer.py` through the real `signals/` and `execution/`
code - no live API calls anywhere in the loop.

```
backtest/
  data_source.py    HistoricalDataSource reads DataStore and turns stored
                     rows into a single chronologically-merged replay stream
                     (ReplayEvent: one OrderBook snapshot + its MarketMetadata),
                     bounded by an optional [start, end]. infer_resolution()
                     is a pure function that reads a market's *latest known*
                     outcome_prices and, only if the market is closed and
                     those prices are unambiguous (one outcome >= 0.99, the
                     rest <= 0.01), returns {token_id: terminal payout}. This
                     depends on data collection having kept running past
                     resolution - see Status.
  portfolio.py       Portfolio: cash, positions, and realized P&L via
                     standard weighted-average-cost accounting (symmetric for
                     long and short). apply_fill() per trade,
                     mark_to_market(latest_prices) for open-position equity,
                     settle(resolutions) to convert resolved positions into
                     realized P&L at the end of a run.
  metrics.py         Pure functions (mirrors spread.py / sizing.py's
                     isolation pattern), each independently tested against
                     known values: brier_score(), log_loss() (probability
                     calibration - how well a strategy's implied fair-value
                     price matched what actually happened), sharpe_ratio()
                     (mean/stdev of a return series, optional annualization),
                     max_drawdown() (largest peak-to-trough decline as a
                     fraction of the peak).
  engine.py          run_backtest() - the replay loop. For each snapshot: any
                     signals.base.SignalStrategy gets evaluate()'d with the
                     latest known order book for every outcome in its market
                     (mirroring live SignalContext.order_books) and, if it
                     returns a Signal, that Signal goes through
                     execution.order_manager.OrderManager.handle_signal() in
                     dry_run mode - the exact same Kelly sizing and risk gate
                     live trading uses - and is filled immediately at the
                     approved price/size. MarketMakingStrategy isn't a
                     SignalStrategy, so it goes through
                     OrderManager.handle_quote() instead, and a resting quote
                     is filled only if the *next* snapshot for that token
                     crosses through it (one-step-lookahead), calling
                     strategy.record_fill() to keep its inventory in sync.
  report.py          BacktestResult, build_result(), generate_report() (text
                     summary), plot_equity_curve() (matplotlib, imported
                     lazily so computing metrics never requires it installed
                     at import time - it's still a hard dependency for
                     actually plotting).
```

```python
from backtest.engine import run_backtest
from backtest.report import generate_report, plot_equity_curve
from execution.risk import RiskLimits
from signals.complementary_outcomes import ComplementaryOutcomesSignal
from signals.market_making.strategy import MarketMakingStrategy
from signals.market_making.models import PositionLimits
from config import settings

results = run_backtest(
    strategies={
        "arb": ComplementaryOutcomesSignal(),
        "mm": MarketMakingStrategy(position_limits=PositionLimits.from_settings(settings)),
    },
    condition_ids=["0x...", "0x..."],
    db_path=settings.db_path,
    start=datetime(2026, 1, 1), end=datetime(2026, 2, 1),   # optional date range
    risk_limits=RiskLimits.from_settings(settings),
    mode="isolated",   # or "combined" - see below
)
print(generate_report(results["arb"]))
plot_equity_curve(results["arb"], "arb_equity.png")
```

Or from the command line: `uv run python scripts/run_backtest.py --strategies complementary_outcomes,market_making --start 2026-01-01 --end 2026-02-01 --mode isolated` (writes a `*_report.txt` and `*_equity_curve.png` per result to `--output-dir`).

**Isolated vs. combined** (`mode=`), which is how you "test strategies in
isolation or combined": `"isolated"` (default) gives each strategy its own
fresh `Portfolio` and `OrderManager` replaying independently — returns
`{name: BacktestResult}`, so you can compare strategies' standalone P&L.
`"combined"` replays every strategy through *one* shared `OrderManager` and
`Portfolio`, competing for the same exposure caps exactly as they would
live — returns a single `BacktestResult`, with per-strategy attribution via
each `Fill.strategy` in `result.fills`.

**Multi-leg signals** (`ComplementaryOutcomesSignal`'s arb): routed through
`OrderManager.handle_multi_leg_signal()`, which sizes the whole basket at
equal share counts per leg (not Kelly - see "Order execution" above) but
shares the exact same exposure tracker as every other order the engine
submits, live or backtest.

**News signals**: `NewsEdgeSignal` is an ordinary `SignalStrategy`, so it's
supported by the same generic loop, but it will never actually fire here -
`evaluate()` only produces a `Signal` when `SignalContext.metadata["headline"]`
is set, and this engine has no historical headline feed to supply one. Wire
up your own headline replay and call it separately if you need to backtest news.

### Parameter optimization

`backtest/optimize.py` grid-searches each strategy's *internal* parameters
(arb fee/edge thresholds, spread widening, quote size) and ranks configs by
backtest P&L:

```bash
uv run python backtest/optimize.py --strategy complementary_outcomes
uv run python backtest/optimize.py --strategy market_making --min-fills 5
```

It deliberately does **not** sweep `RiskLimits` - position/order/portfolio
caps represent actual risk tolerance, and tuning them to chase backtest P&L
is just relabeling "took more risk" as "optimized," not finding a real edge.
Configs below `--min-fills` (default 3) are flagged and excluded from the
"best" pick, since a P&L from 1-2 lucky fills is mostly noise. Even the
winner is a **hypothesis, not a conclusion** - it's ranked against one
historical sample, so it can easily be curve-fit to quirks of that specific
window. Validate it out-of-sample (a later period the sweep never saw)
before trusting it with real capital.

In practice, a short/thin sample (e.g. a few minutes of collected data, or
markets that were flat/illiquid over that window) will show 0 fills across
the *entire* grid - that's the optimizer correctly refusing to manufacture
a signal that isn't there, not a bug. Collect more data (longer
`scripts/run_data_layer.py` runs) and prefer more actively-traded markets
(`data/market_screener.py --min-volume-24h ...`) before expecting a
meaningful result.

## Running the live pipeline

```bash
# Populate tracked_markets.json first (see "Screening markets" above) - or
# MARKET_CONDITION_IDS in .env if you're not using the screener.
uv run python main.py
```

`main.py` is the full pipeline, wired live: it streams order book updates
via `data/` exactly like `scripts/run_data_layer.py`, but for every update it
also runs each configured strategy (`ComplementaryOutcomesSignal` and
`MarketMakingStrategy` by default - see `build_strategies()`; `NewsEdgeSignal`
isn't included by default for the same reason it doesn't fire in backtests,
no live headline feed is wired up), routes any resulting signal/quote through
`OrderManager` (dry-run by default), and persists both the raw
signal/quote and the resulting decision via `execution.journal.DecisionJournal`
- all to the same `DB_PATH` the data layer already uses.

**Going live is deliberately two separate opt-ins**, not one flag:

```bash
DRY_RUN=false
LIVE_TRADING_CONFIRMED=true
```

`config.Settings.require_live_trading_confirmation()` (called at startup by
`main.py`) is a no-op while `DRY_RUN=true`, but once `DRY_RUN=false` it also
requires `LIVE_TRADING_CONFIRMED=true` *and* full trading credentials
(`POLYMARKET_PRIVATE_KEY`, `CLOB_API_KEY`/`_SECRET`/`_PASSPHRASE`) - raising a
clear `RuntimeError` (turned into a clean `SystemExit`, not a stack trace) if
either is missing. The point is that flipping `DRY_RUN` alone - a single
misread env var, a copy-pasted `.env` - can't silently turn on real order
placement; both have to be set on purpose.

### Checking what the bot is doing

`scripts/cli.py` reads the same `DecisionJournal` and `DataStore` `main.py`
writes to, so it works whether `main.py` is currently running or not:

```bash
uv run python scripts/cli.py positions          # open positions, mark-to-market
uv run python scripts/cli.py signals --limit 10  # recent signals/quotes, newest first
uv run python scripts/cli.py pnl                 # realized/unrealized/total P&L
```

In dry-run mode, `positions` and `pnl` are **simulated**: every
`SUBMITTED`/`DRY_RUN` decision is replayed through a fresh
`backtest.portfolio.Portfolio` as if it filled immediately at its quoted
price - the same simplification `backtest/` uses, reused here because there's
no live fill feed wired up yet (see Status). In live mode this will overstate
fills for any resting order that never actually got hit.

## Running tests

```bash
uv run pytest
```

Tests mirror the package layout (`tests/data/`, `tests/signals/`,
`tests/execution/`, `tests/backtest/`, `tests/scripts/`, plus `tests/test_main.py`
for `main.py`'s helpers). All of them cover real logic without touching the
network, the real Claude API, or the real CLOB - everything is mocked, run
against pure functions, or run against a real temporary SQLite `DataStore` /
`DecisionJournal` seeded with synthetic data: Gamma parsing/retries, WS event
normalization, SQLite persistence (`get_market_metadata`,
`list_order_book_snapshots`, `get_latest_order_book`), the
complementary-outcomes signal against mocked order books with known
mispricings, the news signal against a mocked embedder + mocked Claude
client, the market-making spread-widening math tested in isolation, the
Kelly sizing math checked against the closed-form formula, the risk gate
resizing/rejecting across each cap independently and in combination, order
retry/idempotency behavior against a fake `ClobClient`, `OrderManager`
end-to-end (including `handle_multi_leg_signal`'s equal-share-count sizing
and its shared exposure tracking with the other two entry points),
`tests/backtest/` (P&L/calibration metrics against known values, `Portfolio`'s
weighted-average-cost accounting including partial closes and flipping
through zero, and `test_engine.py` seeding a temp DB with an underpriced
complete set and a market-making crossing scenario), `DecisionJournal`
round-tripping signals/quotes/decisions, `main.py`'s strategy wiring, the
two-opt-in live-trading guard, and its per-update handlers against a real
(temp-file) `OrderManager` + `DecisionJournal`, `data/market_screener.py`'s
filtering/ranking/parsing (`passes_criteria`, `rank_markets`, `_parse_market`
against malformed/missing fields) plus `GammaClient.get_active_events`'s
pagination against a fake HTTP client, `write_tracked_markets`'s
merge/dedupe/never-remove semantics against a real temp file (including
recovery from a malformed existing file), and `config._load_tracked_market_ids`'s
file-present/missing/malformed/empty-entries fallback behavior.
`data/market_screener.py` itself was also smoke-tested against the real
Gamma API while building it (not part of the automated suite, since it
depends on live market data and this sandbox's access to Polymarket's API
has been inconsistent - working in some runs, `Connection reset by peer` in
others, sometimes within the same session).

## Status

`data/`, `signals/`, `execution/`, `backtest/` (including `optimize.py`),
`main.py`, and `scripts/cli.py` are implemented and tested. Documented
placeholders/gaps to revisit before trading live:
- `ComplementaryOutcomesSignal`'s fee model (`taker_fee_bps`, a flat rate per
  leg's notional) should be replaced with Polymarket's real taker fee schedule.
- `signals/news/feed.py`'s `RssNewsFeed` is unimplemented - wire up an actual
  RSS parser or news API client before running the news signal against
  anything but `MockNewsFeed`. Neither the backtest engine nor `main.py` has
  a historical/live headline feed either, so `NewsEdgeSignal` isn't included
  by default in either and never fires unless you wire one up yourself.
- No real fill feed is wired up anywhere yet (e.g. the CLOB WS user channel)
  - `MarketMakingStrategy.record_fill()` and `OrderManager`'s exposure
  tracker are never called from real fills, only from backtest's simulated
  crossing model. Consequently `scripts/cli.py positions`/`pnl` are
  *simulated* even in live mode: they treat every `SUBMITTED` decision as an
  immediate fill, which will overstate fills for resting orders that never
  actually got hit. This is the same limitation dry-run mode has by
  construction, just also true once you go live.
- `OrderManager`'s exposure tracking only ever increases (booked at
  submission time); it doesn't release exposure when an order is cancelled
  or expires unfilled, so a bot that cancels and re-quotes heavily will see
  its exposure caps bind well before actual capital at risk does.
- Backtest resolution inference (`infer_resolution`) reads the market's
  *latest known* `outcome_prices` from `market_metadata` - if data collection
  stopped before a market resolved, its positions are marked-to-market at the
  last observed price instead of settled, which will understate P&L for
  strategies holding through resolution.
- `MAX_DAILY_LOSS_USD` is defined in config but nothing reads it yet -
  there's no realized-P&L circuit breaker, only the position/order/portfolio
  exposure caps `OrderManager` already enforces.
- The market-making fill model is a simple one-step-lookahead crossing check
  against the next snapshot, not a real matching engine - it ignores queue
  position, partial fills, and latency, so its backtest P&L is optimistic
  relative to live performance.
