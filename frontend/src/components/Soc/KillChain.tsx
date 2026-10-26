import { useId } from "react";
import { KILL_CHAIN_STAGES, socApi, type CaseTechniquesDoc, type KillChainDoc } from "../../api/soc";
import { formatTime } from "../../format";
import { useDashboard } from "../../store/store";
import { SocSection } from "./common";
import { useResource } from "./useResource";

const STAGE_LABEL: Record<string, string> = {
  RECONNAISSANCE: "Reconnaissance",
  CREDENTIAL_ACCESS: "Credential access",
  ACCOUNT_TAKEOVER: "Account takeover",
  PAYEE_MANIPULATION: "Payee manipulation",
  CASH_OUT: "Cash-out",
  LAYERING: "Layering",
};

/** Case picker shared with the Live tab: it reads and sets the store's selected Case. */
function CasePicker() {
  const selected = useDashboard((s) => s.selectedCaseId);
  const queue = useDashboard((s) => s.queue);
  const cases = useDashboard((s) => s.cases);
  const selectCase = useDashboard((s) => s.selectCase);
  const id = useId();
  const ids = [...new Set([...queue, ...Object.keys(cases).sort()])];
  if (selected && !ids.includes(selected)) ids.unshift(selected);
  return (
    <div className="field soc-picker">
      <label htmlFor={id}>Case (shared with the Live tab)</label>
      <select id={id} value={selected ?? ""} onChange={(e) => selectCase(e.target.value || null)}>
        <option value="">No case selected</option>
        {ids.map((c) => (
          <option key={c} value={c}>
            {c}
            {cases[c] ? ` · ${cases[c]!.tier} · ${cases[c]!.status}` : ""}
          </option>
        ))}
      </select>
    </div>
  );
}

function StageList({ doc }: { doc: KillChainDoc }) {
  const byStage = new Map(doc.stages.map((s) => [s.stage, s]));
  // Every stage in the fixed order (Req 29.1), plus any stage the server knows and we do not.
  const order: string[] = [...KILL_CHAIN_STAGES, ...doc.stages.map((s) => s.stage).filter((s) => !(KILL_CHAIN_STAGES as readonly string[]).includes(s))];
  return (
    <>
      <p data-testid="killchain-most">
        Most advanced stage:{" "}
        <strong>{doc.most_advanced ? (STAGE_LABEL[doc.most_advanced] ?? doc.most_advanced) : "none (no evidence yet)"}</strong>
        {doc.multiplier.applied ? (
          <span className="badge badge--warn soc-inline">
            Priority boosted ×{doc.multiplier.value}: money is already moving out
          </span>
        ) : null}
      </p>
      <ol className="killchain" aria-label="Kill-chain stages in order">
        {order.map((stage) => {
          const span = byStage.get(stage);
          const most = stage === doc.most_advanced;
          return (
            <li
              key={stage}
              className={`killchain__stage${span ? " killchain__stage--seen" : ""}${most ? " killchain__stage--most" : ""}`}
              aria-current={most ? "step" : undefined}
              data-testid={`killchain-${stage}`}
            >
              <span className="killchain__name">{STAGE_LABEL[stage] ?? stage}</span>
              <span className="killchain__when small">
                {span ? `first seen ${formatTime(span.first_ts)}` : "not reached"}
                {span?.entries !== undefined ? ` · ${span.entries} evidence item${span.entries === 1 ? "" : "s"}` : ""}
              </span>
              {most ? <span className="killchain__tag small">most advanced</span> : null}
            </li>
          );
        })}
      </ol>
      <p className="muted small">
        Expected loss {doc.multiplier.expected_loss}
        {doc.multiplier.expected_loss_base && doc.multiplier.applied ? ` (before boost ${doc.multiplier.expected_loss_base})` : ""} · priority{" "}
        {doc.multiplier.priority} · kill-chain multiplier {doc.multiplier.enabled ? `on (×${doc.multiplier.value})` : "off"}
        {doc.unmapped.length ? ` · ${doc.unmapped.length} unmapped item(s) assigned ${doc.default_stage}` : ""}
      </p>
    </>
  );
}

function Techniques({ doc }: { doc: CaseTechniquesDoc }) {
  return (
    <div className="soc-sub">
      <h3 className="ev-section__title">Attack techniques seen in this case</h3>
      {doc.techniques.length === 0 ? (
        <p className="empty">No techniques tagged yet.</p>
      ) : (
        <ul className="ev-list" data-testid="case-techniques">
          {doc.techniques.map((t) => (
            <li key={t.technique_id}>
              <span className="mono">{t.technique_id}</span> {t.name} <span className="muted small">({t.domain})</span>
            </li>
          ))}
        </ul>
      )}
      <p className="muted small">
        Catalogue {doc.catalogue_version}. {doc.notice}
      </p>
    </div>
  );
}

/** Kill chain of the selected Case (Req 29.4): stages in order with the earliest timestamp,
 *  the most advanced one highlighted (text + border, not colour alone), and its techniques. */
export function KillChainSection() {
  const caseId = useDashboard((s) => s.selectedCaseId);
  const kc = useResource(caseId ? (sig) => socApi.killChain(caseId, sig) : null, `kc:${caseId ?? ""}`);
  const tech = useResource(caseId ? (sig) => socApi.caseTechniques(caseId, sig) : null, `tech:${caseId ?? ""}`);
  return (
    <SocSection
      title="Kill chain"
      area="soc-killchain"
      explain="How far the attacker behind the selected case has got, from first probing to moving money out. Later stages are more urgent."
      resource={caseId ? kc : null}
      idle={<p className="empty">Select a case here or in the Live tab to see its kill chain.</p>}
      footer={
        <>
          <CasePicker />
          {caseId && tech.data ? <Techniques doc={tech.data} /> : null}
        </>
      }
    >
      {(doc) => <StageList doc={doc} />}
    </SocSection>
  );
}
