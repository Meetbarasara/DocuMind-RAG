"use client";

import { useRouter } from "next/navigation";

import CheckHero from "@/components/CheckHero";
import { useSession } from "@/components/SessionProvider";

export default function CheckPage() {
  const { session, signOut } = useSession();
  const router = useRouter();
  return (
    <CheckHero
      embedded
      session={session}
      onSignedIn={() => {}}
      onSignOut={() => {
        signOut();
        router.replace("/login");
      }}
    />
  );
}
