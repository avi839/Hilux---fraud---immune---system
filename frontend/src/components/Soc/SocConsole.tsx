import { useCallback, useId, useState, type ComponentType } from "react";
import { INVESTIGATOR_MAX } from "../Controls/validation";
import { PanelBoundary } from "../common/ErrorBoundary";
import { ViewHeader } from "../common/ViewHeader";
import { AuditSection } from "./Audit";
import { InvestigatorContext, SocFiguresContext, type SocFigures } from "./common";
import { CoverageSection } from "./Coverage";
import { IntelSection } from "./Intel";
import { KillChainSection } from "./KillChain";
import { PlaybooksSection } from "./Playbooks";
import { PurpleTeamSection } from "./PurpleTeam";
import { ResilienceSection } from "./Resilience";
import { RulesSection } from "./Rules";
import { SlaSection } from "./Sla";
import { SocKpis } from "./SocKpis";
import "./soc.css";

/** The SOC console sections in display (and focus) order: [title, grid area, component].
 *  Each one loads its own data and has its own error boundary, so a missing route, disabled
 *  cyber fusion or a render failure in one section leaves the others working (Req 38.10). */
const SECTIONS: ReadonlyArray<readonly [string, string, ComponentType]> = [
  ["SLA timers and handoff", "soc-sla", SlaSection],
  ["Kill chain", "soc-killchain", KillChainSection],
  ["Response playbooks", "soc-playbooks", PlaybooksSection],
  ["Detection rules", "soc-rules", RulesSection],
  ["Coverage matrix", "soc-coverage", CoverageSection],
  ["Threat intel", "soc-intel", IntelSection],
  ["Audit ledger", "soc-audit", AuditSection],
  ["Resilience mode", "soc-resilience", ResilienceSection],
  ["Purple team", "soc-purple", PurpleTeamSection],
];

/** SOC console view (Req 38.2): the security-operations tooling of HELIX in one tab. The
 *  investigator name is typed once here and sent as ``by`` / ``author`` by every control. */
export function SocConsole() {
  const [name, setName] = useState("");
  const [figures, setFigures] = useState<SocFigures>({});
  const report = useCallback((patch: SocFigures) => setFigures((f) => ({ ...f, ...patch })), []);
  const id = useId();
  const hint = useId();
  return (
    <InvestigatorContext.Provider value={name}>
      <SocFiguresContext.Provider value={report}>
        <div className="hx-view soc-console" role="region" aria-labelledby="soc-title">
          <ViewHeader
            id="soc-title"
            eyebrow="Security operations"
            title="SOC console"
            sub="Timers, playbooks, rules, coverage, intel and the audit trail in one place. Every figure comes from the running HELIX server."
            aside={
              <div className="soc-who">
                <label htmlFor={id} className="soc-who__label">
                  Investigator name
                </label>
                <input
                  id={id}
                  type="text"
                  autoComplete="username"
                  maxLength={INVESTIGATOR_MAX}
                  value={name}
                  placeholder="e.g. ana"
                  aria-describedby={hint}
                  onChange={(e) => setName(e.target.value)}
                />
                <p id={hint} className="soc-who__hint">
                  Used for approvals, rule activation and notes.
                </p>
              </div>
            }
          />
          <SocKpis figures={figures} />
          <div className="soc-grid">
            {SECTIONS.map(([title, area, Section]) => (
              <PanelBoundary key={area} title={title} area={area}>
                <Section />
              </PanelBoundary>
            ))}
          </div>
          <p className="soc-footnote">Sections whose feature is off say so, and the rest keep working.</p>
        </div>
      </SocFiguresContext.Provider>
    </InvestigatorContext.Provider>
  );
}
