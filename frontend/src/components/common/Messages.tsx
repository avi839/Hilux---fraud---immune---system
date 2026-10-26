/**
 * Common message components for loading and error states
 */

interface MessageProps {
  children: React.ReactNode;
}

export function LoadingMessage({ children }: MessageProps) {
  return (
    <div
      className="loading-message"
      role="status"
      aria-live="polite"
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: "2rem",
        color: "var(--text-secondary, #94a3b8)",
      }}
    >
      {children}
    </div>
  );
}

export function ErrorMessage({ children }: MessageProps) {
  return (
    <div
      className="error-message"
      role="alert"
      aria-live="assertive"
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: "2rem",
        color: "var(--error-color, #ef4444)",
      }}
    >
      {children}
    </div>
  );
}
