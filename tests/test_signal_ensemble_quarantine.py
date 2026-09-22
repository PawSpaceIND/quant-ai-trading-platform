"""``SignalEnsemble`` must not reach a trading path, and the fence must be structural.

The module scores an opportunity at a fixed 3% win against a fixed 1.5% loss with no cost
charged, on every instrument in every regime. Two consequences, and both are why a comment
saying "research only" is not enough:

**Its EV cannot disagree with its probability.** With the payoffs constant, EV is a strictly
increasing function of p, so the EV filter passes exactly the set the probability filter
already passed. It looks like a second, independent opinion and is arithmetically the first
one restated.

**Its break-even is one in three.** Where the book's own declared payoffs - 1% against 2%,
10 bps round trip - need 0.70 before an entry is worth taking, this module calls 0.34 a
positive-expectancy trade. Wiring it anywhere near a sizer would not be a small error.

So the quarantine is enforced two ways here: no module under ``src`` may import it, and no
live entry point may drag it in transitively. A future import wires itself into these
tests automatically - neither check carries a list of modules to keep up to date.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

from quant_ai.agents import expected_value as valuation
from quant_ai.ai.ensemble import SignalEnsemble
from quant_ai.domain.models import AssetClass, Instrument, Market, Signal

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
QUARANTINED = "quant_ai.ai.ensemble"
# Everything a container actually starts, plus the two agent modules a sizer would be
# reached through. If the module is absent from all of these it is absent from the book.
LIVE_ENTRY_POINTS = (
    "quant_ai.daemon",
    "quant_ai.cli",
    "quant_ai.execution.daemon",
    "quant_ai.agents.atlas",
    "quant_ai.agents.swarm",
    "quant_ai.agents.traded_runtime",
    "quant_ai.governance.pilot",
)
PROBE = """
import sys
sys.path.insert(0, {src!r})
import {module}
print({quarantined!r} in sys.modules)
"""


def _imported_modules(path: Path) -> set[str]:
    """Every dotted name this file imports, absolute and relative alike."""
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    # Only a file under ``src`` has a package for a relative import to resolve against.
    package = path.relative_to(SRC).with_suffix("").parts if path.is_relative_to(SRC) else ()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # A relative import resolves against this module's own package.
                base = ".".join(package[: len(package) - node.level])
                root = f"{base}.{node.module}" if node.module else base
            else:
                root = node.module or ""
            names.add(root)
            names.update(f"{root}.{alias.name}" for alias in node.names)
    return names


def test_no_module_under_src_imports_the_quarantined_ensemble() -> None:
    """A static fence, so a new import fails the suite the moment it is written.

    Checked by reading the source rather than by importing: a module that is never
    imported by a test would otherwise be free to wire itself in unobserved.
    """
    offenders = sorted(
        str(path.relative_to(ROOT))
        for path in SRC.rglob("*.py")
        if path.parent != SRC / "quant_ai" / "ai"
        and any(name == QUARANTINED or name.startswith(QUARANTINED + ".")
                for name in _imported_modules(path))
    )
    assert offenders == [], f"quarantined ensemble imported by: {offenders}"


def test_the_scan_would_actually_catch_an_import() -> None:
    """The fence above passes trivially if the scan reads nothing. Prove it reads.

    ``tests/test_ai_ensemble.py`` imports the module by name, so a scan that works must
    see it there - and must see nothing under ``src``. An empty-set assertion that cannot
    distinguish those two cases states nothing at all.
    """
    assert QUARANTINED + ".SignalEnsemble" in _imported_modules(ROOT / "tests" / "test_ai_ensemble.py")
    assert _imported_modules(SRC / "quant_ai" / "agents" / "atlas.py")


def test_no_live_entry_point_drags_it_in_transitively() -> None:
    """The static scan misses a chain; this catches what actually loads.

    A subprocess per entry point, because ``sys.modules`` in this process already carries
    whatever the rest of the suite imported - including the module under test.
    """
    for module in LIVE_ENTRY_POINTS:
        result = subprocess.run(
            [sys.executable, "-c", PROBE.format(src=str(SRC), module=module, quarantined=QUARANTINED)],
            capture_output=True, text=True, check=True,
        )
        assert result.stdout.strip() == "False", f"{module} loads {QUARANTINED}"


def test_its_expected_value_is_the_probability_restated_and_the_live_one_is_not() -> None:
    """Why the fence exists, in arithmetic rather than in a comment.

    Its EV ranks opportunities in exactly the order its probability already did, so it
    can never veto a trade the probability filter admitted. The live EV can: it reads the
    payoffs each decision declared, so a high probability on a bad payoff still fails.
    """
    def house(probability: Decimal) -> Decimal:
        return probability * Decimal("0.03") - (Decimal(1) - probability) * Decimal("0.015")

    ladder = [Decimal(str(step / 20)) for step in range(21)]
    assert [house(p) for p in ladder] == sorted(house(p) for p in ladder)
    # Break-even at one in three, and no cost charged for the round trip. Pinned by the
    # bracket rather than by an exact root: one third has no exact decimal expansion.
    assert house(Decimal("0.33")) < 0 < house(Decimal("0.34"))

    # The live book, at the payoffs its specialists declare: 0.70, and a cost that is paid.
    def live(probability: Decimal) -> Decimal:
        return valuation.expected_value(
            probability, expected_return=Decimal("0.01"), expected_risk=Decimal("0.02"),
            cost_bps=Decimal(10),
        )

    assert live(Decimal("0.70")) == 0
    # The gap the quarantine exists to keep out of the sizer: a probability this module
    # calls a positive-expectancy trade, on which the book expects to lose.
    assert house(Decimal("0.40")) > 0 and live(Decimal("0.40")) < 0


def test_the_module_still_works_for_the_research_use_it_was_kept_for() -> None:
    """Quarantined is not deleted. A fence that broke it would invite someone to inline it."""
    opportunity = SignalEnsemble().score(
        Instrument("GLD", Market.USA, AssetClass.ETF, "USD", "NYSEARCA"),
        (Signal("trend", Decimal("0.8"), Decimal("0.9"), 240),
         Signal("macro", Decimal("0.6"), Decimal("0.8"), 240),
         Signal("regime", Decimal("0.7"), Decimal("0.7"), 240)),
    )
    assert opportunity is not None and opportunity.expected_value > 0
