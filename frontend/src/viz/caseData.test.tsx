import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { jsonResponse, routeFetch } from "../test/fixtures";
import { EMPTY_TIMELINE_RETRY_MS, useThrottledValue, useTimeline } from "./caseData";

describe("useTimeline", () => {
  it("fetches an empty timeline again (Storage not written yet) and shows the entries once they exist", async () => {
    let calls = 0;
    const entry = { seq_no: 1, event: { tx_id: "T1", ts: "2026-01-01T00:00:00Z", src: "A", dst: "B", amount: "1", currency: "EUR" } };
    routeFetch({
      "/cases/C-9/timeline": () => {
        calls += 1;
        const entries = calls === 1 ? [] : [entry];
        return jsonResponse({ case_id: "C-9", start: "2026-01-01T00:00:00Z", end: "2026-01-01T00:00:00Z", entries });
      },
    });
    const { result } = renderHook(() => useTimeline("C-9", "v1"));
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.status === "ready" && result.current.data.entries).toEqual([]);
    await waitFor(() => expect(result.current.status === "ready" && result.current.data.entries.length).toBe(1), {
      timeout: EMPTY_TIMELINE_RETRY_MS + 2_000,
    });
    expect(calls).toBe(2);
  });
});

describe("useThrottledValue", () => {
  it("passes the first change at once, coalesces later ones within the window, and resets per key", () => {
    vi.useFakeTimers();
    const { result, rerender } = renderHook(({ v, k }) => useThrottledValue(v, k, 5_000), { initialProps: { v: "v1", k: "C-1" } });
    expect(result.current).toBe("v1");

    rerender({ v: "v2", k: "C-1" });
    expect(result.current).toBe("v2"); // first change: immediate

    rerender({ v: "v3", k: "C-1" });
    rerender({ v: "v4", k: "C-1" });
    expect(result.current).toBe("v2"); // within the window: held
    act(() => vi.advanceTimersByTime(5_000));
    expect(result.current).toBe("v4"); // one trailing update with the latest value

    rerender({ v: "w1", k: "C-2" });
    expect(result.current).toBe("w1"); // new key: immediate
    rerender({ v: "w2", k: "C-2" });
    expect(result.current).toBe("w2"); // and its first change too
  });
});
