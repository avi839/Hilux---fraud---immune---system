import { socApi } from "../../api/soc";
import { SocSection, pct, useReportFigure } from "./common";
import { useResource } from "./useResource";

/** Coverage_Matrix (Req 30.6-30.8): per technique, which detectors catch it and the attacker
 *  detection rate. Gaps are flagged in text ("GAP"), not by colour only; the catalogue version
 *  and the server's not-official-MITRE notice are shown verbatim. */
export function CoverageSection() {
  const r = useResource((sig) => socApi.coverage(sig), "coverage");
  useReportFigure("coverage", r.data);
  return (
    <SocSection
      title="Coverage matrix"
      area="soc-coverage"
      explain="Which attack techniques HELIX has at least one detector for, and where it is blind (a gap)."
      resource={r}
    >
      {(doc) => (
        <>
          <p className="soc-notice" data-testid="coverage-notice">
            Technique catalogue version <strong>{doc.catalogue_version}</strong>. {doc.notice}
          </p>
          <p data-testid="coverage-summary">
            {doc.summary.covered} of {doc.summary.techniques} techniques covered ·{" "}
            <strong>{doc.summary.gaps} gap{doc.summary.gaps === 1 ? "" : "s"}</strong>
          </p>
          <div className="scroll scroll--short" tabIndex={0} role="group" aria-label="Coverage matrix table">
            <table className="table table--compact">
              <caption className="sr-only">Coverage matrix: one row per technique</caption>
              <thead>
                <tr>
                  <th scope="col">Technique</th>
                  <th scope="col">Domain</th>
                  <th scope="col">Stage</th>
                  <th scope="col">Detected by</th>
                  <th scope="col" className="num">
                    Attacker detection
                  </th>
                  <th scope="col">Coverage</th>
                </tr>
              </thead>
              <tbody>
                {doc.techniques.map((t) => (
                  <tr key={t.technique_id} className={t.gap ? "row--gap" : undefined} data-testid={`coverage-${t.technique_id}`}>
                    <th scope="row" className="loop-stage">
                      <span className="mono">{t.technique_id}</span> {t.name}
                    </th>
                    <td>{t.domain}</td>
                    <td>{t.kill_chain_stage ?? ""}</td>
                    <td className="wrap">
                      {t.contributors.length
                        ? t.contributors.map((c) => `${c.contributor_id}${c.version !== undefined ? ` v${c.version}` : ""}`).join(", ")
                        : "none"}
                    </td>
                    <td className="num">
                      {t.detection_rate === null ? "n/a" : pct(t.detection_rate)}
                      {t.campaigns ? <span className="muted small"> ({t.campaigns} campaigns)</span> : null}
                    </td>
                    <td>{t.gap ? <span className="badge badge--warn">GAP</span> : <span className="badge badge--ok">covered</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </SocSection>
  );
}
