import { useState } from "react";
import { socApi, type AuditVerifyDoc } from "../../api/soc";
import { formatTime } from "../../format";
import { Message, SocSection, useReportFigure } from "./common";
import { useAction, useResource } from "./useResource";

export const AUDIT_LABEL = "tamper-evident, not tamper-proof";
const PAGE = 50;

function short(hash: string | null | undefined, n = 12): string {
  return hash ? `${hash.slice(0, n)}…` : "none";
}

/** A one-line view of an entry payload (the full JSON stays in the title for hover/AT). */
function payloadLine(p: unknown): string {
  if (p === null || p === undefined) return "";
  const s = typeof p === "string" ? p : JSON.stringify(p);
  return s.length > 120 ? `${s.slice(0, 117)}…` : s;
}

function VerifyResult({ doc }: { doc: AuditVerifyDoc }) {
  return doc.valid ? (
    <p role="status" className="msg msg--ok" data-testid="audit-verify">
      Chain verified: {doc.entries} entr{doc.entries === 1 ? "y" : "ies"} intact. {doc.notice}
    </p>
  ) : (
    <p role="alert" className="msg msg--error" data-testid="audit-verify">
      Chain BROKEN{doc.first_broken_index !== null ? ` at entry ${doc.first_broken_index}` : ""}
      {doc.reason ? `: ${doc.reason}` : ""}. {doc.notice}
    </p>
  );
}

/** Audit_Ledger (Req 35): hash-chained entries, newest page first, with a Verify control and
 *  the "tamper-evident, not tamper-proof" label (Req 38.2). */
export function AuditSection() {
  const [offset, setOffset] = useState(0);
  const r = useResource((sig) => socApi.audit(offset, PAGE, sig), `audit:${offset}`);
  const verify = useAction();
  const [result, setResult] = useState<AuditVerifyDoc | null>(null);
  useReportFigure("auditTotal", r.data?.total);
  useReportFigure("verify", result);

  const doVerify = () => {
    setResult(null);
    void verify.run(async () => {
      const doc = await socApi.verifyAudit();
      setResult(doc);
      return "";
    });
  };

  return (
    <SocSection
      title="Audit ledger"
      area="soc-audit"
      explain="Every analyst and system action, chained by hashes so that any later edit is detectable."
      resource={r}
      footer={
        <div className="soc-sub">
          <button type="button" className="btn" onClick={doVerify} disabled={verify.pending} aria-busy={verify.pending || undefined}>
            {verify.pending ? "Verifying…" : "Verify"}
          </button>
          {result ? <VerifyResult doc={result} /> : null}
          {verify.msg?.kind === "error" ? <Message msg={verify.msg} /> : null}
        </div>
      }
    >
      {(doc) => (
        <>
          <p className="soc-notice" data-testid="audit-label">
            Audit ledger: <strong>{AUDIT_LABEL}</strong>. Anyone with write access to the store can rewrite the whole chain; Verify only shows
            whether it is internally consistent.
          </p>
          <p className="small" data-testid="audit-summary">
            {doc.total} entr{doc.total === 1 ? "y" : "ies"} · head <span className="mono">{short(doc.head?.hash ?? null, 16)}</span>
          </p>
          {doc.entries.length === 0 ? (
            <p className="empty">No audit entries yet.</p>
          ) : (
            <div className="scroll scroll--short" tabIndex={0} role="group" aria-label="Audit ledger entries">
              <table className="table table--compact">
                <caption className="sr-only">
                  Audit ledger entries {doc.offset ?? offset} to {(doc.offset ?? offset) + doc.entries.length - 1}
                </caption>
                <thead>
                  <tr>
                    <th scope="col" className="num">
                      #
                    </th>
                    <th scope="col">Time</th>
                    <th scope="col">Category</th>
                    <th scope="col">Actor</th>
                    <th scope="col">Details</th>
                    <th scope="col">Hash</th>
                  </tr>
                </thead>
                <tbody>
                  {doc.entries.map((e) => (
                    <tr key={e.index} data-testid={`audit-${e.index}`}>
                      <th scope="row" className="num">
                        {e.index}
                      </th>
                      <td>{formatTime(e.ts)}</td>
                      <td>{e.category}</td>
                      <td>{e.actor}</td>
                      <td className="wrap small mono" title={typeof e.payload === "string" ? e.payload : JSON.stringify(e.payload)}>
                        {payloadLine(e.payload)}
                      </td>
                      <td className="mono small">{short(e.hash)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className="buttons soc-row">
            <button type="button" className="btn btn--small" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
              Previous page
            </button>
            <button
              type="button"
              className="btn btn--small"
              disabled={offset + doc.entries.length >= doc.total}
              onClick={() => setOffset(offset + PAGE)}
            >
              Next page
            </button>
          </div>
        </>
      )}
    </SocSection>
  );
}
