# Feature library

`quant_ai.features.library` is a catalogue of signal hypotheses. Before it, the entire
feature surface was three functions in `technical.py` — a return, an ATR and a rolling
volatility. Three functions cannot express a hypothesis worth testing, so a search over them
is a parameter sweep over one idea rather than research.

24 features across six families: trend (6), reversal (5), volatility (5), liquidity (3),
structure (3), seasonality (2).

## Two rules make it a library rather than a pile of formulas

**Every feature states why it might work, in economics, without mentioning results.** The
constructor refuses a rationale shorter than 40 characters, and refuses one containing
result language — `backtest`, `sharpe`, `outperform`, `profitable`, `in-sample`, `p-value`,
`win rate`. A feature justified by its own performance is circular, and writing the reason
first is the cheapest defence against fitting noise: a signal you cannot argue for
beforehand is one you found by looking.

**Insufficient or degenerate history returns `None`, never zero.** `technical.py` returns
`Decimal(0)` when the window is too short. Zero volatility and zero return are meaningful
values, so anything downstream consumes that as a real measurement. A feature that cannot be
computed says so, and a genuinely zero return is still reported as zero — the two are
distinguishable.

`technical.py` itself is unchanged. Its callers are outside the scope of this work.

## The library pays for its own size

`hypothesis_count` is the number of features, and it belongs in the trial register. Every
feature evaluated against a target is a hypothesis tested, so the deflated Sharpe in
`validation/deflated_sharpe.py` charges for the whole search rather than the one candidate
that survived it.

This is the property that makes growing the catalogue safe. Without it, adding features would
quietly lower the bar — more shots at the target, same threshold. With it, adding features
raises the bar. `test_evaluating_the_whole_library_is_a_search_of_that_size` asserts exactly
that: a result that would pass as a single pre-registered hypothesis is refused once all 24
are counted.

## What it is not

A catalogue of hypotheses, not of edges. None of these 24 has been shown to predict anything.
Evaluating them is a search of size 24 and must be registered as such before any result is
believed, and the data they are evaluated on still has to clear the survivorship audit in
`marketdata/point_in_time.py`.
