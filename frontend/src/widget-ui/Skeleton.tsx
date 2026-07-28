import { Loader2 } from "lucide-react";

export function Skeleton({ label = "正在加载" }: { label?: string }) {
  return (
    <div className="widget-skeleton" role="status">
      <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin" />
      {label}
    </div>
  );
}
