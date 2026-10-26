"""Range and cross-field validation for HelixConfig. Collects every error."""

from __future__ import annotations

import typing
from collections.abc import Iterator, Sequence
from typing import Annotated, Any, Optional

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from .schema import TIER_ORDER, Band, ConfigError, Range

if typing.TYPE_CHECKING:
    from .schema import HelixConfig


def range_of(field: FieldInfo) -> Optional[Range]:
    """Return the Range metadata of a field, looking inside Union/Optional args."""
    for m in field.metadata:
        if isinstance(m, Range):
            return m
    return _range_in_annotation(field.annotation)


def _range_in_annotation(ann: Any) -> Optional[Range]:
    if typing.get_origin(ann) is Annotated:
        for m in ann.__metadata__:
            if isinstance(m, Range):
                return m
    for arg in typing.get_args(ann):
        r = _range_in_annotation(arg)
        if r is not None:
            return r
    return None


def walk_ranges(model: BaseModel, prefix: str = "") -> Iterator[tuple[str, Any, Range]]:
    """Yield (dotted path, value, Range) for every range-checked leaf."""
    for name, field in type(model).model_fields.items():
        value = getattr(model, name)
        path = f"{prefix}{name}"
        if isinstance(value, BaseModel):
            yield from walk_ranges(value, path + ".")
            continue
        rng = range_of(field)
        if rng is not None:
            yield path, value, rng


def range_paths(model_cls: type[BaseModel], prefix: str = "") -> dict[str, Range]:
    """All range-checked leaf paths of a model class (used by tests and PATCH)."""
    out: dict[str, Range] = {}
    for name, field in model_cls.model_fields.items():
        ann = field.annotation
        # get_origin guard: on 3.10, isinstance(tuple[Band, ...], type) is True.
        if typing.get_origin(ann) is None and isinstance(ann, type) and issubclass(ann, BaseModel):
            out.update(range_paths(ann, f"{prefix}{name}."))
            continue
        rng = range_of(field)
        if rng is not None:
            out[f"{prefix}{name}"] = rng
    return out


def check(path: str, value: Any, rng: Range) -> Optional[ConfigError]:
    # Non-numeric values (None for unset optionals, "max" for dataset.speed) are
    # type-checked by Pydantic and have no numeric range.
    if value is None or isinstance(value, (str, bool)):
        return None
    if not isinstance(value, (int, float)):
        return None
    if rng.contains(value):
        return None
    return ConfigError(path, value, rng.describe())


def validate_bands(bands: Sequence[Band], alert_threshold: float) -> list[ConfigError]:
    """Bands must be one per tier in tier order, lo < hi, contiguous, covering
    [alert_threshold, 1.0]. Any problem yields one error naming `risk.bands`."""
    problems: list[str] = []
    tiers = tuple(b.tier for b in bands)
    if tiers != TIER_ORDER:
        problems.append(f"tiers must be {[t.value for t in TIER_ORDER]} in order")
    for b in bands:
        if not b.lo < b.hi:
            problems.append(f"{b.tier.value}: lo must be < hi")
    for a, b in zip(bands, bands[1:]):
        if a.hi != b.lo:
            problems.append(f"gap or overlap between {a.tier.value} and {b.tier.value}")
    if bands:
        if bands[0].lo != alert_threshold:
            problems.append("first band must start at risk.alert_threshold")
        if bands[-1].hi != 1.0:
            problems.append("last band must end at 1.0")
    if not problems:
        return []
    return [ConfigError("risk.bands", [b.model_dump(mode="json") for b in bands], "; ".join(problems))]


def validate_config(cfg: "HelixConfig") -> list[ConfigError]:
    """Return every range and cross-field error. Empty list means valid.

    Cross-field rules are skipped when one of their operands is already out of
    range, so each out-of-range setting is reported once and nothing else is
    blamed for it.
    """
    errs: list[ConfigError] = []
    bad: set[str] = set()
    for path, value, rng in walk_ranges(cfg):
        e = check(path, value, rng)
        if e is not None:
            errs.append(e)
            bad.add(path)

    if "risk.alert_threshold" not in bad:
        errs += validate_bands(cfg.risk.bands, cfg.risk.alert_threshold)

    if not {"ingest.dedup_ttl_s", "graph.window_s"} & bad and cfg.ingest.dedup_ttl_s < cfg.graph.window_s:
        errs.append(ConfigError("ingest.dedup_ttl_s", cfg.ingest.dedup_ttl_s, ">= graph.window_s"))

    # Motif spans longer than the Graph_Window could never be filled: edges older than the
    # window are evicted (task 29.1).
    for name in ("fan_span_s", "round_trip_span_s"):
        path = f"graph.{name}"
        if not {path, "graph.window_s"} & bad and getattr(cfg.graph, name) > cfg.graph.window_s:
            errs.append(ConfigError(path, getattr(cfg.graph, name), "<= graph.window_s"))

    if not {"learn.weight_lower", "learn.weight_upper"} & bad and not cfg.learn.weight_lower < cfg.learn.weight_upper:
        errs.append(ConfigError("learn.weight_lower", cfg.learn.weight_lower, "< learn.weight_upper"))

    tr = cfg.triage
    if (not {"triage.suggest_fp_belief", "triage.suggest_fraud_belief"} & bad
            and not tr.suggest_fp_belief < tr.suggest_fraud_belief):
        errs.append(ConfigError("triage.suggest_fp_belief", tr.suggest_fp_belief, "< triage.suggest_fraud_belief"))

    ds = cfg.dataset
    if ds.scope_start is not None and ds.scope_end is not None and not ds.scope_start < ds.scope_end:
        errs.append(ConfigError("dataset.scope_start", ds.scope_start, "< dataset.scope_end"))

    return errs
