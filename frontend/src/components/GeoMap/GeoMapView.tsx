// The WebGL part of the Map panel, in its own chunk (deck.gl is large). deck.gl draws the
// graticule and the per-account hop lines (LineLayer) and the events (ScatterplotLayer) over
// a plain background. No basemap, tiles or other network fetches: see mapStyle.ts.

import { useMemo } from "react";
import { DeckGL, LineLayer, ScatterplotLayer, WebMercatorViewport } from "deck.gl";
import { bounds, formatKm, formatSpeed, type GeoHop, type GeoModel, type GeoPoint } from "../../viz/geo";
import { graticule, MAP_BACKGROUND, type GridLine } from "./mapStyle";

export interface GeoMapViewProps {
  geo: GeoModel;
  width?: number;
  height?: number;
}

type Rgb = [number, number, number];
const PATH_COLOURS: Rgb[] = [
  [110, 168, 255],
  [255, 209, 102],
  [76, 214, 144],
  [199, 146, 234],
  [127, 219, 202],
];
const GRID = graticule(30);

function initialView(points: GeoPoint[], width: number, height: number) {
  const b = bounds(points);
  if (!b) return { longitude: 0, latitude: 20, zoom: 0.5 };
  const [[x0, y0], [x1, y1]] = b;
  if (x1 - x0 < 0.01 && y1 - y0 < 0.01) return { longitude: x0, latitude: y0, zoom: 5 };
  const vp = new WebMercatorViewport({ width, height }).fitBounds(b, { padding: 40 });
  return { longitude: vp.longitude, latitude: vp.latitude, zoom: Math.min(vp.zoom, 8) };
}

function tooltip(object: unknown): string | null {
  if (!object || typeof object !== "object") return null;
  if ("tx_id" in object) {
    const p = object as GeoPoint;
    return `${p.tx_id} · ${p.account} · ${p.ts} · ${p.lat.toFixed(3)}, ${p.lon.toFixed(3)}`;
  }
  if ("fromTx" in object) {
    const h = object as GeoHop;
    return `${h.account}: ${h.fromTx} → ${h.toTx} · ${formatKm(h.distanceKm)} · ${formatSpeed(h.speedKmh)}`;
  }
  return null;
}

export default function GeoMapView({ geo, width = 600, height = 300 }: GeoMapViewProps) {
  const view = useMemo(() => initialView(geo.points, width, height), [geo.points, width, height]);
  const layers = useMemo(() => {
    const accounts = [...new Set(geo.hops.map((h) => h.account))].sort();
    const colour = (account: string) => PATH_COLOURS[accounts.indexOf(account) % PATH_COLOURS.length]!;
    return [
      new LineLayer<GridLine>({
        id: "graticule",
        data: GRID,
        getSourcePosition: (d) => d.from,
        getTargetPosition: (d) => d.to,
        getColor: [40, 38, 66],
        widthUnits: "pixels",
        getWidth: 1,
      }),
      // One segment per consecutive pair of an account's located events, in ts order.
      new LineLayer<GeoHop>({
        id: "hops",
        data: geo.hops,
        getSourcePosition: (d) => d.from,
        getTargetPosition: (d) => d.to,
        getColor: (d) => colour(d.account),
        widthUnits: "pixels",
        getWidth: 2,
        pickable: true,
      }),
      new ScatterplotLayer<GeoPoint>({
        id: "events",
        data: geo.points,
        getPosition: (d) => [d.lon, d.lat],
        getFillColor: (d) => (d.inCase ? [255, 93, 93] : [154, 166, 191]),
        getLineColor: [7, 7, 11],
        stroked: true,
        radiusUnits: "pixels",
        getRadius: (d) => (d.inCase ? 6 : 4),
        lineWidthUnits: "pixels",
        getLineWidth: 1,
        pickable: true,
      }),
    ];
  }, [geo.hops, geo.points]);

  return (
    <div className="geo-canvas" style={{ position: "relative", width: "100%", height, background: MAP_BACKGROUND }}>
      <DeckGL initialViewState={view} controller layers={layers} getTooltip={({ object }: { object?: unknown }) => tooltip(object)} />
    </div>
  );
}
