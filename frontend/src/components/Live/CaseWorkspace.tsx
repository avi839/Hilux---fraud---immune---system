import { useId, useRef, useState, type ComponentType, type KeyboardEvent } from "react";
import type { CaseSummary } from "../../api/types";
import { formatAccounts, formatMoney, formatScore } from "../../format";
import { useDashboard } from "../../store/store";
import { PanelBoundary } from "../common/ErrorBoundary";
import { TierBadge } from "../common/TierBadge";
import { CaseActions } from "../Controls/Controls";
import type { DemoScenarioState } from "../Controls/DemoScenario";
import { EvidencePanel } from "../EvidencePanel/EvidencePanel";
import { percent, SUGGESTION_DISCLAIMER, SUGGESTION_TEXT } from "../EvidencePanel/SuggestedLabel";
import { GeoMap } from "../GeoMap/GeoMap";
import { Network3D } from "../Network3D/Network3D";
import { ReplaySlider } from "../ReplaySlider/ReplaySlider";

interface WorkspaceView {
  id: "evidence" | "network" | "map" | "replay";
  label: string;
  /** PanelBoundary area (also the view's panel modifier class). */
  area: string;
  View: ComponentType;
}

/** The workspace sub-tabs, in order. Only the selected one is mounted, so the WebGL views
 *  (Network, Map) load their chunks and contexts only when chosen. */
export const WORKSPACE_VIEWS: readonly WorkspaceView[] = [
  { id: "evidence", label: "Evidence", area: "evidence", View: EvidencePanel },
  { id: "network", label: "Network", area: "network", View: Network3D },
  { id: "map", label: "Map", area: "geo", View: GeoMap },
  { id: "replay", label: "Replay", area: "replay", View: ReplaySlider },
];

function statusText(c: CaseSummary): string {
  if (c.status === "IN_REVIEW") return `In review${c.assignee ? ` (${c.assignee})` : ""}`;
  if (c.status === "OPEN") return "Open";
  if (c.status === "CLOSED") return "Closed";
  return c.status;
}

/** The selected Case at a glance: id, tier, risk, amount at risk and the Label_Suggester's
 *  advice, which is labelled as a suggestion and has no apply button. */
function CaseHeader({ id, c }: { id: string; c: CaseSummary | undefined }) {
  const s = c?.suggested_label;
  const pct = c ? Math.round(Math.min(1, Math.max(0, c.risk)) * 100) : 0;
  return (
    <div className="lv-case" data-testid="ws-header">
      <div className="lv-case__title">
        <h3 className="lv-case__id mono">{id}</h3>
        {c ? <TierBadge tier={c.tier} /> : null}
        {c ? <span className="muted">{statusText(c)}</span> : null}
      </div>
      {c ? (
        <dl className="lv-case__facts">
          <div className="lv-fact lv-fact--risk">
            <dt>Risk</dt>
            <dd>
              <span className="lv-fact__big" data-testid="ws-risk">
                {formatScore(c.risk)}
              </span>
              <span className="lv-bar" aria-hidden="true">
                <span style={{ width: `${pct}%` }} />
              </span>
            </dd>
          </div>
          <div className="lv-fact">
            <dt>At risk</dt>
            <dd className="lv-fact__mid">
              {c.amount_at_risk.length ? c.amount_at_risk.map((a) => formatMoney(a.amount, a.currency)).join(", ") : "none"}
            </dd>
          </div>
          <div className="lv-fact">
            <dt>Accounts · motifs</dt>
            <dd className="lv-fact__text">
              <span className="mono" title={c.accounts.join(", ")}>
                {formatAccounts(c.accounts, 2)}
              </span>
              <span className="muted">{c.motifs.length ? ` · ${c.motifs.join(", ")}` : ` · ${c.tx_count} tx`}</span>
            </dd>
          </div>
          <div className="lv-fact">
            <dt>Suggested label</dt>
            <dd className="lv-fact__text" data-testid="ws-suggestion">
              {s ? (
                <span className={`hx-chip lv-chip lv-suggest lv-suggest--${s.label.toLowerCase()}`}>
                  {SUGGESTION_TEXT[s.label]} · {percent(s.confidence)}
                </span>
              ) : (
                <span className="muted">No suggestion</span>
              )}
              <span className="lv-suggest__note muted small">{SUGGESTION_DISCLAIMER}</span>
            </dd>
          </div>
        </dl>
      ) : null}
    </div>
  );
}

/** Shown while no Case is selected. The action is named "Run the demo scenario" so the strip's
 *  "Load demo scenario" button stays the only control with that name. */
function EmptyState({ demo }: { demo: DemoScenarioState }) {
  const sending = demo.run.status === "sending";
  return (
    <div className="lv-empty" data-testid="ws-empty">
      <div className="lv-empty__mark" aria-hidden="true">
        <svg viewBox="0 0 24 24" width="22" height="22" focusable="false">
          <path d="M4 6h16M4 12h10M4 18h7" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" fill="none" />
        </svg>
      </div>
      <h3 className="lv-empty__title">No case selected</h3>
      <p className="lv-empty__text">
        Load the demo scenario or pick a Case from the queue. Its evidence, network, map and replay open here, with the actions to label it.
      </p>
      <button type="button" className="lv-linkbtn" disabled={sending} onClick={() => void demo.load()}>
        {sending ? "Sending the demo scenario…" : "Run the demo scenario"} <span aria-hidden="true">→</span>
      </button>
    </div>
  );
}

/** WAI-ARIA tabs (automatic activation): Arrow Left/Right wrap, Home/End jump; only the
 *  selected tab is in the tab order and only its panel is mounted. */
function WorkspaceTabs() {
  const base = useId();
  const [active, setActive] = useState<WorkspaceView["id"]>("evidence");
  const refs = useRef(new Map<string, HTMLButtonElement>());

  const onKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    const i = WORKSPACE_VIEWS.findIndex((v) => v.id === active);
    const n = WORKSPACE_VIEWS.length;
    const next =
      e.key === "ArrowRight" ? (i + 1) % n : e.key === "ArrowLeft" ? (i - 1 + n) % n : e.key === "Home" ? 0 : e.key === "End" ? n - 1 : -1;
    if (next < 0) return;
    e.preventDefault();
    const id = WORKSPACE_VIEWS[next]!.id;
    setActive(id);
    refs.current.get(id)?.focus();
  };

  return (
    <div className="lv-ws__tabs">
      <div className="lv-tabs" role="tablist" aria-label="Case views">
        {WORKSPACE_VIEWS.map((v) => {
          const selected = v.id === active;
          return (
            <button
              key={v.id}
              ref={(el) => {
                if (el) refs.current.set(v.id, el);
                else refs.current.delete(v.id);
              }}
              type="button"
              role="tab"
              id={`${base}-tab-${v.id}`}
              aria-selected={selected}
              aria-controls={`${base}-panel-${v.id}`}
              tabIndex={selected ? 0 : -1}
              className={`lv-tab${selected ? " lv-tab--active" : ""}`}
              onClick={() => setActive(v.id)}
              onKeyDown={onKeyDown}
            >
              {v.label}
            </button>
          );
        })}
      </div>
      {WORKSPACE_VIEWS.map(({ id, label, area, View }) => (
        <div
          key={id}
          id={`${base}-panel-${id}`}
          role="tabpanel"
          aria-labelledby={`${base}-tab-${id}`}
          hidden={id !== active}
          className={`lv-ws__view lv-ws__view--${id}`}
          data-testid={`ws-view-${id}`}
        >
          {id === active ? (
            <PanelBoundary title={label} area={area}>
              <View />
            </PanelBoundary>
          ) : null}
        </div>
      ))}
    </div>
  );
}

/** Right column of the Live dashboard: the selected Case's header, the one CaseActions bar
 *  (it registers the F/P/E shortcut handlers, so it is mounted exactly once and always:
 *  its investigator name is also used by bulk labelling), and the Evidence / Network / Map /
 *  Replay sub-tabs. With no selection it shows a friendly empty state instead. */
export function CaseWorkspace({ demo }: { demo: DemoScenarioState }) {
  const headingId = useId();
  const selectedId = useDashboard((s) => s.selectedCaseId);
  const c = useDashboard((s) => (s.selectedCaseId ? s.cases[s.selectedCaseId] : undefined));

  return (
    <section className="lv-card lv-ws" aria-labelledby={headingId} data-testid="case-workspace">
      <h2 id={headingId} className="lv-title">
        Case workspace
      </h2>
      {selectedId ? <CaseHeader id={selectedId} c={c} /> : <EmptyState demo={demo} />}
      <PanelBoundary title="Case actions" area="actions">
        <CaseActions layout="inline" />
      </PanelBoundary>
      {selectedId ? <WorkspaceTabs /> : null}
    </section>
  );
}
