"""Attacker_Agent and Pattern_Genome (design §17, Requirement 20)."""

from .agent import AttackCampaign, AttackerAgent, IsDetected
from .campaigns import GENERATORS, CampaignContext, TargetPool, generate
from .genome import (
    ALL_TYPES,
    BOUNDS,
    TOPOLOGY_PARAM,
    ParamSpec,
    PatternGenome,
    PatternType,
    default_genome,
    default_params,
    mutate,
)
from .integration import AttackerRunner, attach_attacker

__all__ = [
    "ALL_TYPES",
    "BOUNDS",
    "GENERATORS",
    "TOPOLOGY_PARAM",
    "AttackCampaign",
    "AttackerAgent",
    "AttackerRunner",
    "CampaignContext",
    "IsDetected",
    "ParamSpec",
    "PatternGenome",
    "PatternType",
    "TargetPool",
    "attach_attacker",
    "default_genome",
    "default_params",
    "generate",
    "mutate",
]
