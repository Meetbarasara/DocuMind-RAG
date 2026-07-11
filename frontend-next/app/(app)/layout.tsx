"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

import AppSidebar from "@/components/AppSidebar";
import { useSession } from "@/components/SessionProvider";

// The authenticated app shell: a persistent sidebar + the routed page. Guards
// every /(app) route — an unauthenticated visitor is bounced to /login.
export default function AppLayout({ children }: { children: React.ReactNode }) {
  const { session, ready } = useSession();
  const router = useRouter();

  useEffect(() => {
    if (ready && !session) router.replace("/login");
  }, [ready, session, router]);

  // Wait for the client-side session read; don't flash the app or a redirect.
  if (!ready || !session) return null;

  return (
    <div className="flex min-h-screen">
      <AppSidebar />
      <main className="min-w-0 flex-1">
        <div className="mx-auto w-full max-w-4xl px-5 py-8 sm:px-8">{children}</div>
      </main>
    </div>
  );
}
