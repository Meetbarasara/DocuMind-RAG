-- DocuMind — Supabase SQL Migration
-- Run this in: Supabase Dashboard → SQL Editor → New Query → Run
--
-- Creates the user_documents metadata table with RLS policies.

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
