"use client";

import AskPanel from "@/components/AskPanel";
import { useSession } from "@/components/SessionProvider";

export default function AskPage() {
  const { session } = useSession();
  return <AskPanel session={session} onSignedIn={() => {}} />;
}
