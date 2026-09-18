"""The exchange-archive path: parse, reconstruct a universe, reconcile corporate actions."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from quant_ai.marketdata.action_reconciliation import (
    CorporateActionRecord,
    PriceDiscontinuity,
    back_adjust,
    reconcile,
)
from quant_ai.marketdata.bhavcopy import (
    NSE_NORMAL_SERIES,
    NSE_SAME_DAY_SETTLEMENT_SERIES,
    BhavcopyFormatError,
    parse_bhavcopy,
    read_bhavcopy,
)
from quant_ai.marketdata.listing_reconstruction import (
    reconstruct_universe,
    write_study_inputs,
)
from quant_ai.marketdata.universe_manifest import load_universe_manifest

NSE_HEADER = (
    "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,"
    "TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN,"
)
BSE_HEADER = (
    "SC_CODE,SC_NAME,SC_GROUP,SC_TYPE,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,"
    "NO_TRADES,NO_OF_SHRS,NET_TURNOV,TDCLOINDI,ISIN_CODE,TRADING_DATE,FILLER2,FILLER3"
)
UDIFF_HEADER = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,"
    "FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,"
    "LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,"
    "TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4"
)

MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")

BSE_EQUITY_ROW = (
    "500209,INFOSYS,A,Q,720.00,735.50,718.10,732.40,732.00,719.80,"
    "52000,4521000,3300000000.00,,INE009A01021,,,"
)
# A debenture sharing the file. Not an equity, and not an error either.
BSE_DEBENTURE_ROW = (
    "961234,SOMEBOND,F,D,100.00,100.00,100.00,100.00,100.00,100.00,"
    "10,100,10000.00,,INE111B07011,,,"
)
UDIFF_EQUITY_ROW = (
    "2024-07-22,2024-07-22,CM,NSE,STK,101,INE009A01021,INFY,EQ,,,,,Infosys Ltd,"
    "1600.00,1625.00,1590.00,1610.00,1610.00,1595.00,,,,,4500000,7245000000.00,"
    "52000,F1,1,,,,,"
)
# A dated contract in the same file: an expiry makes it not a cash equity.
UDIFF_FUTURE_ROW = (
    "2024-07-22,2024-07-22,FO,NSE,STF,202,INE009A01021,INFY,EQ,2024-08-29,"
    "2024-08-29,,,Infosys Fut,1600.00,1625.00,1590.00,1610.00,1610.00,1595.00,"
    ",,,,4500000,7245000000.00,52000,F1,1,,,,,"
)


def nse_row(symbol, day, close, previous, *, series="EQ", isin="", volume=500000):
    stamp = f"{day.day:02d}-{MONTHS[day.month - 1]}-{day.year}"
    low = min(close, previous) * Decimal("0.99")
    high = max(close, previous) * Decimal("1.01")
    return (
        f"{symbol},{series},{close},{high},{low},{close},{close},{previous},"
        f"{volume},{volume * close},{stamp},5000,{isin},"
    )


def nse_file(day, rows):
    return "\n".join([NSE_HEADER, *rows]) + "\n"


def test_nse_legacy_parses_and_rejects_a_suspended_scrip_rather_than_inventing_a_bar():
    day = date(2019, 7, 1)
    text = nse_file(day, [
        nse_row("INFY", day, Decimal("732.40"), Decimal("719.80"), isin="INE009A01021"),
        # A suspended scrip the exchange prints at zero. It is not a bar.
        "DEADCO,EQ,0.00,0.00,0.00,0.00,0.00,1.05,0,0.00,01-JUL-2019,0,INE999Z01011,",
    ])
    parsed = parse_bhavcopy(text)

    assert parsed.source_format == "nse-legacy"
    assert parsed.exchange == "NSE"
    assert parsed.trading_day == day
    assert parsed.usable == 1
    assert [item.symbol for item in parsed.rejected] == ["DEADCO"]
    assert "positive" in parsed.rejected[0].reason
    assert parsed.rows[0].key == "INE009A01021"
    assert parsed.rows[0].previous_close == Decimal("719.80")


def test_month_names_parse_without_depending_on_the_process_locale():
    # strptime's %b reads month names through the locale; a container set to anything but
    # English would silently fail on every file. The month table must not care.
    for index, name in enumerate(MONTHS, start=1):
        day = date(2019, index, 5)
        text = nse_file(day, [nse_row("INFY", day, Decimal(100), Decimal(100), isin="X")])
        assert parse_bhavcopy(text).trading_day == date(2019, index, 5)
        assert name in text


def test_a_file_spanning_two_trading_days_is_refused():
    text = "\n".join([
        NSE_HEADER,
        nse_row("A", date(2019, 7, 1), Decimal(10), Decimal(10), isin="I1"),
        nse_row("B", date(2019, 7, 2), Decimal(10), Decimal(10), isin="I2"),
    ])
    with pytest.raises(BhavcopyFormatError, match="one trading day"):
        parse_bhavcopy(text)


def test_a_file_whose_date_contradicts_its_filename_is_refused():
    day = date(2019, 7, 1)
    text = nse_file(day, [nse_row("INFY", day, Decimal(100), Decimal(100), isin="I")])
    with pytest.raises(BhavcopyFormatError, match="misfiled"):
        parse_bhavcopy(text, trading_day=date(2019, 7, 2))


def test_an_unrecognised_header_is_refused_rather_than_guessed_at():
    with pytest.raises(BhavcopyFormatError, match="unrecognised bhavcopy header"):
        parse_bhavcopy("date,ticker,price\n2019-07-01,INFY,100\n")


def test_bse_legacy_filters_non_equity_and_takes_its_day_from_the_filename():
    text = f"{BSE_HEADER}\n{BSE_EQUITY_ROW}\n{BSE_DEBENTURE_ROW}\n"
    parsed = parse_bhavcopy(text, trading_day=date(2019, 7, 1))

    assert parsed.source_format == "bse-legacy"
    assert parsed.exchange == "BSE"
    assert parsed.trading_day == date(2019, 7, 1)
    assert [item.symbol for item in parsed.rows] == ["500209"]
    assert parsed.rows[0].security_name == "INFOSYS"


def test_bse_vintage_without_a_date_column_and_without_a_filename_day_says_so():
    text = f"{BSE_HEADER}\n{BSE_EQUITY_ROW}\n"
    with pytest.raises(BhavcopyFormatError, match="no usable rows"):
        parse_bhavcopy(text)


def test_udiff_parses_both_venues_and_skips_dated_contracts():
    text = f"{UDIFF_HEADER}\n{UDIFF_EQUITY_ROW}\n{UDIFF_FUTURE_ROW}\n"
    parsed = parse_bhavcopy(text, exchange="BSE")

    assert parsed.source_format == "udiff"
    assert parsed.exchange == "BSE"
    assert [item.symbol for item in parsed.rows] == ["INFY"]
    assert parsed.rows[0].previous_close == Decimal("1595.00")


def test_reading_a_zipped_bhavcopy_matches_reading_the_csv(tmp_path):
    import zipfile

    day = date(2019, 7, 1)
    text = nse_file(day, [nse_row("INFY", day, Decimal(100), Decimal(100), isin="I")])
    plain = tmp_path / "cm01JUL2019bhav.csv"
    plain.write_text(text, encoding="utf-8")
    archive = tmp_path / "cm01JUL2019bhav.csv.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("cm01JUL2019bhav.csv", text)

    assert read_bhavcopy(archive).rows == read_bhavcopy(plain).rows


# --- reconstruction -------------------------------------------------------------------

def sessions(count, start=date(2020, 1, 1)):
    """Weekday sessions, which is close enough to a trading calendar for these tests."""
    days, cursor = [], start
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def archive(spec, days):
    """Build one parsed file per day from {symbol: (isin, first_index, last_index)}."""
    files = []
    for index, day in enumerate(days):
        rows = []
        for symbol, (isin, first, last) in spec.items():
            if first <= index <= last:
                rows.append(nse_row(symbol, day, Decimal(100), Decimal(100), isin=isin))
        if rows:
            files.append(parse_bhavcopy(nse_file(day, rows)))
    return files


def test_a_name_that_stops_trading_becomes_a_delisting_and_the_survivor_does_not():
    days = sessions(120)
    files = archive({
        "ALIVE": ("INE001A01011", 0, 119),
        "GONE": ("INE002A01011", 0, 59),
    }, days)

    result = reconstruct_universe(files, source="NSE bhavcopy archive, test fixture")

    listings = {item.symbol: item for item in result.universe.listings}
    assert listings["ALIVE"].delisted_on is None
    assert listings["GONE"].delisted_on == days[59] + timedelta(days=1)
    # The final session it traded on is still tradeable; the day after is not.
    assert result.universe.was_tradeable("GONE", days[59])
    assert not result.universe.was_tradeable("GONE", days[60])
    assert result.report.ceased == 1
    assert result.report.still_listed == 1


def test_a_name_quiet_only_inside_the_active_tail_is_not_called_dead():
    days = sessions(120)
    # ANCHOR keeps the archive spanning all 120 sessions. Without it the file for a day
    # nothing traded on is simply absent, the observed calendar would stop at THIN's last
    # session, and the tail rule would never be exercised at all.
    files = archive({
        "ANCHOR": ("INE003A01011", 0, 119),
        "THIN": ("INE013A01011", 0, 109),  # quiet for the last 10 sessions
    }, days)

    result = reconstruct_universe(files, source="NSE bhavcopy archive, test fixture")

    listings = {item.symbol: item for item in result.universe.listings}
    assert listings["THIN"].delisted_on is None
    assert result.report.ceased == 0
    assert result.report.sessions_observed == 120

    # And the rule is load-bearing rather than incidental: tighten the tail past THIN's
    # silence and it is called dead.
    tightened = reconstruct_universe(
        files, source="NSE bhavcopy archive, test fixture", active_tail_sessions=5
    )
    assert {item.symbol for item in tightened.universe.listings
            if item.delisted_on is not None} == {"THIN"}


def test_isin_keying_carries_a_renamed_company_through_as_one_continuous_listing():
    days = sessions(120)
    rows_by_day = []
    for index, day in enumerate(days):
        name = "OLDNAME" if index < 60 else "NEWNAME"
        rows_by_day.append(
            parse_bhavcopy(nse_file(day, [
                nse_row(name, day, Decimal(100), Decimal(100), isin="INE004A01011")
            ]))
        )

    result = reconstruct_universe(rows_by_day, source="NSE bhavcopy archive, test fixture")

    assert len(result.universe.listings) == 1
    listing = result.universe.listings[0]
    assert listing.symbol == "NEWNAME"
    assert listing.delisted_on is None
    assert result.histories[0].symbols == ("OLDNAME", "NEWNAME")
    assert result.report.renamed == 1


def test_without_an_isin_the_same_rename_fabricates_a_death_and_a_birth():
    # The counter-test for the one above: this is precisely the damage symbol keying does,
    # and why ISIN keying is a correctness requirement rather than a nicety.
    days = sessions(120)
    files = []
    for index, day in enumerate(days):
        name = "OLDNAME" if index < 60 else "NEWNAME"
        files.append(parse_bhavcopy(nse_file(day, [
            nse_row(name, day, Decimal(100), Decimal(100), isin="")
        ])))

    result = reconstruct_universe(files, source="NSE bhavcopy archive, test fixture")

    assert len(result.universe.listings) == 2
    assert result.report.ceased == 1  # OLDNAME looks delisted, and never was
    assert result.report.rows_without_isin == 2
    assert any("keyed by symbol" in note for note in result.report.caveats)


def test_names_already_trading_on_the_first_session_are_flagged_as_left_censored():
    days = sessions(120)
    files = archive({
        "OLD": ("INE005A01011", 0, 119),
        "NEW": ("INE006A01011", 30, 119),
    }, days)

    result = reconstruct_universe(files, source="NSE bhavcopy archive, test fixture")

    assert result.report.left_censored == 1
    assert any("real listing dates are earlier" in note for note in result.report.caveats)


def test_the_same_security_day_twice_keeps_the_first_and_counts_the_rest():
    days = sessions(40)
    files = archive({"DUP": ("INE007A01011", 0, 39)}, days)

    result = reconstruct_universe(files + files, source="NSE bhavcopy archive, test fixture")

    assert result.histories[0].sessions == 40
    assert result.report.rejected_rows == 40
    assert any("duplicate security-days" in note for note in result.report.caveats)


def test_an_archive_with_real_failures_passes_the_survivorship_audit():
    # 40 names over ~4 years, 6 of which stop trading. That is the shape of a real venue,
    # and it is what the audit is looking for: it refuses a universe with no failures.
    days = sessions(1000)
    spec = {}
    for index in range(40):
        isin = f"INE{index:03d}A01011"
        if index < 6:
            spec[f"GONE{index}"] = (isin, 0, 200 + index * 50)
        else:
            spec[f"ALIVE{index}"] = (isin, 0, 999)
    result = reconstruct_universe(archive(spec, days), source="NSE bhavcopy archive 2020-2023")

    audit = result.universe.audit()
    assert audit.delisted == 6
    assert audit.verdict == "plausible"
    assert audit.usable_for_research
    # And the caveat that this rate is an upper bound is stated, not left to be discovered.
    assert any("upper bound" in note for note in result.report.caveats)


def test_a_survivor_only_archive_still_fails_the_audit_after_reconstruction():
    # Reconstruction does not launder a bad archive. If nothing ever died in the source,
    # the audit must still refuse it.
    days = sessions(1000)
    spec = {f"ALIVE{index}": (f"INE{index:03d}A01011", 0, 999) for index in range(40)}
    result = reconstruct_universe(archive(spec, days), source="survivor-only test archive")

    assert result.universe.audit().verdict == "survivor_only"
    assert not result.universe.audit().usable_for_research


def test_written_study_inputs_load_back_through_the_manifest_loader(tmp_path):
    days = sessions(300)
    files = archive({
        "ALIVE": ("INE008A01011", 0, 299),
        "GONE": ("INE009A01011", 0, 100),
    }, days)
    result = reconstruct_universe(files, source="NSE bhavcopy archive, test fixture")

    summary = write_study_inputs(result, tmp_path)

    universe = load_universe_manifest(summary["manifest"])
    assert {item.symbol for item in universe.listings} == {"ALIVE", "GONE"}
    assert summary["files_written"] == 2

    dataset = json.loads((tmp_path / "datasets" / "NSE_GONE.json").read_text())
    assert dataset["provenance"]["instrument"]["symbol"] == "GONE"
    assert dataset["provenance"]["instrument"]["market"] == "INDIA"
    assert dataset["provenance"]["adjusted"] is False
    assert len(dataset["bars"]) == 101
    # Bars carry the Indian session close, so a study cannot mistake them for UTC midnight.
    assert dataset["bars"][0]["timestamp"].endswith("+00:00")


# --- corporate actions ----------------------------------------------------------------

def split_history(factor=Decimal(10)):
    """Two sessions either side of an ex-date the exchange signalled via PREVCLOSE."""
    days = sessions(4)
    pre = Decimal("1000.00")
    post = pre / factor
    rows = [
        parse_bhavcopy(nse_file(days[0], [
            nse_row("SPLIT", days[0], pre, pre, isin="INE010A01011")])),
        parse_bhavcopy(nse_file(days[1], [
            nse_row("SPLIT", days[1], pre, pre, isin="INE010A01011")])),
        # Ex-date: the exchange restates the previous close on the adjusted basis.
        parse_bhavcopy(nse_file(days[2], [
            nse_row("SPLIT", days[2], post, post, isin="INE010A01011")])),
        parse_bhavcopy(nse_file(days[3], [
            nse_row("SPLIT", days[3], post, post, isin="INE010A01011")])),
    ]
    result = reconstruct_universe(rows, source="NSE bhavcopy archive, test fixture")
    return days, result


def test_a_split_is_recovered_from_the_exchanges_own_restated_previous_close():
    days, result = split_history()

    report = reconcile(result.histories)

    assert report.detected == 1
    found = report.discontinuities[0]
    assert found.detector == "exchange-prevclose"
    assert found.ex_date == days[2]
    assert found.implied_factor == Decimal(10)
    assert report.verdict == "self_adjusting"


def test_a_large_move_the_venue_never_signalled_is_reported_as_an_unexplained_gap():
    days = sessions(3)
    # Price falls 40% with the previous close left agreeing: no action was announced.
    rows = [
        parse_bhavcopy(nse_file(days[0], [
            nse_row("GAPPY", days[0], Decimal("100.00"), Decimal("100.00"), isin="INE011A01011")])),
        parse_bhavcopy(nse_file(days[1], [
            nse_row("GAPPY", days[1], Decimal("60.00"), Decimal("100.00"), isin="INE011A01011")])),
        parse_bhavcopy(nse_file(days[2], [
            nse_row("GAPPY", days[2], Decimal("60.00"), Decimal("60.00"), isin="INE011A01011")])),
    ]
    result = reconstruct_universe(rows, source="NSE bhavcopy archive, test fixture")

    report = reconcile(result.histories)

    assert report.detected == 1
    assert report.discontinuities[0].detector == "unexplained-gap"
    assert report.verdict == "unreconciled"
    assert not report.usable_for_research


def test_supplying_the_matching_record_resolves_the_discontinuity_and_clears_the_verdict():
    days, result = split_history()

    report = reconcile(result.histories, records=[
        CorporateActionRecord(
            key="INE010A01011", ex_date=days[2], kind="split",
            factor=Decimal(10), purpose="face value 10 to 1",
        )
    ])

    assert report.unexplained == 0
    assert report.records_matched == 1
    assert report.verdict == "clean"
    assert report.usable_for_research
    assert any("confirmed against" in note for note in report.notes)


def test_a_record_the_prices_never_show_says_the_detector_assumption_did_not_hold():
    # The self-check that matters: if the venue does not restate PREVCLOSE, supplied records
    # will not be found, and the report must say so rather than report a clean archive.
    days, result = split_history()

    report = reconcile(result.histories, records=[
        CorporateActionRecord(key="INE010A01011", ex_date=days[2], kind="split",
                              factor=Decimal(10)),
        CorporateActionRecord(key="INE010A01011", ex_date=days[3], kind="bonus",
                              factor=Decimal(2)),
    ])

    assert report.records_supplied == 2
    assert report.records_matched == 1
    assert any("does not hold for this venue" in note for note in report.notes)


def test_back_adjustment_makes_the_series_continuous_across_the_split():
    _days, result = split_history()
    history = result.histories[0]
    report = reconcile([history])

    adjusted = back_adjust(history.bars, report.discontinuities)

    closes = [item.close for item in adjusted]
    assert closes == [Decimal(100), Decimal(100), Decimal(100), Decimal(100)]
    # Traded value is preserved: the share count rises by exactly the factor prices fell by.
    assert adjusted[0].volume == history.bars[0].volume * Decimal(10)
    assert adjusted[-1].close == history.bars[-1].close  # the newest basis is untouched


def test_back_adjustment_refuses_to_invent_a_factor_for_an_unexplained_gap():
    days, result = split_history()
    history = result.histories[0]

    # A discontinuity carrying a factor but no exchange signal. reconcile never produces
    # one - it fixes an unexplained gap's factor at 1 - so it is built here directly:
    # back_adjust takes discontinuities from any source, and the guard that makes it ignore
    # unsignalled ones has to hold for a caller or a future detector that supplies a factor.
    invented = PriceDiscontinuity(
        key=history.key,
        symbol=history.symbol,
        ex_date=days[2],
        actual_previous_close=Decimal("1000.00"),
        stated_previous_close=Decimal("1000.00"),
        implied_factor=Decimal(10),
        overnight_move=Decimal("-0.9"),
        detector="unexplained-gap",
    )

    assert back_adjust(history.bars, [invented]) == history.bars
    # The same discontinuity from the exchange itself is applied, so the test is showing
    # the detector being honoured and not merely that back_adjust does nothing.
    signalled = replace(invented, detector="exchange-prevclose")
    assert back_adjust(history.bars, [signalled])[0].close == Decimal(100)


def test_the_report_separates_what_the_archive_can_fix_from_what_it_cannot():
    # These two counts drive the purchasing decision and must not be conflated: a break the
    # exchange signalled costs nothing to fix, one it did not is what a vendor would be for.
    _days, result = split_history()
    gap_days = sessions(3, start=date(2021, 6, 1))
    gap_rows = [
        parse_bhavcopy(nse_file(gap_days[0], [
            nse_row("GAPPY", gap_days[0], Decimal("100.00"), Decimal("100.00"),
                    isin="INE014A01011")])),
        parse_bhavcopy(nse_file(gap_days[1], [
            nse_row("GAPPY", gap_days[1], Decimal("60.00"), Decimal("100.00"),
                    isin="INE014A01011")])),
        parse_bhavcopy(nse_file(gap_days[2], [
            nse_row("GAPPY", gap_days[2], Decimal("60.00"), Decimal("60.00"),
                    isin="INE014A01011")])),
    ]
    gappy = reconstruct_universe(gap_rows, source="NSE bhavcopy archive, test fixture")

    report = reconcile(list(result.histories) + list(gappy.histories))

    assert report.detected == 2
    assert report.self_adjustable == 1
    assert report.unsignalled_gaps == 1
    assert report.verdict == "unreconciled"
    evidence = report.as_evidence()
    assert evidence["self_adjustable"] == 1
    assert evidence["unsignalled_gaps"] == 1


def test_an_archive_the_exchange_signalled_everything_in_needs_no_vendor():
    _, result = split_history()

    report = reconcile(result.histories)

    assert report.unsignalled_gaps == 0
    assert report.self_adjustable == 1
    assert report.verdict == "self_adjusting"
    assert report.usable_for_research


def udiff_file(day, rows):
    return "\n".join([UDIFF_HEADER, *rows]) + "\n"


def udiff_row(symbol, day, close, previous, isin):
    high, low = close * Decimal("1.01"), min(close, previous) * Decimal("0.99")
    return (
        f"{day.isoformat()},{day.isoformat()},CM,NSE,STK,1,{isin},{symbol},EQ,,,,,"
        f"{symbol} Ltd,{close},{high},{low},{close},{close},{previous},,,,,"
        f"500000,50000000,5000,F1,1,,,,,"
    )


def test_the_newer_udiff_format_reconstructs_a_universe_as_the_legacy_one_does():
    # Everything published from mid-2024 is UDiFF, so the format that carries the archive
    # forward has to survive the whole path, not just the parser.
    days = sessions(120)
    files = []
    for index, day in enumerate(days):
        rows = [udiff_row("ULIVE", day, Decimal(100), Decimal(100), "INE020A01011")]
        if index <= 59:
            rows.append(udiff_row("UGONE", day, Decimal(50), Decimal(50), "INE021A01011"))
        files.append(parse_bhavcopy(udiff_file(day, rows)))

    result = reconstruct_universe(files, source="NSE UDiFF bhavcopy archive, test fixture")

    assert result.report.sessions_observed == 120
    listings = {item.symbol: item for item in result.universe.listings}
    assert listings["ULIVE"].delisted_on is None
    assert listings["UGONE"].delisted_on == days[59] + timedelta(days=1)


def test_the_same_day_settlement_series_is_not_kept_by_default():
    # T0 repeats a security that is already in EQ under the same ISIN, and NSE stamps its
    # close with the regular segment's price while its high and low describe whatever tiny
    # volume traded in T0. Keeping it would put duplicate security-days into the
    # reconstruction for no gain.
    assert "T0" in NSE_SAME_DAY_SETTLEMENT_SERIES
    assert "T0" not in NSE_NORMAL_SERIES


def test_a_row_whose_close_sits_below_its_low_is_refused_not_clamped():
    # The shape of the real NSE T0 row for SBIN on 2024-09-05: one trade at 820.00, so the
    # range is a point, while the close carries the regular segment's 818.75.
    day = date(2024, 9, 5)
    text = "\n".join([
        UDIFF_HEADER,
        (
            f"{day.isoformat()},{day.isoformat()},CM,NSE,STK,23256,INE062A01020,SBIN,T0,"
            ",,,,STATE BANK OF INDIA,820.00,820.00,820.00,818.75,820.00,816.50,,818.75,"
            ",,1,820.00,1,F1,1,,,,,"
        ),
        (
            f"{day.isoformat()},{day.isoformat()},CM,NSE,STK,3045,INE062A01020,SBIN,EQ,"
            ",,,,STATE BANK OF INDIA,818.35,822.15,814.40,818.75,818.00,816.50,,818.75,"
            ",,8395074,6878027979.75,146739,F1,1,,,,,"
        ),
    ]) + "\n"

    parsed = parse_bhavcopy(text)

    assert parsed.usable == 1
    assert parsed.rows[0].series == "EQ"
    assert parsed.rows[0].close == Decimal("818.75")
    assert [item.symbol for item in parsed.rejected] == ["SBIN"]
    assert "close outside" in parsed.rejected[0].reason


def test_a_stock_closing_at_its_circuit_limit_is_not_called_a_corporate_action():
    # Indian circuit bands are 2/5/10/20 percent. A stock that closes at the widest band has
    # moved exactly 0.20, so a detector firing at ">= 0.20" reports every circuit day in the
    # market as an unexplained action. Measured on a real NSE archive, that was thousands of
    # false positives arguing for vendor data nobody needs.
    days = sessions(3)
    rows = [
        parse_bhavcopy(nse_file(days[0], [
            nse_row("CIRCUIT", days[0], Decimal("100.00"), Decimal("100.00"),
                    isin="INE015A01011")])),
        parse_bhavcopy(nse_file(days[1], [
            nse_row("CIRCUIT", days[1], Decimal("120.00"), Decimal("100.00"),
                    isin="INE015A01011")])),
        parse_bhavcopy(nse_file(days[2], [
            nse_row("CIRCUIT", days[2], Decimal("120.00"), Decimal("120.00"),
                    isin="INE015A01011")])),
    ]
    result = reconstruct_universe(rows, source="NSE bhavcopy archive, test fixture")

    report = reconcile(result.histories)

    assert report.unsignalled_gaps == 0
    assert report.detected == 0

    # A move genuinely past the band still registers, so the detector is not simply blind.
    wider = [
        parse_bhavcopy(nse_file(days[0], [
            nse_row("BROKEN", days[0], Decimal("100.00"), Decimal("100.00"),
                    isin="INE016A01011")])),
        parse_bhavcopy(nse_file(days[1], [
            nse_row("BROKEN", days[1], Decimal("60.00"), Decimal("100.00"),
                    isin="INE016A01011")])),
        parse_bhavcopy(nse_file(days[2], [
            nse_row("BROKEN", days[2], Decimal("60.00"), Decimal("60.00"),
                    isin="INE016A01011")])),
    ]
    broken = reconstruct_universe(wider, source="NSE bhavcopy archive, test fixture")
    assert reconcile(broken.histories).unsignalled_gaps == 1


def test_a_stock_resuming_after_a_suspension_is_not_an_unexplained_corporate_action():
    # An instrument's bars are the days it traded, not the days the market was open, so a
    # security suspended for two years has adjacent bars two years apart. Measuring an
    # overnight move across that hole gives a multi-year return. On a real NSE archive the
    # largest such readings were penny stocks resuming from suspension - VISESHINFO at
    # +2000% on a previous close of 0.05 - every one of them reported as an unexplained
    # corporate action, and every one of them a resumption carrying no such information.
    before, after = date(2020, 1, 6), date(2022, 3, 29)
    rows = [
        parse_bhavcopy(nse_file(before, [
            nse_row("SUSPENDED", before, Decimal("0.05"), Decimal("0.05"),
                    isin="INE017A01011")])),
        parse_bhavcopy(nse_file(after, [
            nse_row("SUSPENDED", after, Decimal("1.05"), Decimal("0.05"),
                    isin="INE017A01011")])),
    ]
    result = reconstruct_universe(
        rows, source="NSE bhavcopy archive, test fixture", active_tail_sessions=1
    )

    report = reconcile(result.histories)

    assert report.unsignalled_gaps == 0

    # The same move between genuinely adjacent sessions is still caught, so the detector is
    # gated on adjacency rather than switched off.
    # A month-long suspension is the common case and pins the window far more tightly than
    # a two-year one: a window wide enough to swallow this would let ordinary suspensions
    # back in as corporate actions.
    month = [
        parse_bhavcopy(nse_file(date(2020, 1, 6), [
            nse_row("PAUSED", date(2020, 1, 6), Decimal("0.05"), Decimal("0.05"),
                    isin="INE019A01011")])),
        parse_bhavcopy(nse_file(date(2020, 2, 10), [
            nse_row("PAUSED", date(2020, 2, 10), Decimal("1.05"), Decimal("0.05"),
                    isin="INE019A01011")])),
    ]
    paused = reconstruct_universe(
        month, source="NSE bhavcopy archive, test fixture", active_tail_sessions=1
    )
    assert reconcile(paused.histories).unsignalled_gaps == 0

    adjacent = sessions(2, start=date(2020, 1, 6))
    tight = [
        parse_bhavcopy(nse_file(adjacent[0], [
            nse_row("JUMPY", adjacent[0], Decimal("0.05"), Decimal("0.05"),
                    isin="INE018A01011")])),
        parse_bhavcopy(nse_file(adjacent[1], [
            nse_row("JUMPY", adjacent[1], Decimal("1.05"), Decimal("0.05"),
                    isin="INE018A01011")])),
    ]
    jumpy = reconstruct_universe(
        tight, source="NSE bhavcopy archive, test fixture", active_tail_sessions=1
    )
    assert reconcile(jumpy.histories).unsignalled_gaps == 1


def test_two_companies_that_used_the_same_ticker_do_not_overwrite_each_other():
    # ISIN keying separates a company that renamed from the name it left behind. This is the
    # mirror case it does not help with: a ticker released by one company and later taken by
    # another. Two securities, two ISINs, one symbol - and everything downstream files by
    # symbol, so the manifest listed the name twice and both wrote to the same dataset file.
    # Found on a real NSE archive as "SRPL is listed twice on INDIA".
    days = sessions(200)
    files = []
    for index, day in enumerate(days):
        rows = [nse_row("ANCHOR", day, Decimal(100), Decimal(100), isin="INE030A01011")]
        if index <= 59:
            rows.append(nse_row("SRPL", day, Decimal(10), Decimal(10), isin="INE031A01011"))
        elif index >= 120:
            rows.append(nse_row("SRPL", day, Decimal(90), Decimal(90), isin="INE032A01011"))
        files.append(parse_bhavcopy(nse_file(day, rows)))

    result = reconstruct_universe(files, source="NSE bhavcopy archive, test fixture")

    names = sorted(item.symbol for item in result.universe.listings)
    assert len(names) == len(set(names)), "the manifest must not list a symbol twice"
    # The current holder keeps the plain ticker; the earlier company is qualified.
    assert "SRPL" in names
    qualified = [name for name in names if name.startswith("SRPL~")]
    assert len(qualified) == 1

    # And it is the *current* holder that keeps it, not whichever sorted first. In this
    # fixture the second company is still trading at the end and the first ceased at
    # session 59, so the plain ticker must belong to the one still listed.
    by_name = {item.symbol: item for item in result.universe.listings}
    assert by_name["SRPL"].delisted_on is None
    assert by_name[qualified[0]].delisted_on == days[59] + timedelta(days=1)

    assert any("used by more than one security" in note for note in result.report.caveats)


def test_each_security_that_shared_a_ticker_gets_its_own_dataset_file(tmp_path):
    days = sessions(200)
    files = []
    for index, day in enumerate(days):
        rows = [nse_row("ANCHOR", day, Decimal(100), Decimal(100), isin="INE033A01011")]
        if index <= 59:
            rows.append(nse_row("DUP", day, Decimal(10), Decimal(10), isin="INE034A01011"))
        elif index >= 120:
            rows.append(nse_row("DUP", day, Decimal(90), Decimal(90), isin="INE035A01011"))
        files.append(parse_bhavcopy(nse_file(day, rows)))
    result = reconstruct_universe(files, source="NSE bhavcopy archive, test fixture")

    summary = write_study_inputs(result, tmp_path)

    written = sorted(path.name for path in (tmp_path / "datasets").iterdir())
    # Three securities, three files. Keyed by symbol alone this was two, with one company's
    # prices silently replaced by the other's while the count still said three.
    assert len(written) == 3 == summary["files_written"]
    assert len(set(written)) == 3
    universe = load_universe_manifest(summary["manifest"])
    assert len(universe.listings) == 3

    # And the manifest and the datasets agree on the names, which is what the study joins on.
    manifest_names = {item.symbol for item in universe.listings}
    dataset_names = {
        json.loads((tmp_path / "datasets" / name).read_text())["provenance"]["instrument"]["symbol"]
        for name in written
    }
    assert manifest_names == dataset_names


def test_the_purchase_decision_list_excludes_breaks_the_archive_fixes_itself():
    # worst_unexplained exists to size a purchase. With no records supplied nothing is
    # "explained", so ranking every unmatched discontinuity put the largest
    # exchange-signalled actions at the top - the ones back_adjust corrects for free. On a
    # real NSE archive that filled all ten slots with penny-stock resumptions the venue had
    # restated, and made a report arguing for vendor data that was not needed.
    days, split = split_history()
    gap_days = sessions(3, start=date(2021, 6, 1))
    gap_rows = [
        parse_bhavcopy(nse_file(gap_days[0], [
            nse_row("GAPPY", gap_days[0], Decimal("100.00"), Decimal("100.00"),
                    isin="INE040A01011")])),
        parse_bhavcopy(nse_file(gap_days[1], [
            nse_row("GAPPY", gap_days[1], Decimal("60.00"), Decimal("100.00"),
                    isin="INE040A01011")])),
        parse_bhavcopy(nse_file(gap_days[2], [
            nse_row("GAPPY", gap_days[2], Decimal("60.00"), Decimal("60.00"),
                    isin="INE040A01011")])),
    ]
    gappy = reconstruct_universe(gap_rows, source="NSE bhavcopy archive, test fixture")

    report = reconcile(list(split.histories) + list(gappy.histories))

    # The split moves -90%, far larger than the gap's -40%, so ranking by size alone would
    # put it first. It is signalled, so it does not belong in the purchase list at all.
    assert report.self_adjustable == 1
    assert report.unsignalled_gaps == 1
    assert [item.symbol for item in report.worst_unexplained()] == ["GAPPY"]
    assert all(item.detector == "unexplained-gap" for item in report.worst_unexplained())
    assert [item.symbol for item in report.largest_self_adjusting()] == ["SPLIT"]

    evidence = report.as_evidence()
    assert [item["symbol"] for item in evidence["worst_unexplained"]] == ["GAPPY"]
    assert [item["symbol"] for item in evidence["largest_self_adjusting"]] == ["SPLIT"]
