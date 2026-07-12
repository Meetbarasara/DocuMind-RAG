"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

import SignIn from "@/components/SignIn";
import { useSession } from "@/components/SessionProvider";

export default function LoginPage() {
  const { session, ready, signIn } = useSession();
  const router = useRouter();

  // Already signed in → go straight to the app.
  useEffect(() => {
    if (ready && session) router.replace("/dashboard");
  }, [ready, session, router]);

  return (
    <div className="mx-auto flex min-h-screen w-full max-w-md flex-col justify-center px-5 py-10">
      <div className="mb-6 text-center">
        <div className="accent-btn mx-auto mb-3 flex h-11 w-11 items-center justify-center rounded-xl text-lg">
          ⚖
        </div>
        <h1 className="text-2xl font-semibold text-[var(--fg)]">KYC Compliance</h1>
        <p className="mt-1 text-sm text-[var(--muted)]">
          Sign in to check your policy against a regulation.
        </p>
      </div>
      <div className="glass rounded-3xl p-5 sm:p-6">
        <SignIn
          onSignedIn={(s) => {
            signIn(s);
            router.replace("/dashboard");
          }}
        />
      </div>
    </div>
  );
}
