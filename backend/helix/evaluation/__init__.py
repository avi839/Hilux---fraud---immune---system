"""HELIX evaluation package: Evaluation_Monitor and the ground-truth read side (design §19)."""

from .ground_truth import (
    GroundTruthReader,
    GTView,
    InMemoryGroundTruthReader,
    StoreGroundTruthReader,
    StoreGroundTruthWriter,
    ground_truth_channel,
)
from .monitor import (
    NOT_APPLICABLE,
    DatasetSummary,
    EvaluationMonitor,
    EvaluationPeriodReport,
    LineageStats,
    Metrics,
    PatternTypeStats,
)
from .sim_investigator import SimInvestigator, SimInvestigatorConfig, SimLabel
from .sim_investigator import status_doc as sim_investigator_status_doc

__all__ = [
    "SimInvestigator",
    "SimInvestigatorConfig",
    "SimLabel",
    "sim_investigator_status_doc",
    "NOT_APPLICABLE",
    "DatasetSummary",
    "EvaluationMonitor",
    "EvaluationPeriodReport",
    "GTView",
    "GroundTruthReader",
    "InMemoryGroundTruthReader",
    "LineageStats",
    "Metrics",
    "PatternTypeStats",
    "StoreGroundTruthReader",
    "StoreGroundTruthWriter",
    "ground_truth_channel",
]
