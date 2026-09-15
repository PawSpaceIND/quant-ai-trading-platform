"""Fetch many years of closed daily bars and write replay datasets, one file per symbol.

Read-only. Output loads through ``quant_ai.backtesting.replay.load_replay_dataset``.
Prices are Yahoo's raw quote series; ``adjclose`` is never read. Every file states that in
its own ``provenance`` block - see ``quant_ai.backtesting.history`` for why.

    python scripts/fetch_historical_bars.py --years 10 --out-dir var/replay-datasets

With no ``--symbols`` the pilot watchlist is read from deploy/founder-directives.example.json
so this tool and the pilot can never disagree about what is being traded. Chunks are
cached under ``--cache-dir`` and reused, so an interrupted run resumes; a chunk that fails
aborts that symbol rather than writing a short series that looks complete.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant_ai.backtesting.history import (  # noqa: E402
    DEFAULT_CHUNK_DAYS,
    DEFAULT_REQUESTS_PER_SECOND,
    DEFAULT_YEARS,
    NIFTY_50,
    HistoryFetchError,
    build_datasets,
    default_fetcher,
    watchlist_instruments,
    write_dataset,
)
from quant_ai.domain.models import AssetClass, Instrument, Market  # noqa: E402

DAYS_PER_YEAR = 365


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=None,
                        help="NSE symbols, asset class assumed EQUITY; default is the "
                             "founder-directives watchlist, which carries the real one")
    parser.add_argument("--directives", type=Path, default=None,
                        help="founder directives JSON the default watchlist is read from")
    parser.add_argument("--years", type=int, default=DEFAULT_YEARS)
    parser.add_argument("--end", default=None, help="ISO end instant; default is now (UTC)")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, default=None,
                        help="chunk cache for resume; default <out-dir>/.chunk-cache")
    parser.add_argument("--chunk-days", type=int, default=DEFAULT_CHUNK_DAYS)
    parser.add_argument("--requests-per-second", type=float, default=DEFAULT_REQUESTS_PER_SECOND)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.years < 1:
        print("--years must be at least one", file=sys.stderr)
        return 2
    end = datetime.fromisoformat(args.end) if args.end else datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    start = end - timedelta(days=args.years * DAYS_PER_YEAR)
    if args.symbols:
        instruments = tuple(
            Instrument(symbol.strip().upper(), Market.INDIA, AssetClass.EQUITY, "INR", "NSE")
            for symbol in args.symbols
        )
    else:
        instruments = watchlist_instruments(args.directives)
    cache_dir = args.cache_dir or args.out_dir / ".chunk-cache"
    fetcher = default_fetcher(
        chunk_days=args.chunk_days,
        cache_dir=cache_dir,
        requests_per_second=args.requests_per_second,
    )
    written: list[dict[str, object]] = []
    try:
        for instrument, payload in build_datasets(
            fetcher,
            instruments,
            benchmark=NIFTY_50,
            start=start,
            end=end,
            years=args.years,
            fetched_at=end,
        ):
            path = write_dataset(args.out_dir / f"{instrument.symbol}.json", payload)
            provenance = payload["provenance"]
            written.append({
                "symbol": instrument.symbol,
                "path": str(path),
                "bars": provenance["bar_count"],
                "firstBar": provenance["first_bar"],
                "lastBar": provenance["last_bar"],
                "missingSessions": provenance["missing_sessions"]["count"],
            })
    except (HistoryFetchError, OSError, TypeError, ValueError) as exc:
        # Loud and partial, never quiet and short: the finished symbols stand, the failing
        # one has no file, and the cache means a rerun refetches only what is missing.
        print(json.dumps({"status": "incomplete", "written": written, "error": str(exc)}),
              file=sys.stderr)
        return 2
    print(json.dumps({
        "status": "complete",
        "benchmark": NIFTY_50.name,
        "priceSeries": "raw_quote_unadjusted_close",
        "written": written,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
