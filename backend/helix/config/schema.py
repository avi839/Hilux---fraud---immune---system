"""HelixConfig: nested, frozen Pydantic models with `Range` metadata.

Ranges are attached as `Annotated` metadata instead of Pydantic constraints so
that loading never stops at the first bad value. Pydantic only checks types;
`HelixConfig.validate()` (see `validation.py`) checks every range and
cross-field rule and returns all problems at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Literal, Optional, Union

from pydantic import AwareDatetime, BaseModel, ConfigDict


@dataclass(frozen=True)
class Range:
    """Permitted closed range [lo, hi]; `lo_open` makes it (lo, hi].

    `runtime` marks settings that may be changed via PATCH /api/v1/config.
    """

    lo: float
    hi: float
    lo_open: bool = False
    runtime: bool = False

    def contains(self, value: float) -> bool:
        if value != value:  # NaN is never in range
            return False
        lower_ok = value > self.lo if self.lo_open else value >= self.lo
        return lower_ok and value <= self.hi

    def describe(self) -> str:
        left = "(" if self.lo_open else "["
        return f"must be in {left}{_fmt(self.lo)}, {_fmt(self.hi)}]"


def _fmt(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else repr(x)


@dataclass(frozen=True)
class ConfigError:
    path: str  # e.g. "fusion.high_conflict"
    value: object
    rule: str  # e.g. "must be in (0, 1]"

    def __str__(self) -> str:
        return f"{self.path}={self.value!r}: {self.rule}"


class SeverityTier(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


TIER_ORDER: tuple[SeverityTier, ...] = (
    SeverityTier.LOW,
    SeverityTier.MEDIUM,
    SeverityTier.HIGH,
    SeverityTier.CRITICAL,
)


class Band(BaseModel):
    """Risk_Score band [lo, hi); the CRITICAL band also includes 1.0."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    tier: SeverityTier
    lo: float
    hi: float


DEFAULT_BANDS: tuple[Band, ...] = (
    Band(tier=SeverityTier.LOW, lo=0.6, hi=0.7),
    Band(tier=SeverityTier.MEDIUM, lo=0.7, hi=0.8),
    Band(tier=SeverityTier.HIGH, lo=0.8, hi=0.9),
    Band(tier=SeverityTier.CRITICAL, lo=0.9, hi=1.0),
)

_DAY = 86_400
_SECTION = ConfigDict(frozen=True, extra="forbid")


class IngestConfig(BaseModel):  # Req 1.4, 1.6, 1.8
    model_config = _SECTION
    lateness_tolerance_s: Annotated[float, Range(0, _DAY)] = 300
    backpressure_limit: Annotated[int, Range(100, 1_000_000)] = 10_000
    # Must be >= graph.window_s (Req 1.6). Default equals the 7-day Graph_Window.
    dedup_ttl_s: Annotated[float, Range(60, 30 * _DAY)] = 7 * _DAY


class FusionConfig(BaseModel):  # Req 5.14, 6.7, 6.11
    model_config = _SECTION
    high_conflict: Annotated[float, Range(0, 1, lo_open=True)] = 0.95
    sensor_budget_ms: Annotated[float, Range(1, 1_000)] = 10


class SensorConfig(BaseModel):  # Glossary Minimum_History_Count, Req 5.7
    model_config = _SECTION
    min_history_count: Annotated[int, Range(1, 100)] = 5
    plausible_speed_kmh: Annotated[float, Range(1, 20_000)] = 900


class MemoryConfig(BaseModel):  # Req 8.6, 8.8
    model_config = _SECTION
    half_life_days: Annotated[float, Range(1, 365)] = 30


class ChangePointConfig(BaseModel):  # Req 9.3, 9.5, 9.6, glossary
    model_config = _SECTION
    threshold: Annotated[float, Range(0, 1)] = 0.5
    regime_confirmation_count: Annotated[int, Range(1, 1_000)] = 10
    abrupt_break_mass: Annotated[float, Range(0, 1)] = 0.4
    regime_shift_mass: Annotated[float, Range(0, 1)] = 0.4


class GraphConfig(BaseModel):  # Graph_Window, Reporting_Threshold, Req 11.10, 11.12
    model_config = _SECTION
    window_s: Annotated[float, Range(3_600, 30 * _DAY)] = 7 * _DAY
    motif_budget_ms: Annotated[float, Range(1, 1_000)] = 20
    reporting_threshold: Annotated[float, Range(0, 1e12, lo_open=True)] = 10_000
    # Motif_Detector shape parameters (task 29.1). Defaults are the Req 11 values; the
    # AMLworld config (config/amlworld.json) widens them for multi-day laundering patterns.
    fan_min_counterparties: Annotated[int, Range(2, 1_000)] = 8
    fan_span_s: Annotated[float, Range(60, 30 * _DAY)] = _DAY
    round_trip_max_accounts: Annotated[int, Range(2, 12)] = 6
    round_trip_span_s: Annotated[float, Range(60, 30 * _DAY)] = 3 * _DAY


class RiskConfig(BaseModel):  # Req 13.4, 13.7, 13.11, 13.12
    model_config = _SECTION
    alert_threshold: Annotated[float, Range(0, 1)] = 0.6
    bands: tuple[Band, ...] = DEFAULT_BANDS
    # Case size bound (task 19.1, Req 12, 25): routing, Motif grouping and merging never grow
    # an open Case beyond this many accounts / Transaction_Events. A Case that is full takes
    # no more growth; new evidence goes to another Case (see helix.cases.manager).
    case_max_accounts: Annotated[int, Range(2, 100_000)] = 50
    case_max_events: Annotated[int, Range(1, 1_000_000)] = 500
    # Motif instances per Case: a full Case takes further instances only of a MotifType it
    # does not hold yet (so stage-2 risk is unchanged: one instance per type is fused).
    case_max_motifs: Annotated[int, Range(1, 1_000_000)] = 100


class PrioritiserConfig(BaseModel):  # Req 14.3, 14.10, 14.15
    model_config = _SECTION
    alert_budget: Annotated[int, Range(1, 1_000, runtime=True)] = 20
    investigation_cost: Annotated[float, Range(0, 1e9, runtime=True)] = 50.0
    period_s: Annotated[int, Range(60, _DAY)] = 3_600


class ReplayConfig(BaseModel):  # Req 17.1, 17.3, 17.4, 17.9
    model_config = _SECTION
    hops: Annotated[int, Range(1, 4)] = 2
    max_nodes: Annotated[int, Range(50, 500)] = 500
    lead_h: Annotated[float, Range(0, 168)] = 24


class StorageConfig(BaseModel):  # Req 24.3, 24.4
    model_config = _SECTION
    path: str = "helix.duckdb"
    snapshot_interval_s: Annotated[float, Range(5, 3_600)] = 60


class GeneratorConfig(BaseModel):  # Req 3.1-3.12
    model_config = _SECTION
    accounts: Annotated[int, Range(1, 100_000)] = 1_000
    default_seed: Annotated[int, Range(0, 4_294_967_295)] = 42
    rate_eps: Annotated[float, Range(1, 1_000)] = 50
    change_fraction: Annotated[float, Range(0, 1)] = 0.05
    omit_frac: Annotated[float, Range(0, 1)] = 0.10
    # Not specified in requirements; range chosen for the travel behaviour change.
    travel_duration_h: Annotated[float, Range(1, 24 * 30)] = 72


class DatasetConfig(BaseModel):  # Req 4.4, 4.12, 4.13, 4.16
    model_config = _SECTION
    speed: Union[Literal["max"], Annotated[float, Range(1, 1_000_000)]] = 60.0
    timezone: str = "UTC"
    scope_start: Optional[AwareDatetime] = None
    scope_end: Optional[AwareDatetime] = None
    scope_max_rows: Optional[Annotated[int, Range(1, 10**12)]] = None


class LearnConfig(BaseModel):  # Req 19.3
    model_config = _SECTION
    weight_lower: Annotated[float, Range(0, 1)] = 0.1
    weight_upper: Annotated[float, Range(0, 1)] = 1.0
    learning_rate: Annotated[float, Range(0, 0.5, lo_open=True)] = 0.05


class AttackerConfig(BaseModel):  # Req 20.3-20.15
    model_config = _SECTION
    enabled: bool = False
    launch_interval_s: Annotated[float, Range(1, _DAY)] = 300
    max_events: Annotated[int, Range(1, 10_000)] = 200
    detection_window_s: Annotated[float, Range(60, 7 * _DAY)] = _DAY
    top_k: Annotated[int, Range(1, 100)] = 5
    children_per_parent: Annotated[int, Range(1, 100)] = 3
    attack_fraction: Annotated[float, Range(0, 1)] = 0.02
    active_genome_cap: Annotated[int, Range(1, 10_000)] = 100


class ImmuneConfig(BaseModel):  # Req 21.1-21.11
    model_config = _SECTION
    enabled: bool = False
    detection_deadline_s: Annotated[float, Range(1, _DAY)] = 600
    candidates_per_trigger: Annotated[int, Range(1, 50)] = 10
    self_set_min: Annotated[int, Range(1, 1_000_000)] = 1_000
    self_match_limit: Annotated[float, Range(0, 1)] = 0.005
    hold_expiry_s: Annotated[float, Range(60, _DAY)] = 3_600
    pool_max: Annotated[int, Range(1, 10_000)] = 200
    affinity_window_s: Annotated[float, Range(60, 30 * _DAY)] = _DAY
    fp_limit: Annotated[int, Range(0, 1_000)] = 3
    # Not specified in requirements beyond the defaults of Req 21.2 / design §18.
    # ±10% of each signature parameter; capped at 0.5 so a clone's half-width factor
    # (1 - r) stays >= 0.5 and every clone still covers its origin signature.
    mutation_rate: Annotated[float, Range(0, 0.5)] = 0.1
    # Base half-width w per dimension; the signature half-width is (w + r·c) / (1 - r).
    antibody_half_width: Annotated[float, Range(0, 1, lo_open=True)] = 0.05
    antibody_mass: Annotated[float, Range(0, 1, lo_open=True)] = 0.5  # m({FRAUD}) on a match
    self_set_capacity: Annotated[int, Range(1, 1_000_000)] = 5_000  # reservoir size (Algorithm R)


class NarrativeConfig(BaseModel):  # Req 26.6
    model_config = _SECTION
    enabled: bool = False
    timeout_s: Annotated[float, Range(1, 120)] = 15


class KillChainStage(str, Enum):
    """Kill_Chain_Stage (Req 29.1). Declaration order is the stage order."""

    RECONNAISSANCE = "RECONNAISSANCE"
    CREDENTIAL_ACCESS = "CREDENTIAL_ACCESS"
    ACCOUNT_TAKEOVER = "ACCOUNT_TAKEOVER"
    PAYEE_MANIPULATION = "PAYEE_MANIPULATION"
    CASH_OUT = "CASH_OUT"
    LAYERING = "LAYERING"

    def __str__(self) -> str:
        return self.value


KILL_CHAIN_ORDER: tuple[KillChainStage, ...] = tuple(KillChainStage)

#: Contributor-to-stage mapping (Req 29.2). Keys are the stage keys computed by
#: ``helix.cases.killchain.stage_keys``; anything not listed gets ``kill_chain_default_stage``
#: and is marked unmapped (Req 29.3). Velocity, timing, geo, drift, antibody, rule and
#: indicator evidence is deliberately left unmapped.
DEFAULT_KILL_CHAIN_MAPPING: dict[str, KillChainStage] = {
    "auth.failed_logins": KillChainStage.CREDENTIAL_ACCESS,
    "auth.mfa_failure": KillChainStage.CREDENTIAL_ACCESS,
    "auth.password_reset": KillChainStage.ACCOUNT_TAKEOVER,
    "auth.new_device": KillChainStage.ACCOUNT_TAKEOVER,
    "auth.new_payee": KillChainStage.PAYEE_MANIPULATION,
    "sensor.amount": KillChainStage.CASH_OUT,  # large transfer (FRAUD mass > 0)
    "sensor.counterparty": KillChainStage.CASH_OUT,  # unknown counterparty (FRAUD mass > 0)
    "motif.ROUND_TRIP_CYCLE": KillChainStage.LAYERING,
    "motif.FAN_OUT": KillChainStage.LAYERING,
    "motif.FAN_IN": KillChainStage.LAYERING,
    "motif.STRUCTURING": KillChainStage.LAYERING,
    "motif.MULE_CHAIN": KillChainStage.LAYERING,
    # Entries that carry no FRAUD evidence never advance a Case: they sit at the floor.
    "evidence.non_indicative": KillChainStage.RECONNAISSANCE,
    "auth.linked": KillChainStage.RECONNAISSANCE,
    "record.FUSED": KillChainStage.RECONNAISSANCE,
    "record.MISSING_FIELD": KillChainStage.RECONNAISSANCE,
    "record.SENSOR_FAILURE": KillChainStage.RECONNAISSANCE,
    "record.LATE": KillChainStage.RECONNAISSANCE,
    "record.TRUNCATION": KillChainStage.RECONNAISSANCE,
    "record.CHANGE_POINT": KillChainStage.RECONNAISSANCE,
}


class SocConfig(BaseModel):  # Req 28, 29 (Increment 4 cyber fusion); everything is off by default
    model_config = _SECTION
    enabled: bool = False
    # Auth_Correlation_Window and Suspicious_Auth_Activity (glossary)
    auth_correlation_window_s: Annotated[float, Range(60, _DAY)] = 1_800
    failed_login_count: Annotated[int, Range(1, 100)] = 5
    failed_login_window_s: Annotated[float, Range(60, _DAY)] = 600
    # Bounded auth history (retention is at least the correlation window in code)
    auth_retention_s: Annotated[float, Range(60, 30 * _DAY)] = _DAY
    auth_history_max_events: Annotated[int, Range(10, 100_000)] = 500
    auth_max_accounts: Annotated[int, Range(1, 10_000_000)] = 1_000_000
    known_devices_max: Annotated[int, Range(1, 10_000)] = 64
    # Auth_Sensor masses on {FRAUD} per signal, combined as 1 - prod(1 - m), capped
    m_failed_logins: Annotated[float, Range(0, 1)] = 0.35
    m_mfa_failure: Annotated[float, Range(0, 1)] = 0.25
    m_password_reset: Annotated[float, Range(0, 1)] = 0.30
    m_new_device: Annotated[float, Range(0, 1)] = 0.35
    m_new_payee: Annotated[float, Range(0, 1)] = 0.30
    m_payee_match: Annotated[float, Range(0, 1)] = 0.45  # new payee == this transfer's dst
    max_fraud_mass: Annotated[float, Range(0, 1)] = 0.9
    benign_link_legit_mass: Annotated[float, Range(0, 1)] = 0.1
    # Synthetic_Generator benign auth traffic (Req 28.5)
    login_failure_frac: Annotated[float, Range(0, 1)] = 0.02
    # Kill chain (Req 29.2, 29.3, 29.6, 29.8). Below 1 fails validation, so the change is
    # rejected and the previous value kept (Req 29.8).
    kill_chain_multiplier: Annotated[float, Range(1, 100)] = 1.5
    kill_chain_default_stage: KillChainStage = KillChainStage.ACCOUNT_TAKEOVER
    kill_chain_mapping: dict[str, KillChainStage] = DEFAULT_KILL_CHAIN_MAPPING
    # Detection_Rules (Req 31.1): YAML directory loaded at startup; None = API-submitted only
    rules_dir: Optional[str] = None
    # Response_Playbooks (Req 32.5, 32.6): Playbook_Approval_Threshold and global dry run
    playbook_approval_threshold: SeverityTier = SeverityTier.HIGH
    playbook_dry_run: bool = False
    # Canaries (Req 33.1): per-kind counts of the seeded decoy set
    canary_accounts: Annotated[int, Range(0, 10_000)] = 10
    canary_payees: Annotated[int, Range(0, 10_000)] = 10
    canary_devices: Annotated[int, Range(0, 10_000)] = 10
    # Indicator_Store defaults (Req 34.1, 34.3)
    indicator_default_confidence: Annotated[int, Range(0, 100)] = 75
    indicator_default_expiry_days: Annotated[float, Range(0, 3_650, lo_open=True)] = 30.0
    indicator_base_mass: Annotated[float, Range(0, 1)] = 0.8


class TriageConfig(BaseModel):  # Req 41.1, 41.4 (investigator workload reduction)
    model_config = _SECTION
    # Auto_Triage is off by default (Req 41.1). Idle time is stream time (SimClock).
    enabled: bool = False
    auto_clear_after_s: Annotated[float, Range(60, 30 * _DAY)] = _DAY
    sweep_interval_s: Annotated[float, Range(1, _DAY)] = 60
    # Label_Suggester thresholds on Bel(FRAUD); cross-field: suggest_fp_belief < suggest_fraud_belief
    suggest_fraud_belief: Annotated[float, Range(0, 1)] = 0.7
    suggest_fp_belief: Annotated[float, Range(0, 1)] = 0.2


class ApiConfig(BaseModel):  # Req 23.8, 27.1, 27.3
    model_config = _SECTION
    bind_host: str = "127.0.0.1"
    port: Annotated[int, Range(1, 65_535)] = 8000
    dashboard_origin: str = "http://localhost:5173"
    page_size: Annotated[int, Range(1, 100)] = 50


class HelixConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ingest: IngestConfig = IngestConfig()
    fusion: FusionConfig = FusionConfig()
    sensors: SensorConfig = SensorConfig()
    memory: MemoryConfig = MemoryConfig()
    changepoint: ChangePointConfig = ChangePointConfig()
    graph: GraphConfig = GraphConfig()
    risk: RiskConfig = RiskConfig()
    prioritiser: PrioritiserConfig = PrioritiserConfig()
    replay: ReplayConfig = ReplayConfig()
    storage: StorageConfig = StorageConfig()
    generator: GeneratorConfig = GeneratorConfig()
    dataset: DatasetConfig = DatasetConfig()
    learn: LearnConfig = LearnConfig()
    attacker: AttackerConfig = AttackerConfig()
    immune: ImmuneConfig = ImmuneConfig()
    narrative: NarrativeConfig = NarrativeConfig()
    api: ApiConfig = ApiConfig()
    soc: SocConfig = SocConfig()
    triage: TriageConfig = TriageConfig()

    # Overrides Pydantic's deprecated `BaseModel.validate` classmethod on purpose.
    def validate(self) -> list[ConfigError]:  # type: ignore[override]
        from .validation import validate_config

        return validate_config(self)
