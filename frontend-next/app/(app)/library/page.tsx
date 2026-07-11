"use client";

import LibraryScreen from "@/components/LibraryScreen";
import { useSession } from "@/components/SessionProvider";

export default function LibraryPage() {
  const { session } = useSession();
  return <LibraryScreen session={session} onSignedIn={() => {}} />;
}
