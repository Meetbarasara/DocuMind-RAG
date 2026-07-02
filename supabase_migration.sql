-- DocuMind — Supabase SQL Migration
-- Run this in: Supabase Dashboard → SQL Editor → New Query → Run
--
-- Creates the user_documents metadata table, the conversations/messages tables
-- (persistent chat history), and the compliance tables (regulations +
-- compliance_checks), all with RLS policies. Idempotent — safe to re-run.

CREATE TABLE IF NOT EXISTS public.user_documents (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL,
    filename    TEXT NOT NULL,
    file_type   TEXT,
    size_bytes  BIGINT,
    uploaded_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (user_id, filename)
);

-- Enable Row Level Security so users only see their own rows
ALTER TABLE public.user_documents ENABLE ROW LEVEL SECURITY;

-- Policy: authenticated users can only read/write their own rows
DROP POLICY IF EXISTS user_documents_self ON public.user_documents;
CREATE POLICY user_documents_self
    ON public.user_documents
    FOR ALL
    USING (auth.uid() = user_id)
    WITH CHECK (auth.uid() = user_id);

-- Service role can access all rows (needed for backend inserts)
DROP POLICY IF EXISTS user_documents_service_role ON public.user_documents;
CREATE POLICY user_documents_service_role
    ON public.user_documents
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);


-- ════════════════════════════════════════════════════════════════════════════
--  Persistent chat history (Claude-style conversations + messages)
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS public.conversations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL,
    title       TEXT NOT NULL DEFAULT 'New chat',
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS conversations_user_updated
    ON public.conversations (user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS public.messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES public.conversations(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL,                  -- denormalized so RLS needs no join
    role            TEXT NOT NULL,                  -- 'human' | 'ai'
    content         TEXT NOT NULL,
    sources         JSONB,                          -- ai turn's source list (nullable)
    run_id          TEXT,                           -- LangSmith trace id (nullable)
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS messages_conversation_created
    ON public.messages (conversation_id, created_at);

-- RLS — same shape as user_documents: a user sees only their own rows; the
-- service-role (backend) client bypasses it for inserts/reads on their behalf.
ALTER TABLE public.conversations ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS conversations_self ON public.conversations;
CREATE POLICY conversations_self ON public.conversations
    FOR ALL USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);
DROP POLICY IF EXISTS conversations_service_role ON public.conversations;
CREATE POLICY conversations_service_role ON public.conversations
    FOR ALL TO service_role USING (true) WITH CHECK (true);

ALTER TABLE public.messages ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS messages_self ON public.messages;
CREATE POLICY messages_self ON public.messages
    FOR ALL USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);
DROP POLICY IF EXISTS messages_service_role ON public.messages;
CREATE POLICY messages_service_role ON public.messages
    FOR ALL TO service_role USING (true) WITH CHECK (true);


-- ════════════════════════════════════════════════════════════════════════════
--  Compliance gap-analysis (KYC): shared regulations + per-user check results
-- ════════════════════════════════════════════════════════════════════════════

-- Regulations are SHARED reference data (e.g. an RBI circular), ingested once by
-- an admin/seed step. `requirements` caches the extracted, atomic requirement
-- list ([{id,text,page,section}, ...]) so a check never re-extracts (expensive).
-- Any authenticated user may read them; only the service-role (backend/seed) writes.
CREATE TABLE IF NOT EXISTS public.regulations (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         TEXT NOT NULL UNIQUE,
    regulator    TEXT,                                  -- e.g. 'RBI'
    circular_id  TEXT,
    namespace    TEXT NOT NULL DEFAULT 'regulations',   -- shared Pinecone namespace
    requirements JSONB,
    ingested_at  TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE public.regulations ENABLE ROW LEVEL SECURITY;
-- Shared read: any authenticated user may list/read regulations.
DROP POLICY IF EXISTS regulations_read ON public.regulations;
CREATE POLICY regulations_read ON public.regulations
    FOR SELECT TO authenticated USING (true);
-- Only the backend (service-role) writes them (seed/admin path).
DROP POLICY IF EXISTS regulations_service_role ON public.regulations;
CREATE POLICY regulations_service_role ON public.regulations
    FOR ALL TO service_role USING (true) WITH CHECK (true);


-- A persisted gap-check result, per user (like conversations). `rows` is the
-- full cited gap table, `summary` the status counts. Persisted so re-opening a
-- check is instant and never re-burns judge budget.
CREATE TABLE IF NOT EXISTS public.compliance_checks (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID NOT NULL,
    policy_label  TEXT,
    regulation_id UUID REFERENCES public.regulations(id) ON DELETE SET NULL,
    summary       JSONB,                                 -- {total, Covered, Partial, Gap, Conflict, "Needs review"}
    rows          JSONB,                                 -- [{requirement, status, policy_quote, ...}, ...]
    created_at    TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS compliance_checks_user_created
    ON public.compliance_checks (user_id, created_at DESC);

ALTER TABLE public.compliance_checks ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS compliance_checks_self ON public.compliance_checks;
CREATE POLICY compliance_checks_self ON public.compliance_checks
    FOR ALL USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);
DROP POLICY IF EXISTS compliance_checks_service_role ON public.compliance_checks;
CREATE POLICY compliance_checks_service_role ON public.compliance_checks
    FOR ALL TO service_role USING (true) WITH CHECK (true);
