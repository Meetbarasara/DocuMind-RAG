import { redirect } from "next/navigation";

// The app entry. Sends you into the gap-check screen; the /(app) guard bounces
// you to /login if you're not signed in.
export default function Home() {
  redirect("/check");
}
