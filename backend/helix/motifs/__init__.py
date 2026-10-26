"""HELIX motifs package: incremental temporal Motif_Detectors (design §10)."""

from helix.motifs.detectors import (
    FanInDetector,
    FanOutDetector,
    MotifDetector,
    MuleChainDetector,
    RoundTripDetector,
    StructuringDetector,
)
from helix.motifs.engine import (
    DEFAULT_BUDGET_MS,
    DEFAULT_MOTIF_MASSES,
    EngineResult,
    MotifEngine,
    contributor_id,
    motif_mass,
)
from helix.motifs.models import MotifInstance, MotifResult, MotifType, motif_id_for

__all__ = [
    "DEFAULT_BUDGET_MS",
    "DEFAULT_MOTIF_MASSES",
    "EngineResult",
    "FanInDetector",
    "FanOutDetector",
    "MotifDetector",
    "MotifEngine",
    "MotifInstance",
    "MotifResult",
    "MotifType",
    "MuleChainDetector",
    "RoundTripDetector",
    "StructuringDetector",
    "contributor_id",
    "motif_id_for",
    "motif_mass",
]
