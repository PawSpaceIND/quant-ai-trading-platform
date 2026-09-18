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
    totals = {"rows": 0, "rejected": 0, "without_isin": 0, "files": 0}
    reasons: dict = {}
    series_totals: dict = {}

    def tally(parsed) -> None:
        totals["files"] += 1
        totals["rows"] += parsed.usable
        totals["rejected"] += len(parsed.rejected)
        totals["without_isin"] += parsed.rows_without_isin
        for item in parsed.rejected:
            # Group by the reason's shape, not its text: every message names the symbol,
            # so keying on the raw string would make every rejection look unique.
            kind = item.reason.split(":")[-1].strip()
            reasons.setdefault(kind, []).append(f"{parsed.trading_day} {item.symbol}")
        for name, count in parsed.series_counts().items():
            series_totals[name] = series_totals.get(name, 0) + count

    for path in paths[: args.limit]:
        try:
            parsed = read_bhavcopy(path, exchange=args.exchange)
        except (BhavcopyFormatError, OSError, ValueError) as error:
            failures += 1
            print(f"{path.name}: COULD NOT PARSE -> {error}\n")
            continue
        tally(parsed)
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
        print(f"(detail limited to {args.limit}; parsing the remaining "
              f"{len(paths) - args.limit} for the totals below)")
        for path in paths[args.limit:]:
            try:
                tally(read_bhavcopy(path, exchange=args.exchange))
            except (BhavcopyFormatError, OSError, ValueError) as error:
                failures += 1
                print(f"  {path.name}: {error}", file=sys.stderr)

    print(f"\n===== across {totals['files']} files =====")
    print(f"  usable rows     {totals['rows']}")
    share = (totals["rejected"] / (totals["rows"] + totals["rejected"]) * 100
             if totals["rows"] + totals["rejected"] else 0.0)
    print(f"  rejected        {totals['rejected']}  ({share:.3f}% of rows read)")
    print(f"  without ISIN    {totals['without_isin']}")
    if reasons:
        # The shape of the rejections is what says whether this is ordinary data noise or a
        # parser reading the wrong column: noise is scattered, a mapping error clusters.
        print("\n  rejection reasons, most common first:")
        for kind, where in sorted(reasons.items(), key=lambda item: -len(item[1])):
            print(f"    {len(where):>5}  {kind}")
            for one in where[:5]:
                print(f"             {one}")
            if len(where) > 5:
                print(f"             ... and {len(where) - 5} more")
    kept = sorted(series_totals.items(), key=lambda item: -item[1])
    print("\n  series          " + ", ".join(f"{n}={c}" for n, c in kept[:10]))

    if failures:
        print(f"\n{failures} of {len(paths)} files could not be parsed.", file=sys.stderr)
        return 1
    print(f"\nall {len(paths)} files parsed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
