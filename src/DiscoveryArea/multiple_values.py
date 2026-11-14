"""Reduce per-record matches to one decision per item."""
import numpy as np


def any_value_matches(values, predicate):
    owners, flattened = [], []
    for index, entries in enumerate(values):
        owners.extend([index] * len(entries))
        flattened.extend(entries)
    result = np.zeros(len(values), dtype=bool)
    if flattened:
        matches = predicate(np.asarray(flattened, dtype=np.str_))
        np.logical_or.at(result, np.asarray(owners), matches)
    return result


def membership_mask(scalars, allowed, multiple=None):
    if multiple is None:
        return np.isin(scalars, np.asarray(tuple(allowed), dtype=np.str_))
    return any_value_matches(multiple, lambda values: np.isin(values, tuple(allowed)))
