// The Workflow_Scene (Req 43.2-43.5, 43.10, 43.15): an R3F canvas drawn from primitives built
// in code. No models, textures, HDRs or fonts are loaded: labels are drawn on canvases with
// the page's own fonts, lights are local, and the "bloom" is additive halos on emissive
// colours. Particles are instanced. The frame loop is "demand": it renders only when the
// simulation clock ticks (paused or hidden = no frames), the camera moves or props change.

import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { Grid, Line, OrbitControls } from "@react-three/drei";
import { useEffect, useLayoutEffect, useMemo, useRef, type ElementRef, type MutableRefObject } from "react";
import * as THREE from "three";
import type { Tier } from "../../api/types";
import {
  ACT_INDEX,
  ATTACKER_POS,
  CANARY_POS,
  CARD_SIZE,
  CASE_FORM_POS,
  CORE_Y,
  DESK_POS,
  INBOUND_LIFT,
  INVESTIGATOR_POS,
  LEARN_INDEX,
  MERGE_LIFT,
  OUTBOUND_LIFT,
  PERCEIVE_INDEX,
  PROBE_LIFT,
  RECEIVERS,
  RING_POS,
  RING_R,
  RING_ROT_Y,
  SCREEN_POS,
  SENDERS,
  STAGES,
  arcPoints,
  chest,
  pulsePoint,
  type Figure,
  type Vec3,
} from "./layout";
import type { Palette } from "./palette";
import { MAX_CARDS, MAX_PULSES, PARTICLE_CAP, cardPose, isFlagged, particlePosition, pulsePosition, type SimCase } from "./simModel";
import type { Overlays } from "./storyScript";
import type { SimHandle } from "./useSimDriver";
import type { CameraPreset } from "./WorkflowView";

export interface WorkflowSceneProps {
  sim: SimHandle;
  overlays: Overlays;
  camera: { preset: CameraPreset; nonce: number };
  reducedMotion: boolean;
  palette: Palette;
  /** Stage ring of the current story step, kept lit (null in Live_Mode). */
  highlight: number | null;
}

type Controls = ElementRef<typeof OrbitControls>;

const FONT = '"Inter Variable", Inter, system-ui, -apple-system, "Segoe UI", sans-serif';
const ADD = THREE.AdditiveBlending;
const BODY = "#2a2a4a";
const SKIN = "#c7c2e8";

// --- canvas textures (no network) -----------------------------------------------------

interface TextTex {
  tex: THREE.CanvasTexture;
  aspect: number;
}
const textCache = new Map<string, TextTex>();
/** Keys of per-transaction textures (tags, live cards); only these are evicted. */
const dynamicKeys: string[] = [];
const DYNAMIC_MAX = 160;

function remember(key: string, value: TextTex, dynamic: boolean): TextTex {
  textCache.set(key, value);
  if (dynamic) {
    dynamicKeys.push(key);
    if (dynamicKeys.length > DYNAMIC_MAX) {
      const old = dynamicKeys.shift()!;
      textCache.get(old)?.tex.dispose();
      textCache.delete(old);
    }
  }
  return value;
}

function canvasTexture(c: HTMLCanvasElement): THREE.CanvasTexture {
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 4;
  return t;
}

/** Rounded-rectangle path (falls back to a plain rectangle where roundRect is missing). */
function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  ctx.beginPath();
  if (typeof ctx.roundRect === "function") ctx.roundRect(x, y, w, h, r);
  else ctx.rect(x, y, w, h);
}

/** A one-line label on a dark pill, cached by content. ``dynamic`` labels (per-transaction
 *  tags) are evicted and disposed oldest-first; static ones live as long as the page. */
function labelTexture(text: string, color: string, bg = "rgba(10,10,24,0.78)", border = "rgba(255,255,255,0.18)", dynamic = false): TextTex {
  const key = `${text}|${color}|${bg}|${border}`;
  const hit = textCache.get(key);
  if (hit) return hit;
  const px = 44;
  const c = document.createElement("canvas");
  const ctx = c.getContext("2d");
  let w = 256;
  if (ctx) {
    ctx.font = `600 ${px}px ${FONT}`;
    w = Math.ceil(ctx.measureText(text).width) + 48;
  }
  const h = 72;
  c.width = w;
  c.height = h;
  if (ctx) {
    ctx.font = `600 ${px}px ${FONT}`;
    ctx.fillStyle = bg;
    ctx.strokeStyle = border;
    ctx.lineWidth = 3;
    roundRect(ctx, 2, 2, w - 4, h - 4, 18);
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = color;
    ctx.textBaseline = "middle";
    ctx.textAlign = "center";
    ctx.fillText(text, w / 2, h / 2 + 2);
  }
  return remember(key, { tex: canvasTexture(c), aspect: w / h }, dynamic);
}

let haloTex: THREE.CanvasTexture | null = null;
function halo(): THREE.CanvasTexture {
  if (haloTex) return haloTex;
  const c = document.createElement("canvas");
  c.width = c.height = 128;
  const ctx = c.getContext("2d");
  if (ctx) {
    const g = ctx.createRadialGradient(64, 64, 0, 64, 64, 64);
    g.addColorStop(0, "rgba(255,255,255,1)");
    g.addColorStop(0.25, "rgba(255,255,255,0.45)");
    g.addColorStop(0.6, "rgba(255,255,255,0.08)");
    g.addColorStop(1, "rgba(255,255,255,0)");
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, 128, 128);
  }
  haloTex = canvasTexture(c);
  return haloTex;
}

const TIER_COLOUR = (p: Palette): Record<Tier, string> => ({ LOW: p.cyan, MEDIUM: "#fbbf24", HIGH: "#fb923c", CRITICAL: p.fraud });

function cardTexture(key: string, draw: (ctx: CanvasRenderingContext2D, w: number, h: number) => void): THREE.CanvasTexture {
  const hit = textCache.get(key);
  if (hit) return hit.tex;
  const c = document.createElement("canvas");
  c.width = 512;
  c.height = 320;
  const ctx = c.getContext("2d");
  if (ctx) draw(ctx, 512, 320);
  return remember(key, { tex: canvasTexture(c), aspect: 1.6 }, true).tex;
}

function cardFrame(ctx: CanvasRenderingContext2D, w: number, h: number, edge: string) {
  ctx.fillStyle = "#0d0d22";
  ctx.strokeStyle = edge;
  ctx.lineWidth = 10;
  roundRect(ctx, 6, 6, w - 12, h - 12, 28);
  ctx.fill();
  ctx.stroke();
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
}

/** Draws one centred line, shrinking the font until the text fits ``maxW`` pixels. */
function fitText(ctx: CanvasRenderingContext2D, text: string, x: number, y: number, maxW: number, weight: number, px: number) {
  let size = px;
  ctx.font = `${weight} ${size}px ${FONT}`;
  while (size > 14 && ctx.measureText(text).width > maxW) {
    size -= 2;
    ctx.font = `${weight} ${size}px ${FONT}`;
  }
  ctx.fillText(text, x, y);
}

function cardFront(c: SimCase, p: Palette): THREE.CanvasTexture {
  const edge = TIER_COLOUR(p)[c.tier];
  const risk = c.risk !== null ? `risk ${c.risk.toFixed(c.risk >= 0.95 ? 3 : 2)}` : "";
  return cardTexture(`front|${c.id}|${c.tier}|${risk}|${edge}`, (ctx, w, h) => {
    cardFrame(ctx, w, h, edge);
    ctx.fillStyle = "#9a9cab";
    ctx.font = `600 34px ${FONT}`;
    ctx.fillText("CASE", w / 2, 62);
    ctx.fillStyle = "#ffffff";
    fitText(ctx, c.id, w / 2, 142, w - 64, 700, 92);
    ctx.fillStyle = edge;
    ctx.font = `700 50px ${FONT}`;
    ctx.fillText(c.tier, w / 2, 222);
    ctx.fillStyle = "#c9c9d6";
    ctx.font = `500 34px ${FONT}`;
    ctx.fillText(risk, w / 2, 274);
  });
}

function cardBack(c: SimCase, p: Palette): THREE.CanvasTexture {
  const fraud = c.label === "CONFIRMED_FRAUD";
  const text = c.label ? (fraud ? "CONFIRMED FRAUD" : "FALSE POSITIVE") : "AWAITING LABEL";
  const edge = c.label ? (fraud ? p.fraud : p.legit) : "#9a9cab";
  return cardTexture(`back|${c.id}|${text}|${edge}`, (ctx, w, h) => {
    cardFrame(ctx, w, h, edge);
    ctx.fillStyle = edge;
    fitText(ctx, text, w / 2, 112, w - 64, 800, 58);
    ctx.fillStyle = "#c9c9d6";
    fitText(ctx, `${c.id} · labelled by`, w / 2, 186, w - 64, 500, 38);
    fitText(ctx, "the investigator", w / 2, 230, w - 64, 500, 38);
    ctx.fillStyle = edge;
    fitText(ctx, fraud ? "✗ fraud" : c.label ? "✓ legit" : "", w / 2, 276, w - 64, 700, 36);
  });
}

// --- small building blocks -------------------------------------------------------------

function Label({ text, color = "#ededf2", position, height = 0.4, bg }: { text: string; color?: string; position: Vec3; height?: number; bg?: string }) {
  const t = useMemo(() => labelTexture(text, color, bg), [text, color, bg]);
  return (
    <sprite position={position as [number, number, number]} scale={[height * t.aspect, height, 1]} renderOrder={10}>
      <spriteMaterial map={t.tex} transparent depthWrite={false} depthTest={false} toneMapped={false} />
    </sprite>
  );
}

function Halo({ position, size, color, opacity }: { position: Vec3; size: number; color: string; opacity: number }) {
  return (
    <sprite position={position as [number, number, number]} scale={[size, size, 1]}>
      <spriteMaterial map={halo()} color={color} transparent opacity={opacity} blending={ADD} depthWrite={false} toneMapped={false} />
    </sprite>
  );
}

function Person({ figure, color = BODY, accent, rotationY = 0, hooded = false }: { figure: Figure; color?: string; accent: string; rotationY?: number; hooded?: boolean }) {
  return (
    <group position={figure.pos as [number, number, number]} rotation={[0, rotationY, 0]}>
      <mesh position={[0, 0.72, 0]}>
        <capsuleGeometry args={[0.26, 0.62, 3, 8]} />
        <meshStandardMaterial color={color} emissive={accent} emissiveIntensity={0.12} flatShading roughness={0.7} />
      </mesh>
      <mesh position={[0, 1.42, 0]}>
        <icosahedronGeometry args={[0.21, 1]} />
        <meshStandardMaterial color={hooded ? "#120a14" : SKIN} emissive={accent} emissiveIntensity={0.08} flatShading />
      </mesh>
      {hooded ? (
        <>
          <mesh position={[0, 1.52, 0]}>
            <coneGeometry args={[0.34, 0.62, 6]} />
            <meshStandardMaterial color="#1a0a12" emissive="#f43f5e" emissiveIntensity={0.18} flatShading />
          </mesh>
          <mesh position={[0.07, 1.42, 0.18]}>
            <sphereGeometry args={[0.03, 6, 6]} />
            <meshBasicMaterial color="#f43f5e" toneMapped={false} />
          </mesh>
          <mesh position={[-0.07, 1.42, 0.18]}>
            <sphereGeometry args={[0.03, 6, 6]} />
            <meshBasicMaterial color="#f43f5e" toneMapped={false} />
          </mesh>
        </>
      ) : null}
      <mesh position={[0, 0.02, 0]} rotation={[-Math.PI / 2, 0, 0]}>
        <ringGeometry args={[0.36, 0.46, 24]} />
        <meshBasicMaterial color={accent} transparent opacity={0.55} blending={ADD} depthWrite={false} toneMapped={false} />
      </mesh>
      <Label text={figure.name} position={[0, 2.1, 0]} height={0.5} />
    </group>
  );
}

function Building({ figure, accent }: { figure: Figure; accent: string }) {
  const bank = figure.kind === "bank";
  return (
    <group position={figure.pos as [number, number, number]}>
      <mesh position={[0, 0.6, 0]}>
        <boxGeometry args={[1.5, 1.2, 1.1]} />
        <meshStandardMaterial color="#1d1d3a" emissive={accent} emissiveIntensity={0.1} flatShading />
      </mesh>
      {bank ? (
        <>
          <mesh position={[0, 1.45, 0]} rotation={[0, Math.PI / 4, 0]}>
            <coneGeometry args={[1.15, 0.5, 4]} />
            <meshStandardMaterial color="#26264a" emissive={accent} emissiveIntensity={0.2} flatShading />
          </mesh>
          {[-0.5, 0, 0.5].map((x) => (
            <mesh key={x} position={[x, 0.6, 0.6]}>
              <cylinderGeometry args={[0.07, 0.07, 1.1, 6]} />
              <meshStandardMaterial color="#3a3a66" emissive={accent} emissiveIntensity={0.25} />
            </mesh>
          ))}
        </>
      ) : (
        <>
          <mesh position={[0, 1.15, 0.62]} rotation={[0.5, 0, 0]}>
            <boxGeometry args={[1.6, 0.06, 0.5]} />
            <meshStandardMaterial color={accent} emissive={accent} emissiveIntensity={0.5} />
          </mesh>
          <mesh position={[0, 0.55, 0.56]}>
            <planeGeometry args={[0.9, 0.5]} />
            <meshBasicMaterial color={accent} transparent opacity={0.35} toneMapped={false} />
          </mesh>
        </>
      )}
      <Label text={figure.name} position={[0, 2.2, 0]} height={0.5} />
    </group>
  );
}

function FigureView({ figure, accent }: { figure: Figure; accent: string }) {
  return figure.kind === "person" ? <Person figure={figure} accent={accent} rotationY={figure.pos[0] < 0 ? Math.PI / 2 : -Math.PI / 2} /> : <Building figure={figure} accent={accent} />;
}

/** A sprite whose label text is computed each frame (texture swapped only on change). */
function DynamicLabel({ get, position, height = 0.34 }: { get: () => { text: string; color: string } | null; position: Vec3; height?: number }) {
  const ref = useRef<THREE.Sprite>(null);
  const last = useRef("");
  useFrame(() => {
    const s = ref.current;
    if (!s) return;
    const v = get();
    s.visible = !!v;
    if (!v) return;
    const key = `${v.text}|${v.color}`;
    if (key === last.current) return;
    last.current = key;
    const t = labelTexture(v.text, v.color, undefined, undefined, true);
    const m = s.material as THREE.SpriteMaterial;
    m.map = t.tex;
    m.needsUpdate = true;
    s.scale.set(height * t.aspect, height, 1);
  });
  return (
    <sprite ref={ref} position={position as [number, number, number]} renderOrder={11}>
      <spriteMaterial transparent depthWrite={false} depthTest={false} toneMapped={false} />
    </sprite>
  );
}

// --- the HELIX core and its six Stage_Rings -------------------------------------------

function Core({ sim, palette }: { sim: SimHandle; palette: Palette }) {
  const wire = useRef<THREE.Mesh>(null);
  const helix = useRef<THREE.InstancedMesh>(null);
  const spin = useRef<THREE.Group>(null);
  const N = 26;
  useLayoutEffect(() => {
    const m = helix.current;
    if (!m) return;
    const o = new THREE.Object3D();
    const ca = new THREE.Color(palette.cyan);
    const cb = new THREE.Color(palette.accent);
    for (let strand = 0; strand < 2; strand++) {
      for (let k = 0; k < N; k++) {
        const a = k * 0.5 + strand * Math.PI;
        o.position.set(Math.cos(a) * 1.15, 0.35 + k * 0.11, Math.sin(a) * 1.15);
        o.scale.setScalar(0.075);
        o.updateMatrix();
        m.setMatrixAt(strand * N + k, o.matrix);
        m.setColorAt(strand * N + k, strand ? cb : ca);
      }
    }
    m.instanceMatrix.needsUpdate = true;
    if (m.instanceColor) m.instanceColor.needsUpdate = true;
  }, [palette]);
  useFrame(() => {
    const t = sim.time();
    if (wire.current) wire.current.rotation.set(t * 0.1, t * 0.25, 0);
    if (spin.current) spin.current.rotation.y = t * 0.35;
  });
  return (
    <group>
      <mesh position={[0, 0.05, 0]}>
        <cylinderGeometry args={[RING_R + 0.9, RING_R + 1.1, 0.1, 6]} />
        <meshStandardMaterial color="#0f0f26" emissive={palette.accent2} emissiveIntensity={0.12} flatShading />
      </mesh>
      <mesh position={[0, CORE_Y, 0]}>
        <icosahedronGeometry args={[0.72, 1]} />
        <meshStandardMaterial color={palette.accent2} emissive={palette.accent} emissiveIntensity={1.5} flatShading roughness={0.35} />
      </mesh>
      <mesh ref={wire} position={[0, CORE_Y, 0]}>
        <icosahedronGeometry args={[1.0, 1]} />
        <meshBasicMaterial color={palette.cyan} wireframe transparent opacity={0.4} blending={ADD} depthWrite={false} toneMapped={false} />
      </mesh>
      <Halo position={[0, CORE_Y, 0]} size={6} color={palette.accent} opacity={0.6} />
      <group ref={spin}>
        <instancedMesh ref={helix} args={[undefined, undefined, N * 2]} frustumCulled={false}>
          <icosahedronGeometry args={[1, 1]} />
          <meshBasicMaterial toneMapped={false} />
        </instancedMesh>
      </group>
      <mesh position={[0, CORE_Y, 0]} rotation={[Math.PI / 2, 0, 0]}>
        <torusGeometry args={[RING_R, 0.025, 6, 120]} />
        <meshBasicMaterial color={palette.accent2} transparent opacity={0.55} blending={ADD} depthWrite={false} toneMapped={false} />
      </mesh>
      {/* On the floor in front of the base: above the core it would sit on the back ring labels. */}
      <Label text="HELIX core" position={[0, 0.45, RING_R + 1.6]} height={0.56} color={palette.cyan} />
    </group>
  );
}

function Rings({ sim, palette, highlight, overlays }: { sim: SimHandle; palette: Palette; highlight: number | null; overlays: Overlays }) {
  const ringMats = useRef<Array<THREE.MeshBasicMaterial | null>>([]);
  const haloMats = useRef<Array<THREE.SpriteMaterial | null>>([]);
  const ringGroups = useRef<Array<THREE.Group | null>>([]);
  const shieldMats = useRef<Array<THREE.MeshBasicMaterial | null>>([]);
  const shields = useRef<Array<THREE.Mesh | null>>([]);
  const base = useMemo(() => STAGES.map((_, i) => new THREE.Color(i === LEARN_INDEX ? palette.accent : i % 2 ? palette.accent2 : palette.cyan)), [palette]);
  const white = useMemo(() => new THREE.Color("#ffffff"), []);
  const hl = useRef(highlight);
  hl.current = highlight;
  const rb = useRef(overlays.redBlue);
  rb.current = overlays.redBlue;

  useFrame(() => {
    const s = sim.get();
    for (let i = 0; i < STAGES.length; i++) {
      const g = Math.max(s.glow[i] ?? 0, hl.current === i ? 0.55 : 0);
      const m = ringMats.current[i];
      if (m) {
        m.color.copy(base[i]!).lerp(white, g * 0.55);
        m.opacity = 0.5 + 0.5 * g;
      }
      const h = haloMats.current[i];
      if (h) h.opacity = 0.1 + 0.65 * g;
      const grp = ringGroups.current[i];
      if (grp) grp.scale.setScalar(1 + 0.18 * g + (i === PERCEIVE_INDEX ? 0.25 * s.weightFlash : 0));
    }
    for (let k = 0; k < shields.current.length; k++) {
      const sh = shields.current[k];
      if (sh) sh.visible = rb.current && k < s.antibodies;
      const sm = shieldMats.current[k];
      if (sm) sm.opacity = 0.5 + 0.45 * s.shieldFlash;
    }
  });

  return (
    <group>
      {STAGES.map((name, i) => {
        const p = RING_POS[i]!;
        return (
          <group key={name} position={p as unknown as [number, number, number]}>
            <group rotation={[0, RING_ROT_Y[i]!, 0]}>
              <group ref={(el) => void (ringGroups.current[i] = el)}>
                <mesh>
                  <torusGeometry args={[0.66, 0.065, 10, 48]} />
                  <meshBasicMaterial
                    ref={(el) => void (ringMats.current[i] = el)}
                    color={base[i]}
                    transparent
                    opacity={0.6}
                    blending={ADD}
                    depthWrite={false}
                    toneMapped={false}
                  />
                </mesh>
              </group>
              {i === PERCEIVE_INDEX
                ? Array.from({ length: 6 }, (_, k) => {
                    const a = (k / 6) * Math.PI * 2;
                    return (
                      <mesh
                        key={k}
                        ref={(el) => void (shields.current[k] = el)}
                        position={[Math.cos(a) * 1.05, Math.sin(a) * 1.05, 0]}
                        rotation={[Math.PI / 2, 0, 0]}
                        visible={false}
                      >
                        <cylinderGeometry args={[0.36, 0.36, 0.04, 6]} />
                        <meshBasicMaterial
                          ref={(el) => void (shieldMats.current[k] = el)}
                          color={palette.cyan}
                          transparent
                          opacity={0.35}
                          blending={ADD}
                          depthWrite={false}
                          toneMapped={false}
                        />
                      </mesh>
                    );
                  })
                : null}
            </group>
            <sprite scale={[2.4, 2.4, 1]}>
              <spriteMaterial
                ref={(el) => void (haloMats.current[i] = el)}
                map={halo()}
                color={base[i]}
                transparent
                opacity={0.15}
                blending={ADD}
                depthWrite={false}
                toneMapped={false}
              />
            </sprite>
            <Label text={`${i + 1} · ${name}`} position={[0, 1.1, 0]} height={0.5} color={i === LEARN_INDEX ? "#ddd6fe" : "#ededf2"} />
          </group>
        );
      })}
      <DynamicLabel
        position={[RING_POS[PERCEIVE_INDEX]![0], CORE_Y + 1.85, RING_POS[PERCEIVE_INDEX]![2]]}
        height={0.42}
        get={() => {
          const s = sim.get();
          return s.weightFlash > 0.02
            ? { text: `${s.weightNote} · ×${s.weightScale.toFixed(2)}`, color: "#ddd6fe" }
            : { text: `sensor weights ×${s.weightScale.toFixed(2)}`, color: "#9a9cab" };
        }}
      />
    </group>
  );
}

// --- static paths ------------------------------------------------------------------------

const pts = (a: Vec3[]) => a.map((p) => [p[0], p[1], p[2]] as [number, number, number]);

function Paths({ palette, overlays }: { palette: Palette; overlays: Overlays }) {
  const inbound = useMemo(() => SENDERS.map((f) => pts(arcPoints(chest(f), RING_POS[0]!, INBOUND_LIFT))), []);
  const outbound = useMemo(() => RECEIVERS.map((f) => pts(arcPoints(RING_POS[ACT_INDEX]!, chest(f), OUTBOUND_LIFT))), []);
  const merge = useMemo(() => pts(arcPoints(RING_POS[ACT_INDEX]!, CASE_FORM_POS, MERGE_LIFT)), []);
  const pulse = useMemo(() => pts(Array.from({ length: 41 }, (_, i) => pulsePoint(i / 40))), []);
  const attack = useMemo(() => pts(arcPoints(chest(ATTACKER_POS), RING_POS[0]!, INBOUND_LIFT)), []);
  const probe = useMemo(() => pts(arcPoints(chest(ATTACKER_POS), CANARY_POS, PROBE_LIFT)), []);
  return (
    <group>
      {inbound.map((p, i) => (
        <Line key={`in${i}`} points={p} color={palette.accent2} lineWidth={1} transparent opacity={0.28} />
      ))}
      {outbound.map((p, i) => (
        <Line key={`out${i}`} points={p} color={palette.legit} lineWidth={1} transparent opacity={0.22} />
      ))}
      <Line points={merge} color={palette.fraud} lineWidth={1.2} transparent opacity={0.3} />
      <Line points={pulse} color={palette.accent} lineWidth={1} transparent opacity={0.3} dashed dashSize={0.25} gapSize={0.2} />
      {overlays.redBlue ? <Line points={attack} color="#f97316" lineWidth={1} transparent opacity={0.35} dashed dashSize={0.3} gapSize={0.2} /> : null}
      {overlays.soc ? <Line points={probe} color="#f97316" lineWidth={1} transparent opacity={0.25} dashed dashSize={0.3} gapSize={0.3} /> : null}
    </group>
  );
}

// --- particles, pulses, tags --------------------------------------------------------

const HALO_CAP = PARTICLE_CAP + MAX_PULSES;
const TAG_SLOTS = 6;

function Particles({ sim, palette }: { sim: SimHandle; palette: Palette }) {
  const round = useRef<THREE.InstancedMesh>(null);
  const spiky = useRef<THREE.InstancedMesh>(null);
  const diamond = useRef<THREE.InstancedMesh>(null);
  const pulses = useRef<THREE.InstancedMesh>(null);
  const halos = useRef<THREE.InstancedMesh>(null);
  const tags = useRef<Array<THREE.Sprite | null>>([]);
  const tagKeys = useRef<string[]>([]);
  const tmp = useMemo(
    () => ({ m: new THREE.Matrix4(), q: new THREE.Quaternion(), e: new THREE.Euler(), p: new THREE.Vector3(), s: new THREE.Vector3() }),
    [],
  );
  const col = useMemo(
    () => ({
      neutral: new THREE.Color(palette.cyan),
      legit: new THREE.Color(palette.legit),
      fraud: new THREE.Color(palette.fraud),
      attack: new THREE.Color("#f97316"),
      pulse: new THREE.Color(palette.accent),
      white: new THREE.Color("#ffffff"),
    }),
    [palette],
  );

  useLayoutEffect(() => {
    for (const r of [round, halos]) {
      const m = r.current;
      if (!m) continue;
      for (let i = 0; i < m.count; i++) m.setColorAt(i, col.white);
      m.count = 0;
    }
    for (const r of [spiky, diamond, pulses]) if (r.current) r.current.count = 0;
  }, [col]);

  useFrame(({ camera }) => {
    const s = sim.get();
    const R = round.current;
    const S = spiky.current;
    const D = diamond.current;
    const P = pulses.current;
    const H = halos.current;
    if (!R || !S || !D || !P || !H) return;
    let nr = 0;
    let ns = 0;
    let nd = 0;
    let nh = 0;
    let nt = 0;
    const { m, q, e, p, s: sc } = tmp;
    for (const pt of s.particles) {
      const [x, y, z] = particlePosition(pt);
      p.set(x, y, z);
      let c: THREE.Color;
      let haloSize = 0.75;
      if (pt.kind !== "tx") {
        e.set(0, pt.age * 2.4, 0);
        q.setFromEuler(e);
        sc.set(0.15, 0.32, 0.15);
        m.compose(p, q, sc);
        D.setMatrixAt(nd++, m);
        c = col.attack;
      } else if (isFlagged(pt)) {
        e.set(pt.age * 2.2, pt.age * 1.5, 0);
        q.setFromEuler(e);
        sc.setScalar(pt.tier === "CRITICAL" ? 0.27 : 0.22);
        m.compose(p, q, sc);
        S.setMatrixAt(ns++, m);
        c = col.fraud;
        haloSize = 1.25;
      } else {
        q.identity();
        sc.setScalar(pt.group ? 0.17 : 0.12);
        m.compose(p, q, sc);
        c = pt.phase === "outbound" ? col.legit : col.neutral;
        R.setMatrixAt(nr, m);
        R.setColorAt(nr++, c);
      }
      if (nh < HALO_CAP) {
        sc.setScalar(pt.group ? haloSize * 1.4 : haloSize);
        m.compose(p, camera.quaternion, sc);
        H.setMatrixAt(nh, m);
        H.setColorAt(nh++, c);
      }
      if (pt.tag && nt < TAG_SLOTS) {
        const spr = tags.current[nt];
        if (spr) {
          const color = pt.kind !== "tx" ? "#fdba74" : isFlagged(pt) ? "#fecdd3" : "#cffafe";
          const key = `${pt.tag}|${color}`;
          if (tagKeys.current[nt] !== key) {
            tagKeys.current[nt] = key;
            const t = labelTexture(pt.tag, color, undefined, undefined, true);
            const mat = spr.material as THREE.SpriteMaterial;
            mat.map = t.tex;
            mat.needsUpdate = true;
            spr.scale.set(0.4 * t.aspect, 0.4, 1);
          }
          spr.position.set(x, y + 0.56, z);
          spr.visible = true;
        }
        nt++;
      }
    }
    let np = 0;
    for (const pl of s.pulses) {
      const [x, y, z] = pulsePosition(pl);
      p.set(x, y, z);
      q.identity();
      sc.setScalar(0.3);
      m.compose(p, q, sc);
      P.setMatrixAt(np++, m);
      if (nh < HALO_CAP) {
        sc.setScalar(2.8);
        m.compose(p, camera.quaternion, sc);
        H.setMatrixAt(nh, m);
        H.setColorAt(nh++, col.pulse);
      }
    }
    for (let k = nt; k < TAG_SLOTS; k++) {
      const spr = tags.current[k];
      if (spr) spr.visible = false;
    }
    R.count = nr;
    S.count = ns;
    D.count = nd;
    P.count = np;
    H.count = nh;
    for (const mesh of [R, S, D, P, H]) mesh.instanceMatrix.needsUpdate = true;
    if (R.instanceColor) R.instanceColor.needsUpdate = true;
    if (H.instanceColor) H.instanceColor.needsUpdate = true;
  });

  return (
    <group>
      <instancedMesh ref={round} args={[undefined, undefined, PARTICLE_CAP]} frustumCulled={false}>
        <icosahedronGeometry args={[1, 2]} />
        <meshBasicMaterial toneMapped={false} />
      </instancedMesh>
      <instancedMesh ref={spiky} args={[undefined, undefined, PARTICLE_CAP]} frustumCulled={false}>
        <octahedronGeometry args={[1, 0]} />
        <meshBasicMaterial color={palette.fraud} toneMapped={false} />
      </instancedMesh>
      <instancedMesh ref={diamond} args={[undefined, undefined, PARTICLE_CAP]} frustumCulled={false}>
        <octahedronGeometry args={[1, 0]} />
        <meshBasicMaterial color="#f97316" wireframe={false} toneMapped={false} />
      </instancedMesh>
      <instancedMesh ref={pulses} args={[undefined, undefined, MAX_PULSES]} frustumCulled={false}>
        <icosahedronGeometry args={[1, 2]} />
        <meshBasicMaterial color={palette.accent} toneMapped={false} />
      </instancedMesh>
      <instancedMesh ref={halos} args={[undefined, undefined, HALO_CAP]} frustumCulled={false} renderOrder={5}>
        <planeGeometry args={[1, 1]} />
        <meshBasicMaterial map={halo()} transparent blending={ADD} depthWrite={false} opacity={0.85} toneMapped={false} />
      </instancedMesh>
      {Array.from({ length: TAG_SLOTS }, (_, k) => (
        <sprite key={k} ref={(el) => void (tags.current[k] = el)} visible={false} renderOrder={12}>
          <spriteMaterial transparent depthWrite={false} depthTest={false} toneMapped={false} />
        </sprite>
      ))}
    </group>
  );
}

// --- Case cards, the investigator, receivers, overlays ---------------------------------

function Cards({ sim, palette }: { sim: SimHandle; palette: Palette }) {
  const groups = useRef<Array<THREE.Group | null>>([]);
  const fronts = useRef<Array<THREE.MeshBasicMaterial | null>>([]);
  const backs = useRef<Array<THREE.MeshBasicMaterial | null>>([]);
  const holds = useRef<Array<THREE.Sprite | null>>([]);
  const holdTex = useMemo(() => labelTexture("HOLD", "#1a1a1a", "#f59e0b", "#fde68a"), []);

  useFrame(() => {
    const s = sim.get();
    for (let i = 0; i < MAX_CARDS; i++) {
      const g = groups.current[i];
      if (!g) continue;
      const c = s.cases[i];
      if (!c) {
        g.visible = false;
        continue;
      }
      const pose = cardPose(c);
      g.visible = true;
      g.position.set(pose.pos[0], pose.pos[1], pose.pos[2]);
      g.rotation.y = pose.flip;
      g.scale.setScalar(pose.scale);
      const f = fronts.current[i];
      const b = backs.current[i];
      if (f) {
        const t = cardFront(c, palette);
        if (f.map !== t) {
          f.map = t;
          f.needsUpdate = true;
        }
        f.opacity = pose.opacity;
      }
      if (b) {
        const t = cardBack(c, palette);
        if (b.map !== t) {
          b.map = t;
          b.needsUpdate = true;
        }
        b.opacity = pose.opacity;
      }
      const h = holds.current[i];
      if (h) h.visible = c.hold;
    }
  });

  return (
    <group>
      {Array.from({ length: MAX_CARDS }, (_, i) => (
        <group key={i} ref={(el) => void (groups.current[i] = el)} visible={false}>
          {/* Cards are the focus of the story: drawn after (over) the ring and figure labels. */}
          <mesh renderOrder={13}>
            <planeGeometry args={[1.3 * CARD_SIZE, 0.8125 * CARD_SIZE]} />
            <meshBasicMaterial ref={(el) => void (fronts.current[i] = el)} transparent depthTest={false} side={THREE.FrontSide} toneMapped={false} />
          </mesh>
          <mesh rotation={[0, Math.PI, 0]} renderOrder={13}>
            <planeGeometry args={[1.3 * CARD_SIZE, 0.8125 * CARD_SIZE]} />
            <meshBasicMaterial ref={(el) => void (backs.current[i] = el)} transparent depthTest={false} side={THREE.FrontSide} toneMapped={false} />
          </mesh>
          <sprite
            ref={(el) => void (holds.current[i] = el)}
            position={[0.5 * CARD_SIZE, 0.52 * CARD_SIZE, 0.05]}
            scale={[0.38 * holdTex.aspect, 0.38, 1]}
            visible={false}
            renderOrder={14}
          >
            <spriteMaterial map={holdTex.tex} transparent depthWrite={false} depthTest={false} toneMapped={false} />
          </sprite>
        </group>
      ))}
    </group>
  );
}

function Investigator({ palette }: { palette: Palette }) {
  const screenTex = useMemo(() => labelTexture("CASE QUEUE", palette.cyan, "rgba(8,30,40,0.95)", palette.cyan), [palette]);
  return (
    <group>
      <group position={DESK_POS as unknown as [number, number, number]}>
        <mesh position={[0, 1.0, 0]}>
          <boxGeometry args={[4.6, 0.1, 1.5]} />
          <meshStandardMaterial color="#1c1c38" emissive={palette.accent2} emissiveIntensity={0.15} flatShading />
        </mesh>
        {[-2.1, 2.1].map((x) => (
          <mesh key={x} position={[x, 0.5, 0]}>
            <boxGeometry args={[0.1, 1.0, 1.3]} />
            <meshStandardMaterial color="#15152c" />
          </mesh>
        ))}
      </group>
      <mesh position={[SCREEN_POS[0], SCREEN_POS[1] - 0.45, SCREEN_POS[2]]}>
        <boxGeometry args={[0.12, 0.3, 0.12]} />
        <meshStandardMaterial color="#15152c" />
      </mesh>
      <mesh position={SCREEN_POS as unknown as [number, number, number]}>
        <boxGeometry args={[1.7, 0.95, 0.06]} />
        <meshStandardMaterial color="#0a0a18" />
      </mesh>
      <mesh position={[SCREEN_POS[0], SCREEN_POS[1], SCREEN_POS[2] + 0.035]}>
        <planeGeometry args={[1.56, 0.82]} />
        <meshBasicMaterial map={screenTex.tex} toneMapped={false} />
      </mesh>
      <Halo position={[SCREEN_POS[0], SCREEN_POS[1], SCREEN_POS[2] + 0.1]} size={2.4} color={palette.cyan} opacity={0.25} />
      <Person figure={{ name: "Investigator", kind: "person", pos: INVESTIGATOR_POS }} accent={palette.cyan} rotationY={Math.PI} color="#24304a" />
    </group>
  );
}

function ReceiverChecks({ sim, palette }: { sim: SimHandle; palette: Palette }) {
  const refs = useRef<Array<THREE.SpriteMaterial | null>>([]);
  const tex = useMemo(() => labelTexture("✓ legit", "#062b1f", palette.legit, "#a7f3d0"), [palette]);
  useFrame(() => {
    const s = sim.get();
    for (let i = 0; i < RECEIVERS.length; i++) {
      const m = refs.current[i];
      if (m) m.opacity = s.receiverFlash[i] ?? 0;
    }
  });
  return (
    <group>
      {RECEIVERS.map((f, i) => (
        <sprite key={i} position={[f.pos[0], 2.85, f.pos[2]]} scale={[0.42 * tex.aspect, 0.42, 1]} renderOrder={12}>
          <spriteMaterial ref={(el) => void (refs.current[i] = el)} map={tex.tex} transparent opacity={0} depthWrite={false} depthTest={false} toneMapped={false} />
        </sprite>
      ))}
    </group>
  );
}

function Canary({ sim, palette }: { sim: SimHandle; palette: Palette }) {
  const mat = useRef<THREE.MeshStandardMaterial>(null);
  const haloMat = useRef<THREE.SpriteMaterial>(null);
  const node = useRef<THREE.Mesh>(null);
  useFrame(() => {
    const s = sim.get();
    const f = s.canaryFlash;
    if (mat.current) mat.current.emissiveIntensity = 0.4 + 2.5 * f;
    if (haloMat.current) haloMat.current.opacity = 0.12 + 0.8 * f;
    if (node.current) node.current.rotation.y = sim.time() * 0.8;
  });
  return (
    <group>
      <mesh position={[CANARY_POS[0], 0.4, CANARY_POS[2]]}>
        <cylinderGeometry args={[0.25, 0.4, 0.8, 6]} />
        <meshStandardMaterial color="#1c1c38" flatShading />
      </mesh>
      <mesh ref={node} position={CANARY_POS as unknown as [number, number, number]}>
        <octahedronGeometry args={[0.32, 0]} />
        <meshStandardMaterial ref={mat} color="#fde047" emissive="#facc15" emissiveIntensity={0.4} flatShading />
      </mesh>
      <sprite position={CANARY_POS as unknown as [number, number, number]} scale={[2.2, 2.2, 1]}>
        <spriteMaterial ref={haloMat} map={halo()} color="#facc15" transparent opacity={0.12} blending={ADD} depthWrite={false} toneMapped={false} />
      </sprite>
      <Label text="Canary decoy" position={[CANARY_POS[0], CANARY_POS[1] + 0.85, CANARY_POS[2]]} height={0.46} color="#fde68a" />
      <Label text="SOC" position={[CANARY_POS[0], CANARY_POS[1] + 1.4, CANARY_POS[2]]} height={0.36} color={palette.cyan} />
    </group>
  );
}

// --- camera -------------------------------------------------------------------------------

const PRESETS: Record<CameraPreset, { pos: THREE.Vector3; target: THREE.Vector3 }> = {
  // Higher than eye level so the depth of the scene (attacker at the back, investigator at
  // the front) fills the canvas height instead of leaving empty floor and sky.
  overview: { pos: new THREE.Vector3(0, 11.6, 17), target: new THREE.Vector3(0, 1, 2.3) },
  core: { pos: new THREE.Vector3(0.4, 6.4, 10.4), target: new THREE.Vector3(0, 1.8, 0.2) },
  investigator: { pos: new THREE.Vector3(9.8, 6.8, 14.6), target: new THREE.Vector3(5.6, 2.4, 4.4) },
};

function CameraRig({
  preset,
  nonce,
  sim,
  reducedMotion,
  controls,
}: {
  preset: CameraPreset;
  nonce: number;
  sim: SimHandle;
  reducedMotion: boolean;
  controls: MutableRefObject<Controls | null>;
}) {
  const { camera, invalidate } = useThree();
  const st = useRef({ moving: true, user: false });
  const goal = useMemo(() => new THREE.Vector3(), []);

  useEffect(() => {
    st.current = { moving: true, user: false };
    invalidate();
  }, [preset, nonce, invalidate]);

  useEffect(() => {
    const c = controls.current;
    if (!c) return;
    const onStart = () => {
      st.current = { moving: false, user: true };
    };
    c.addEventListener("start", onStart);
    return () => c.removeEventListener("start", onStart);
  }, [controls]);

  useFrame((_, delta) => {
    const c = controls.current;
    if (!c) return;
    const p = PRESETS[preset];
    const drift = reducedMotion ? 0 : Math.sin(sim.time() * 0.07) * 0.16;
    goal.copy(p.pos).sub(p.target).applyAxisAngle(THREE.Object3D.DEFAULT_UP, drift).add(p.target);
    if (st.current.moving) {
      const k = reducedMotion ? 1 : 1 - Math.exp(-Math.min(delta, 0.1) * 3.2);
      camera.position.lerp(goal, k);
      c.target.lerp(p.target, k);
      if (camera.position.distanceTo(goal) < 0.02 && c.target.distanceTo(p.target) < 0.02) st.current.moving = false;
      else invalidate();
      c.update();
    } else if (!st.current.user && !reducedMotion) {
      camera.position.copy(goal);
      c.target.copy(p.target);
      c.update();
    }
  });
  return null;
}

/** Redraw on each simulation tick (and only then, apart from camera moves). */
function SimInvalidator({ sim, deps }: { sim: SimHandle; deps: unknown[] }) {
  const invalidate = useThree((s) => s.invalidate);
  useEffect(() => sim.subscribe(() => invalidate()), [sim, invalidate]);
  useEffect(() => invalidate(), [invalidate, ...deps]);
  return null;
}

function World({ sim, overlays, camera, reducedMotion, palette, highlight }: WorkflowSceneProps) {
  const controls = useRef<Controls | null>(null);
  return (
    <>
      <color attach="background" args={[palette.bg]} />
      <fog attach="fog" args={[palette.bg, 26, 55]} />
      <ambientLight intensity={0.35} />
      <hemisphereLight args={["#6366f1", "#05050a", 0.6]} />
      <directionalLight position={[6, 12, 10]} intensity={0.9} color="#e0e7ff" />
      <pointLight position={[0, CORE_Y, 0]} intensity={40} distance={16} decay={2} color={palette.accent} />
      <pointLight position={[SCREEN_POS[0], 2.2, SCREEN_POS[2] + 0.8]} intensity={6} distance={6} decay={2} color={palette.cyan} />

      <Grid
        position={[0, 0, 0]}
        args={[80, 80]}
        cellSize={1}
        cellThickness={0.5}
        cellColor="#1b1b3a"
        sectionSize={5}
        sectionThickness={1}
        sectionColor={palette.accent2}
        fadeDistance={48}
        fadeStrength={1.6}
        infiniteGrid
      />

      {SENDERS.map((f, i) => (
        <FigureView key={`s${i}`} figure={f} accent={palette.accent2} />
      ))}
      {RECEIVERS.map((f, i) => (
        <FigureView key={`r${i}`} figure={f} accent={palette.legit} />
      ))}
      <Label text="Customers" position={[-8.8, 3.6, 0.6]} height={0.62} color={palette.cyan} />
      <Label text="Receivers" position={[8.8, 4.0, -1.8]} height={0.62} color={palette.legit} />

      <Core sim={sim} palette={palette} />
      <Rings sim={sim} palette={palette} highlight={highlight} overlays={overlays} />
      <Paths palette={palette} overlays={overlays} />
      <Investigator palette={palette} />
      <ReceiverChecks sim={sim} palette={palette} />
      <Particles sim={sim} palette={palette} />
      <Cards sim={sim} palette={palette} />

      {overlays.redBlue || overlays.soc ? (
        <Person figure={{ name: "Attacker", kind: "person", pos: ATTACKER_POS }} accent={palette.fraud} color="#2a0f1c" hooded rotationY={0.6} />
      ) : null}
      {overlays.soc ? <Canary sim={sim} palette={palette} /> : null}

      <OrbitControls
        ref={controls}
        makeDefault
        enablePan={false}
        enableDamping
        dampingFactor={0.08}
        minDistance={5}
        maxDistance={32}
        minPolarAngle={0.25}
        maxPolarAngle={1.38}
        minAzimuthAngle={-1.25}
        maxAzimuthAngle={1.25}
      />
      <CameraRig preset={camera.preset} nonce={camera.nonce} sim={sim} reducedMotion={reducedMotion} controls={controls} />
      <SimInvalidator sim={sim} deps={[overlays.redBlue, overlays.soc, highlight, camera.preset, camera.nonce]} />
    </>
  );
}

export default function WorkflowScene(props: WorkflowSceneProps) {
  return (
    <Canvas
      className="wf-canvas"
      dpr={[1, 2]}
      frameloop="demand"
      camera={{ position: [0, 11.6, 17], fov: 45, near: 0.1, far: 160 }}
      gl={{ antialias: true, alpha: false, powerPreference: "high-performance" }}
    >
      <World {...props} />
    </Canvas>
  );
}
