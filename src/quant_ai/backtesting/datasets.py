"""Reading a replay dataset off disk, without the engine that replays it.

These types and loaders lived in :mod:`quant_ai.backtesting.replay`, which imports the
whole agent stack - the swarm runtime, Atlas, the Anthropic client and its SDK - because
the replay *engine* genuinely needs them. Reading a JSON file of bars does not, and the
coupling meant a feature study could not run on a machine with no model SDK installed. A
research path that fails on a missing LLM dependency it never calls is a packaging accident,
not a design.

``replay`` re-exports everything here, so existing callers are unchanged.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from quant_ai.backtesting.intrabar import IntrabarWindow
from quant_ai.domain.models import AssetClass, Instrument, Market
from quant_ai.intelligence.providers import NewsSignal
from quant_ai.marketdata.models import Candle


@dataclass(frozen=True)
class HistoricalMacroEvent:
    observed_at: datetime
    indicators: dict[str, Decimal]


@dataclass(frozen=True)
class HistoricalFundamentalEvent:
    observed_at: datetime
    subject: str
    metrics: dict[str, Decimal]


@dataclass(frozen=True)
class HistoricalReplayDataset:
    bars: tuple[Candle, ...]
    macro: tuple[HistoricalMacroEvent, ...] = ()
    news: tuple[NewsSignal, ...] = ()
    fundamentals: tuple[HistoricalFundamentalEvent, ...] = ()
    benchmark_closes: tuple[Decimal, ...] = ()
    intrabar_windows: tuple[IntrabarWindow, ...] = ()



def load_replay_dataset(path: str | Path, instrument: Instrument) -> HistoricalReplayDataset:
    source = Path(path)
    if source.suffix.lower() == ".json":
        payload = json.loads(source.read_text())
        bars = tuple(_bar_from_mapping(item, instrument) for item in payload.get("bars", ()))
        macro = tuple(
            HistoricalMacroEvent(
                datetime.fromisoformat(item["observed_at"]),
                {key: Decimal(str(value)) for key, value in item["indicators"].items()},
            )
            for item in payload.get("macro", ())
        )
        news = tuple(
            NewsSignal(
                item["subject"],
                item["headline"],
                Decimal(str(item["sentiment"])),
                item.get("source", "historical"),
                datetime.fromisoformat(item["published_at"]),
            )
            for item in payload.get("news", ())
        )
        fundamentals = tuple(
            HistoricalFundamentalEvent(
                datetime.fromisoformat(item["observed_at"]),
                item["subject"],
                {key: Decimal(str(value)) for key, value in item["metrics"].items()},
            )
            for item in payload.get("fundamentals", ())
        )
        benchmark = tuple(Decimal(str(item)) for item in payload.get("benchmark_closes", ()))
        windows = tuple(
            IntrabarWindow(
                datetime.fromisoformat(item["parent_timestamp"]),
                datetime.fromisoformat(item["start"]),
                item["interval_seconds"],
                tuple(_bar_from_mapping(bar, instrument) for bar in item["bars"]),
            )
            for item in payload.get("intrabar_windows", ())
        )
        return HistoricalReplayDataset(bars, macro, news, fundamentals, benchmark, windows)
    with source.open(newline="") as handle:
        rows = tuple(csv.DictReader(handle))
    return HistoricalReplayDataset(tuple(_bar_from_mapping(item, instrument) for item in rows))


def dataset_instrument(path: str | Path) -> Instrument | None:
    """The instrument a replay dataset declares about itself, or ``None`` if it declares none.

    ``load_replay_dataset`` stamps every bar with whatever instrument its caller hands in
    and never reads the file's own ``provenance.instrument`` block. A caller that guesses
    wrong therefore produces a report, a trial-register study and a proof that all name one
    security while the prices inside them belong to another - and nothing in the output
    says so. A dataset written by ``scripts/fetch_historical_bars.py`` states exactly what
    it holds, down to the asset class and the exchange; this reads that statement so the
    caller does not have to guess.

    A file that declares nothing returns ``None``: hand-written fixtures and CSV exports
    predate the provenance block and are still legitimate, and the caller falls back to
    saying what they are. A file that declares something unusable raises instead, because
    a dataset that says what it is and says it wrong is worse than one that stays silent
    (``TypeError`` when the block is not a mapping, ``ValueError`` when a field inside it
    is missing or names something that is not a market or an asset class).
    """
    source = Path(path)
    if source.suffix.lower() != ".json":
        return None
    payload = json.loads(source.read_text())
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        return None
    declared = provenance.get("instrument")
    if declared is None:
        return None
    if not isinstance(declared, dict):
        raise TypeError("dataset_instrument_malformed")
    try:
        return Instrument(
            str(declared["symbol"]),
            Market(str(declared["market"])),
            AssetClass(str(declared["asset_class"])),
            str(declared["currency"]),
            str(declared["exchange"]),
        )
    except (KeyError, ValueError) as error:
        raise ValueError("dataset_instrument_malformed") from error


def _bar_from_mapping(item: dict[str, object], instrument: Instrument) -> Candle:
    return Candle(
        instrument,
        datetime.fromisoformat(str(item["timestamp"])),
        Decimal(str(item["open"])),
        Decimal(str(item["high"])),
        Decimal(str(item["low"])),
        Decimal(str(item["close"])),
        Decimal(str(item["volume"])),
    )
