import { useEffect, useId, useMemo, useState, type ReactNode } from "react";
import { CASE_SHORTCUTS } from "../Controls/caseActions";
import { ClosureNote, SuggestedLabel } from "./SuggestedLabel";
import { useShortcuts } from "./useShortcuts";
import { useCaseVersion } from "../../viz/caseData";
import { evidenceAt } from "../../viz/replay";
import { formatUtc } from "../../viz/time";
import { api, ApiError } from "../../api/client";
import type { CaseDetail, CaseSummary, ChainEntry, EvidenceDoc, ExplanationItemDoc } from "../../api/types";
import { formatScore, formatTime } from "../../format";
import { useDashboard } from "../../store/store";
import { Panel } from "../common/Panel";
import { TierBadge } from "../common/TierBadge";
import {
  contributorBars,
  fmt,
  groupBySensor,
  segmentPercents,
  uncertaintyConditions,
  type ContributorBar,
} from "./evidence";

type Load =
  | { status: "idle" }
  | { status: "loading"; id: string }
  | { status: "error"; id: string; message: string }
  | { status: "ready"; id: string; evidence: EvidenceDoc; detail: CaseDetail | null; detailError: string | null; refreshing: boolean };

function describe(err: unknown): string {
  return err instanceof ApiError ? err.describe() : err instanceof Error ? err.message : String(err);
}

const isAbort = (err: unknown) => err instanceof ApiError && err.kind === "aborted";

/**
 * Loads GET /cases/{id}/evidence and GET /cases/{id} for the selected Case. A new selection
 * (or a newer version of the same Case from the live feed) aborts the in-flight requests, so
 * a slow response for a previous Case can never overwrite the current one.
 */
function useCaseEvidence(id: string | null, version: string | undefined, reload: number): Load {
  const [load, setLoad] = useState<Load>({ status: "idle" });
  useEffect(() => {
    if (!id) {
      setLoad({ status: "idle" });
      return;
    }
    const ctrl = new AbortController();
    // Keep showing the same Case while it refreshes; a different Case starts from "loading".
    setLoad((prev) => (prev.status === "ready" && prev.id === id ? { ...prev, refreshing: true } : { status: "loading", id }));
    void Promise.allSettled([api.getEvidence(id, ctrl.signal), api.getCase(id, ctrl.signal)]).then(([ev, det]) => {
      if (ctrl.signal.aborted) return;
      if (ev.status === "rejected") {
        if (isAbort(ev.reason)) return;
        setLoad({ status: "error", id, message: describe(ev.reason) });
        return;
      }
      setLoad({
        status: "ready",
        id,
        evidence: ev.value,
        detail: det.status === "fulfilled" ? det.value : null,
        detailError: det.status === "rejected" && !isAbort(det.reason) ? describe(det.reason) : null,
        refreshing: false,
      });
    });
    return () => ctrl.abort();
  }, [id, version, reload]);
  return load;
}

/** A headed sub-section (a div, not a <section>, so the page is not flooded with landmarks;
 *  the h3 headings give screen-reader users the structure). */
function Section({ title, count, children }: { title: string; count?: number; children: ReactNode }) {
  return (
    <div className="ev-section">
      <h3 className="ev-section__title">
        {title}
        {count !== undefined ? <span className="muted"> ({count})</span> : null}
      </h3>
      {children}
    </div>
  );
}

/** Transaction ids shown inline per explanation item / condition; the rest are counted. A
 *  large Case cites thousands of transactions, and listing them all made the panel unusable. */
export const TX_IDS_SHOWN = 6;
/** Evidence_Chain rows rendered in the table (the full chain stays in the API response). */
export const CHAIN_ROWS_SHOWN = 300;

export function txList(ids: readonly string[], max = TX_IDS_SHOWN): string {
  if (ids.length <= max) return ids.join(", ");
  return `${ids.slice(0, max).join(", ")} … +${ids.length - max} more`;
}

function Item({ it }: { it: ExplanationItemDoc }) {
  return (
    <li className="ev-item">
      <div>
        <strong className="ev-item__label">{it.label}</strong>
        {it.generic ? <span className="tag">generic</span> : null}
        <span className="ev-item__cmp">: {it.comparison}</span>
      </div>
      <div className="ev-item__meta muted small">
        weight {fmt(it.weight, 2)} · F′ {fmt(it.f)} vs L′ {fmt(it.l)} · tx {txList(it.tx_ids)}
      </div>
    </li>
  );
}

function ItemList({ items, ordered, empty }: { items: ExplanationItemDoc[]; ordered?: boolean; empty: string }) {
  if (items.length === 0) return <p className="empty">{empty}</p>;
  const children = items.map((it) => <Item key={it.name} it={it} />);
  return ordered ? <ol className="ev-list ev-list--numbered">{children}</ol> : <ul className="ev-list">{children}</ul>;
}

/** Stacked bar of one contributor's discounted masses. The segments' widths are f', l' and
 *  Θ' = 1 − f' − l' of the full width; the bar is an image whose label states the values,
 *  and the same values are printed beside it (hidden from assistive tech to avoid repeats). */
export function MassBar({ bar }: { bar: ContributorBar }) {
  const [f, l, t] = segmentPercents(bar);
  const tx = bar.txCount > 1 ? `tx ${bar.txId}, representative of ${bar.txCount}` : `tx ${bar.txId}`;
  const label = `${bar.label}, ${tx}: fraud ${fmt(bar.f)}, legit ${fmt(bar.l)}, uncertainty ${fmt(bar.theta)}`;
  return (
    <li className="bar-row" data-testid={`bar-${bar.name}`}>
      <span className="bar-row__name">
        {bar.label} <span className="muted small">{tx}</span>
      </span>
      <div className="massbar" role="img" aria-label={label}>
        <span className="massbar__seg massbar__seg--f" data-seg="f" style={{ width: `${f}%` }} />
        <span className="massbar__seg massbar__seg--l" data-seg="l" style={{ width: `${l}%` }} />
        <span className="massbar__seg massbar__seg--t" data-seg="t" style={{ width: `${t}%` }} />
      </div>
      <span className="bar-row__values mono small" aria-hidden="true">
        F′ {fmt(bar.f)} · L′ {fmt(bar.l)} · Θ′ {fmt(bar.theta)}
      </span>
    </li>
  );
}

function ChainTable({ entries: all }: { entries: ChainEntry[] }) {
  const entries = all.length > CHAIN_ROWS_SHOWN ? all.slice(0, CHAIN_ROWS_SHOWN) : all;
  return (
    <div className="scroll scroll--short" tabIndex={0} role="region" aria-label="Evidence chain entries">
      {all.length > entries.length ? (
        <p className="muted small" data-testid="ev-chain-capped">
          Showing the first {entries.length} of {all.length} entries.
        </p>
      ) : null}
      <table className="table table--compact">
        <thead>
          <tr>
            <th scope="col">Time (UTC)</th>
            <th scope="col">Tx</th>
            <th scope="col">Contributor</th>
            <th scope="col">Kind</th>
            <th scope="col" className="num">
              f
            </th>
            <th scope="col" className="num">
              l
            </th>
            <th scope="col" className="num">
              Θ
            </th>
            <th scope="col" className="num">
              Weight
            </th>
          </tr>
        </thead>
        <tbody>
          {entries.map((e) => (
            <tr key={`${e.tx_id}|${e.contributor_id}|${e.ts}`}>
              <td className="mono">
                <time dateTime={e.ts}>{formatTime(e.ts)}</time>
              </td>
              <td className="mono">{e.tx_id}</td>
              <td className="mono">{e.contributor_id}</td>
              <td>{e.kind}</td>
              <td className="num">{e.mass ? fmt(e.mass.f) : "–"}</td>
              <td className="num">{e.mass ? fmt(e.mass.l) : "–"}</td>
              <td className="num">{e.mass ? fmt(e.mass.t) : "–"}</td>
              <td className="num">{e.weight !== null ? fmt(e.weight, 2) : "–"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function EvidenceBody({ evidence, detail, uncertainty }: { evidence: EvidenceDoc; detail: CaseDetail | null; uncertainty: number | null | undefined }) {
  const ex = evidence.explanation;
  const chain = evidence.chain;
  const bars = contributorBars(chain, ex);
  const conditions = groupBySensor(uncertaintyConditions(chain, ex));
  return (
    <>
      <p className="ev-summary mono" data-testid="ev-summary">
        {ex.summary}
      </p>
      {ex.message ? (
        <p className="ev-message" data-testid="ev-message">
          {ex.message}
        </p>
      ) : null}

      <Section title="Uncertainty">
        <p className="ev-uncertainty">
          Uncertainty_Mass <strong data-testid="ev-uncertainty">{formatScore(uncertainty, 2)}</strong>
        </p>
        {conditions.length === 0 ? (
          <p className="muted" data-testid="ev-conditions-none">
            Missing-field or insufficient-history conditions: none contributed.
          </p>
        ) : (
          <ul className="ev-list" data-testid="ev-conditions" aria-label="Missing-field and insufficient-history conditions">
            {conditions.map((g) => (
              <li key={g.sensor}>
                <strong>{g.label}</strong>:{" "}
                {g.conditions.map((c, i) => (
                  <span key={c.reason}>
                    {i > 0 ? "; " : ""}
                    {c.reason} <span className="muted">(tx {txList(c.txIds)})</span>
                  </span>
                ))}
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title="Reasons" count={ex.reasons.length}>
        <ItemList items={ex.reasons} ordered empty="No contributor favours fraud." />
      </Section>
      <Section title="Mitigating" count={ex.mitigating.length}>
        <ItemList items={ex.mitigating} empty="No mitigating factors." />
      </Section>
      <Section title="Insufficient evidence" count={ex.insufficient.length}>
        <ItemList items={ex.insufficient} empty="None." />
      </Section>

      <Section title="Contributor masses (discounted)" count={bars.length}>
        {bars.length === 0 ? (
          <p className="empty">No contributors.</p>
        ) : (
          <>
            <p className="legend small" aria-hidden="true">
              <span className="legend__key legend__key--f" /> F′ fraud <span className="legend__key legend__key--l" /> L′ legit{" "}
              <span className="legend__key legend__key--t" /> Θ′ uncertainty
            </p>
            <ul className="bars">{bars.map((b) => <MassBar key={b.name} bar={b} />)}</ul>
            <p className="muted small">
              f′ = w·f, l′ = w·l, Θ′ = 1 − f′ − l′. A contributor seen on several transactions shows its representative entry (largest
              f′ for a reason, largest l′ for a mitigating factor, otherwise the first).
            </p>
          </>
        )}
      </Section>

      <Section title="Records" count={ex.notes.length}>
        {ex.notes.length === 0 ? (
          <p className="empty">None.</p>
        ) : (
          <ul className="ev-list">
            {ex.notes.map((n, i) => (
              <li key={`${i}-${n}`}>{n}</li>
            ))}
          </ul>
        )}
      </Section>

      <details className="ev-details">
        <summary>Evidence chain ({chain?.entries.length ?? 0} entries)</summary>
        {chain && chain.entries.length ? <ChainTable entries={chain.entries} /> : <p className="empty">No Evidence_Chain stored for this case.</p>}
      </details>

      <details className="ev-details">
        <summary>Case history ({detail?.history.length ?? 0})</summary>
        {detail && detail.history.length ? (
          <ol className="ev-list" aria-label="Case history, oldest first">
            {detail.history.map((h, i) => (
              <li key={`${i}-${h.ts}-${h.action}`}>
                <time dateTime={h.ts} className="mono">
                  {formatTime(h.ts)}
                </time>{" "}
                <strong>{h.action}</strong> {h.prev ?? "–"} → {h.new ?? "–"}
                {h.actor ? <span className="muted"> by {h.actor}</span> : null}
              </li>
            ))}
          </ol>
        ) : (
          <p className="empty">No history available.</p>
        )}
      </details>
    </>
  );
}

/** The evidence as of the shared replay position (Req 22.6): chain entries at or before T and
 *  the explanation items that cite them. */
function ReplayFiltered({ evidence, detail, uncertainty }: { evidence: EvidenceDoc; detail: CaseDetail | null; uncertainty: number | null | undefined }) {
  const replayT = useDashboard((s) => s.replayT);
  const view = useMemo(() => evidenceAt(evidence, replayT), [evidence, replayT]);
  return (
    <>
      {replayT !== null ? (
        <p className="msg muted small" data-testid="ev-replay">
          Replay at {formatUtc(replayT)}: showing {view.kept} of {view.total} chain entries at or before T. The summary line and
          Uncertainty_Mass are the Case&apos;s current values.
        </p>
      ) : null}
      <EvidenceBody evidence={view.evidence} detail={detail} uncertainty={uncertainty} />
    </>
  );
}

/** Explanation, Evidence_Chain, per-contributor mass bars, uncertainty conditions and case
 *  history for the selected Case (Req 22.3, 16, 7.8, 7.9). */
export function EvidencePanel() {
  const selected = useDashboard((s) => s.selectedCaseId);
  const summary = useDashboard((s) => (s.selectedCaseId ? s.cases[s.selectedCaseId] : undefined));
  const [reload, setReload] = useState(0);
  const version = useCaseVersion(selected);
  const load = useCaseEvidence(selected, version, reload);

  const current = load.status !== "idle" && load.id === selected ? load : null;
  const ready = current?.status === "ready" ? current : null;
  const tier = ready?.detail?.tier ?? summary?.tier;
  const busy = current?.status === "loading" || (ready?.refreshing ?? false);
  const keys = useShortcuts(selected);
  const helpId = useId();
  // The live summary is newer than a loaded detail while the detail refreshes.
  const caseNow: CaseSummary | undefined = summary ?? ready?.detail ?? undefined;

  return (
    <Panel
      title="Evidence"
      area="evidence"
      scrollable
      busy={busy}
      focusTarget
      onKeyDown={keys.onKeyDown}
      actions={
        selected ? (
          <span className="panel__case">
            <span className="mono">{selected}</span> {tier ? <TierBadge tier={tier} /> : null}
            {ready?.refreshing ? <span className="muted small"> Refreshing…</span> : null}
          </span>
        ) : null
      }
    >
      <div className="shortcuts" data-testid="ev-shortcuts">
        <p className="small muted shortcuts__line">
          Keyboard (focus in this panel): <kbd>F</kbd> confirm fraud · <kbd>P</kbd> false positive · <kbd>E</kbd> escalate ·{" "}
          <kbd>?</kbd> help
        </p>
        <button
          type="button"
          className="btn btn--small"
          aria-expanded={keys.helpOpen}
          aria-controls={helpId}
          aria-keyshortcuts="?"
          onClick={() => keys.setHelpOpen(!keys.helpOpen)}
        >
          Shortcut help
        </button>
        <div id={helpId} hidden={!keys.helpOpen} className="shortcuts__help" data-testid="ev-shortcut-help">
          <dl className="kv">
            {CASE_SHORTCUTS.map((s) => (
              <div key={s.key}>
                <dt>
                  <kbd>{s.key}</kbd>
                </dt>
                <dd>{s.label} on the selected case, exactly as the Case actions button (same name check and confirmation)</dd>
              </div>
            ))}
            <div>
              <dt>
                <kbd>?</kbd>
              </dt>
              <dd>Show or hide this help (Escape hides it)</dd>
            </div>
          </dl>
          <p className="small muted">
            Shortcuts work only while focus is inside this panel, and never while typing in a field or with Ctrl, Alt or Meta held.
          </p>
        </div>
        <p role="status" className="small" data-testid="ev-shortcut-note">
          {keys.note}
        </p>
      </div>
      {!selected ? (
        <p className="empty">Select a case in the queue to see its evidence.</p>
      ) : !current || current.status === "loading" ? (
        <p role="status" className="muted" data-testid="ev-loading">
          Loading evidence for {selected}…
        </p>
      ) : current.status === "error" ? (
        <div role="alert" className="msg msg--error" data-testid="ev-error">
          <p className="msg__text">Could not load evidence for {selected}: {current.message}</p>
          <button type="button" className="btn" onClick={() => setReload((n) => n + 1)}>
            Retry
          </button>
        </div>
      ) : (
        <>
          {current.detailError ? <p className="msg muted small">Case detail unavailable: {current.detailError}</p> : null}
          {caseNow ? <ClosureNote c={caseNow} closedAt={current.detail?.closed_at} /> : null}
          <SuggestedLabel suggestion={caseNow?.suggested_label ?? current.detail?.suggested_label} />
          <ReplayFiltered evidence={current.evidence} detail={current.detail} uncertainty={current.detail?.uncertainty ?? summary?.uncertainty} />
        </>
      )}
    </Panel>
  );
}
