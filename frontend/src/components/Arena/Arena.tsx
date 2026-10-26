/**
 * Red-vs-Blue arena view (Req 38.3): the Attacker_Agent (red) against HELIX detection and the
 * Immune_System (blue). Shows the Attack_Campaigns, the evasion mutations of each
 * Pattern_Genome, the campaigns HELIX missed, the Antibody_Pool size, each Antibody's lineage
 * and the detection rate over time.
 *
 * Data: GET /attacker and GET /immune (helix/api/immune_routes.py), GET /metrics for the
 * Evaluation_Monitor recall (normalised by the LoopPanel view-model). Every score and state
 * is written out as text; colour and marker shape only repeat it.
 */

import { useEffect, useId, useMemo, useState, type ReactNode } from "react";
import { ApiError, READ_TIMEOUT_MS, request } from "../../api/client";
import { formatTime } from "../../format";
import { ViewHeader } from "../common/ViewHeader";
import { formatRatio, normaliseMetrics, NOT_APPLICABLE, type LoopView } from "../LoopPanel/loop";
import {
  chronological,
  detectionSeries,
  genomeChain,
  missedCampaigns,
  mutationsOf,
  normaliseAttacker,
  normaliseImmune,
  pct,
  type AttackerView,
  type CampaignView,
  type DetectionPoint,
  type GenomeView,
  type ImmuneView,
} from "./arenaModel";
import "./arena.css";

/** Refresh period of the arena documents. */
export const ARENA_POLL_MS = 5_000;
/** Recent items requested from /attacker and /immune (the server's maximum). */
export const ARENA_RECENT = 200;

type Source<T> = { kind: "loading" } | { kind: "ok"; view: T } | { kind: "absent" } | { kind: "error"; message: string };

function settle<T>(r: PromiseSettledResult<unknown>, normalise: (d: unknown) => T | null, prev: Source<T>): Source<T> {
  if (r.status === "fulfilled") {
    const view = normalise(r.value);
    return view === null ? { kind: "error", message: "unexpected response" } : { kind: "ok", view };
  }
  const e = r.reason;
  if (e instanceof ApiError && e.kind === "aborted") return prev;
  if (e instanceof ApiError && e.kind === "http" && (e.status === 404 || e.status === 501)) return { kind: "absent" };
  return { kind: "error", message: e instanceof ApiError ? e.describe() : String(e) };
}

function useArenaData(pollMs: number) {
  const [attacker, setAttacker] = useState<Source<AttackerView>>({ kind: "loading" });
  const [immune, setImmune] = useState<Source<ImmuneView>>({ kind: "loading" });
  const [metrics, setMetrics] = useState<Partial<LoopView>>({});
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);

  useEffect(() => {
    let alive = true;
    let ctrl: AbortController | null = null;
    const tick = async () => {
      ctrl?.abort();
      const c = new AbortController();
      ctrl = c;
      const opts = { signal: c.signal, timeoutMs: READ_TIMEOUT_MS };
      const [at, im, me] = await Promise.allSettled([
        request<unknown>(`/attacker?recent=${ARENA_RECENT}`, opts),
        request<unknown>(`/immune?recent=${ARENA_RECENT}`, opts),
        request<unknown>("/metrics", opts),
      ]);
      if (!alive || c.signal.aborted) return;
      setAttacker((p) => settle(at, normaliseAttacker, p));
      setImmune((p) => settle(im, normaliseImmune, p));
      if (me.status === "fulfilled") setMetrics(normaliseMetrics(me.value));
      setUpdatedAt(Date.now());
    };
    void tick();
    const timer = setInterval(() => void tick(), pollMs);
    return () => {
      alive = false;
      clearInterval(timer);
      ctrl?.abort();
    };
  }, [pollMs]);

  return { attacker, immune, metrics, updatedAt };
}

const t = (iso: string | null) => (iso === null ? "n/a" : formatTime(iso));
const ev3 = (x: number | null) => (x === null ? "n/a" : x.toFixed(3));

// Why a value reads "n/a". Shown as a tooltip and as screen-reader text (table cells), or as a
// small muted note (scoreboard), so no "n/a" is left unexplained.
export const NA_REASON = {
  deadline: "not scored yet: scored after the 10-min detection deadline",
  notRunning: "attacker not running",
  notReported: "not reported by this server",
  paramsNotReported: "parameters not reported by this server",
  noParent: "seed genome: no parent to compare with",
  parentUnknown: "parent's evasion not in the reported genomes",
  noEvents: "no Transaction_Events injected",
  immuneOff: "Immune_System not enabled",
  missOff: "miss detection is off on this server",
  noMetrics: "GET /metrics has not reported evaluation",
} as const;

/** "n/a" with its reason: title plus screen-reader text, or a visible muted note. */
function NA({ reason, visible = false, big = false }: { reason: string; visible?: boolean; big?: boolean }) {
  const na = big ? <Big>n/a</Big> : "n/a";
  return (
    <span className="arena-na" title={reason} data-testid="arena-na">
      {na}
      {visible ? <span className="muted small arena-na__reason"> ({reason})</span> : <span className="sr-only"> ({reason})</span>}
    </span>
  );
}

function sourceReason(name: string, s: Source<unknown>): string {
  if (s.kind === "loading") return `waiting for GET ${name}`;
  if (s.kind === "absent") return `GET ${name} is not available on this server`;
  if (s.kind === "error") return `GET ${name} failed`;
  return NA_REASON.notReported;
}

/** Reason the red-side scores are n/a. */
function attackerReason(src: Source<AttackerView>, a: AttackerView | null): string {
  if (a === null) return sourceReason("/attacker", src);
  if (!a.configured || a.running === false) return NA_REASON.notRunning;
  return NA_REASON.deadline;
}

/** Reason the blue-side Immune_System figures are n/a. */
function immuneReason(src: Source<ImmuneView>, im: ImmuneView | null): string {
  if (im === null) return sourceReason("/immune", src);
  return im.enabled ? NA_REASON.notReported : NA_REASON.immuneOff;
}

/** Section with its own heading, labelled for landmark navigation. */
function Section({ title, className, hint, children }: { title: string; className?: string; hint?: string; children: ReactNode }) {
  const id = useId();
  return (
    <section className={`arena-card ${className ?? ""}`} aria-labelledby={id}>
      <h3 id={id} className="arena-card__title hx-view-card__title">
        {title}
      </h3>
      {hint ? <p className="arena-card__hint">{hint}</p> : null}
      {children}
    </section>
  );
}

/** One scoreboard figure: label, then the value (its headline number set large by <Big>). */
function Stat({ label, testId, children }: { label: string; testId?: string; children: ReactNode }) {
  return (
    <div className="arena-stat">
      <dt>{label}</dt>
      <dd data-testid={testId}>{children}</dd>
    </div>
  );
}

function Big({ children }: { children: ReactNode }) {
  return <span className="arena-stat__big">{children}</span>;
}

/** Horizontally scrollable table wrapper that can be scrolled from the keyboard. */
function TableScroll({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="arena-scroll" tabIndex={0} role="group" aria-label={label}>
      {children}
    </div>
  );
}

function Empty({ children }: { children: ReactNode }) {
  return <p className="muted arena-empty">{children}</p>;
}

function sourceNote(name: string, s: Source<unknown>): ReactNode {
  if (s.kind === "loading") return <Empty>Loading {name}…</Empty>;
  if (s.kind === "absent") return <Empty>GET {name} is not available on this server.</Empty>;
  if (s.kind === "error")
    return (
      <p className="msg msg--error small" role="alert">
        GET {name} failed: {s.message}
      </p>
    );
  return null;
}

// --- red side ------------------------------------------------------------------------------

function attackerState(a: AttackerView | null): string {
  if (a === null) return "n/a";
  if (!a.configured) return "not attached";
  return a.running === null ? "attached" : a.running ? "running" : "stopped";
}

function detectionOutcome(c: CampaignView): { text: string; tone: "ok" | "warn" | "bad" | "none" } {
  if (!c.completed) return { text: "In progress (detection window open)", tone: "none" };
  if (c.evasion === null) return { text: "Not scored (no Transaction_Events injected)", tone: "none" };
  if (c.evasion <= 0) return { text: "Detected: 100% of events", tone: "ok" };
  if (c.evasion >= 1) return { text: "Missed: 0% of events detected", tone: "bad" };
  return { text: `Partially missed: ${pct(1 - c.evasion)} detected`, tone: "warn" };
}

function CampaignsTable({ campaigns }: { campaigns: CampaignView[] }) {
  if (campaigns.length === 0) return <Empty>No Attack_Campaigns yet.</Empty>;
  return (
    <TableScroll label="Attack_Campaigns table">
      <table className="table table--compact" data-testid="arena-campaigns">
        <caption className="sr-only">Attack_Campaigns, newest first, with their detection outcome</caption>
        <thead>
          <tr>
            <th scope="col">Campaign</th>
            <th scope="col">Pattern</th>
            <th scope="col">Genome</th>
            <th scope="col">Started</th>
            <th scope="col" className="num">
              Planned
            </th>
            <th scope="col" className="num">
              Injected
            </th>
            <th scope="col" className="num">
              Withheld
            </th>
            <th scope="col" className="num">
              Evasion
            </th>
            <th scope="col">Outcome</th>
          </tr>
        </thead>
        <tbody>
          {campaigns.map((c) => {
            const o = detectionOutcome(c);
            return (
              <tr key={c.id} data-testid={`campaign-${c.id}`}>
                <th scope="row" className="mono">
                  {c.id}
                </th>
                <td>{c.patternType ?? <NA reason={NA_REASON.notReported} />}</td>
                <td className="mono">{c.genomeId}</td>
                <td>{t(c.startedAt)}</td>
                <td className="num">{c.events}</td>
                <td className="num">{c.injected}</td>
                <td className="num">{c.withheld}</td>
                <td className="num">
                  {c.evasion === null ? <NA reason={c.completed ? NA_REASON.noEvents : NA_REASON.deadline} /> : ev3(c.evasion)}
                </td>
                <td>
                  <span className={`arena-outcome arena-outcome--${o.tone}`}>{o.text}</span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </TableScroll>
  );
}

function GenomesTable({ genomes }: { genomes: GenomeView[] }) {
  const byId = useMemo(() => new Map(genomes.map((g) => [g.id, g])), [genomes]);
  if (genomes.length === 0) return <Empty>No scored Pattern_Genomes yet.</Empty>;
  const anyParams = genomes.some((g) => g.params !== null);
  return (
    <>
      {anyParams ? null : (
        <p className="muted small" data-testid="arena-params-note">
          Genome parameters are not reported by this server (no params in GET /attacker), so each mutation is shown as
          its lineage step (parent → child generation) and its effect on evasion.
        </p>
      )}
      <TableScroll label="Pattern_Genome mutations table">
        <table className="table table--compact" data-testid="arena-genomes">
          <caption className="sr-only">Top Pattern_Genomes by evasion, with the mutations that produced them</caption>
          <thead>
            <tr>
              <th scope="col">Genome</th>
              <th scope="col">Pattern</th>
              <th scope="col" className="num">
                Generation
              </th>
              <th scope="col">Mutated from</th>
              <th scope="col">Changed parameters</th>
              <th scope="col" className="num">
                Evasion
              </th>
              <th scope="col" className="num">
                vs parent
              </th>
              <th scope="col" className="num">
                Campaigns
              </th>
              <th scope="col">State</th>
            </tr>
          </thead>
          <tbody>
            {genomes.map((g) => {
              const chain = genomeChain(g.id, byId);
              const muts = mutationsOf(g, byId);
              const parentEv = g.parentId !== null ? (byId.get(g.parentId)?.evasion ?? null) : null;
              const delta = g.evasion !== null && parentEv !== null ? g.evasion - parentEv : null;
              return (
                <tr key={g.id} data-testid={`genome-${g.id}`}>
                  <th scope="row" className="mono">
                    {g.id}
                  </th>
                  <td>{g.patternType ?? <NA reason={NA_REASON.notReported} />}</td>
                  <td className="num">{g.generation ?? <NA reason={NA_REASON.notReported} />}</td>
                  <td className="mono">
                    {g.parentId === null ? "seed genome (no parent)" : chain.join(" → ")}
                  </td>
                  <td>
                    {g.parentId === null
                      ? "none"
                      : muts === null
                        ? (
                            <span title={NA_REASON.paramsNotReported}>
                              not reported<span className="sr-only"> ({NA_REASON.paramsNotReported})</span>
                            </span>
                          )
                        : muts.length === 0
                          ? "none"
                          : muts.map((m) => `${m.param} ${m.from} → ${m.to}`).join("; ")}
                  </td>
                  <td className="num">{g.evasion === null ? <NA reason={NA_REASON.deadline} /> : ev3(g.evasion)}</td>
                  <td className="num">
                    {delta === null ? (
                      <NA
                        reason={
                          g.parentId === null ? NA_REASON.noParent : g.evasion === null ? NA_REASON.deadline : NA_REASON.parentUnknown
                        }
                      />
                    ) : (
                      `${delta >= 0 ? "+" : "−"}${Math.abs(delta).toFixed(3)}`
                    )}
                  </td>
                  <td className="num">{g.scoredCampaigns ?? <NA reason={NA_REASON.notReported} />}</td>
                  <td>{g.retired ? "retired" : "active"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </TableScroll>
    </>
  );
}

// --- blue side -----------------------------------------------------------------------------

function AntibodyTable({ immune }: { immune: ImmuneView }) {
  const abs = immune.antibodies;
  if (abs.length === 0) return <Empty>No Antibodies created yet.</Empty>;
  return (
    <>
      {immune.antibodiesTotal !== null && immune.antibodiesTotal > abs.length ? (
        <p className="muted small">
          Showing the newest {abs.length} of {immune.antibodiesTotal} Antibodies.
        </p>
      ) : null}
      <TableScroll label="Antibody lineage table">
        <table className="table table--compact" data-testid="arena-antibodies">
          <caption className="sr-only">Antibody lineage, newest first</caption>
          <thead>
            <tr>
              <th scope="col">Antibody</th>
              <th scope="col">Status</th>
              <th scope="col">Origin</th>
              <th scope="col">Parent</th>
              <th scope="col">Created</th>
              <th scope="col">Activated</th>
              <th scope="col">Dimensions</th>
              <th scope="col" className="num">
                Matches
              </th>
              <th scope="col" className="num">
                Confirmed / false positive
              </th>
              <th scope="col">Ended</th>
            </tr>
          </thead>
          <tbody>
            {abs.map((a) => (
              <tr key={a.id} data-testid={`antibody-${a.id}`}>
                <th scope="row" className="mono">
                  {a.id}
                </th>
                <td>
                  <span className="arena-status">{a.status}</span>
                </td>
                <td className="mono">{a.origin ?? <NA reason={NA_REASON.notReported} />}</td>
                <td className="mono">{a.parentId ?? "none"}</td>
                <td>{t(a.createdAt)}</td>
                <td>{a.activatedAt === null ? "not activated" : t(a.activatedAt)}</td>
                <td>{a.dims.length ? a.dims.join(", ") : <NA reason={NA_REASON.notReported} />}</td>
                <td className="num">{a.matches}</td>
                <td className="num">
                  {a.confirmedFraudMatches} / {a.falsePositiveMatches}
                </td>
                <td>{a.endedAt === null ? "—" : `${t(a.endedAt)} (${a.endReason ?? "reason n/a"})`}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </TableScroll>
    </>
  );
}

function MissedTable({ attacker, immune }: { attacker: AttackerView; immune: ImmuneView | null }) {
  const rows = missedCampaigns(attacker.campaigns, immune?.recentMisses ?? []);
  if (rows.length === 0)
    return <Empty>{attacker.campaignsCompleted === 0 ? "No completed campaigns yet." : "HELIX detected every scored campaign shown."}</Empty>;
  return (
    <TableScroll label="Missed campaigns table">
      <table className="table table--compact" data-testid="arena-missed">
        <caption className="sr-only">Completed Attack_Campaigns not fully detected by HELIX, newest first</caption>
        <thead>
          <tr>
            <th scope="col">Campaign</th>
            <th scope="col">Pattern</th>
            <th scope="col">Genome</th>
            <th scope="col">Outcome</th>
            <th scope="col" className="num">
              Undetected
            </th>
            <th scope="col" className="num">
              Miss records
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map(({ campaign: c, outcome, missRecords }) => (
            <tr key={c.id} data-testid={`missed-${c.id}`}>
              <th scope="row" className="mono">
                {c.id}
              </th>
              <td>{c.patternType ?? <NA reason={NA_REASON.notReported} />}</td>
              <td className="mono">{c.genomeId}</td>
              <td>
                <span className={`arena-outcome arena-outcome--${outcome === "fully missed" ? "bad" : "warn"}`}>
                  {outcome === "fully missed" ? "Fully missed" : "Partially missed"}
                </span>
              </td>
              <td className="num">{pct(c.evasion)}</td>
              <td className="num">{immune?.missDetection ? missRecords : <NA reason={NA_REASON.missOff} />}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </TableScroll>
  );
}

// --- detection rate over time --------------------------------------------------------------

const W = 1080;
const H = 220;
const PAD = { l: 44, r: 12, t: 14, b: 28 };

function DetectionChart({ series }: { series: DetectionPoint[] }) {
  const titleId = useId();
  const descId = useId();
  if (series.length === 0) return <Empty>No scored campaigns yet; the detection rate appears once a campaign completes.</Empty>;
  const n = series.length;
  const x = (i: number) => PAD.l + (n === 1 ? (W - PAD.l - PAD.r) / 2 : (i * (W - PAD.l - PAD.r)) / (n - 1));
  const y = (r: number) => PAD.t + (1 - r) * (H - PAD.t - PAD.b);
  const last = series[n - 1]!;
  const rates = series.map((p) => p.rate);
  const summary =
    `${n} scored campaign${n === 1 ? "" : "s"}, oldest to newest. Latest ${last.campaignId}: ${pct(last.rate)} detected; ` +
    `running mean ${pct(last.cumulative)}. Lowest ${pct(Math.min(...rates))}, highest ${pct(Math.max(...rates))}.`;
  const line = series.map((p, i) => `${x(i).toFixed(1)},${y(p.cumulative).toFixed(1)}`).join(" ");

  return (
    <figure className="arena-chart" data-testid="arena-detection">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-labelledby={`${titleId} ${descId}`} className="arena-chart__svg">
        <title id={titleId}>Detection rate over time</title>
        <desc id={descId}>{summary}</desc>
        {[0, 0.5, 1].map((r) => (
          <g key={r} aria-hidden="true">
            <line x1={PAD.l} x2={W - PAD.r} y1={y(r)} y2={y(r)} className="arena-chart__grid" />
            <text x={PAD.l - 6} y={y(r) + 4} textAnchor="end" className="arena-chart__axis">
              {r * 100}%
            </text>
          </g>
        ))}
        <polyline points={line} className="arena-chart__mean" aria-hidden="true" />
        {series.map((p, i) => (
          <rect
            key={p.campaignId}
            x={x(i) - 3.5}
            y={y(p.rate) - 3.5}
            width={7}
            height={7}
            className="arena-chart__point"
            aria-hidden="true"
          />
        ))}
        <text x={PAD.l} y={H - 8} className="arena-chart__axis" aria-hidden="true">
          {series[0]!.campaignId}
        </text>
        <text x={W - PAD.r} y={H - 8} textAnchor="end" className="arena-chart__axis" aria-hidden="true">
          {last.campaignId}
        </text>
      </svg>
      <figcaption className="small">
        <span className="arena-legend">
          <span className="arena-legend__point" aria-hidden="true" /> squares: detection rate of each campaign
        </span>
        <span className="arena-legend">
          <span className="arena-legend__mean" aria-hidden="true" /> dashed line: running mean
        </span>
        <span className="sr-only">{summary}</span>
      </figcaption>
      <details className="arena-details">
        <summary>Detection rate data table</summary>
        <TableScroll label="Detection rate data">
          <table className="table table--compact" data-testid="arena-detection-table">
            <caption className="sr-only">Detection rate per scored campaign, oldest first</caption>
            <thead>
              <tr>
                <th scope="col">Campaign</th>
                <th scope="col">Started</th>
                <th scope="col" className="num">
                  Detected
                </th>
                <th scope="col" className="num">
                  Running mean
                </th>
              </tr>
            </thead>
            <tbody>
              {series.map((p) => (
                <tr key={p.campaignId}>
                  <th scope="row" className="mono">
                    {p.campaignId}
                  </th>
                  <td>{t(p.startedAt)}</td>
                  <td className="num">{pct(p.rate)}</td>
                  <td className="num">{pct(p.cumulative)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
      </details>
    </figure>
  );
}

// --- view ----------------------------------------------------------------------------------

function attackerHelp(a: AttackerView): string {
  return a.available
    ? "The Attacker_Agent is not attached. Start the API server with --attacker (or attacker.enabled) to run campaigns."
    : "The Attacker_Agent is not available: it needs the Synthetic_Generator, which this server runs without.";
}

const IMMUNE_HELP = "The Immune_System is not enabled. Start the API server with --immune (or immune.enabled) to grow Antibodies.";

export function Arena({ pollMs = ARENA_POLL_MS }: { pollMs?: number }) {
  const { attacker, immune, metrics, updatedAt } = useArenaData(pollMs);
  const a = attacker.kind === "ok" ? attacker.view : null;
  const im = immune.kind === "ok" ? immune.view : null;
  const series = useMemo(() => (a ? detectionSeries(a.campaigns) : []), [a]);
  const missed = useMemo(() => (a ? missedCampaigns(a.campaigns, im?.recentMisses ?? []) : []), [a, im]);
  const campaigns = useMemo(() => (a ? chronological(a.campaigns).reverse() : []), [a]);
  const ev = metrics.evaluation;
  const busy = attacker.kind === "loading" || immune.kind === "loading";
  const immuneOn = im !== null && im.enabled;
  const boardId = useId();
  const fullyMissed = missed.filter((m) => m.outcome === "fully missed").length;
  const poolOn = im !== null && im.enabled && im.pool !== null;
  const redReason = attackerReason(attacker, a);

  return (
    <div className="hx-view arena" role="region" aria-labelledby="arena-title" aria-busy={busy || undefined}>
      <ViewHeader
        id="arena-title"
        eyebrow="Red vs Blue"
        title="Red vs Blue arena"
        sub="An automated attacker keeps inventing new fraud patterns; HELIX and its immune system try to catch every one of them."
        aside={
          <span className="arena-updated" data-testid="arena-updated">
            {updatedAt ? `Updated ${formatTime(new Date(updatedAt).toISOString())}` : "Loading…"}
          </span>
        }
      />

      <section className="arena-board" aria-labelledby={boardId} data-testid="arena-score">
        <h3 id={boardId} className="sr-only">
          Scoreboard
        </h3>
        <div className="arena-board__grid" data-testid="arena-sides">
          <div className="arena-team arena-team--red">
            <p className="arena-team__name">
              Red team (Attacker_Agent): <strong className="arena-team__state">{a === null ? <NA reason={redReason} /> : attackerState(a)}</strong>
            </p>
            <p className="arena-team__role">The attacker: launches campaigns and mutates the patterns that slip through.</p>
            <dl className="arena-stats">
              <Stat label="Attack_Campaigns">
                {a ? (
                  <>
                    <Big>{a.campaignsTotal}</Big> total, {a.campaignsCompleted} completed, {a.campaignsLive} live
                  </>
                ) : (
                  <NA big visible reason={redReason} />
                )}
              </Stat>
              <Stat label="Mean evasion" testId="arena-mean-evasion">
                {a && a.meanEvasion !== null ? <Big>{ev3(a.meanEvasion)}</Big> : <NA big visible reason={redReason} />}
              </Stat>
              <Stat label="Campaigns missed" testId="arena-missed-count">
                {a ? (
                  <>
                    <Big>{missed.length}</Big> of {series.length} scored ({fullyMissed} fully)
                  </>
                ) : (
                  <NA big visible reason={redReason} />
                )}
              </Stat>
            </dl>
          </div>

          <p className="arena-vs" aria-hidden="true">
            vs
          </p>

          <div className="arena-team arena-team--blue">
            <p className="arena-team__name">
              Blue team (HELIX + Immune_System):{" "}
              <strong className="arena-team__state">
                {im === null ? <NA reason={immuneReason(immune, im)} /> : im.enabled ? "enabled" : "not enabled"}
              </strong>
            </p>
            <p className="arena-team__role">The defender: HELIX detection, plus antibodies grown from every miss.</p>
            <dl className="arena-stats">
              <Stat label="Detection rate (mean over campaigns)" testId="arena-rate">
                {a && a.meanEvasion !== null ? <Big>{pct(1 - a.meanEvasion)}</Big> : <NA big visible reason={redReason} />}
              </Stat>
              <Stat label="Antibody_Pool size" testId="arena-pool">
                {poolOn ? (
                  <Big>{`${im.pool!.size}${im.pool!.max !== null ? ` / ${im.pool!.max}` : ""}`}</Big>
                ) : (
                  <NA big visible reason={immuneReason(immune, im)} />
                )}
                {poolOn ? " active" : ""}
                {im && im.enabled ? `, ${im.held} held` : ""}
              </Stat>
              <Stat label="Miss records (Req 21.1)">
                {im && im.missDetection ? (
                  <>
                    <Big>{im.misses}</Big> plus {im.pendingAttackEvents} attack events awaiting their deadline
                  </>
                ) : (
                  <NA big visible reason={im && im.enabled ? NA_REASON.missOff : immuneReason(immune, im)} />
                )}
              </Stat>
              <Stat label="Recall, all labelled fraud" testId="arena-recall">
                {ev === undefined ? (
                  <NA big visible reason={NA_REASON.noMetrics} />
                ) : ev === NOT_APPLICABLE ? (
                  "not applicable (no Evaluation_Monitor)"
                ) : (
                  <Big>{formatRatio(ev.recall)}</Big>
                )}
              </Stat>
            </dl>
          </div>
        </div>
      </section>

      <div className="arena-grid">
        <Section
          title="Detection rate over time"
          className="arena-wide"
          hint="Share of each completed campaign's events that HELIX flagged, oldest to newest."
        >
          {a && a.configured ? <DetectionChart series={series} /> : <Empty>n/a (no Attacker_Agent campaigns to score)</Empty>}
        </Section>

        <Section
          title="Red: Attack_Campaigns"
          className="arena-red"
          hint="Every attack the red team launched, newest first, and how much of it was caught."
        >
          {sourceNote("/attacker", attacker)}
          {a && !a.configured ? <Empty>{attackerHelp(a)}</Empty> : null}
          {a && a.configured ? <CampaignsTable campaigns={campaigns} /> : null}
        </Section>

        <Section
          title="Blue: Antibody_Pool and lineage"
          className="arena-blue"
          hint="Antibodies grown from missed attacks, and the antibody each one descends from."
        >
          {sourceNote("/immune", immune)}
          {im && !im.enabled ? <Empty>{IMMUNE_HELP}</Empty> : null}
          {immuneOn ? (
            <>
              <p className="arena-note" data-testid="arena-pool-detail">
                Pool: <strong>{im.pool?.size ?? "n/a"}</strong>
                {im.pool?.max != null ? ` of ${im.pool.max}` : ""} active, {im.held} held candidates. Self_Set {im.selfSetSize}
                {im.selfSetMin !== null ? ` (minimum ${im.selfSetMin} before activation)` : ""}. {im.triggers} triggers,{" "}
                {im.discards} discards, {im.retirements} retirements.
              </p>
              <AntibodyTable immune={im} />
            </>
          ) : null}
        </Section>

        <Section
          title="Pattern_Genome evasion mutations"
          className="arena-red"
          hint="The attack recipes that evade best, and what changed from their parent."
        >
          {a && a.configured ? <GenomesTable genomes={a.genomes} /> : <Empty>n/a ({redReason})</Empty>}
        </Section>

        <Section
          title="Campaigns missed by HELIX"
          className="arena-red"
          hint="Completed campaigns HELIX did not fully catch; each one teaches the immune system."
        >
          {a && a.configured ? <MissedTable attacker={a} immune={im} /> : <Empty>n/a ({redReason})</Empty>}
        </Section>
      </div>
    </div>
  );
}
