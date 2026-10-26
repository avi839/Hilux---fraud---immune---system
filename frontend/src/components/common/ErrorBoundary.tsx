import { Component, type ErrorInfo, type ReactNode } from "react";
import { Panel } from "./Panel";

interface BoundaryProps {
  /** Rendered instead of ``children`` after a render error; ``reset`` retries the children. */
  fallback: (error: Error, reset: () => void) => ReactNode;
  children: ReactNode;
}

interface BoundaryState {
  error: Error | null;
}

/** Catches render/lifecycle errors (including a lazy chunk that fails to load) below it, so
 *  one failing view cannot blank the whole dashboard. */
export class ErrorBoundary extends Component<BoundaryProps, BoundaryState> {
  state: BoundaryState = { error: null };

  static getDerivedStateFromError(error: unknown): BoundaryState {
    return { error: error instanceof Error ? error : new Error(String(error)) };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("HELIX view failed:", error, info.componentStack);
  }

  reset = () => this.setState({ error: null });

  render(): ReactNode {
    return this.state.error ? this.props.fallback(this.state.error, this.reset) : this.props.children;
  }
}

/** A dashboard panel wrapped in an ErrorBoundary; on failure the panel's grid slot shows the
 *  error and a Retry button instead of disappearing. */
export function PanelBoundary({ title, area, children }: { title: string; area: string; children: ReactNode }) {
  return (
    <ErrorBoundary
      fallback={(error, reset) => (
        <Panel title={title} area={area}>
          <div role="alert" className="msg msg--error" data-testid={`panel-error-${area}`}>
            <p className="msg__text">
              The {title.toLowerCase()} view failed: {error.message}. The rest of the dashboard keeps working.
            </p>
            <button type="button" className="btn" onClick={reset}>
              Retry
            </button>
          </div>
        </Panel>
      )}
    >
      {children}
    </ErrorBoundary>
  );
}

/** Inline fallback for a WebGL view (3D network, map): explain and point at the list view. */
export function WebGLBoundary({ what, onList, children }: { what: string; onList: () => void; children: ReactNode }) {
  return (
    <ErrorBoundary
      fallback={(error) => (
        <div role="alert" className="msg msg--error" data-testid="webgl-error">
          <p className="msg__text">
            The {what} could not be drawn ({error.message}).
          </p>
          <button type="button" className="btn" onClick={onList}>
            Show list view
          </button>
        </div>
      )}
    >
      {children}
    </ErrorBoundary>
  );
}
