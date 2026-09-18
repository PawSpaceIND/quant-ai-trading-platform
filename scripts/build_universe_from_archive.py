"""Turn a downloaded bhavcopy archive into a point-in-time universe and study datasets.

    python scripts/build_universe_from_archive.py --archive var/bhavcopy/nse \
        --out-dir var/study-inputs --source "NSE bhavcopy archive 2015-2025"

Writes ``universe.json`` and one replay dataset per instrument, which is exactly what
``run_universe_study(datasets, manifest, register=...)`` reads. Prints the survivorship
audit and the corporate-action reconciliation, which are the two questions that decide
whether the data is fit to study:

*Survivorship* — does the universe remember the companies that failed? A ``plausible``
verdict means it does.

*Corporate actions* — bhavcopy prices are raw, so a split reads as a crash. The
reconciliation counts how many price discontinuities the archive cannot account for on its
own. That number is the input to a purchasing decision: near zero and no corporate-actions
vendor is needed at all.

A gap in the archive is checked for before anything is built. A run of missing days would
otherwise register as every security in the universe ceasing to trade at once, and a
universe reconstructed from a half-finished download is worse than none.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant_ai.marketdata.action_reconciliation import reconcile
from quant_ai.marketdata.bhavcopy import (
    BSE_NORMAL_GROUPS,
    NSE_NORMAL_SERIES,
    BhavcopyFormatError,
    read_bhavcopy,
)
from quant_ai.marketdata.listing_reconstruction import (
    DEFAULT_ACTIVE_TAIL_SESSIONS,
    reconstruct_universe,
    write_study_inputs,
)

DEFAULT_MAX_SESSION_GAP_DAYS = 12

#: Files between progress lines while reading the archive.
PROGRESS_EVERY = 250


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True,
                        help="directory of bhavcopy files written by fetch_bhavcopy_archive")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--source", required=True,
                        help="where this archive came from; the manifest refuses to omit it")
    parser.add_argument("--exchange", choices=("NSE", "BSE"), default="NSE",
                        help="names the venue for UDiFF files, which are shaped identically")
    parser.add_argument("--series", nargs="*", default=None,
                        help="series/groups to keep; default is normal rolling settlement "
                             "only, because the cost and impact models assume it")
    parser.add_argument("--active-tail-sessions", type=int,
                        default=DEFAULT_ACTIVE_TAIL_SESSIONS,
                        help="a security trading in any of the final N sessions is treated "
                             "as still listed")
    parser.add_argument("--max-session-gap-days", type=int,
                        default=DEFAULT_MAX_SESSION_GAP_DAYS,
                        help="refuse an archive with a hole longer than this; 0 disables")
    parser.add_argument("--min-sessions", type=int, default=1,
                        help="drop securities with fewer sessions; raising this is itself a "
                             "survivorship filter, so it defaults to keeping everything")
    args = parser.parse_args(argv)

    paths = sorted(
        path for path in args.archive.iterdir()
        if path.suffix.lower() in {".zip", ".csv"} and not path.name.endswith(".part")
    )
    if not paths:
        parser.error(f"no bhavcopy files in {args.archive}")

    files, unreadable = [], []
    try:
        for index, path in enumerate(paths, start=1):
            try:
                files.append(read_bhavcopy(path, exchange=args.exchange))
            except (BhavcopyFormatError, OSError, ValueError) as error:
                unreadable.append((path.name, str(error)))
            if index % PROGRESS_EVERY == 0:
                # Reading 2,500 files takes minutes with nothing on screen, which is
                # indistinguishable from a hang. It also makes the point at which someone
                # interrupts legible afterwards.
                print(f"  read {index}/{len(paths)} files", file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        # Stopping a multi-minute read is a normal thing to do, not a crash. A traceback
        # through the CSV parser says nothing; the count reached and the fact that nothing
        # was written do.
        print(
            f"\nstopped after reading {len(files)} of {len(paths)} files. Nothing was "
            "written, so no partial universe was left behind - re-run to start again.",
            file=sys.stderr,
        )
        return 130
    if unreadable:
        for name, why in unreadable[:20]:
            print(f"  unreadable {name}: {why}", file=sys.stderr)
        print(f"{len(unreadable)} of {len(paths)} files could not be read. Fix or remove "
              "them before building; a universe missing days is not a universe.",
              file=sys.stderr)
        return 1

    keep = args.series or (
        list(NSE_NORMAL_SERIES) if args.exchange == "NSE" else list(BSE_NORMAL_GROUPS)
    )
    files = [item.filter_series(keep) for item in files]
    files = [item for item in files if item.rows]
    if not files:
        parser.error(f"no rows left after keeping series {keep}")

    days = sorted({item.trading_day for item in files})
    if args.max_session_gap_days > 0:
        holes = [
            (days[index - 1], days[index])
            for index in range(1, len(days))
            if (days[index] - days[index - 1]) > timedelta(days=args.max_session_gap_days)
        ]
        if holes:
            for before, after in holes[:10]:
                print(f"  gap: nothing between {before} and {after}", file=sys.stderr)
            print(f"{len(holes)} gaps longer than {args.max_session_gap_days} days. Every "
                  "security goes quiet across a hole, so a reconstruction would record mass "
                  "delistings that never happened. Finish the download first.", file=sys.stderr)
            return 1

    rejected = sum(len(item.rejected) for item in files)
    result = reconstruct_universe(
        files,
        source=args.source,
        active_tail_sessions=args.active_tail_sessions,
        minimum_sessions=args.min_sessions,
    )
    summary = write_study_inputs(result, args.out_dir)
    audit = result.universe.audit()
    reconciliation = reconcile(result.histories)

    report = {
        "archive": {
            "files": len(files),
            "sessions": len(days),
            "first_session": days[0].isoformat(),
            "last_session": days[-1].isoformat(),
            "series_kept": keep,
            "rows_rejected": rejected,
        },
        "reconstruction": result.report.as_evidence(),
        "survivorship_audit": audit.as_evidence(),
        "corporate_actions": reconciliation.as_evidence(),
        "written": summary,
    }
    (args.out_dir / "archive_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2, sort_keys=True))

    print(
        f"\nuniverse: {audit.listings} instruments, {audit.delisted} ceased trading, "
        f"{audit.annual_delisting_rate:.2%} a year -> {audit.verdict}", file=sys.stderr
    )
    print(
        f"corporate actions: {reconciliation.detected} discontinuities, "
        f"{reconciliation.self_adjustable} adjustable from the archive itself, "
        f"{reconciliation.unsignalled_gaps} the venue never signalled "
        f"-> {reconciliation.verdict}",
        file=sys.stderr,
    )
    if reconciliation.unsignalled_gaps:
        print(
            f"{reconciliation.unsignalled_gaps} unsignalled breaks is the number that "
            "decides whether corporate-action data has to be bought. Inspect "
            "worst_unexplained in the report before paying anyone.", file=sys.stderr
        )
    if not audit.usable_for_research:
        print("\nThis universe is not fit to study. " + " ".join(audit.reasons), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
