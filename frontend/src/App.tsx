import { Splash } from "./components/Brand/Splash";
import { LiveDashboard } from "./components/Live/LiveDashboard";
import { AppHeader, SkipLink } from "./components/Shell/Header";
import { AlertAnnouncer, ConnectionBanner } from "./components/Shell/StatusBar";
import { ShellPanels, ShellProvider } from "./components/Shell/TabbedDashboard";

/** App shell. The app opens on Home; the header carries the view tabs (Req 38.1). The
 *  Dashboard tab (the live investigation grid) stays mounted while hidden because it holds
 *  the WebSocket-driven state; the other views mount only while selected. The starfield and
 *  spotlight are fixed decorative layers behind everything. The splash overlay plays once
 *  per session on top, without blocking the app underneath. */
export function App() {
  return (
    <ShellProvider>
      <div className="app">
        <div className="hx-starfield" aria-hidden="true" />
        <div className="hx-spotlight hx-spotlight--app" aria-hidden="true" />
        <SkipLink />
        <AppHeader />
        <ConnectionBanner />
        <ShellPanels>
          <LiveDashboard />
        </ShellPanels>
        <AlertAnnouncer />
      </div>
      <Splash />
    </ShellProvider>
  );
}
