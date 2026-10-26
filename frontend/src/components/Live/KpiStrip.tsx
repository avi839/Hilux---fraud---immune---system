import { useId, type ReactNode, type RefObject } from "react";
import { useDashboard } from "../../store/store";
import { DemoScenarioButton, type DemoScenarioState } from "../Controls/DemoScenario";
import { n2, type LoopData } from "../LoopPanel/LoopPanel";
import { NOT_APPLICABLE, throughput, type Ratio } from "../LoopPanel/loop";

const CONNECTION_TEXT = { open: "Live", connecting: "Connecting…", down: "Disconnected" } as const;

/** A KPI value of a ratio the server may not report: a number, or "n/a" (also for "not
 *  applicable", which is spelled out under More metrics in the HELIX loop card). */
function ratio(r: Ratio | null | undefined): string {
  return typeof r === "number" ? r.toFixed(2) : "n/a";
}

function Kpi({ label, testId, children, index, text }: { label: string; testId: string; children: ReactNode; index: number; text?: boolean }) {
  return (
    <div className="hx-kpi lv-kpi" style={{ ["--i" as string]: index }}>
      <dt className="lv-kpi__label">{label}</dt>
      <dd className={`lv-kpi__value${text ? " lv-kpi__value--text" : ""}`} data-testid={testId}>
        {children}
      </dd>
    </div>
  );
}

interface KpiStripProps {
  loop: LoopData;
  demo: DemoScenarioState;
  controlsOpen: boolean;
  drawerId: string;
  triggerRef: RefObject<HTMLButtonElement>;
  onOpenControls: () => void;
}

/** Top row of the Live dashboard: six stat tiles read from the store and the shared loop
 *  metrics (never invented: anything the server does not report is "n/a"), then the
 *  Controls drawer trigger and the primary "Load demo scenario" action. */
export function KpiStrip({ loop, demo, controlsOpen, drawerId, triggerRef, onOpenControls }: KpiStripProps) {
  const headingId = useId();
  const active = useDashboard((s) => s.queue.length);
  const backlog = useDashboard((s) => s.backlogCount);
  const connection = useDashboard((s) => s.connection);
  const eps = throughput(loop.samples, "END_TO_END");
  const breach = loop.view.breach ?? null;
  const ev = loop.view.evaluation;
  const evaluated = ev !== undefined && ev !== NOT_APPLICABLE ? ev : null;

  return (
    <section className="lv-strip" aria-labelledby={headingId}>
      <h2 id={headingId} className="sr-only">
        Key metrics
      </h2>
      <dl className="lv-kpis">
        <Kpi label="Active cases" testId="kpi-active" index={0}>
          {active}
        </Kpi>
        <Kpi label="Backlog" testId="kpi-backlog" index={1}>
          {backlog}
        </Kpi>
        <Kpi label="Events/s, end to end" testId="kpi-eps" index={2}>
          {n2(eps, 1)}
        </Kpi>
        <Kpi label="p95 latency" testId="kpi-p95" index={3}>
          {breach?.p95Ms !== null && breach?.p95Ms !== undefined ? (
            <>
              {n2(breach.p95Ms)}
              <span className="lv-kpi__unit"> ms</span>
            </>
          ) : (
            "n/a"
          )}
          {breach && breach.p95Ms !== null ? (
            <span className={`hx-chip lv-chip ${breach.active ? "hx-chip--warn" : "hx-chip--ok"}`} data-testid="kpi-p95-chip">
              {breach.active ? "over target" : "within target"}
            </span>
          ) : null}
        </Kpi>
        <Kpi label="Precision / recall" testId="kpi-eval" index={4} text={!!evaluated}>
          {evaluated ? `${ratio(evaluated.precision)} / ${ratio(evaluated.recall)}` : "n/a"}
        </Kpi>
        <Kpi label="Connection" testId="kpi-connection" index={5} text>
          <span className={`lv-conn lv-conn--${connection}`}>
            <span className="lv-conn__dot" aria-hidden="true" />
            {CONNECTION_TEXT[connection]}
          </span>
        </Kpi>
      </dl>
      <div className="lv-strip__actions">
        <button
          ref={triggerRef}
          type="button"
          className="hx-pill lv-pill"
          aria-haspopup="dialog"
          aria-expanded={controlsOpen}
          aria-controls={controlsOpen ? drawerId : undefined}
          onClick={onOpenControls}
        >
          <svg className="lv-pill__icon" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" focusable="false">
            <path d="M2 4h7M13 4h1M2 12h1M7 12h7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            <circle cx="11" cy="4" r="2" fill="none" stroke="currentColor" strokeWidth="1.5" />
            <circle cx="5" cy="12" r="2" fill="none" stroke="currentColor" strokeWidth="1.5" />
          </svg>
          Controls
        </button>
        <DemoScenarioButton demo={demo} className="hx-pill hx-pill--primary lv-pill lv-pill--primary" />
      </div>
    </section>
  );
}
