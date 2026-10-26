// The WebGL part of the Network panel, split into its own chunk (three.js and the force
// layout are large). Rotate (left drag), pan (right drag) and zoom (wheel) come from the
// library's trackball controls; clicking a node asks the panel to expand it.

import { useEffect, useMemo, useRef, useState } from "react";
import ForceGraph3D from "react-force-graph-3d";
import {
  LINK_COLOUR,
  NODE_COLOUR,
  linkLabel,
  motifColour,
  nodeLabel,
  type GraphLink,
  type GraphModel,
  type GraphNode,
} from "../../viz/graph";

export interface ForceGraphViewProps {
  graph: GraphModel;
  onNodeClick: (account: string) => void;
  height?: number;
}

type FgNode = GraphNode & { x?: number; y?: number; z?: number };
type FgLink = Omit<GraphLink, "source" | "target"> & { source: string | FgNode; target: string | FgNode };

function useWidth(ref: React.RefObject<HTMLDivElement>, fallback: number): number {
  const [w, setW] = useState(fallback);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    setW(el.clientWidth || fallback);
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(([entry]) => entry && setW(Math.max(200, Math.floor(entry.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref, fallback]);
  return w;
}

export default function ForceGraphView({ graph, onNodeClick, height = 360 }: ForceGraphViewProps) {
  const box = useRef<HTMLDivElement>(null);
  const width = useWidth(box, 600);
  // The library mutates node and link objects (positions, resolved endpoints), so it gets
  // its own copies, rebuilt only when the model changes.
  const data = useMemo(
    () => ({
      nodes: graph.nodes.map((n): FgNode => ({ ...n })),
      links: graph.links.map((l): FgLink => ({ ...l })),
    }),
    [graph],
  );
  const types = graph.motifTypes;

  return (
    <div ref={box} className="fg-canvas">
      <ForceGraph3D<FgNode, FgLink>
        graphData={data}
        width={width}
        height={height}
        backgroundColor="#07070b"
        showNavInfo={false}
        nodeId="id"
        nodeRelSize={4}
        nodeVal={(n) => (n.isCase ? 8 : 1.5)}
        nodeColor={(n) => (n.isCase ? NODE_COLOUR.case : n.expanded ? NODE_COLOUR.expanded : NODE_COLOUR.other)}
        nodeLabel={(n) => nodeLabel(n as GraphNode)}
        linkColor={(l) => (l.motifs.length ? motifColour(l.motifs[0]!, types) : LINK_COLOUR)}
        linkWidth={(l) => (l.motifs.length ? 2.5 : 0.6)}
        linkLabel={(l) => linkLabel({ ...(l as FgLink), source: endpoint(l.source), target: endpoint(l.target) })}
        linkDirectionalArrowLength={3}
        linkDirectionalArrowRelPos={1}
        linkDirectionalParticles={(l) => (l.motifs.length ? 2 : 0)}
        cooldownTicks={120}
        onNodeClick={(n) => onNodeClick(String(n.id))}
      />
    </div>
  );
}

function endpoint(v: unknown): string {
  return typeof v === "object" && v !== null && "id" in v ? String((v as { id: unknown }).id) : String(v);
}
