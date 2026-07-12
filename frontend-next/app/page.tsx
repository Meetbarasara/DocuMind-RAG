import Landing from "@/components/Landing";

// The public showcase — a logged-out visitor can replay a real gap analysis
// without a login, then sign in to check their own policy. The authenticated
// app lives under /(app); this page is intentionally outside that guard.
export default function Home() {
  return <Landing />;
}
