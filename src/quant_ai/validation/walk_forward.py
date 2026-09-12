from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WalkForwardSplit:
    train_start: int
    train_end: int
    test_start: int
    test_end: int


def walk_forward_splits(length: int, train_size: int, test_size: int) -> tuple[WalkForwardSplit, ...]:
    if min(length, train_size, test_size) <= 0:
        raise ValueError("length and split sizes must be positive")
    splits: list[WalkForwardSplit] = []
    train_start = 0
    while True:
        train_end = train_start + train_size
        test_end = train_end + test_size
        if test_end > length:
            break
        splits.append(WalkForwardSplit(train_start, train_end, train_end, test_end))
        train_start += test_size
    return tuple(splits)
