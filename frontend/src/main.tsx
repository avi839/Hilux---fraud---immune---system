import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { startLive } from "./api/live";
import { App } from "./App";
import { dashboard } from "./store/store";
// Self-hosted Inter (body) and Sora (display headings and the wordmark), both variable-weight;
// Vite bundles the woff2 files, so there is no CDN request.
import "@fontsource-variable/inter";
import "@fontsource-variable/sora";
import "./styles.css";

// The live connection lives outside React so StrictMode's double effects cannot open
// duplicate sockets.
startLive({ store: dashboard });

const root = document.getElementById("root");
if (!root) throw new Error("#root element missing from index.html");
createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
