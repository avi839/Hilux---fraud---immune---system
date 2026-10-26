"""Property tests for helix.sim.generator (Synthetic_Generator)."""

from __future__ import annotations

import json
from itertools import islice

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from helix.codec import OPTIONAL_FIELDS, TransactionEvent, format, parse
from helix.config.schema import GeneratorConfig
from helix.sim import InMemoryGroundTruth, ManualClock, SyntheticGenerator

configs = st.builds(
    GeneratorConfig,
    accounts=st.integers(1, 300),
    rate_eps=st.floats(1, 1_000),
    change_fraction=st.floats(0, 1),
    omit_frac=st.floats(0, 1),
    travel_duration_h=st.floats(1, 720),
)


def _run(cfg: GeneratorConfig, seed: int, n: int):
    gt = InMemoryGroundTruth()
    gen = SyntheticGenerator(cfg, ManualClock(), gt, seed=seed)
    return list(islice(gen, n)), gt.records


# Feature: helix-fraud-immune-system, Property 7: Generator determinism and validity
@pytest.mark.property
@settings(max_examples=60)
@given(seed=st.integers(0, 2**32 - 1), cfg=configs)
def test_generator_determinism_and_validity(seed, cfg):
    """**Validates: Requirements 3.3, 3.10, 3.11**"""
    first, gt1 = _run(cfg, seed, 150)
    second, gt2 = _run(cfg, seed, 150)
    assert first == second
    assert gt1 == gt2
    for ev in first:
        raw = format(ev)
        back = parse(raw)
        assert isinstance(back, TransactionEvent), back
        assert back == ev
        assert ev.src != ev.dst
        doc = json.loads(raw)
        for f in OPTIONAL_FIELDS:
            # Omitted fields are absent, never placeholders.
            assert (f in doc) == (getattr(ev, f) is not None)
