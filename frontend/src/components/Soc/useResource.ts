import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../../api/client";
import { describeSocError, unavailableKind } from "../../api/soc";

export type ResourceStatus = "loading" | "ok" | "absent" | "disabled" | "error";

export interface Resource<T> {
  status: ResourceStatus;
  /** Last successfully loaded document; kept while a later refresh fails. */
  data: T | null;
  error: string | null;
  loading: boolean;
  updatedAt: number | null;
  reload: () => void;
}

/** Loads one SOC document, optionally re-polling every ``pollMs``. A 404 "Not Found" (route
 *  missing) or 503 (cyber fusion off) is reported as a status of its own, so a section can say
 *  so and the rest of the view keeps working (Req 38.10). ``load`` is keyed by ``key``: a change
 *  of key (e.g. the selected Case) drops the old document and loads again. */
export function useResource<T>(load: ((signal: AbortSignal) => Promise<T>) | null, key: string, pollMs?: number): Resource<T> {
  const [state, setState] = useState<Omit<Resource<T>, "reload">>({
    status: "loading",
    data: null,
    error: null,
    loading: load !== null,
    updatedAt: null,
  });
  const [nonce, setNonce] = useState(0);
  const loadRef = useRef(load);
  loadRef.current = load;
  const reload = useCallback(() => setNonce((n) => n + 1), []);

  // A new key starts from scratch.
  useEffect(() => {
    setState({ status: "loading", data: null, error: null, loading: loadRef.current !== null, updatedAt: null });
  }, [key]);

  useEffect(() => {
    const fn = loadRef.current;
    if (!fn) return;
    let alive = true;
    let ctrl: AbortController | null = null;
    const tick = async () => {
      ctrl?.abort();
      const c = new AbortController();
      ctrl = c;
      setState((s) => ({ ...s, loading: true }));
      try {
        const data = await fn(c.signal);
        if (!alive || c.signal.aborted) return;
        setState({ status: "ok", data, error: null, loading: false, updatedAt: Date.now() });
      } catch (err) {
        if (!alive || c.signal.aborted || (err instanceof ApiError && err.kind === "aborted")) return;
        const u = unavailableKind(err);
        setState((s) => ({
          status: u ?? "error",
          data: u ? null : s.data,
          error: describeSocError(err),
          loading: false,
          updatedAt: s.updatedAt,
        }));
      }
    };
    void tick();
    const timer = pollMs ? setInterval(() => void tick(), pollMs) : null;
    return () => {
      alive = false;
      if (timer) clearInterval(timer);
      ctrl?.abort();
    };
  }, [key, nonce, pollMs]);

  return { ...state, reload };
}

export type Msg = { kind: "ok" | "error"; text: string } | null;

/** One control action: pending flag plus the last confirmation or error message. */
export function useAction() {
  const [pending, setPending] = useState(false);
  const [msg, setMsg] = useState<Msg>(null);
  const run = useCallback(async (fn: () => Promise<string>) => {
    setPending(true);
    setMsg(null);
    try {
      setMsg({ kind: "ok", text: await fn() });
    } catch (err) {
      setMsg({ kind: "error", text: describeSocError(err) });
    } finally {
      setPending(false);
    }
  }, []);
  return { pending, msg, setMsg, run };
}
