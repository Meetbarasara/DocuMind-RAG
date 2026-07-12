// Small shared formatters used across the app screens.

/** A compact "Jul 5, 03:14 PM"-style timestamp; empty string on bad input. */
export function fmtDateTime(iso?: string): string {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return "";
  }
}
