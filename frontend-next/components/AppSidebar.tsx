"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

import { useSession } from "@/components/SessionProvider";

function Icon({ d }: { d: string }) {
  return (
    <svg viewBox="0 0 20 20" className="h-4 w-4 shrink-0" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
      <path d={d} />
    </svg>
  );
}

const NAV = [
  { href: "/dashboard", label: "Dashboard", d: "M4 4h5v5H4zM11 4h5v5h-5zM4 11h5v5H4zM11 11h5v5h-5z" },
  { href: "/check/new", label: "New check", d: "M4 5h9M4 10h9M4 15h5M14.5 13.5l1.5 1.5 3-3.5" },
  { href: "/checks", label: "Checks", d: "M10 5.5v4.5l3 2M10 3.5a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13z" },
  { href: "/policies", label: "Policies", d: "M6 3h6l3 3v11H6zM12 3v3h3" },
  { href: "/regulations", label: "Regulations", d: "M4 4h5v12H4zM11 4h5v12h-5zM4 16h12" },
  { href: "/ask", label: "Ask", d: "M4 5h12v8H9l-3 2v-2H4z" },
];

export default function AppSidebar() {
  const pathname = usePathname();
  const router = useRouter();
  const { session, signOut } = useSession();

  function handleSignOut() {
    signOut();
    router.replace("/login");
  }

  return (
    <aside className="flex w-60 shrink-0 flex-col border-r border-[var(--line)] bg-[var(--surface)] px-3 py-4">
      <div className="mb-6 flex items-center gap-2.5 px-2">
        <span className="accent-btn flex h-8 w-8 items-center justify-center rounded-lg text-base">⚖</span>
        <div className="leading-tight">
          <div className="text-sm font-semibold text-[var(--fg)]">KYC Compliance</div>
          <div className="text-[0.7rem] text-[var(--muted)]">Gap analysis</div>
        </div>
      </div>

      <nav className="flex flex-col gap-1">
        {NAV.map((item) => {
          const active = pathname === item.href || pathname.startsWith(item.href + "/");
          return (
            <Link
              key={item.href}
              href={item.href}
              className={`flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors ${
                active
                  ? "bg-[var(--active)] font-medium text-[var(--fg)]"
                  : "text-[var(--muted)] hover:bg-[var(--hover)] hover:text-[var(--fg)]"
              }`}
            >
              <Icon d={item.d} />
              {item.label}
            </Link>
          );
        })}
      </nav>

      <div className="mt-auto border-t border-[var(--line)] pt-3">
        {session?.email && (
          <div className="truncate px-2 text-xs text-[var(--muted)]" title={session.email}>
            {session.email}
          </div>
        )}
        <button
          onClick={handleSignOut}
          className="mt-1 flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-xs text-[var(--muted)] transition-colors hover:bg-[var(--hover)] hover:text-[var(--fg)]"
        >
          <Icon d="M13 15l3-5-3-5M16 10H7M9 4H5v12h4" />
          Sign out
        </button>
      </div>
    </aside>
  );
}
