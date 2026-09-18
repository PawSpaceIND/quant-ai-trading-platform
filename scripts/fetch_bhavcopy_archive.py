"""Download NSE and BSE daily bhavcopy archives, one file per trading day.

Each bhavcopy is the exchange's own record of everything that traded on one day, and the
files are never rewritten. An archive of them is therefore a genuine point-in-time dataset
including every company that has since been delisted — the thing a survivorship-free
backtest needs and that no free symbol-list API can provide.

    python scripts/fetch_bhavcopy_archive.py --exchange NSE --from 2015-01-01 \
        --to 2025-01-01 --out-dir var/bhavcopy/nse

The download is resumable: a day already on disk is never refetched, so an interrupted run
continues where it stopped. Requests are rate-limited and sent with browser-like headers
because the exchanges reject bare scrapers.

**Missing days are reported, not swallowed.** A market holiday and a failed download look
identical from here — both are simply an absent file — so the two are counted separately
and the summary states each. An archive with download failures in it must not be built into
a universe: a contiguous block of missing days at the end would make every live security
appear to have stopped trading. ``build_universe_from_archive.py`` checks for that, but the
place to notice it is here.

The URL patterns below are the ones the exchanges have published at; they do change. A day
that 404s on every pattern is reported as missing rather than retried forever, and the
summary makes a systematic pattern change obvious — it shows up as every day failing.
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")

NSE_HOME = "https://www.nseindia.com"
BSE_HOME = "https://www.bseindia.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def nse_urls(day: date) -> list:
    month = MONTHS[day.month - 1]
    stamp = day.strftime("%Y%m%d")
    archives = "https://nsearchives.nseindia.com"
    return [
        # UDiFF, from mid-2024.
        f"{archives}/content/cm/BhavCopy_NSE_CM_0_0_0_{stamp}_F_0000.csv.zip",
        # Legacy, until mid-2024.
        (
            f"{archives}/content/historical/EQUITIES/{day.year}/{month}"
            f"/cm{day.day:02d}{month}{day.year}bhav.csv.zip"
        ),
    ]


def bse_urls(day: date) -> list:
    stamp = day.strftime("%Y%m%d")
    downloads = "https://www.bseindia.com/download/BhavCopy/Equity"
    return [
        f"{downloads}/BhavCopy_BSE_CM_0_0_0_{stamp}_F_0000.CSV",
        f"{downloads}/EQ{day.strftime('%d%m%y')}_CSV.ZIP",
    ]


def opener_for(exchange: str):
    """An opener carrying the cookies the exchange sets on its home page.

    Both venues reject archive requests that arrive without a session cookie and a
    plausible referer, which is why this is not a plain urlretrieve.
    """
    handler = urllib.request.HTTPCookieProcessor()
    opener = urllib.request.build_opener(handler)
    home = NSE_HOME if exchange == "NSE" else BSE_HOME
    opener.addheaders = list(HEADERS.items()) + [("Referer", home)]
    try:
        opener.open(home, timeout=30).read(1024)
    except (urllib.error.URLError, TimeoutError) as error:
        print(f"warning: could not reach {home} to establish a session ({error}); "
              "downloads will probably be rejected", file=sys.stderr)
    return opener


def fetch_day(opener, exchange: str, day: date, out_dir: Path, timeout: int):
    """Return ('saved'|'missing'|'failed', detail). Never writes a partial file."""
    stem = f"{exchange}_{day.isoformat()}"
    for existing in (out_dir / f"{stem}.zip", out_dir / f"{stem}.csv"):
        if existing.exists():
            return "cached", existing.name

    urls = nse_urls(day) if exchange == "NSE" else bse_urls(day)
    notfound = 0
    last = ""
    for url in urls:
        try:
            with opener.open(url, timeout=timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            last = f"HTTP {error.code}"
            if error.code in (403, 404):
                notfound += 1
            continue
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last = str(error)
            continue
        if not payload:
            last = "empty response"
            continue
        # The extension has to follow the URL that answered, not the venue: BSE's legacy
        # endpoint serves a ZIP, and read_bhavcopy dispatches on the suffix, so saving a
        # zip as .csv would make every one of those days unreadable later.
        suffix = ".zip" if url.lower().endswith(".zip") else ".csv"
        target = out_dir / f"{stem}{suffix}"
        # Written via a temporary name so an interrupted run never leaves a truncated file
        # that the next run would treat as cached.
        staging = target.with_suffix(target.suffix + ".part")
        staging.write_bytes(payload)
        staging.rename(target)
        return "saved", target.name
    if notfound == len(urls):
        return "missing", "no file at any known URL (market holiday, or the pattern changed)"
    return "failed", last


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exchange", choices=("NSE", "BSE"), required=True)
    parser.add_argument("--from", dest="start", required=True, help="ISO date, inclusive")
    parser.add_argument("--to", dest="end", required=True, help="ISO date, inclusive")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--requests-per-second", type=float, default=2.0,
                        help="be polite; the exchanges throttle aggressively")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args(argv)

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if end < start:
        parser.error("--to must not precede --from")
    args.out_dir.mkdir(parents=True, exist_ok=True)

    opener = opener_for(args.exchange)
    delay = 1.0 / args.requests_per_second if args.requests_per_second > 0 else 0.0

    saved = cached = missing = failed = 0
    failures = []
    day = start
    while day <= end:
        if day.weekday() >= 5:  # The exchanges do not settle at weekends.
            day += timedelta(days=1)
            continue
        status, detail = fetch_day(opener, args.exchange, day, args.out_dir, args.timeout)
        if status == "saved":
            saved += 1
            if delay:
                time.sleep(delay)
        elif status == "cached":
            cached += 1
        elif status == "missing":
            missing += 1
        else:
            failed += 1
            failures.append((day, detail))
            if delay:
                time.sleep(delay)
        day += timedelta(days=1)

    print(f"saved {saved}, already had {cached}, absent {missing}, failed {failed}")
    if missing and not saved and not cached:
        print("every day was absent: the URL patterns in this script are almost certainly "
              "out of date, not the market closed for years", file=sys.stderr)
    for when, why in failures[:20]:
        print(f"  failed {when}: {why}", file=sys.stderr)
    if failed:
        print(f"{failed} days failed to download. Re-run to retry them; do NOT build a "
              "universe until this is zero, because missing days at the end of the archive "
              "make live securities look as though they stopped trading.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
