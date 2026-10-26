import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import fc from "fast-check";
import { describe, expect, it } from "vitest";
import { axe } from "vitest-axe";
import type { CaseDetail, SuggestedLabel as SuggestedLabelDoc } from "../../api/types";
import { dashboard } from "../../store/store";
import { jsonResponse, routeFetch } from "../../test/fixtures";
import { aliceDetail, aliceEvidence } from "../../test/sampleRun";
import { Controls } from "../Controls/Controls";
import { EvidencePanel } from "./EvidencePanel";
import { AUTO_CLEARED_TEXT, SUGGESTION_DISCLAIMER } from "./SuggestedLabel";
import { isTextEntry, shortcutFor } from "./useShortcuts";

const AXE_OPTIONS = { rules: { "color-contrast": { enabled: false } } };

const FRAUD_SUGGESTION: SuggestedLabelDoc = {
  label: "CONFIRMED_FRAUD",
  confidence: 0.949,
  reason: "Belief 0.95, plausibility 1.00, unknown 0.05: committed evidence supports fraud",
};

const withSuggestion = (extra: Partial<CaseDetail> = {}): CaseDetail => ({
  ...aliceDetail,
  closure_reason: null,
  suggested_label: FRAUD_SUGGESTION,
  ...extra,
});

type Call = { path: string; body: unknown };

function serve(calls: Call[], detail: CaseDetail = withSuggestion()) {
  const post = (status: string, extra: Partial<CaseDetail> = {}) => (init: RequestInit | undefined, url: string) => {
    calls.push({ path: url.replace(/^.*\/api\/v1/, ""), body: init?.body ? JSON.parse(String(init.body)) : undefined });
    return jsonResponse({ ...detail, status, assignee: "alice", updated_at: "2024-06-05T11:30:00+00:00", ...extra });
  };
  routeFetch({
    "/cases/C-1": detail,
    "/cases/C-1/evidence": aliceEvidence,
    "/cases/C-1/open": post("IN_REVIEW"),
    "/cases/C-1/feedback": post("CLOSED", { label: "CONFIRMED_FRAUD", placement: "NONE", closure_reason: "LABELLED" }),
    "/cases/C-1/escalate": post("OPEN"),
  });
}

async function setup(detail: CaseDetail = withSuggestion()) {
  const calls: Call[] = [];
  serve(calls, detail);
  act(() => {
    dashboard.getState().upsertCase(detail);
    dashboard.getState().selectCase("C-1");
  });
  const user = userEvent.setup();
  const utils = render(
    <>
      <Controls />
      <EvidencePanel />
    </>,
  );
  await screen.findByTestId("ev-summary");
  const panel = screen.getByRole("region", { name: "Evidence" });
  return { calls, user, panel, ...utils };
}

describe("Suggested label in the EvidencePanel", () => {
  it("shows label, confidence %, reason and the disclaimer, with no apply button", async () => {
    const { container } = await setup();
    const box = screen.getByTestId("ev-suggestion");
    expect(box).toHaveTextContent("Confirmed fraud (CONFIRMED_FRAUD) · confidence 95%");
    expect(box).toHaveTextContent(FRAUD_SUGGESTION.reason);
    expect(box).toHaveTextContent(SUGGESTION_DISCLAIMER);
    expect(box.querySelector("button")).toBeNull();
    const results = await axe(container, AXE_OPTIONS);
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  });

  it('shows an AUTO_CLEARED closure as "Auto-cleared (not a label)"', async () => {
    await setup(
      withSuggestion({
        status: "CLOSED",
        placement: "NONE",
        closure_reason: "AUTO_CLEARED",
        closed_at: "2024-06-06T11:20:00+00:00",
        suggested_label: { label: "NONE", confidence: 0, reason: "Belief 0.40, plausibility 0.90, unknown 0.50: between thresholds" },
      }),
    );
    const note = screen.getByTestId("ev-closure");
    expect(note).toHaveTextContent(AUTO_CLEARED_TEXT);
    expect(note).toHaveTextContent("no Feedback_Label was stored");
    expect(screen.getByTestId("ev-suggestion")).toHaveTextContent("No suggestion (NONE) · confidence 0%");
  });
});

describe("EvidencePanel keyboard shortcuts", () => {
  it("lists the shortcuts and exposes them on the Case action buttons", async () => {
    const { user, panel } = await setup();
    expect(screen.getByTestId("ev-shortcuts")).toHaveTextContent("F confirm fraud · P false positive · E escalate · ? help");
    expect(screen.getByRole("button", { name: "Confirm fraud" })).toHaveAttribute("aria-keyshortcuts", "F");
    expect(screen.getByRole("button", { name: "False positive" })).toHaveAttribute("aria-keyshortcuts", "P");
    expect(screen.getByRole("button", { name: "Escalate" })).toHaveAttribute("aria-keyshortcuts", "E");

    const help = screen.getByTestId("ev-shortcut-help");
    expect(help).not.toBeVisible();
    act(() => panel.focus());
    await user.keyboard("?");
    expect(help).toBeVisible();
    expect(screen.getByRole("button", { name: "Shortcut help" })).toHaveAttribute("aria-expanded", "true");
    await user.keyboard("{Escape}");
    expect(help).not.toBeVisible();
  });

  it("F runs the Confirm fraud button's path: name check first, then open + feedback", async () => {
    const { calls, user, panel } = await setup();
    act(() => panel.focus());
    await user.keyboard("f");
    // Same validation as the button: no name, nothing sent.
    expect(screen.getByRole("alert")).toHaveTextContent("Enter your investigator name first.");
    expect(calls).toEqual([]);

    await user.type(screen.getByLabelText("Investigator name"), "alice");
    // Typing "f"/"p"/"e" in the name field never fires a shortcut (the field is outside the
    // panel here, and text entry is ignored inside it too).
    expect(calls).toEqual([]);
    act(() => panel.focus());
    await user.keyboard("F");
    await waitFor(() => expect(screen.getByText(/C-1 is CLOSED with label CONFIRMED_FRAUD/)).toBeInTheDocument());
    expect(calls).toEqual([
      { path: "/cases/C-1/open", body: { investigator: "alice" } },
      { path: "/cases/C-1/feedback", body: { by: "alice", label: "CONFIRMED_FRAUD", note: "" } },
    ]);
    expect(screen.getByTestId("ev-shortcut-note")).toHaveTextContent("Confirm fraud requested for C-1");

    // The Case is now CLOSED, so the buttons are disabled and P does nothing.
    act(() => panel.focus());
    await user.keyboard("p");
    expect(calls).toHaveLength(2);
    expect(screen.getByTestId("ev-shortcut-note")).toHaveTextContent("False positive is not available for C-1");
  });

  it("ignores modifiers and key repeat; E escalates", async () => {
    const { calls, user, panel } = await setup();
    await user.type(screen.getByLabelText("Investigator name"), "alice");
    act(() => panel.focus());
    await user.keyboard("{Control>}f{/Control}{Alt>}p{/Alt}{Meta>}e{/Meta}{Shift>}F{/Shift}");
    fireEvent.keyDown(panel, { key: "f", repeat: true });
    expect(calls).toEqual([]);
    await user.keyboard("e");
    await waitFor(() => expect(calls).toEqual([{ path: "/cases/C-1/escalate", body: { by: "alice" } }]));
  });

  it("never fires in an input, textarea, select or editable element inside the panel", async () => {
    const { calls, panel } = await setup();
    act(() => dashboard.getState().setInvestigator("alice"));
    const body = panel.querySelector(".panel__body")!;
    for (const html of ["<input>", "<textarea></textarea>", "<select><option>a</option></select>", '<div contenteditable="true"></div>']) {
      const host = document.createElement("div");
      host.innerHTML = html;
      body.appendChild(host);
      const el = host.firstElementChild as HTMLElement;
      fireEvent.keyDown(el, { key: "f" });
      fireEvent.keyDown(el, { key: "e" });
      host.remove();
    }
    expect(calls).toEqual([]);
    // Control: the same key on non-editable panel content does run the action.
    fireEvent.keyDown(body, { key: "e" });
    await waitFor(() => expect(calls.map((c) => c.path)).toEqual(["/cases/C-1/escalate"]));
  });
});

describe("shortcutFor", () => {
  const base = { ctrlKey: false, altKey: false, metaKey: false, shiftKey: false, repeat: false, target: document.body };

  it("maps f/F, p/P, e/E and ?", () => {
    expect(shortcutFor({ ...base, key: "f" })).toBe("CONFIRMED_FRAUD");
    expect(shortcutFor({ ...base, key: "F" })).toBe("CONFIRMED_FRAUD");
    expect(shortcutFor({ ...base, key: "p" })).toBe("FALSE_POSITIVE");
    expect(shortcutFor({ ...base, key: "E" })).toBe("ESCALATE");
    expect(shortcutFor({ ...base, key: "?", shiftKey: true })).toBe("HELP");
    expect(shortcutFor({ ...base, key: "x" })).toBeNull();
    expect(shortcutFor({ ...base, key: "Enter" })).toBeNull();
  });

  it("treats inputs, textareas, selects and contenteditable as text entry", () => {
    for (const tag of ["input", "textarea", "select"]) expect(isTextEntry(document.createElement(tag))).toBe(true);
    const ed = document.createElement("div");
    ed.setAttribute("contenteditable", "true");
    const child = document.createElement("span");
    ed.appendChild(child);
    expect(isTextEntry(child)).toBe(true);
    expect(isTextEntry(document.createElement("button"))).toBe(false);
  });

  // Property: with any of Ctrl/Alt/Meta held, on key repeat, or in a text field, no key maps
  // to a shortcut. **Validates: Requirements 41.9**
  it("never fires with a modifier, on repeat or in a text field (property)", () => {
    const input = document.createElement("input");
    fc.assert(
      fc.property(
        fc.constantFrom("f", "F", "p", "P", "e", "E", "?", "x", "Tab"),
        fc.record({ ctrlKey: fc.boolean(), altKey: fc.boolean(), metaKey: fc.boolean(), shiftKey: fc.boolean(), repeat: fc.boolean() }),
        fc.boolean(),
        (key, mods, inField) => {
          const blocked = mods.ctrlKey || mods.altKey || mods.metaKey || mods.repeat || inField;
          const r = shortcutFor({ key, ...mods, target: inField ? input : document.body });
          return blocked ? r === null : true;
        },
      ),
    );
  });
});
