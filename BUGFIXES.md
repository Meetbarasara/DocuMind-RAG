# Bug Fix Log

A running record of real bugs found and fixed in DocuMind: what broke, why it broke, how it was fixed, and how the fix was verified. Kept as a separate file (instead of only inline comments) so there's a complete, reviewable history of debugging work — e.g. for explaining "how do you handle bugs" with a concrete example instead of describing it abstractly.

**Format per entry:** Symptom → Root Cause → Fix → Why this approach → Verification.

---

## BUG-1: Chat endpoints completely broken — async pipeline methods called without `await`

**Status:** Fixed 2026-06-22

### Symptom
Both chat endpoints were non-functional:
- `POST /api/chat/query` → `500 Internal Server Error` on every request.
- `POST /api/chat/query/stream` → connection opened (200 headers sent) then died with no data.

### Root Cause
`RAGPipeline.query()` ([pipeline.py](src/pipeline/pipeline.py)) is defined as `async def`, and `RAGPipeline.query_stream()` is an `async def` generator. The route handlers in [chat.py](src/api/router/chat.py) were written as if these were synchronous:

1. **Blocking route** (`chat.py`, `query()`):
   ```python
   result = pipeline.query(...)        # missing await
   ...
   answer=result["answer"],            # result is a coroutine object, not a dict
   ```
   Calling an `async def` function without `await` doesn't run it — it just returns a coroutine object. Subscripting that object (`result["answer"]`) raised:
   ```
   TypeError: 'coroutine' object is not subscriptable
   ```

2. **Streaming route** (`chat.py`, `query_stream()`):
   ```python
   def event_generator():                       # sync generator
       yield from pipeline.query_stream(...)    # pipeline.query_stream(...) is an async generator
   ```
   `yield from` delegates to an iterator via `__iter__`/`__next__`. Async generators only implement `__aiter__`/`__anext__`, so the first time Starlette pulled a chunk from `event_generator()`, it raised:
   ```
   TypeError: 'async_generator' object is not iterable
   ```
   Because `StreamingResponse` had already sent a `200` and the headers before this point, the client saw a "successful" connection that then just died.

The same defect was also present in `RAGPipeline.ingest_and_query()` (a convenience method that ingests a file then immediately calls `self.query(...)` without awaiting it) and in the module's own `if __name__ == "__main__":` smoke test.

**Why it shipped:** CI only does a syntax check + import check (`python -c "from src.api.router import ..."`) — neither executes the endpoints, so a pure runtime/control-flow bug like this passes CI every time. No test ever actually called the route with a request.

### Fix
1. [chat.py](src/api/router/chat.py:68) — `await pipeline.query(...)` in the blocking route.
2. [chat.py](src/api/router/chat.py:110) — changed `event_generator` from a sync generator doing `yield from` to an `async def` generator doing `async for event in pipeline.query_stream(...): yield event`.
3. [pipeline.py](src/pipeline/pipeline.py:280) — made `ingest_and_query` itself `async def` and added `await` on `self.query(...)`.
4. [pipeline.py](src/pipeline/pipeline.py:307) — wrapped the `__main__` smoke test in an `async def _smoke_test()` run via `asyncio.run(...)`, with `await` on `pipeline.query(...)`.

### Why this approach
- The fix is "make the caller match the callee's actual contract," not "make `query`/`query_stream` synchronous again." `query()` awaits an LLM rewrite call and `generate()` (which itself awaits OpenAI), and `query_stream` streams tokens as they arrive — both are async by necessity for a server handling concurrent users. Reverting them to sync would block FastAPI's event loop on every network call and defeat the purpose of SSE streaming entirely. Fixing the three call sites to properly `await`/`async for` is the minimal, correct change.
- For the streaming route specifically, switching the *generator itself* to `async def` (rather than e.g. wrapping the async generator in a sync adapter) is the idiomatic Starlette pattern — `StreamingResponse` natively understands async generators and drives them on the event loop without needing a thread pool.

### Verification
Wrote [tests/test_chat_routes.py](tests/test_chat_routes.py) — an integration test hitting the *real* FastAPI app via `httpx.AsyncClient` + `ASGITransport`, with `get_pipeline`/`get_current_user` dependency-overridden with a fake async pipeline (so no real OpenAI/Pinecone/Supabase credentials are needed). [tests/conftest.py](tests/conftest.py) stubs `unstructured`'s heavy partition modules so the test suite stays fast and doesn't need network access just to import the app.

- **Before the fix:** ran the test against the original code — both tests failed with exactly the predicted errors (`'coroutine' object is not subscriptable` on the blocking route, `'async_generator' object is not iterable` on the streaming route). This confirmed the bug was real and reproducible, not just a theoretical read of the code.
- **After the fix:** re-ran the identical test, unchanged — both passed:
  ```
  tests/test_chat_routes.py::test_chat_query_returns_real_answer PASSED
  tests/test_chat_routes.py::test_chat_query_stream_returns_sse_tokens PASSED
  2 passed in 11.08s
  ```
- This test is now a permanent regression check — it stays in the repo, so this exact class of bug fails CI immediately if it ever recurs (previously CI had zero coverage that would have caught it).

---

## SEC-2: Path traversal via unsanitized `filename` (upload / download / delete)

**Status:** Fixed 2026-06-23

### Symptom
`filename` from the client was used raw to build local filesystem paths and Supabase Storage keys in four places:
- [documents.py](src/api/router/documents.py) `upload_document` — `tmp_path = tmp_dir / file.filename`
- [database.py](src/components/database.py) `download_file` — `tmp_path = tmp_dir / filename`
- [database.py](src/components/database.py) `_storage_path` — `f"{user_id}/{filename}"`
- [documents.py](src/api/router/documents.py) `delete_document` — raw `{filename}` path param

A filename like `"../escape.txt"` could write a local temp file outside the intended upload sandbox, or move a storage key outside the user's own prefix.

### Root Cause
`file.filename` on a multipart upload comes straight from the client's `Content-Disposition` header — FastAPI/Starlette does not parse or validate it as a path, it's an opaque string. Nothing downstream stripped directory components before joining it onto a real path with `Path.__truediv__` or an f-string.

**Important nuance found while reproducing this (documented here instead of silently "fixing" a non-bug, same spirit as the review's own SEC-1 retraction):** the `DELETE /api/documents/{filename}` route turned out to be **not actually reachable** with a traversal payload over HTTP. I verified this empirically with three probes before assuming it was exploitable:
- A literal `..` in the URL (`/api/documents/..`) gets collapsed by RFC 3986 dot-segment removal — both client-side (httpx normalizes it to `/api` before sending) and server-side — so it never matches the route at all (404).
- Percent-encoded variants (`%2e%2e`, `..%2F..%2Fescape.txt`) also returned 404 — FastAPI's default (non-`:path`) string converter for `{filename}` rejects "/" in the segment, and the encoded dot-segments don't survive routing either.
- Only a filename with **no slash and not exactly `.`/`..`** can ever reach the handler — and without a slash, there's no way to escape the `{user_id}/` prefix it's joined onto.

So unlike the upload vector (genuinely exploitable, confirmed below), the delete route's traversal risk is already neutralized by Starlette's own routing. I still added sanitization there, because relying on routing internals as the *only* safety net is fragile (e.g. it would silently break if the route ever switched to a `:path` converter) — but I'm not claiming I found a live exploit on that endpoint, only closing a latent risk.

### Fix
1. [utils.py](src/utils.py) — added `sanitize_filename()`: normalizes backslashes to forward slashes (so Windows-style `..\\..\\evil.txt` is caught too, not just POSIX `../`), then takes `PurePosixPath(...).name` to strip all directory components. Raises `ValueError` if nothing safe remains (empty, `"."`, or `".."`).
2. [documents.py](src/api/router/documents.py) `upload_document` — sanitizes `file.filename` once up front into `safe_filename`, used for every downstream step (storage upload, temp path, ingestion, metadata, response).
3. [documents.py](src/api/router/documents.py) `delete_document` — sanitizes the `filename` path param up front; invalid input → `400`.
4. [database.py](src/components/database.py) `_storage_path` — sanitizes internally so every caller (upload/download/delete) gets the protection even if a router forgets to.
5. [database.py](src/components/database.py) `download_file` — sanitizes before building the *local* tmp path (a separate join from `_storage_path`'s storage key).

### Why this approach
- Strip-to-basename (not reject-on-any-dots) is the correct behavior: a filename like `"../other_user/secret.pdf"` should silently become `"secret.pdf"` and upload normally — the user didn't do anything wrong by having slashes in a filename their OS or zip tool produced, they just don't get to choose *where* it lands. Only degenerate input that leaves *nothing* usable (`""`, `"."`, `".."`) is a hard `400`.
- Sanitizing in `_storage_path` *and* at each router entry point is deliberate defense-in-depth (the review explicitly called this out) — one missing call site shouldn't be the only thing standing between a client and arbitrary file write.

### Verification
Added [tests/test_path_traversal.py](tests/test_path_traversal.py):
- **Before the fix:** `test_upload_traversal_filename_stays_inside_sandbox` failed — a multipart file named `"../escape.txt"` was ingested from a path that resolved to the *parent* of the upload sandbox, proving the escape was real (contained the whole time inside pytest's own disposable `tmp_path`, never touching the real filesystem).
- The DELETE-route HTTP probe (`..`, `%2e%2e`, `..%2F..%2F`) all returned `404` pre-fix too — confirming that vector was never reachable, which is why the permanent test calls `documents.delete_document(...)` directly instead of over HTTP, to test the actual code path rather than a routing illusion.
- **After the fix:** all 18 tests pass, including:
  - normal filenames still work end-to-end (no regression)
  - `sanitize_filename` correctly reduces 6 traversal variants (POSIX, Windows-style, absolute, embedded, drive-letter) to safe basenames, and rejects 4 degenerate inputs
  - `SupabaseManager._storage_path` and `download_file`, called directly with malicious strings (bypassing HTTP entirely), are safe on their own — not just safe because a router happens to sanitize first
  ```
  18 passed in 11.57s
  ```
- `pyflakes` clean on all changed files.

---

## SEC-7: Rate limiter built but never enforced on any route

**Status:** Fixed 2026-06-23

### Symptom
`main.py` constructed a slowapi `Limiter` and registered its exception handler, but no endpoint ever had an `@limiter.limit(...)` decorator. A loop in `main.py` set `router_module.router.state = ... or {}` on each router — that line did nothing useful (a stray attribute slowapi never reads). Login, signup, upload, and chat could all be hammered with no throttling.

### Root Cause
The decorator-based slowapi pattern requires the `limiter` instance to be imported directly into each router module that wants to use it (`@limiter.limit("5/minute")` on the route, plus a `request: Request` parameter so slowapi can read the caller's IP). But `limiter` was defined *inside* `main.py`, and `main.py` is what imports the routers (`from src.api.router import auth, chat, documents, evaluate`) — so the routers could never import it back without a circular import. The limiter object existed; it just had no path to reach the routes.

### Fix
1. [src/api/limiter.py](src/api/limiter.py) — new module, holds just the shared `limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])` instance. No dependency on `main.py`, so anything can import it.
2. [main.py](src/api/main.py) — imports `limiter` from the new module instead of constructing it inline. Deleted the dead `router.state = ...` loop.
3. Added `request: Request` + `@limiter.limit(...)` to the endpoints the review specifically flagged as abuse-prone: [auth.py](src/api/router/auth.py) `signup`/`login` (5/minute — unauthenticated, the classic brute-force/spam target), [documents.py](src/api/router/documents.py) `upload_document` (10/minute — expensive: storage + embedding + Pinecone), [chat.py](src/api/router/chat.py) `query`/`query_stream` (20/minute — every call spends OpenAI quota).

### Why this approach
- A dedicated module for shared singletons that multiple routers need (and that `main.py` also needs) is the standard fix for this exact circular-import shape — same reason `dependencies.py` already exists separately from `main.py`.
- Decorators on the specific sensitive routes (not a blanket global middleware) matches what the review asked for and what slowapi's `Limiter.limit()` is designed for; routes like `/me` or listing documents don't need throttling and weren't touched.

### Verification
Added [tests/test_rate_limiting.py](tests/test_rate_limiting.py):
- **Before the fix:** fired 10 rapid login attempts (wrong password, so each would normally just be a `401`) at the real app. Got `[401, 401, 401, 401, 401, 401, 401, 401, 401, 401]` — never throttled, proving the limiter genuinely had no effect.
- **After the fix:** re-ran the identical test — a `429` now appears partway through the 10 attempts, while the very first request still succeeds normally (no false-positive throttling of legitimate single requests).

---

## SEC-6: Unbounded upload size

**Status:** Fixed 2026-06-23

### Symptom
[documents.py](src/api/router/documents.py) `upload_document` did `file_bytes = await file.read()` — the entire file, however large, read into memory in one shot before anything else happened (size check, virus scan, nothing). A handful of large uploads is a straightforward memory/CPU exhaustion DoS, especially once it's handed to `unstructured` for parsing.

### Root Cause
Nothing ever compared the upload's size against any limit — there *was* no limit, configured or otherwise.

### Fix
1. [config.py](src/components/config.py) — added `MAX_UPLOAD_SIZE_BYTES` (default 50MB).
2. [documents.py](src/api/router/documents.py) — added `_read_upload_within_limit()`, which reads the upload in 1MB chunks and raises `413` the moment the running total crosses the limit, instead of buffering the whole thing first. `upload_document` now calls this instead of the unbounded `file.read()`.

### Why this approach
Chunked reading with an early abort caps actual memory use at roughly the configured limit regardless of how large the attacker's file claims to be — it doesn't depend on a (spoofable) `Content-Length` header. A full streaming-to-disk redesign of the upload pipeline would also work but is a much bigger change than this problem calls for; capping the read is the minimal fix that removes the unbounded-memory risk.

### Verification
Added [tests/test_upload_size_limit.py](tests/test_upload_size_limit.py) (using a 1KB test-only limit so the test stays fast):
- **Before the fix:** a 2KB upload against a 1KB limit was happily accepted — `201 Created`, because nothing ever checked.
- **After the fix:** the identical 2KB upload now gets `413 Payload Too Large`, and a normal small upload still succeeds (`201`) — confirming the cap doesn't break legitimate use.

Full suite after both fixes: 21 passed. `pyflakes` and `ruff --select E,F,I` clean on every changed file.
