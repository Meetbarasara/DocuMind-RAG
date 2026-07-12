import { redirect } from "next/navigation";

// The app entry. Sends you to the dashboard; the /(app) guard bounces you to
// /login if you're not signed in.
export default function Home() {
  redirect("/dashboard");
}
