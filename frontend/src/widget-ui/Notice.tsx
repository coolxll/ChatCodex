import React from "react";
import { CircleAlert, CircleCheck, Info, TriangleAlert } from "lucide-react";

export function Notice({
  tone = "info",
  children,
  role,
}: React.PropsWithChildren<{
  tone?: "info" | "success" | "warning" | "danger";
  role?: "alert" | "status";
}>) {
  const Icon = tone === "success"
    ? CircleCheck
    : tone === "warning"
      ? TriangleAlert
      : tone === "danger"
        ? CircleAlert
        : Info;
  return (
    <div className="widget-notice" data-tone={tone} role={role}>
      <Icon aria-hidden="true" className="widget-notice-icon" />
      <div>{children}</div>
    </div>
  );
}
