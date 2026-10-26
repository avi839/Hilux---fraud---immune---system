import { useEffect, useState, type ReactNode } from "react";
import { socApi, type AuditVerifyDoc } from "../../api/soc";
import { pct, type SocFigures } from "./common";

const NA = "n/a";

function Kpi({ label, value, note, testId }: { label: string; value: string; note?: ReactNode; testId: string }) {
  return (
    <li className="hx-kpi soc-kpi" data-testid={testId}>
      <span className="soc-kpi__label">{label}</span>
      <strong className="soc-kpi__value">{value}</strong>
      {note ? <span className="soc-kpi__note">{note}</span> : null}
    </li>
  );
}

/** The SOC console at a glance: six figures taken from the documents the sections below have
 *  loaded from the running server, or "n/a" while a route is missing, disabled or not loaded.
 *  Nothing here is estimated. The audit chain is checked once (read-only) on open; the Verify
 *  control of the Audit ledger section updates it. */
export function SocKpis({ figures }: { figures: SocFigures }) {
  const [checked, setChecked] = useState<AuditVerifyDoc | null>(null);
  useEffect(() => {
    let alive = true;
    socApi.verifyAudit().then(
      (doc) => alive && setChecked(doc),
      () => undefined, // absent / disabled / failed: stays n/a
    );
    return () => {
      alive = false;
    };
  }, []);

  const { console: c, pending, rules, coverage, auditTotal } = figures;
  const chain = figures.verify ?? checked;
  const cov = coverage?.summary;
  const activeRules = rules ? rules.rules.filter((r) => r.active_version !== null).length : null;

  return (
    <ul className="soc-kpis" aria-label="SOC console at a glance">
      <Kpi
        testId="soc-kpi-breaches"
        label="Open SLA breaches"
        value={c ? String(c.breached_cases) : NA}
        note={c ? `of ${c.open_cases} open cases` : undefined}
      />
      <Kpi
        testId="soc-kpi-pending"
        label="Awaiting approval"
        value={pending ? String(pending.count ?? pending.actions.length) : NA}
        note={pending ? "playbook actions" : undefined}
      />
      <Kpi
        testId="soc-kpi-rules"
        label="Detection rules"
        value={rules ? String(rules.rules.length) : NA}
        note={activeRules !== null ? `${activeRules} active` : undefined}
      />
      <Kpi
        testId="soc-kpi-coverage"
        label="Technique coverage"
        value={cov && cov.techniques > 0 ? pct(cov.covered / cov.techniques) : NA}
        note={cov ? `${cov.covered} of ${cov.techniques} techniques` : undefined}
      />
      <Kpi
        testId="soc-kpi-audit"
        label="Audit entries"
        value={auditTotal !== undefined ? String(auditTotal) : NA}
        note={auditTotal !== undefined ? "hash-chained" : undefined}
      />
      <Kpi
        testId="soc-kpi-chain"
        label="Chain verified"
        value={chain ? (chain.valid ? "Yes" : "No, broken") : NA}
        note={chain ? `${chain.entries} entr${chain.entries === 1 ? "y" : "ies"} checked` : undefined}
      />
    </ul>
  );
}
