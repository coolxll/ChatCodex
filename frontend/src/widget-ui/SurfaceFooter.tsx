import React from "react";

export function SurfaceFooter({
  children,
  note,
}: React.PropsWithChildren<{ note?: React.ReactNode }>) {
  return (
    <footer className="surface-footer">
      {note && <div className="surface-footer-note">{note}</div>}
      <div className="surface-footer-actions">{children}</div>
    </footer>
  );
}
