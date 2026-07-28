import React from "react";

export function SurfaceHeader({
  title,
  description,
  icon,
  actions,
}: {
  title: React.ReactNode;
  description?: React.ReactNode;
  icon?: React.ReactNode;
  actions?: React.ReactNode;
}) {
  return (
    <header className="surface-header">
      {icon && <span className="surface-header-icon">{icon}</span>}
      <div className="surface-header-copy">
        <h1 className="surface-title">{title}</h1>
        {description && <p className="surface-description">{description}</p>}
      </div>
      {actions && <div className="surface-header-actions">{actions}</div>}
    </header>
  );
}
