-- DocuMind — Supabase SQL Migration
-- Run this in: Supabase Dashboard → SQL Editor → New Query → Run
--
-- Creates the user_documents metadata table + the conversations/messages tables
-- (persistent chat history), all with RLS policies. Idempotent — safe to re-run.

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
