from quant_ai.validation.walk_forward import walk_forward_splits


def test_walk_forward_is_non_overlapping_out_of_sample() -> None:
    splits = walk_forward_splits(100, 40, 10)
    assert splits[0].train_end == splits[0].test_start
    assert all(split.test_end <= 100 for split in splits)
    assert len(splits) == 6
