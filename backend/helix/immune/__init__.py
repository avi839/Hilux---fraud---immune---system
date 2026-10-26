"""HELIX Immune_System (design §18, Requirement 21).

``helix.immune.miss_detector`` (the only immune module that reads ground truth) is
deliberately not re-exported here: importing ``helix.immune`` never pulls in the
Ground_Truth_Store read side. Import ``helix.immune.miss_detector.MissDetector`` directly.
"""

from .features import D, DIMS, INDEX, TECHNIQUE_PREFIX, UNATTRIBUTED, feature_vector, technique_tag
from .integration import ImmuneStage1Engine, attach_immune, immune_engine_factory, match_result
from .system import (
    CONTRIBUTOR_PREFIX,
    Antibody,
    AntibodyMatch,
    DiscardRecord,
    ImmuneSystem,
    Reason,
    Status,
    TriggerRecord,
    antibody_ids,
)

__all__ = [
    "CONTRIBUTOR_PREFIX",
    "D",
    "DIMS",
    "INDEX",
    "TECHNIQUE_PREFIX",
    "UNATTRIBUTED",
    "Antibody",
    "AntibodyMatch",
    "DiscardRecord",
    "ImmuneStage1Engine",
    "ImmuneSystem",
    "Reason",
    "Status",
    "TriggerRecord",
    "antibody_ids",
    "attach_immune",
    "feature_vector",
    "immune_engine_factory",
    "match_result",
    "technique_tag",
]
