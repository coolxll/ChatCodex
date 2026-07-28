import React from "react";

export function Section({
  title,
  description,
  actions,
  children,
  className = "",
}: React.PropsWithChildren<{
  title?: React.ReactNode;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  className?: string;
}>) {
  return (
    <section className={`widget-section ${className}`.trim()}>
      {(title || actions) && (
        <div className="widget-section-heading">
          <div>
            {title && <h2 className="widget-section-title">{title}</h2>}
            {description && <p className="widget-section-description">{description}</p>}
          </div>
          {actions && <div className="widget-section-actions">{actions}</div>}
        </div>
      )}
      {children}
    </section>
  );
}
