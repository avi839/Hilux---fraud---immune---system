import { render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { axe } from "vitest-axe";
import type { DatasetDoc } from "../../api/types";
import { jsonResponse, routeFetch } from "../../test/fixtures";
import { DATASET_DISABLED_TEXT, DatasetPanel, precisionText, progressPercent, UNMAPPED_PRECISION } from "./DatasetPanel";

const AXE_OPTIONS = { rules: { "color-contrast": { enabled: false } } };

// Keys exactly as helix/api/dataset_routes.py dataset_doc / evaluation_doc.
const DISABLED: DatasetDoc = {
  status: "DISABLED",
  kind: null,
  files: null,
  rows_read: null,
  skipped: null,
  skip_reasons: null,
  in_scope: null,
  submitted: null,
  progress: null,
  speed: null,
  max_rows: null,
  warmup: null,
  error: null,
  evaluation: null,
};

const STREAMING: DatasetDoc = {
  status: "STREAMING",
  kind: "amlworld",
  files: { trans: "data/HI-Small_Trans.csv", patterns: "data/HI-Small_Patterns.txt", mapping: null },
  rows_read: 20_000,
  skipped: 7,
  skip_reasons: { bad_amount: 3, unknown_currency: 4 },
  in_scope: 19_993,
  submitted: 9_000,
  progress: 9_000 / 19_993,
  speed: "max",
  max_rows: 20_000,
  warmup: {
    until: null,
    done: true,
    events: 5_000,
    duration_s: 12.5,
    events_per_s: 400,
    accounts: 900,
    accounts_ready: 640,
    min_history: 20,
  },
  error: null,
  evaluation: {
    precision: 0.42,
    recall: 0.31,
    captured_fraud_fraction: 0.5,
    pattern_types: [
      {
        pattern_type: "CYCLE",
        mapped: true,
        motif: "ROUND_TRIP_CYCLE",
        patterns: 4,
        detected_patterns: 3,
        detection_rate: 0.75,
        recall: 0.6,
        precision: 0.9,
        precision_reason: null,
      },
      {
        pattern_type: "STACK",
        mapped: false,
        motif: null,
        patterns: 2,
        detected_patterns: 0,
        detection_rate: 0,
        recall: 0,
        precision: null,
        precision_reason: "unmapped",
      },
      {
        pattern_type: "FAN-IN",
        mapped: true,
        motif: "FAN_IN",
        patterns: 1,
        detected_patterns: 0,
        detection_rate: null,
        recall: null,
        precision: null,
        precision_reason: "no_cases",
      },
    ],
  },
};

describe("DatasetPanel", () => {
  it("shows a compact note when dataset streaming is DISABLED and stops polling", async () => {
    const fetchFn = routeFetch({ "/dataset": DISABLED });
    const { container } = render(<DatasetPanel pollMs={20} />);
    expect(await screen.findByTestId("dataset-disabled")).toHaveTextContent(DATASET_DISABLED_TEXT);
    await new Promise((r) => setTimeout(r, 80));
    expect(fetchFn).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    const results = await axe(container, AXE_OPTIONS);
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  });

  it("handles a server without GET /dataset (404)", async () => {
    routeFetch({});
    render(<DatasetPanel />);
    expect(await screen.findByTestId("dataset-absent")).toHaveTextContent("not available on this server");
  });

  it("shows status, progress text, skip reasons, warm-up report and per-pattern evaluation", async () => {
    routeFetch({ "/dataset": STREAMING });
    const { container } = render(<DatasetPanel />);
    expect(await screen.findByTestId("dataset-status")).toHaveTextContent("Streaming");
    expect(screen.getByRole("progressbar", { name: "Dataset submission progress" })).toBeInTheDocument();
    expect(screen.getByTestId("dataset-progress")).toHaveTextContent("45% (9,000 of 19,993 in-scope events submitted)");
    expect(screen.getByTestId("dataset-rows")).toHaveTextContent("Rows read 20,000 · skipped 7: bad_amount 3, unknown_currency 4");
    expect(screen.getByTestId("dataset-warmup")).toHaveTextContent(
      "Warm-up done: 5,000 events in 12.5 s (400.0 events/s); 640 of 900 accounts reached 20 events of history.",
    );
    expect(screen.getByTestId("dataset-eval")).toHaveTextContent("Precision 0.420 · recall 0.310 · captured fraud fraction 0.500");
    const cycle = within(screen.getByTestId("pattern-CYCLE")).getAllByRole("cell");
    expect(cycle.map((c) => c.textContent)).toEqual(["ROUND_TRIP_CYCLE", "4", "3 (75%)", "0.600", "0.900"]);
    expect(screen.getByTestId("pattern-STACK")).toHaveTextContent(UNMAPPED_PRECISION);
    expect(screen.getByTestId("pattern-FAN-IN")).toHaveTextContent("n/a (no cases yet)");
    const results = await axe(container, AXE_OPTIONS);
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  });

  it("polls and keeps the last values when a poll fails", async () => {
    let n = 0;
    routeFetch({
      "/dataset": () => {
        n += 1;
        return n === 1 ? jsonResponse({ ...STREAMING, submitted: 100, progress: 0.005 }) : jsonResponse({ detail: "boom" }, 500);
      },
    });
    render(<DatasetPanel pollMs={20} />);
    expect(await screen.findByTestId("dataset-progress")).toHaveTextContent("0% (100 of");
    expect(await screen.findByRole("alert")).toHaveTextContent("GET /dataset failed");
    expect(screen.getByTestId("dataset-progress")).toHaveTextContent("0% (100 of");
  });

  it("shows FAILED with the error and a warm-up still in progress", async () => {
    routeFetch({
      "/dataset": {
        ...STREAMING,
        status: "FAILED",
        error: "trans: bad header",
        progress: null,
        warmup: { ...STREAMING.warmup!, done: false, until: "2022-09-02T00:00:00+00:00" },
        evaluation: null,
      },
    });
    render(<DatasetPanel />);
    expect(await screen.findByTestId("dataset-status")).toHaveTextContent("Failed");
    expect(screen.getByTestId("dataset-error")).toHaveTextContent("trans: bad header");
    expect(screen.getByTestId("dataset-progress")).toHaveTextContent("Progress n/a");
    expect(screen.getByTestId("dataset-warmup")).toHaveTextContent("Warm-up in progress (rows before 2022-09-02T00:00:00+00:00)");
    await waitFor(() => expect(screen.queryByTestId("dataset-patterns")).not.toBeInTheDocument());
  });

  it("formats progress and precision", () => {
    expect(progressPercent(null)).toBeNull();
    expect(progressPercent(0.999)).toBe(99);
    expect(progressPercent(1)).toBe(100);
    expect(progressPercent(1.2)).toBe(100);
    expect(precisionText(STREAMING.evaluation!.pattern_types[1]!)).toBe(UNMAPPED_PRECISION);
  });
});
