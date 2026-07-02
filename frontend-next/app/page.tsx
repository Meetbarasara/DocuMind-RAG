import CheckHero from "@/components/CheckHero";

export default function Home() {
  return (
    <main className="mx-auto w-full max-w-4xl px-5 py-10 sm:py-16">
      <header className="mb-8">
        <div className="mb-3 inline-flex items-center gap-2 rounded-full border border-white/12 bg-white/5 px-3 py-1 text-xs font-medium text-[var(--muted)]">
          <span className="st-conflict st-bar h-1.5 w-1.5 rounded-full" />
          RBI KYC · India
        </div>
        <h1 className="text-3xl font-semibold tracking-tight text-[var(--fg)] sm:text-4xl">
          Compliance gap analysis
        </h1>
        <p className="mt-3 max-w-2xl text-[15px] leading-relaxed text-[var(--muted)]">
          Pick a regulation and get a cited, requirement-by-requirement gap table
          — each clause judged{" "}
          <span className="st-covered st-fg font-medium">Covered</span>,{" "}
          <span className="st-partial st-fg font-medium">Partial</span>,{" "}
          <span className="st-gap st-fg font-medium">Gap</span>, or{" "}
          <span className="st-conflict st-fg font-medium">Conflict</span>, with
          your clause and the RBI clause shown side by side.
        </p>
      </header>

      <CheckHero />

      <footer className="mt-10 border-t border-white/10 pt-5 text-xs text-[var(--muted)]">
        Assisted review — not legal advice. Every finding is cited to a clause for
        a human to verify.
      </footer>
    </main>
  );
}
