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
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

#: Trading days between progress lines. Frequent enough that a stall shows up within a
#: minute or two of starting, sparse enough that the log stays readable over ten years.
PROGRESS_EVERY = 25

MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")

NSE_HOME = "https://www.nseindia.com"
BSE_HOME = "https://www.bseindia.com"

def _ssl_context() -> ssl.SSLContext:
    """A verifying context that also works on a Python with no usable CA store.

    macOS Python installs from python.org do not read the system keychain, so every HTTPS
    request fails with CERTIFICATE_VERIFY_FAILED until someone runs an installer script
    most people have never heard of. ``certifi`` ships the same CA bundle the rest of the
    world uses, so it is preferred when present.

    Verification is never disabled. A download that cannot be authenticated is not a
    download worth having: the whole point of this archive is that it is the exchange's
    record, and an unverified connection cannot say who it came from.
    """
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


#: Both venues changed over to the UDiFF format around here. The date only orders which
#: pattern is tried first — the other is still tried — so being a few weeks out costs one
#: extra request on a handful of days rather than losing them.
UDIFF_CHANGEOVER = date(2024, 7, 1)


def _by_era(day: date, udiff: str, legacy: str) -> list:
    """Try the pattern that era actually used first.

    Always trying the newer URL first would spend a wasted 404 on every day before the
    changeover. Over a ten-year archive that is thousands of pointless requests, which
    doubles the run time and, more to the point, doubles the reasons for the exchange to
    start refusing us.
    """
    return [udiff, legacy] if day >= UDIFF_CHANGEOVER else [legacy, udiff]


def nse_urls(day: date) -> list:
    month = MONTHS[day.month - 1]
    stamp = day.strftime("%Y%m%d")
    archives = "https://nsearchives.nseindia.com"
    return _by_era(
        day,
        f"{archives}/content/cm/BhavCopy_NSE_CM_0_0_0_{stamp}_F_0000.csv.zip",
        (
            f"{archives}/content/historical/EQUITIES/{day.year}/{month}"
            f"/cm{day.day:02d}{month}{day.year}bhav.csv.zip"
        ),
    )


def bse_urls(day: date) -> list:
    stamp = day.strftime("%Y%m%d")
    downloads = "https://www.bseindia.com/download/BhavCopy/Equity"
    return _by_era(
        day,
        f"{downloads}/BhavCopy_BSE_CM_0_0_0_{stamp}_F_0000.CSV",
        f"{downloads}/EQ{day.strftime('%d%m%y')}_CSV.ZIP",
    )


def opener_for(exchange: str):
    """An opener carrying the cookies the exchange sets on its home page.

    Both venues reject archive requests that arrive without a session cookie and a
    plausible referer, which is why this is not a plain urlretrieve.
    """
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(),
        urllib.request.HTTPSHandler(context=_ssl_context()),
    )
    home = NSE_HOME if exchange == "NSE" else BSE_HOME
    opener.addheaders = list(HEADERS.items()) + [("Referer", home)]
    try:
        opener.open(home, timeout=30).read(1024)
    except (urllib.error.URLError, TimeoutError) as error:
        # Not fatal, and not a prediction of failure: the archive hosts are separate from
        # the home page and have been observed serving files normally while the home page
        # answers 403 to a datacentre IP. Say what happened and let the download report
        # its own result.
        print(f"note: {home} did not set a session cookie ({error}). The archive host is "
              "separate and often serves anyway - the per-day counts below are the real "
              "answer.", file=sys.stderr)
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
    parser.add_argument("--timeout", type=int, default=30,
                        help="seconds per request; a throttled exchange hangs rather than "
                             "refusing, so a long timeout turns a rate limit into a stall")
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
    interrupted = None
    considered = 0
    started_at = time.monotonic()
    try:
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
            considered += 1
            if considered % PROGRESS_EVERY == 0:
                # A run of this length with no output cannot be told from a hung one. The
                # line carries the day reached and the elapsed time so a stall is obvious
                # from the log rather than only from counting files on disk.
                elapsed = time.monotonic() - started_at
                rate = saved / elapsed if elapsed > 0 else 0.0
                print(
                    f"  {day.isoformat()}: saved {saved}, absent {missing}, failed {failed}"
                    f" ({rate * 60:.0f}/min)",
                    file=sys.stderr, flush=True,
                )
            day += timedelta(days=1)
    except KeyboardInterrupt:
        # Stopping a download that takes hours is a normal thing to do, not a crash. The
        # counts so far and how to resume are what the person needs; a stack trace through
        # the socket layer is not.
        interrupted = day

    print(f"saved {saved}, already had {cached}, absent {missing}, failed {failed}")
    if interrupted is not None:
        print(
            f"\nstopped at {interrupted}. Nothing is lost - every completed day is on disk "
            "and the same command resumes from here.", file=sys.stderr
        )
    if failed and any("CERTIFICATE_VERIFY" in why for _, why in failures):
        print(
            "\nThese are certificate failures, not exchange refusals: this Python has no "
            "usable CA store. Install the bundle and re-run - the download resumes where it "
            "stopped:\n    python3 -m pip install --user certifi",
            file=sys.stderr,
        )
    if missing and not saved and not cached:
        print("every day was absent: the URL patterns in this script are almost certainly "
              "out of date, not the market closed for years", file=sys.stderr)
    for when, why in failures[:20]:
        print(f"  failed {when}: {why}", file=sys.stderr)
    if interrupted is not None:
        return 130
    if failed:
        print(f"{failed} days failed to download. Re-run to retry them; do NOT build a "
              "universe until this is zero, because missing days at the end of the archive "
              "make live securities look as though they stopped trading.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
