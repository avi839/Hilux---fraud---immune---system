"""HELIX fusion package: pure Dempster-Shafer Evidence_Fuser (design §5)."""

from helix.fusion.core import (
    TOL,
    VACUOUS,
    Contribution,
    ContributorType,
    FusedAssessment,
    FusionError,
    Mass,
    conjunctive,
    discount,
    fuse,
    fuse_stage2,
    is_valid,
    normalise,
)

__all__ = [
    "TOL",
    "VACUOUS",
    "Contribution",
    "ContributorType",
    "FusedAssessment",
    "FusionError",
    "Mass",
    "conjunctive",
    "discount",
    "fuse",
    "fuse_stage2",
    "is_valid",
    "normalise",
]
