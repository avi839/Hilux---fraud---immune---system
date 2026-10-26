"""Cyber-fusion layer (Increment 4, Req 28 and 29): one import point for the SOC pieces.

Design decisions:
- The layer is spread over the packages it extends instead of living here, so the detection
  core keeps one code path (design §23, "no structural change"):

  =================================  ==================================================
  piece                              module
  =================================  ==================================================
  Auth_Event, Auth_Event_Codec       ``helix.codec.auth``
  Stream_Ingestor ``submit_auth``    ``helix.ingest.ingestor``
  auth history, linkage, Auth_Sensor ``helix.sensors.auth``
  pipeline recording + chain entries ``helix.core.pipeline`` (``Helix.process_auth``)
  kill chain + multiplier            ``helix.cases.killchain``, ``CaseManager._recompute``
  explanation templates              ``helix.explain.engine`` (``t_auth``, AUTH_EVENT notes)
  benign auth traffic                ``helix.sim.generator`` (``soc=``)
  attacker auth sequences            ``helix.attacker.campaigns`` (``auth_leg``)
  REST routes                        ``helix.api.soc_routes``
  technique catalogue + mapping      ``helix.soc.techniques`` (Req 30.1-30.5, data/*.yaml)
  Coverage_Matrix                    ``helix.soc.coverage`` (Req 30.6-30.8)
  Detection_Rule engine              ``helix.soc.rules`` (Req 31), example rules in
                                     ``backend/rules/``
  =================================  ==================================================

- Everything is behind ``HelixConfig.soc.enabled`` (default False). With the flag off no
  Auth_Sensor runs, Auth_Events are ignored by the pipeline, Expected_Loss is never
  multiplied and the generator and attacker emit no Auth_Events, so existing behaviour and
  payloads are unchanged.
- ``soc_config`` builds a config with the flag on (plus overrides) for demos and tests.
"""

from __future__ import annotations

from typing import Any, Optional

from helix.cases.killchain import (
    MULTIPLIED_STAGES,
    KillChain,
    StageAssignment,
    StageSpan,
    apply_multiplier,
    assign_stage,
    check_multiplier,
    max_stage,
    reconstruct,
    stage_keys,
    stage_rank,
)
from helix.codec.auth import AuthEvent, AuthEventType, format_auth, parse_auth
from helix.config import KILL_CHAIN_ORDER, HelixConfig, KillChainStage, SocConfig, ensure_valid
from helix.sensors.auth import AuthHistory, AuthLink, AuthSensor, SuspiciousActivity, signal_mass

from .coverage import CoverageMatrix, coverage_matrix, pipeline_coverage
from .rules import (
    DetectionRule,
    RuleEngine,
    RuleValidationError,
    attach_rules,
    backtest_rule,
    format_rule,
    parse_rule,
)
from .techniques import (
    Catalogue,
    ContributorMapping,
    UnknownTechniqueError,
    case_techniques,
    default_catalogue,
    default_mapping,
    entry_techniques,
    genome_techniques,
)


def soc_config(base: Optional[HelixConfig] = None, **soc_overrides: Any) -> HelixConfig:
    """``base`` (default ``HelixConfig()``) with ``soc.enabled = True`` and ``soc_overrides``,
    validated (raises ``ConfigValidationError`` on an out-of-range value)."""
    cfg = base if base is not None else HelixConfig()
    soc = cfg.soc.model_copy(update={"enabled": True, **soc_overrides})
    return ensure_valid(cfg.model_copy(update={"soc": SocConfig.model_validate(soc.model_dump())}))


__all__ = [
    "KILL_CHAIN_ORDER",
    "MULTIPLIED_STAGES",
    "AuthEvent",
    "AuthEventType",
    "AuthHistory",
    "AuthLink",
    "AuthSensor",
    "Catalogue",
    "ContributorMapping",
    "CoverageMatrix",
    "DetectionRule",
    "RuleEngine",
    "RuleValidationError",
    "UnknownTechniqueError",
    "attach_rules",
    "backtest_rule",
    "case_techniques",
    "coverage_matrix",
    "default_catalogue",
    "default_mapping",
    "entry_techniques",
    "format_rule",
    "genome_techniques",
    "parse_rule",
    "pipeline_coverage",
    "KillChain",
    "KillChainStage",
    "SocConfig",
    "StageAssignment",
    "StageSpan",
    "SuspiciousActivity",
    "apply_multiplier",
    "assign_stage",
    "check_multiplier",
    "format_auth",
    "max_stage",
    "parse_auth",
    "reconstruct",
    "signal_mass",
    "soc_config",
    "stage_keys",
    "stage_rank",
]
