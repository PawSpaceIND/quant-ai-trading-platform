"""Purged and embargoed cross-validation for overlapping financial labels.

Ordinary K-fold leaks. A label built from a forward window — "the return over the next ten
bars" — overlaps its neighbours, so an observation in the training set can share most of
its outcome with an observation in the test set. The model then scores well by having
already seen the answer, and the backtest is contaminated in a way no amount of extra data
fixes.

Two corrections, both from Lopez de Prado's "Advances in Financial Machine Learning":

* **Purge** — drop from training any observation whose own label window overlaps the test
  window at either end.
* **Embargo** — additionally drop a stretch of observations immediately after the test
  window, because serial correlation leaks information forward even when the label windows
  no longer strictly overlap.

The existing ``walk_forward_splits`` produces contiguous train/test windows and does not
purge, which is correct for a pure walk-forward simulation and wrong for fitting anything
on overlapping labels.
"""

from __future__ import annotations

from dataclasses import dataclass

SCHEMA = "pramana.purged_cv.v1"


@dataclass(frozen=True)
class PurgedFold:
    train: tuple[int, ...]
    test: tuple[int, ...]

    @property
    def leaks(self) -> bool:
        """True if any index appears on both sides. Always False by construction; asserted in tests."""
        return bool(set(self.train) & set(self.test))


def purged_kfold(
    observations: int,
    *,
    splits: int = 5,
    label_span: int = 0,
    embargo: float = 0.0,
) -> tuple[PurgedFold, ...]:
    """Contiguous test folds with overlapping training observations removed.

    ``label_span`` is how many observations forward an outcome reaches: a ten-bar forward
    return is 10. ``embargo`` is a fraction of the total sample dropped after each test
    window; 0.01 is the commonly used default.
    """
    if observations < 2:
        raise ValueError("purged folds need at least two observations")
    if splits < 2 or splits > observations:
        raise ValueError("splits must be between two and the number of observations")
    if label_span < 0:
        raise ValueError("label span cannot be negative")
    if not 0.0 <= embargo < 1.0:
        raise ValueError("embargo must be a fraction of the sample in [0, 1)")

    embargo_size = int(observations * embargo)
    boundaries = [round(index * observations / splits) for index in range(splits + 1)]

    folds: list[PurgedFold] = []
    for index in range(splits):
        start, stop = boundaries[index], boundaries[index + 1]
        if stop <= start:
            continue
        test = tuple(range(start, stop))

        train: list[int] = []
        for position in range(observations):
            if start <= position < stop:
                continue
            # Purge: this observation's label reaches into the test window.
            if position < start and position + label_span >= start:
                continue
            # Purge: this observation's label window began inside the test window.
            if position >= stop and position - label_span < stop:
                continue
            # Embargo: serial correlation immediately after the test window.
            if stop <= position < stop + embargo_size:
                continue
            train.append(position)
        folds.append(PurgedFold(tuple(train), test))
    return tuple(folds)
