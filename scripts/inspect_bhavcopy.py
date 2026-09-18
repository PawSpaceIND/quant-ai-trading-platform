"""Report what a downloaded bhavcopy actually contains, before building anything from it.

    python3 scripts/inspect_bhavcopy.py /tmp/probe

The parsers were written against the published formats and exercised on fixtures. This
prints what they make of real exchange bytes: the format detected, how many rows survived,
how many were rejected and why, ISIN coverage, and the series breakdown. Read it once
against a few days before starting a ten-year download, because every one of those numbers
is load-bearing later and a silent mismatch is expensive to discover after the fact.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant_ai.marketdata.bhavcopy import BhavcopyFormatError, read_bhavcopy


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="directory of downloaded bhavcopy files")
    parser.add_argument("--exchange", choices=("NSE", "BSE"), default="NSE")
    parser.add_argument("--limit", type=int, default=5, help="files to report in detail")
    args = parser.parse_args(argv)

    paths = sorted(
        path for path in args.archive.iterdir()
        if path.suffix.lower() in {".zip", ".csv"} and not path.name.endswith(".part")
    )
    if not paths:
        parser.error(f"no bhavcopy files in {args.archive}")
    print(f"{len(paths)} files in {args.archive}\n")

    failures = 0
    for path in paths[: args.limit]:
        try:
            parsed = read_bhavcopy(path, exchange=args.exchange)
        except (BhavcopyFormatError, OSError, ValueError) as error:
            failures += 1
            print(f"{path.name}: COULD NOT PARSE -> {error}\n")
            continue
        series = sorted(parsed.series_counts().items(), key=lambda item: -item[1])
        print(f"{path.name}")
        print(f"  format        {parsed.source_format}   exchange {parsed.exchange}")
        print(f"  trading day   {parsed.trading_day}")
        print(f"  usable rows   {parsed.usable}")
        print(f"  rejected      {len(parsed.rejected)}")
        for row in parsed.rejected[:3]:
            print(f"      line {row.line} {row.symbol}: {row.reason}")
        print(f"  without ISIN  {parsed.rows_without_isin}"
              + ("   <- renames in this group become phantom delistings"
                 if parsed.rows_without_isin else "   (good: renames stay continuous)"))
        print("  series        " + ", ".join(f"{name}={count}" for name, count in series[:8]))
        sample = parsed.rows[0]
        print(f"  sample        {sample.symbol} {sample.series} close={sample.close} "
              f"prev={sample.previous_close} vol={sample.volume} isin={sample.isin or '-'}")
        print()

    if len(paths) > args.limit:
        print(f"(detail limited to {args.limit}; parsing all {len(paths)} to count failures)")
        for path in paths[args.limit:]:
            try:
                read_bhavcopy(path, exchange=args.exchange)
            except (BhavcopyFormatError, OSError, ValueError) as error:
                failures += 1
                print(f"  {path.name}: {error}", file=sys.stderr)

    if failures:
        print(f"\n{failures} of {len(paths)} files could not be parsed.", file=sys.stderr)
        return 1
    print(f"all {len(paths)} files parsed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
