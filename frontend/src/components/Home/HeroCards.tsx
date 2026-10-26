// The fanned product cards under the hero: static, decorative miniatures of real HELIX
// screens. Every number comes from the README walkthrough of docs/sample_transactions.jsonl
// (T007, Case C-1, the Carl → Dave → Erin → Finn mule chain). The group is aria-hidden; a
// visually hidden sentence carries the same facts for screen readers.

import { useRef, type RefObject } from "react";
import { useParallax } from "./motion";

const MASSES: { name: string; fraud: number; text: string }[] = [
  { name: "amount", fraud: 0.7, text: "0.70" },
  { name: "geo", fraud: 0.638, text: "0.638" },
  { name: "counterparty", fraud: 0.35, text: "0.35" },
  { name: "timing", fraud: 0.3, text: "0.30" },
];

export const CARDS_SUMMARY =
  "Product preview. Sensor fusion for transaction T007: amount 0.70, geo 0.638, counterparty 0.35 and timing 0.30 fraud mass, fused belief 0.949. " +
  "Case C-1 is CRITICAL with risk 0.973 and 4,800.00 EUR at risk, because the amount of 4,800.00 is far above the baseline mean of 37.69 and the implied travel speed of 4,454 km/h exceeds a plausible 900 km/h; suggested label confirmed fraud at 95%, a suggestion only. " +
  "A mule chain was detected: Carl to Dave 5,000, Dave to Erin 4,600, Erin to Finn 4,300.";

function FusionCard() {
  return (
    <div className="home-card home-card--left hx-card">
      <div className="home-card__float">
        <div className="home-card__head">
          <span className="home-card__title">Sensor fusion · T007</span>
          <span className="home-card__meta">PERCEIVE</span>
        </div>
        <ul className="home-mass">
          {MASSES.map((m) => (
            <li key={m.name} className="home-mass__row">
              <span className="home-mass__name">{m.name}</span>
              <span className="home-mass__track">
                <span className="home-mass__f" style={{ width: `${m.fraud * 100}%` }} />
              </span>
              <span className="home-mass__val">{m.text}</span>
            </li>
          ))}
        </ul>
        <p className="home-card__legend">
          <span className="home-key home-key--f" /> fraud mass <span className="home-key home-key--u" /> can&apos;t tell
        </p>
        <div className="home-card__foot">
          <span>Fused belief</span>
          <strong className="home-card__big">0.949</strong>
        </div>
      </div>
    </div>
  );
}

function CaseCard() {
  return (
    <div className="home-card home-card--centre hx-card">
      <div className="home-card__float">
        <div className="home-card__head">
          <span className="home-card__title">Case C-1</span>
          <span className="hx-chip home-chip home-chip--critical">CRITICAL</span>
        </div>
        <div className="home-case__stats">
          <div>
            <span className="home-case__label">Risk</span>
            <strong className="home-card__big">0.973</strong>
          </div>
          <div>
            <span className="home-case__label">At risk</span>
            <strong className="home-case__money">
              4,800.00 <span>EUR</span>
            </strong>
          </div>
        </div>
        <ul className="home-case__reasons">
          <li>
            <span className="home-case__tag">Amount</span> 4,800.00 vs baseline mean 37.69
          </li>
          <li>
            <span className="home-case__tag">Geo</span> 4,454 km/h vs plausible 900 km/h
          </li>
        </ul>
        <p className="home-case__suggest">
          <span className="home-case__label">Suggested</span> Confirmed fraud · 95% <em>(suggestion)</em>
        </p>
        <div className="home-case__actions">
          <span className="home-fakebtn home-fakebtn--fraud">Confirm fraud</span>
          <span className="home-fakebtn">False positive</span>
        </div>
      </div>
    </div>
  );
}

const CHAIN = [
  { name: "Carl", x: 34, y: 44 },
  { name: "Dave", x: 120, y: 112 },
  { name: "Erin", x: 206, y: 44 },
  { name: "Finn", x: 292, y: 112 },
];
const HOPS = ["5,000", "4,600", "4,300"];

function MuleCard() {
  return (
    <div className="home-card home-card--right hx-card">
      <div className="home-card__float">
        <div className="home-card__head">
          <span className="home-card__title">Mule chain detected</span>
          <span className="home-card__meta">REASON</span>
        </div>
        <svg className="home-chain" viewBox="0 0 326 150" focusable="false">
          <defs>
            <marker id="home-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
              <path d="M0 0 8 4 0 8Z" className="home-chain__arrow" />
            </marker>
          </defs>
          {HOPS.map((amount, i) => {
            const a = CHAIN[i]!;
            const b = CHAIN[i + 1]!;
            const dx = b.x - a.x;
            const dy = b.y - a.y;
            const len = Math.hypot(dx, dy);
            const ux = dx / len;
            const uy = dy / len;
            const r = 17;
            const mx = (a.x + b.x) / 2;
            const my = (a.y + b.y) / 2;
            return (
              <g key={amount}>
                <line
                  className="home-chain__edge"
                  x1={a.x + ux * r}
                  y1={a.y + uy * r}
                  x2={b.x - ux * (r + 3)}
                  y2={b.y - uy * (r + 3)}
                  markerEnd="url(#home-arrow)"
                />
                <text className="home-chain__amt" x={mx + (dy > 0 ? 14 : -14)} y={my + (dy > 0 ? -2 : 8)} textAnchor="middle">
                  {amount}
                </text>
              </g>
            );
          })}
          {CHAIN.map((n) => (
            <g key={n.name}>
              <circle className="home-chain__node" cx={n.x} cy={n.y} r={17} />
              <text className="home-chain__name" x={n.x} y={n.y + 3.5} textAnchor="middle">
                {n.name}
              </text>
            </g>
          ))}
        </svg>
        <div className="home-card__foot">
          <span className="hx-chip home-chip">MULE_CHAIN</span>
          <span className="home-card__note">3 hops · 25 and 23 min apart</span>
        </div>
      </div>
    </div>
  );
}

export function HeroCards({ hostRef }: { hostRef: RefObject<HTMLElement | null> }) {
  const fan = useRef<HTMLDivElement | null>(null);
  useParallax(fan, hostRef, 4);
  return (
    <div className="home-fan-wrap">
      <p className="sr-only">{CARDS_SUMMARY}</p>
      <div className="home-fan" ref={fan} aria-hidden="true" data-testid="home-cards">
        <FusionCard />
        <MuleCard />
        <CaseCard />
      </div>
    </div>
  );
}
