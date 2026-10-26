// HELIX Benchmark: HELIX against a rule baseline, Isolation Forest and supervised XGBoost on
// IBM AMLworld test data. Every number comes from ./data/summary.json through benchmarkModel;
// the winner badges are computed per metric, so HELIX only earns a badge the data gives it.
// The "Why HELIX" matrix is qualitative (design capabilities) and labelled as such.

import "@fontsource-variable/sora";
import { useId, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { ViewHeader } from "../common/ViewHeader";
import {
  BADGE_TEXT,
  CAPACITIES,
  SUMMARY,
  TABLE_METRICS,
  at,
  dec3,
  dec5,
  hours,
  int,
  kpis,
  pct,
  randomPrAuc,
  rankingRows,
  badges,
  seedRange,
  systemsOf,
  tableBadges,
  usd,
  type Badge,
  type BenchmarkSummary,
  type Capacity,
  type SystemView,
} from "./benchmarkModel";
import "./benchmark.css";

const KPI_N: Capacity = "25";

// --- small pieces --------------------------------------------------------------------------

function Section({ title, eyebrow, children, testId }: { title: string; eyebrow?: string; children: ReactNode; testId?: string }) {
  const id = useId();
  return (
    <section className="hx-card bench-card" aria-labelledby={id} data-testid={testId}>
      {eyebrow ? <p className="bench-eyebrow">{eyebrow}</p> : null}
      <h3 id={id} className="hx-view-card__title bench-card__title">
        {title}
      </h3>
      {children}
    </section>
  );
}

function StarIcon() {
  return (
    <svg className="bench-badge__icon" viewBox="0 0 12 12" aria-hidden="true" focusable="false">
      <path d="M6 1.2 7.4 4.3l3.3.3-2.5 2.2.8 3.3L6 8.4 3 10.1l.8-3.3L1.3 4.6l3.3-.3z" fill="currentColor" />
    </svg>
  );
}

function BadgeChip({ badge }: { badge: Badge }) {
  return (
    <span className={`hx-chip bench-badge bench-badge--${badge}`} data-badge={badge}>
      <StarIcon />
      {BADGE_TEXT[badge]}
    </span>
  );
}

/** N = 25 | 50 | 100 as a radio group: arrow keys move and select, only the checked option is
 *  in the tab order, and a decorative pill slides under it (Pricing style). */
function CapacityToggle({ value, onChange }: { value: Capacity; onChange: (n: Capacity) => void }) {
  const refs = useRef(new Map<Capacity, HTMLButtonElement>());
  const index = CAPACITIES.indexOf(value);
  const onKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    const last = CAPACITIES.length - 1;
    let i: number | null = null;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") i = index === last ? 0 : index + 1;
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") i = index === 0 ? last : index - 1;
    else if (e.key === "Home") i = 0;
    else if (e.key === "End") i = last;
    if (i === null) return;
    e.preventDefault();
    const next = CAPACITIES[i]!;
    onChange(next);
    refs.current.get(next)?.focus();
  };
  return (
    <div
      className="bench-toggle"
      role="radiogroup"
      aria-label="Analyst capacity: alerts per hour"
      data-index={index}
      style={{ ["--bench-n" as string]: CAPACITIES.length, ["--bench-i" as string]: index }}
    >
      <span className="bench-toggle__pill" aria-hidden="true" />
      {CAPACITIES.map((n) => {
        const checked = n === value;
        return (
          <button
            key={n}
            ref={(el) => {
              if (el) refs.current.set(n, el);
              else refs.current.delete(n);
            }}
            type="button"
            role="radio"
            aria-checked={checked}
            tabIndex={checked ? 0 : -1}
            className="bench-toggle__opt"
            onClick={() => onChange(n)}
            onKeyDown={onKeyDown}
          >
            N = {n}
          </button>
        );
      })}
    </div>
  );
}

// --- KPI row ---------------------------------------------------------------------------------

function KpiRow({ systems }: { systems: SystemView[] }) {
  const tiles = useMemo(() => kpis(systems, KPI_N), [systems]);
  return (
    <ul className="bench-kpis" aria-label={`HELIX at N = ${KPI_N} alerts per hour`}>
      {tiles.map((k) => (
        <li key={k.metric.key} className="hx-kpi bench-kpi" data-testid={`kpi-${k.metric.key}`}>
          <span className="bench-kpi__label">{k.title}</span>
          <strong className="bench-kpi__value">{k.helix === null ? "n/a" : k.metric.format(k.helix)}</strong>
          {k.rival ? (
            <span className={`bench-kpi__cmp bench-kpi__cmp--${k.verdict}`} data-testid={`kpi-${k.metric.key}-cmp`}>
              <span className="bench-kpi__mark" aria-hidden="true">
                {k.verdict === "better" ? "▲" : k.verdict === "worse" ? "▼" : "="}
              </span>
              {k.verdict === "better" ? "Better than" : k.verdict === "worse" ? "Worse than" : "Equal to"} best label-free baseline ({k.rival.label}:{" "}
              {k.metric.format(k.rival.value)})
            </span>
          ) : null}
          {k.supervised ? (
            <span className="bench-kpi__sup">
              Supervised {k.supervised.label}: {k.metric.format(k.supervised.value)}
            </span>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

// --- comparison table ----------------------------------------------------------------------

function ComparisonTable({ systems, n, hoursTested }: { systems: SystemView[]; n: Capacity; hoursTested: number }) {
  const captionId = useId();
  const marks = useMemo(() => tableBadges(systems, n), [systems, n]);
  return (
    <div className="bench-scroll" role="region" aria-labelledby={captionId} tabIndex={0}>
      <table className="table bench-table" data-testid="bench-table">
        <caption id={captionId}>
          Results at N = {n} alerts per hour over the {hoursTested} h test window
        </caption>
        <thead>
          <tr>
            <th scope="col">System</th>
            <th scope="col">Labels needed</th>
            {TABLE_METRICS.map((m) => (
              <th key={m.key} scope="col" className="num">
                {m.label}
                {m.better ? <span className="bench-dir">{m.better === "higher" ? " (higher is better)" : " (lower is better)"}</span> : null}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {systems.map((s) => {
            const r = at(s, n);
            return (
              <tr key={s.id} data-testid={`bench-row-${s.id}`} className={s.family === "helix" ? "bench-row--helix" : undefined}>
                <th scope="row">
                  <span className="bench-sys">{s.label}</span>
                  <span className="bench-sys__key">{s.key}</span>
                </th>
                <td data-testid={`bench-labels-${s.id}`}>{s.labelsNeeded ? "Yes" : "No"}</td>
                {TABLE_METRICS.map((m) => {
                  const v = r?.[m.key];
                  const badge = marks[m.key]?.get(s.id);
                  const sr = r ? seedRange(r, m.key) : null;
                  return (
                    <td key={m.key} className="num" data-testid={`bench-cell-${s.id}-${m.key}`}>
                      <span className="bench-val">{typeof v === "number" ? m.format(v) : "n/a"}</span>
                      {sr ? (
                        <span className="bench-seed">
                          seeds {m.format(sr[0])} to {m.format(sr[1])}
                        </span>
                      ) : null}
                      {badge ? <BadgeChip badge={badge} /> : null}
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// --- bar chart -----------------------------------------------------------------------------

const PATTERNS = ["solid", "diag", "dots", "cross", "lines"] as const;
const W = 760;
const ROW = 46;
const LABEL_W = 170;
const VALUE_W = 92;

function ValueChart({ systems, n, cap }: { systems: SystemView[]; n: Capacity; cap: number }) {
  const rawId = useId();
  const uid = rawId.replace(/[^a-zA-Z0-9_-]/g, "");
  const titleId = `${uid}-title`;
  const descId = `${uid}-desc`;
  const rows = systems.map((s, i) => ({ s, v: at(s, n)?.value_capped_per_inv_hour_usd ?? 0, pattern: PATTERNS[i % PATTERNS.length]! }));
  const max = Math.max(1, ...rows.map((r) => r.v));
  const H = rows.length * ROW + 12;
  const span = W - LABEL_W - VALUE_W;
  const ranked = [...rows].sort((a, b) => b.v - a.v);
  const summary =
    `Capped value per investigator-hour at N = ${n}, highest first: ` +
    ranked.map((r) => `${r.s.label}${r.s.labelsNeeded ? " (supervised)" : ""} ${usd(r.v)}`).join("; ") +
    ".";
  return (
    <figure className="bench-chart" data-testid="bench-chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-labelledby={`${titleId} ${descId}`} className="bench-chart__svg">
        <title id={titleId}>Capped value per investigator-hour, N = {n}</title>
        <desc id={descId}>{summary}</desc>
        <defs>
          <pattern id={`${uid}-diag`} width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <rect width="8" height="8" className="bench-bar__bg" />
            <rect width="3.5" height="8" className="bench-bar__ink" />
          </pattern>
          <pattern id={`${uid}-dots`} width="7" height="7" patternUnits="userSpaceOnUse">
            <rect width="7" height="7" className="bench-bar__bg" />
            <circle cx="3.5" cy="3.5" r="1.8" className="bench-bar__ink" />
          </pattern>
          <pattern id={`${uid}-cross`} width="8" height="8" patternUnits="userSpaceOnUse">
            <rect width="8" height="8" className="bench-bar__bg" />
            <path d="M0 0 8 8M8 0 0 8" className="bench-bar__stroke" />
          </pattern>
          <pattern id={`${uid}-lines`} width="6" height="6" patternUnits="userSpaceOnUse">
            <rect width="6" height="6" className="bench-bar__bg" />
            <rect width="6" height="2.4" className="bench-bar__ink" />
          </pattern>
        </defs>
        {rows.map((r, i) => {
          const y = 6 + i * ROW;
          const w = Math.max(2, (r.v / max) * span);
          const fill = r.pattern === "solid" ? undefined : `url(#${uid}-${r.pattern})`;
          return (
            <g key={r.s.id}>
              <text x={LABEL_W - 12} y={y + ROW / 2 - 2} textAnchor="end" className="bench-chart__label">
                {r.s.label}
              </text>
              <text x={LABEL_W - 12} y={y + ROW / 2 + 12} textAnchor="end" className="bench-chart__note">
                {r.s.labelsNeeded ? "supervised" : "label-free"}
              </text>
              <rect
                x={LABEL_W}
                y={y + 9}
                width={w}
                height={ROW - 18}
                rx={4}
                className={`bench-bar bench-bar--${r.pattern}${r.s.family === "helix" ? " bench-bar--helix" : ""}`}
                fill={fill}
              />
              <text x={LABEL_W + w + 8} y={y + ROW / 2 + 4} className="bench-chart__value">
                {usd(r.v)}
              </text>
            </g>
          );
        })}
      </svg>
      <figcaption className="bench-chart__caption">
        Each bar has its own fill pattern and a printed value. Capped value counts each laundering transaction at most{" "}
        {usd(cap)}.<span className="sr-only"> {summary}</span>
      </figcaption>
      <details className="bench-details">
        <summary>Chart data table</summary>
        <div className="bench-scroll" role="group" aria-label="Chart data" tabIndex={0}>
          <table className="table table--compact" data-testid="bench-chart-table">
            <caption className="sr-only">Capped value per investigator-hour at N = {n}</caption>
            <thead>
              <tr>
                <th scope="col">System</th>
                <th scope="col" className="num">
                  Capped value
                </th>
                <th scope="col" className="num">
                  Investigator hours
                </th>
                <th scope="col" className="num">
                  Capped value / hour
                </th>
              </tr>
            </thead>
            <tbody>
              {systems.map((s) => {
                const r = at(s, n);
                return (
                  <tr key={s.id}>
                    <th scope="row">{s.label}</th>
                    <td className="num">{r ? usd(r.value_capped_usd) : "n/a"}</td>
                    <td className="num">{r ? hours(r.investigator_hours) : "n/a"}</td>
                    <td className="num">{r ? usd(r.value_capped_per_inv_hour_usd) : "n/a"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  );
}

// --- ranking metrics -----------------------------------------------------------------------

function RankingTable({ summary, systems }: { summary: BenchmarkSummary; systems: SystemView[] }) {
  const rows = useMemo(() => rankingRows(summary, systems), [summary, systems]);
  const rocBadges = useMemo(() => badges(systems, (s) => rows.find((r) => r.system.id === s.id)?.roc, "higher"), [rows, systems]);
  const prBadges = useMemo(() => badges(systems, (s) => rows.find((r) => r.system.id === s.id)?.pr, "higher"), [rows, systems]);
  const random = randomPrAuc(summary);
  const captionId = useId();
  return (
    <>
      <div className="bench-scroll" role="region" aria-labelledby={captionId} tabIndex={0}>
        <table className="table bench-table bench-table--small" data-testid="bench-ranking">
          <caption id={captionId}>Transaction ranking quality (seed {summary.headline_seed ?? summary.seeds[0]})</caption>
          <thead>
            <tr>
              <th scope="col">System</th>
              <th scope="col" className="num">
                ROC-AUC
              </th>
              <th scope="col" className="num">
                PR-AUC
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.system.id} data-testid={`bench-rank-${r.system.id}`}>
                <th scope="row">{r.system.label}</th>
                <td className="num">
                  <span className="bench-val">{r.roc === null ? "n/a" : dec3(r.roc)}</span>
                  {r.rocRange ? (
                    <span className="bench-seed">
                      seeds {dec3(r.rocRange[0])} to {dec3(r.rocRange[1])}
                    </span>
                  ) : null}
                  {rocBadges.get(r.system.id) ? <BadgeChip badge={rocBadges.get(r.system.id)!} /> : null}
                </td>
                <td className="num">
                  <span className="bench-val">{r.pr === null ? "n/a" : dec5(r.pr)}</span>
                  {r.prRange ? (
                    <span className="bench-seed">
                      seeds {dec5(r.prRange[0])} to {dec5(r.prRange[1])}
                    </span>
                  ) : null}
                  {prBadges.get(r.system.id) ? <BadgeChip badge={prBadges.get(r.system.id)!} /> : null}
                </td>
              </tr>
            ))}
            <tr className="bench-row--ref">
              <th scope="row">Random ranker (reference)</th>
              <td className="num">0.500</td>
              <td className="num" data-testid="bench-random-pr">
                {dec5(random)}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p className="bench-note">
        Random PR-AUC is the laundering share of the test rows ({int(summary.rows.test_laundering)} of {int(summary.rows.test)}). HELIX scores are
        coarse, case-level ones (every transaction in a Case shares its score), so as a transaction ranker HELIX is close to random. HELIX is built to
        pick a few explained Cases per hour, not to rank every transaction.
      </p>
    </>
  );
}

// --- why HELIX: qualitative capability matrix ------------------------------------------------

type Cap = "yes" | "no" | "partial";
const CAP_TEXT: Record<Cap, string> = { yes: "Yes", no: "No", partial: "Partial" };
const CAP_COLS: { id: string; label: string }[] = [
  { id: "helix", label: "HELIX" },
  { id: "rule", label: "Rule baseline" },
  { id: "iforest", label: "Isolation Forest" },
  { id: "xgboost", label: "XGBoost" },
];

const CAPABILITIES: { text: string; cells: [Cap, string?][] }[] = [
  { text: "Needs no labelled history", cells: [["yes"], ["yes", "thresholds from percentiles"], ["yes"], ["no", "needs a labelled fit window"]] },
  { text: "Learns online from each investigator label", cells: [["yes"], ["no"], ["no"], ["partial", "offline retrain only"]] },
  {
    text: "Groups transactions into explained Cases with an evidence chain",
    cells: [["yes"], ["partial", "the rule name explains a hit"], ["no"], ["no", "needs an add-on such as SHAP"]],
  },
  {
    text: "Works under a hard alert budget",
    cells: [["yes", "built-in budget per hour"], ["partial", "top-N cut by the harness"], ["partial", "top-N cut by the harness"], ["partial", "top-N cut by the harness"]],
  },
  { text: "Fuses SOC and login signals", cells: [["yes", "with the SOC layer"], ["no"], ["no"], ["no", "transaction features only here"]] },
  { text: "Adversarial immune loop", cells: [["yes", "Red vs Blue"], ["no"], ["no"], ["no"]] },
  { text: "Tamper-evident audit trail", cells: [["yes", "hash-chained ledger"], ["no"], ["no"], ["no"]] },
  { text: "Runs locally, no labels leave the system", cells: [["yes"], ["yes"], ["yes"], ["yes", "as run here"]] },
];

function CapIcon({ cap }: { cap: Cap }) {
  return (
    <svg className={`bench-cap__icon bench-cap__icon--${cap}`} viewBox="0 0 16 16" fill="none" aria-hidden="true" focusable="false">
      {cap === "yes" ? (
        <>
          <circle cx="8" cy="8" r="7" fill="currentColor" opacity="0.16" />
          <path d="M4.8 8.2 7 10.3l4.2-4.6" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
        </>
      ) : cap === "partial" ? (
        <>
          <circle cx="8" cy="8" r="6.25" stroke="currentColor" strokeWidth="1.3" />
          <path d="M8 1.75a6.25 6.25 0 0 1 0 12.5z" fill="currentColor" />
        </>
      ) : (
        <path d="M5 8h6" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
      )}
    </svg>
  );
}

function CapabilityMatrix({ systems }: { systems: SystemView[] }) {
  const captionId = useId();
  const label = (id: string, fallback: string) => systems.find((s) => s.id === id)?.label ?? fallback;
  return (
    <div className="bench-scroll" role="region" aria-labelledby={captionId} tabIndex={0}>
      <table className="table bench-table bench-matrix" data-testid="bench-matrix">
        <caption id={captionId}>Capabilities by design (qualitative, not measured in this benchmark)</caption>
        <thead>
          <tr>
            <th scope="col">Capability</th>
            {CAP_COLS.map((c) => (
              <th key={c.id} scope="col">
                {label(c.id, c.label)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {CAPABILITIES.map((row) => (
            <tr key={row.text}>
              <th scope="row">{row.text}</th>
              {row.cells.map(([cap, note], i) => (
                <td key={CAP_COLS[i]!.id} className={`bench-cap bench-cap--${cap}`}>
                  <span className="bench-cap__main">
                    <CapIcon cap={cap} />
                    {CAP_TEXT[cap]}
                  </span>
                  {note ? <span className="bench-cap__note">{note}</span> : null}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// --- page ----------------------------------------------------------------------------------

function windowText(w: string[] | undefined): string {
  return w && w.length === 2 ? `${w[0]} to ${w[1]}` : "n/a";
}

export function BenchmarkPage({ summary = SUMMARY }: { summary?: BenchmarkSummary }) {
  const [n, setN] = useState<Capacity>(KPI_N);
  const systems = useMemo(() => systemsOf(summary), [summary]);
  const a = summary.assumptions;
  const out = summary.rows.test_rows_helix_cannot_ingest;
  const helix = systems.find((s) => s.id === "helix");
  const sup = systems.find((s) => s.labelsNeeded);
  const testWin = summary.windows.test;
  const dataset = summary.dataset.replace(/\s*\(.*\)\s*$/, "");

  return (
    <div className="hx-view bench" role="region" aria-labelledby="bench-title" data-testid="benchmark">
      <ViewHeader
        id="bench-title"
        eyebrow="Benchmark"
        title="HELIX vs ML baselines"
        sub={`${dataset}, on AMLworld test data: one ${summary.test_hours} h test window (${windowText(testWin)} UTC).`}
      />

      <section className="bench-first" aria-labelledby="bench-first-title" data-testid="bench-read-first">
        <h3 id="bench-first-title" className="bench-first__title">
          Read this first
        </h3>
        <ul>
          <li>
            One {summary.test_hours} h test window, {int(summary.rows.test)} transactions, {int(summary.rows.test_laundering)} of them laundering.
          </li>
          <li>{a.value_cap_note ?? `Value is capped at ${usd(a.value_cap_usd)} per laundering transaction.`} The capped metric is shown.</li>
          {out ? (
            <li>
              {int(out.rows)} test rows ({pct(out.laundering_value_share)} of the laundering value) are outside HELIX&apos;s amount range and cannot be
              ingested.
            </li>
          ) : null}
          <li>HELIX is untuned ({a.helix_config ?? "default config"}).</li>
          {sup ? <li>{sup.label} is supervised: it was trained on labelled laundering from the fit window. The others use no labels.</li> : null}
        </ul>
      </section>

      <section className="bench-block" aria-labelledby="bench-kpi-title">
        <h3 id="bench-kpi-title" className="bench-block__title">
          HELIX at N = {KPI_N} alerts per hour
        </h3>
        <KpiRow systems={systems} />
        <p className="bench-frame">
          XGBoost is the accuracy ceiling when you already have labelled laundering history; HELIX is the system you can deploy on day one without labels,
          that explains every alert and improves as investigators label.
        </p>
      </section>

      <Section title="Comparison at equal analyst capacity" eyebrow="Measured" testId="bench-compare">
        <div className="bench-controls">
          <CapacityToggle value={n} onChange={setN} />
          <p className="bench-note bench-note--inline">
            Each system takes its own top N alerts per clock hour.
            {helix && a.helix_alert_budget_per_hour !== undefined
              ? ` HELIX raises at most ${a.helix_alert_budget_per_hour} Cases per hour by itself, so its row does not change with N.`
              : null}
          </p>
        </div>
        <p className="sr-only" role="status">
          Showing N = {n} alerts per hour.
        </p>
        <ComparisonTable systems={systems} n={n} hoursTested={summary.test_hours} />
        <p className="bench-note">
          Badges are computed from the numbers above: &quot;Best overall&quot; marks the best value in a column, &quot;Best without labels&quot; the
          best system that needs no labels when a supervised one is best overall. Alerts are not ranked. Seed ranges are shown where a baseline was run
          with several seeds ({summary.seeds.join(", ")}).
        </p>
      </Section>

      <Section title="Capped value per investigator-hour" eyebrow="Measured" testId="bench-value">
        <ValueChart systems={systems} n={n} cap={summary.assumptions.value_cap_usd} />
      </Section>

      <Section title="Ranking metrics" eyebrow="Measured" testId="bench-rank">
        <RankingTable summary={summary} systems={systems} />
      </Section>

      <Section title="Why HELIX" eyebrow="Capabilities, not measured" testId="bench-why">
        <p className="bench-note bench-note--lead">
          What each system offers by design. These rows describe capabilities, not accuracy; the measured results are above.
        </p>
        <CapabilityMatrix systems={systems} />
      </Section>

      <Section title="Assumptions" eyebrow="From the results file" testId="bench-assumptions">
        <dl className="bench-assume">
          <div>
            <dt>Minutes per case</dt>
            <dd>{a.minutes_per_case} min</dd>
          </div>
          {a.analyst_cost_per_hour !== undefined ? (
            <div>
              <dt>Analyst cost per hour</dt>
              <dd>{usd(a.analyst_cost_per_hour)}</dd>
            </div>
          ) : null}
          {a.cost_per_case !== undefined ? (
            <div>
              <dt>Cost per case</dt>
              <dd>{usd(a.cost_per_case)}</dd>
            </div>
          ) : null}
          <div>
            <dt>Value cap</dt>
            <dd>{usd(a.value_cap_usd)} per laundering transaction</dd>
          </div>
          <div>
            <dt>Currency</dt>
            <dd>{a.currency}</dd>
          </div>
          <div>
            <dt>Baseline fit window</dt>
            <dd>{windowText(summary.windows.baseline_fit)}</dd>
          </div>
          <div>
            <dt>HELIX warm-up window</dt>
            <dd>{windowText(summary.windows.helix_warmup)}</dd>
          </div>
          <div>
            <dt>Test window</dt>
            <dd>
              {windowText(testWin)} ({summary.windows.timezone ?? "UTC"})
            </dd>
          </div>
          {a.alert_unit ? (
            <div className="bench-assume--wide">
              <dt>Alert unit</dt>
              <dd>{a.alert_unit}</dd>
            </div>
          ) : null}
          {a.true_positive ? (
            <div className="bench-assume--wide">
              <dt>True positive</dt>
              <dd>{a.true_positive}</dd>
            </div>
          ) : null}
        </dl>
      </Section>

      <p className="bench-honesty">
        Measured on AMLworld test data only; these are benchmark results, not a savings estimate. Every number on this page is read from
        benchmarks/results/summary.json. Full method and caveats: the README section &quot;Benchmark vs baselines&quot; and BENCHMARK_NOTES.md in the
        repository.
      </p>
    </div>
  );
}
