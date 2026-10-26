"""Property tests for helix.config."""

from __future__ import annotations

import math
import typing
from typing import Annotated, Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

from helix.config import (
    ConfigValidationError,
    HelixConfig,
    Range,
    ensure_valid,
    range_paths,
    with_updates,
)

RANGES: dict[str, Range] = range_paths(HelixConfig)

# Operands of cross-field rules keep their defaults when not deliberately broken,
# so that in-range perturbations never create a cross-field error.
CROSS_FIELD = {
    "risk.alert_threshold",
    "ingest.dedup_ttl_s",
    "graph.window_s",
    "learn.weight_lower",
    "learn.weight_upper",
    "triage.suggest_fraud_belief",
    "triage.suggest_fp_belief",
}


def _is_int_leaf(path: str) -> bool:
    model: type[BaseModel] = HelixConfig
    *parents, leaf = path.split(".")
    for p in parents:
        model = model.model_fields[p].annotation  # type: ignore[assignment]
    return _contains_int(model.model_fields[leaf].annotation)


def _contains_int(ann: Any) -> bool:
    if typing.get_origin(ann) is Annotated:
        return _contains_int(typing.get_args(ann)[0])
    if ann is int:
        return True
    return any(_contains_int(a) for a in typing.get_args(ann))


INT_LEAVES = {p for p in RANGES if _is_int_leaf(p)}


def out_of_range(path: str) -> st.SearchStrategy:
    r = RANGES[path]
    if path in INT_LEAVES:
        below = st.integers(max_value=math.ceil(r.lo) - (0 if r.lo_open else 1))
        above = st.integers(min_value=math.floor(r.hi) + 1)
        return below | above
    # nextafter (not exclude_min/max) so -0.0 is never drawn as "below 0".
    below_max = r.lo if r.lo_open else math.nextafter(r.lo, -math.inf)
    below = st.floats(max_value=below_max, allow_nan=False)
    above = st.floats(min_value=math.nextafter(r.hi, math.inf), allow_nan=False)
    return below | above | st.just(math.nan)


def in_range(path: str) -> st.SearchStrategy:
    r = RANGES[path]
    if path in INT_LEAVES:
        return st.integers(min_value=math.ceil(r.lo) + (1 if r.lo_open else 0), max_value=math.floor(r.hi))
    lo = math.nextafter(r.lo, math.inf) if r.lo_open else r.lo
    return st.floats(min_value=lo, max_value=r.hi, allow_nan=False)


@st.composite
def configs(draw):
    """A HelixConfig with a chosen set of out-of-range settings (possibly empty)."""
    paths = sorted(RANGES)
    broken = draw(st.sets(st.sampled_from(paths), max_size=len(paths)))
    updates: dict[str, Any] = {p: draw(out_of_range(p)) for p in sorted(broken)}
    for p in paths:
        if p not in broken and p not in CROSS_FIELD and draw(st.booleans()):
            updates[p] = draw(in_range(p))
    return with_updates(HelixConfig(), updates), broken


# Feature: helix-fraud-immune-system, Property 8: Configuration range validation
@pytest.mark.property
@given(case=configs())
def test_config_range_validation(case):
    """**Validates: Requirements 3.12, 6.11, 8.8, 11.12, 13.12, 14.15, 17.9, 19.3, 24.4**"""
    cfg, broken = case
    errors = cfg.validate()
    named = [e.path for e in errors]
    # Every out-of-range setting is named, exactly once, and nothing else is.
    assert sorted(named) == sorted(broken)
    # Startup is refused iff the set is non-empty, and the refusal lists every error.
    if broken:
        with pytest.raises(ConfigValidationError) as exc:
            ensure_valid(cfg)
        assert {e.path for e in exc.value.errors} == broken
    else:
        assert ensure_valid(cfg) is cfg
