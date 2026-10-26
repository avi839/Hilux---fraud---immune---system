import { useId, useState, type ChangeEvent } from "react";
import { socApi, type ImportReport } from "../../api/soc";
import { formatTime } from "../../format";
import { Message, SocSection } from "./common";
import { useAction, useResource } from "./useResource";

/** Save ``doc`` as a JSON file through a temporary object URL. */
function download(doc: unknown, filename: string) {
  const blob = new Blob([JSON.stringify(doc, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

function count(v: ImportReport["skipped"]): number {
  return Array.isArray(v) ? v.length : typeof v === "number" ? v : 0;
}

function ExportImport({ onImported }: { onImported: () => void }) {
  const [includeRed, setIncludeRed] = useState(false);
  const exp = useAction();
  const imp = useAction();
  const [report, setReport] = useState<ImportReport | null>(null);
  const redId = useId();
  const fileId = useId();

  const doExport = () =>
    void exp.run(async () => {
      const bundle = await socApi.exportIndicators(includeRed);
      const n = Array.isArray(bundle.objects) ? bundle.objects.length : 0;
      download(bundle, `helix-indicators${includeRed ? "-incl-red" : ""}.stix.json`);
      return `Exported a STIX bundle with ${n} object${n === 1 ? "" : "s"}${includeRed ? " (TLP:RED included)" : " (TLP:RED left out)"}.`;
    });

  const onFile = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    setReport(null);
    void imp.run(async () => {
      const r = await socApi.importIndicators(await file.text());
      setReport(r);
      onImported();
      return `Imported ${r.imported}, rejected ${r.rejected.length}, skipped ${count(r.skipped)} from ${file.name}.`;
    });
  };

  return (
    <div className="soc-sub">
      <div className="buttons soc-row">
        <button type="button" className="btn" disabled={exp.pending} onClick={doExport}>
          Export STIX
        </button>
        <span className="soc-check">
          <input id={redId} type="checkbox" checked={includeRed} onChange={(e) => setIncludeRed(e.target.checked)} />
          <label htmlFor={redId}>Include TLP:RED (not for sharing)</label>
        </span>
      </div>
      <Message msg={exp.msg} />
      <div className="field">
        <label htmlFor={fileId}>Import a STIX bundle (.json)</label>
        <input id={fileId} type="file" accept="application/json,.json" disabled={imp.pending} onChange={onFile} />
      </div>
      <Message msg={imp.msg} />
      {report && report.rejected.length ? (
        <ul className="ev-list small" data-testid="import-rejected" aria-label="Rejected indicator objects">
          {report.rejected.map((r, i) => (
            <li key={`${r.id ?? "?"}-${i}`}>
              <span className="mono">{r.id ?? "(no id)"}</span>: {r.errors.map((e) => `${e.field} ${e.reason}`).join("; ")}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

/** Threat-intel Indicator_Store (Req 34): stored indicators, STIX export (TLP:RED only on
 *  request) and import with per-object rejection reasons. */
export function IntelSection() {
  const r = useResource((sig) => socApi.indicators(0, 100, sig), "indicators");
  return (
    <SocSection
      title="Threat intel"
      area="soc-intel"
      explain="Mule accounts, devices, IPs and merchants from confirmed fraud, remembered so they are recognised next time and shareable as STIX."
      resource={r}
      footer={<ExportImport onImported={r.reload} />}
    >
      {(doc) =>
        doc.indicators.length === 0 ? (
          <p className="empty">No indicators yet. They appear when a case is labelled as confirmed fraud.</p>
        ) : (
          <div className="scroll scroll--short" tabIndex={0} role="group" aria-label="Indicators table">
            <table className="table table--compact">
              <caption className="sr-only">
                Indicators, {doc.count} of {doc.total}
              </caption>
              <thead>
                <tr>
                  <th scope="col">Value</th>
                  <th scope="col">Type</th>
                  <th scope="col" className="num">
                    Confidence
                  </th>
                  <th scope="col">TLP</th>
                  <th scope="col">Source</th>
                  <th scope="col">Last seen</th>
                  <th scope="col">Expiry</th>
                </tr>
              </thead>
              <tbody>
                {doc.indicators.map((i) => (
                  <tr key={`${i.type}:${i.value}`}>
                    <th scope="row" className="mono loop-stage">
                      {i.value}
                    </th>
                    <td>{i.type}</td>
                    <td className="num">{i.confidence}</td>
                    <td>{i.tlp}</td>
                    <td>{i.source}</td>
                    <td>{formatTime(i.last_seen)}</td>
                    <td>
                      {formatTime(i.expiry)}
                      {i.expired ? <span className="tag">expired</span> : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      }
    </SocSection>
  );
}
