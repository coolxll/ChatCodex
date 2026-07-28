import React from "react";

export function StatusRow({
  tone = "neutral",
  title,
  detail,
  actions,
}: {
  tone?: "neutral" | "running" | "success" | "warning" | "danger";
  title: React.ReactNode;
  detail?: React.ReactNode;
  actions?: React.ReactNode;
}) {
  return (
    <div className="status-row" data-tone={tone}>
      <span className="status-dot" aria-hidden="true" />
      <div className="status-copy">
        <div className="status-title">{title}</div>
        {detail && <div className="status-detail">{detail}</div>}
      </div>
      {actions && <div className="status-actions">{actions}</div>}
    </div>
  );
}
