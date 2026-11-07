"""The (entity keys, prediction time) uniqueness count of the label tables."""

import pyarrow as pa

from bench.kaggle import key_uniqueness


def test_duplicated_pairs_are_counted_with_their_extra_rows():
    table = pa.table(
        {
            "user": ["a", "a", "a", "b", "b", "c"],
            "ts": [1, 1, 1, 1, 2, 1],
            "y": [0, 1, 0, 1, 1, 0],
        }
    )
    found = key_uniqueness.duplicated_pairs(table, ["user"], "ts")
    assert (found["rows"], found["pairs"]) == (6, 4)
    assert (found["duplicated_pairs"], found["extra_rows"]) == (1, 2)
    assert found["examples"] == [["a", "1", "3"]]


def test_a_single_series_has_no_entity_key():
    table = pa.table({"ts": [1, 2, 2, 3]})
    found = key_uniqueness.duplicated_pairs(table, [], "ts")
    assert found["duplicated_pairs"] == 1 and found["extra_rows"] == 1
