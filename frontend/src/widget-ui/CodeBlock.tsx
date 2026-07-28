import React from "react";

export function CodeBlock({
  children,
  label,
  collapsed = false,
}: React.PropsWithChildren<{ label?: string; collapsed?: boolean }>) {
  if (collapsed && label) {
    return (
      <details className="code-details">
        <summary>{label}</summary>
        <pre className="widget-code"><code>{children}</code></pre>
      </details>
    );
  }
  return <pre className="widget-code" aria-label={label}><code>{children}</code></pre>;
}
