import { STATUS_CLASS } from "@/lib/types";

export default function StatusPill({
  status,
  className = "",
}: {
  status: string;
  className?: string;
}) {
  const cls = STATUS_CLASS[status] || "st-review";
  return (
    <span
      className={`${cls} st-chip inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold tracking-wide ${className}`}
    >
      <span className="st-bar h-1.5 w-1.5 rounded-full" />
      {status}
    </span>
  );
}
