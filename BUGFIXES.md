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

### Explain it simply (interview answer)
Imagine you ask a friend to grab you coffee, but instead of waiting for them to come back, you immediately try to drink from the empty cup in your hand. That's what this code did.

In Python, `async` means "this takes time, like a network call." When you call an async function, you're supposed to use `await` to actually wait for the result. My code called `pipeline.query(...)` — which talks to OpenAI — but forgot the `await`. Without it, Python doesn't run the function and hand back an answer; it hands back an empty placeholder called a "coroutine" (basically an IOU). My code then tried to read `result["answer"]` off that IOU, which crashed every single time.

The streaming version had the same root cause in a different shape: it tried to read a live, ongoing stream of tokens using a tool that only knows how to loop over something already finished — like trying to listen to a live radio broadcast with a tool that only plays downloaded files. Also crashed.

**The fix:** add the missing `await` (and the matching `async for` for the streaming version) so the code actually waits for and reads the real answer instead of an empty placeholder.

**How I proved it:** wrote a test that hits the chat endpoint and checked the response. Before the fix, it failed with the exact error I expected (`coroutine object is not subscriptable`). After adding `await`, I ran the *same test* again — it passed, with a real answer coming back.

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

### Explain it simply (interview answer)
My app let users upload files, and whatever name the user gave the file, I trusted completely and used to build a path on the server's disk — basically `save_folder + "/" + filename`.

**The attack:** instead of uploading `report.pdf`, an attacker uploads a file but names it `../../etc/passwd` (or on Windows, `..\..\Windows\System32\evil.dll`). `..` means "go up one folder" to the operating system. So my code's path became `save_folder + "/" + "../../etc/passwd"` — which doesn't save *inside* the safe folder, it walks back *out* of it and writes somewhere else entirely. This is called **path traversal**: using `../` to escape the folder you're supposed to be locked into.

**How I found it was real, not theoretical:** I wrote a test that uploads a fake file literally named `"../escape.txt"`, then checked exactly where the code was about to save it. The test proved the file landed *outside* the intended folder.

**The fix:** before using any filename a user gives me, I strip away everything except the actual file name — the last piece after the last slash. `"../../etc/passwd"` becomes just `"passwd"`. `"report.pdf"` stays `"report.pdf"`. I used Python's `pathlib` (`PurePosixPath(name).name`) to grab just that last piece, throwing away every `../` trick before it. If nothing safe is left (someone sends just `".."` or an empty string), I reject the request instead of guessing.

**How I proved the fix worked:** reran the *exact same test* — now the file landed safely inside the intended folder. Same test, red before, green after.

**Honest bonus point for an interview:** I assumed the *delete* endpoint had the identical bug, but when I actually tried to trigger it, the request never even reached my code — FastAPI's own routing already blocks `/` and `..` in that URL segment. I fixed it there too (defense-in-depth), but I was careful not to claim I'd found a live exploit where there wasn't one — I said so explicitly instead of overstating it.

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

### Explain it simply (interview answer)
I had a "rate limiter" object built and registered in the app — like hiring a bouncer and having them stand near the building. But nobody ever told the bouncer *which door* to actually guard. So people could hammer the login page hundreds of times (password guessing) or spam signups, and the app let every single request through.

**Why it happened:** the limiter was created inside `main.py`, but `main.py` is also the file that imports all the route files (login, upload, chat). So those route files couldn't import the limiter back out — that's a circular import, Python won't allow it. The bouncer existed; he just had no way to get to the door.

**The fix:** I moved the limiter into its own small file, so any route file can import it cleanly. Then I added one line above each sensitive endpoint (login, signup, upload, chat) saying "limit this to N requests per minute."

**How I proved it:** wrote a test that fires 10 fake login attempts back-to-back. Before the fix: all 10 came back as normal "wrong password" responses — never blocked. After the fix: somewhere in those 10, the server starts replying "429 Too Many Requests" instead.

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

### Explain it simply (interview answer)
When someone uploaded a file, my code said "read the whole thing into memory" with no limit at all — like a mailroom that accepts a package of *any* size, sight unseen. Someone could upload a massive file and eat up all the server's memory, crashing it for everyone else — that's a denial-of-service.

**The fix:** I made the upload reader read the file in small pieces (1MB at a time) and keep a running total. The instant that total goes over a limit (50MB by default), it stops and rejects the upload — so the server never holds more than ~50MB in memory no matter how big the attacker's file claims to be.

**How I proved it:** test uploads a file bigger than the limit. Before the fix: accepted fine (`201`) — proving there was no cap. After the fix: rejected with `413 Payload Too Large`. A normal small file still uploads fine either way.

---

## SEC-4: Raw exception text leaked to clients (and SEC-5: user-enumeration on signup/login)

**Status:** Fixed 2026-06-24

### Symptom
Six places put the real exception text straight into a client-facing response:
- [auth.py](src/api/router/auth.py) `signup` — `detail=str(e)` (whatever Supabase's error says, e.g. revealing "user already registered")
- [auth.py](src/api/router/auth.py) `login` — same, on sign-in failure
- [documents.py](src/api/router/documents.py) `upload_document` — `detail=f"Storage upload failed: {e}"` and `detail=f"Ingestion failed: {e}"`
- [documents.py](src/api/router/documents.py) `delete_document` — `detail=f"Failed to delete vectors from Pinecone: {e}"`
- [chat.py](src/api/router/chat.py) `query` — `detail=f"RAG query failed: {e}"`
- [pipeline.py](src/pipeline/pipeline.py) `query_stream` — `{'type': 'error', 'message': str(e)}` sent as an SSE event

Combined with signup/login specifically (SEC-5): a client could distinguish "this email is already registered" from "wrong password" from "something else broke" just by reading the literal backend error text — useful for an attacker enumerating valid accounts.

### Root Cause
Each `except Exception as e:` block treated `str(e)` as if it were a safe, user-facing string. It isn't — it can contain Postgres constraint names, provider error text, internal paths, or anything else the underlying library decided to put in its exception message.

### Fix
1. [src/api/error_utils.py](src/api/error_utils.py) — new `log_and_get_ref(logger, public_message, exc)`: generates a short reference id, logs the *real* exception against that id (with traceback, server-side only), and returns the id so the route can build a generic client message.
2. Every site listed above now does `ref = log_and_get_ref(logger, "<what failed>", e)` then returns/yields a fixed generic message + `(ref: {ref})` instead of `{e}`. `login`/`signup` specifically collapse to "Invalid email or password" / "Sign-up failed" regardless of the real reason — closing SEC-5 as a side effect of the same fix.
3. [pipeline.py](src/pipeline/pipeline.py) `query_stream` does the equivalent inline (it's in `src/pipeline`, which `src/api` depends on, not the other way around, so it can't import the API-layer helper — three lines of the same id+log pattern duplicated locally rather than restructuring the dependency direction for one call site).
4. Added `logger = get_logger(__name__)` to `auth.py`, `documents.py`, `chat.py` — none of them had one before, so there was nowhere to log *to* even if someone had remembered to generalize the message.

### Why this approach
- A reference id (not just "an error occurred") is the practical middle ground: the client/support flow can say "tell us ref abc123" and you grep the logs for it, without ever putting the real error on the wire.
- Generalizing `login`/`signup` messages is the standard fix for credential-related endpoints specifically — "invalid email or password" must be the same string whether the email doesn't exist, the password is wrong, or the account is locked, otherwise the response itself is the leak.
- Didn't touch `documents.py`'s "Failed to delete from storage" message (the one near the top of `delete_document`) — that one's driven by a boolean return value (`storage_deleted`), not a caught exception, so there's no `e` to leak there; out of scope for this specific bug.

### Verification
Added [tests/test_error_leakage.py](tests/test_error_leakage.py) — 6 tests, one per leak site, each injecting an exception with a distinctive "sensitive" string and asserting it never appears in the response body (or the SSE event, for the streaming case).

- **First version of the test was wrong, not the code:** the sensitive string originally contained `"` characters, which JSON escapes to `\"` in the response body — so a plain substring check passed *even pre-fix*, for the wrong reason (all 6 "passed" against the unmodified code). Caught this by noticing a reproduction test passing on the very first try, which should always be treated as suspicious. Rewrote the sensitive string with no quote/backslash characters and reran.
- **Before the fix (corrected test):** all 6 failed, each showing the literal sensitive string sitting in the response body/SSE event — confirmed real leaks, not 6 false positives this time.
- **After the fix:** all 6 pass — generic messages only, real detail confirmed (via the captured log output) to still be logged server-side with its reference id.
- Full suite: 27 passed. `pyflakes` clean on every changed file.

### Explain it simply (interview answer)
When something failed on the backend — a database error, a failed API call — my code's habit was "just tell the user exactly what Python's error message says." That sounds helpful, but it's actually leaking internal information: database constraint names, provider error text, sometimes even internal hostnames, straight into a response a random client can read.

It also created a sneakier problem on login and signup specifically: if "wrong password" and "that email doesn't exist" and "your account is locked" all produce *different* error text, an attacker can use that to map out which emails have accounts — even without ever logging in successfully. That's called user enumeration.

**The fix:** instead of showing the user the real error, I generate a short random reference code (like `ref: a1b2c3d4`), write the *real* error to the server logs next to that code, and show the user only a generic message plus that code. If they contact support, support can search the logs for that exact code and see exactly what happened — but a random visitor on the internet just sees "Invalid email or password" and nothing else, every time, no matter what actually went wrong internally.

**How I proved it:** I wrote a test that makes the backend fail with a fake "sensitive" error message, then checks whether that message shows up in the response. Funny enough, my first version of this test was broken — it said "no leak" even on the *original, unfixed* code, because the test string had quote marks in it that get encoded differently in JSON, so my check wasn't really checking anything. A reproduction test passing on the first try is a red flag, not a relief — I fixed the test itself, reran it, and *then* it correctly failed against the broken code and passed after the real fix.

---

## SEC-3: Service-role key bypasses Row-Level Security — audited, not code-changed

**Status:** Audited 2026-06-24 — no code change (by design, see below)

### Finding
The backend uses Supabase's **service-role key** (`service_client`) for every storage and `user_documents` table operation. That key bypasses Postgres Row-Level Security entirely — so the `auth.uid() = user_id` RLS policies defined in [supabase_migration.sql](supabase_migration.sql) are currently **inert**: they never run, because the backend never authenticates to Supabase as the actual user, only as the service role.

I audited every method in [database.py](src/components/database.py) to check what's actually standing in RLS's place:
- Every `user_documents` table call (`record_upload`, `get_user_documents`, `delete_document_record`) has an explicit `.eq("user_id", user_id)` / `{"user_id": user_id}` filter.
- Every storage call (`upload_file`, `download_file`, `delete_file`) goes through `_storage_path(user_id, filename)`, which — since the SEC-2 fix — now also sanitizes `filename` so it can't escape the `{user_id}/` prefix.

So today, isolation between users holds **entirely because the application code remembers to do it correctly on every call** — there is no second layer (RLS) that would catch a mistake. That matches what the original review found ("it mostly does [pass user_id correctly]"), and SEC-2 closed the one concrete gap (filename traversal) that could have turned a missing check into an actual cross-user leak.

### Why this wasn't turned into a code change
Two ways to close the remaining gap exist, and they're genuinely different in cost/risk, not just two ways to write the same fix:
1. **Per-request, user-JWT-scoped Supabase clients** — so Postgres RLS actually runs as the real safety net. This is the architecturally correct answer, but it's a change to *how the backend authenticates to Supabase on every request*, not a contained patch — `dependencies.py`'s singleton `get_db()` pattern and every `SupabaseManager` method would need rework.
2. **Keep the service-role client, rely on consistent explicit `user_id` filtering** (today's model, now with SEC-2 closed) — zero code change, but no defense-in-depth: the day someone adds a method and forgets the `user_id` filter, nothing catches it.

I can't verify option 1 actually works without a live Supabase project with RLS policies enabled and real JWTs to test against — there's nothing in this local/offline dev environment to point a red/green test at, unlike every other bug in this file. Re-architecting the auth/DB-access layer on a guess, with no way to confirm it behaves correctly, is a worse outcome than documenting the real, current risk clearly and leaving the decision to whoever owns that tradeoff. Asked the user directly rather than picking silently; documenting-only was the chosen path.

### Explain it simply (interview answer)
Think of Row-Level Security (RLS) as a second lock on every user's data, enforced by the database itself: even if my application code has a bug, Postgres double-checks "does this row actually belong to the person asking for it?"

The problem: my backend logs into the database using an admin/service key — like a master key that opens every door in the building, bypassing every individual room lock. So even though I *did* write the RLS policies (the individual room locks), they're never actually checked, because the backend never goes through them. Isolation between users currently depends 100% on my application code remembering to filter by the right user every single time — there's no safety net if I forget.

**Why I didn't just "fix" it:** the real fix means changing *how the backend logs into the database* for every single request — instead of one shared admin key, each request would need to use that specific user's own credentials so the database's locks actually engage. That's a meaningfully bigger, riskier change than a bug patch, and I have no way to verify it actually works without a real, live database with those locks turned on — I don't have one in this environment. So instead of guessing at an authentication rewrite I couldn't test, I audited what's protecting users *today* (confirmed it's consistent), wrote up the real residual risk in plain terms, and gave the decision — and its tradeoffs — back to the person who owns that call, instead of quietly picking one myself.

---

## BUG-3: Synchronous LLM calls inside async methods block the event loop

**Status:** Fixed 2026-06-24

### Symptom
Three places in [generation.py](src/components/generation.py) called LangChain's *synchronous* `chain.invoke()`/`chain.stream()` from inside `async def` methods on a server that's supposed to handle many users concurrently:
- `generate()` — `answer = self.chain.invoke({...})`
- `generate_stream()` — `stream = self.chain.stream({...})` then a plain `for chunk in stream:`
- `generate_multi_queries()` — wasn't even `async def`; `result = chain.invoke({...})` inside a regular `def`

A sync call doesn't yield control back to FastAPI's single-threaded event loop — it just blocks it for the entire duration of the call (an HTTP round-trip to OpenAI, often hundreds of milliseconds to seconds). While one request is inside that call, the event loop can't make progress on *any other* request, including the streaming one that's supposed to be delivering tokens in real time. Two users talking to the bot "at the same time" were actually fully serialized.

### Root Cause
LangChain Runnables expose both a sync (`invoke`/`stream`) and async (`ainvoke`/`astream`) interface. `rewrite_query()` already used `ainvoke` correctly; these three call sites didn't, despite living inside async methods — the sync call still runs to completion, it just does so by monopolizing the event loop instead of yielding to it.

### Fix
1. [generation.py](src/components/generation.py) `generate()` — `self.chain.invoke(...)` → `await self.chain.ainvoke(...)`.
2. [generation.py](src/components/generation.py) `generate_stream()` — `self.chain.stream(...)` + `for chunk in stream:` → `async for chunk in self.chain.astream(...):`.
3. [generation.py](src/components/generation.py) `generate_multi_queries()` — made it `async def`, `chain.invoke(...)` → `await chain.ainvoke(...)`.
4. [pipeline.py](src/pipeline/pipeline.py) `_multi_query_retrieve_async` — its one caller; added the matching `await`.

### Why this approach
Same principle as BUG-1: match the caller to the callee's real (async) contract rather than changing the callee. The chain is built once in `__init__`; swapping `invoke`/`stream` for `ainvoke`/`astream` is a same-behavior, same-prompt, same-output change — only *how* the wait happens differs (cooperative vs. blocking).

### Verification
Added [tests/test_bug3_async_llm_calls.py](tests/test_bug3_async_llm_calls.py) — a `FakeChain` whose sync methods do a real blocking `time.sleep` and whose async methods do `asyncio.sleep` (which actually yields). Each test runs **two calls concurrently** via `asyncio.gather` and checks the *wall-clock time*: truly concurrent calls finish in ~1x one call's delay; blocking calls serialize to ~2x.

- **Before the fix:**
  - `generate()`: 2 concurrent calls took `0.40s` against a `0.30s` threshold (expected ~`0.20s` if non-blocking) — serialized.
  - `generate_stream()`: same — `0.40s`, serialized.
  - `generate_multi_queries()`: failed differently — `TypeError: unhashable type: 'list'` from `asyncio.gather()`, because the function wasn't `async def` yet, so calling it just ran synchronously and handed `gather()` two plain lists instead of two coroutines. A different failure mode than the other two, but the same underlying bug, and just as clearly "red."
  - Side note: monkeypatching `generator.llm.invoke` directly (as an *instance* attribute) doesn't work for testing the multi-query case — `ChatOpenAI` is a Pydantic model and raises `ValueError: "ChatOpenAI" object has no field "invoke"` rather than allowing it. Had to patch the *class* method via `monkeypatch.setattr(type(generator.llm), "invoke", ...)` instead, which Pydantic doesn't intercept.
- **After the fix:** all 3 pass — both timing-based tests finish in ~0.2s instead of ~0.4s, and the multi-query test now gathers two real coroutines successfully.
- Full suite: 30 passed. `pyflakes` clean on every changed file (3 pre-existing unused-import warnings in `generation.py`/`pipeline.py` predate this change and are unrelated).

### Explain it simply (interview answer)
Picture a single waiter (the event loop) serving every table (every user's request) in a restaurant. A `sync` call is like the waiter standing at the kitchen window, arms crossed, refusing to serve anyone else until *this one order* is fully cooked — even though the kitchen (OpenAI's API) is perfectly capable of cooking multiple orders at once if the waiter would just go take other tables' orders in the meantime. An `async` call is the waiter handing the ticket to the kitchen and immediately going to serve someone else, coming back only when that order's ready.

My code had the waiter standing at the window for three different parts of generating an answer — the main answer, the streamed tokens, and a side step that rewrites the search query a few different ways. Two users chatting "at the same time" were actually being served one after another, fully blocked, even though the whole point of using `async`/streaming was to handle many people at once.

**The fix:** swap the blocking calls (`invoke`, `stream`) for their async counterparts (`ainvoke`, `astream`) that LangChain already provides — same prompt, same model, same answer, just "go do something else while waiting" instead of "stand and stare at the kitchen."

**How I proved it:** I built a fake stand-in for the LLM call where the *sync* version does a real blocking 0.2-second pause and the *async* version does a real non-blocking 0.2-second pause, then fired two calls at once and timed it. Blocked: ~0.4 seconds (one after the other). Fixed: ~0.2 seconds (genuinely overlapping). That's a measurable, not just theoretical, proof of the difference. One of the three sub-cases also failed in an unexpected way the first time — Python's plain `gather()` choked because the function wasn't even `async` yet, which was just as valid a "this is broken" signal, in its own way.

---

## BUG-6: Re-ranking ran once *per sub-query*, before the multi-query merge, instead of once after

**Status:** Fixed 2026-06-24

### Symptom
`RetrievalManager.retrieve()` does hybrid search → dedup → re-rank, cutting to `RERANKER_TOP_K` (3) *inside that single call*. Multi-query retrieval ([pipeline.py](src/pipeline/pipeline.py) `_multi_query_retrieve_async`) generates ~4 query variants and called `.retrieve()` once per variant, *then* merged the results. So:
- Each sub-query's results were independently re-ranked and truncated to top-3 **before** the merge ever happened — the final pool was a union of four independent top-3s (≤12 docs), not a global top-3 chosen from everything retrieved.
- The cross-encoder (a real, comparatively slow model call) ran 4 times instead of once, on tiny 3-15 doc batches each, instead of once over the full candidate pool.
- A chunk that was, say, the 4th-best match for sub-query A but never showed up at all for sub-queries B/C/D could outscore everything in sub-query A's surviving top-3 — and it would never get the chance, because it was discarded before any cross-sub-query comparison was possible.

### Root Cause
`retrieve()` bundled three separable steps (retrieve, dedup, rerank) into one method with no way to run the first two without the third. The multi-query caller needed "retrieve `+` dedup, merge across sub-queries, *then* rerank" but only had "retrieve `+` dedup `+` rerank" available per sub-query.

### Fix
1. [retrieval.py](src/components/retrieval.py) — split `retrieve()` into:
   - `retrieve_candidates(query, filename_filter)` — hybrid search + dedup, **no** re-ranking.
   - `rerank(query, docs)` — public wrapper around the existing cross-encoder logic.
   - `retrieve()` — now just `self.rerank(query, self.retrieve_candidates(query, filename_filter))`, unchanged behavior/signature for any single-query caller.
2. [pipeline.py](src/pipeline/pipeline.py) `_multi_query_retrieve_async` — each sub-query now calls `retrieve_candidates()` (full set, untruncated) instead of `retrieve()`; after merging/deduping across all sub-queries, calls `rerank()` **once** on the complete merged pool, using the original rewritten query (not any one sub-query). Wrapped in `run_in_executor` like the retrieval calls above it — the cross-encoder is a blocking CPU call, and leaving it on the event loop would reintroduce the exact class of problem BUG-3 just fixed.

### Why this approach
Splitting `retrieve()` rather than adding a parameter (e.g. `retrieve(query, skip_rerank=True)`) keeps each method doing one thing — `retrieve_candidates` and `rerank` are each independently meaningful and testable, and `retrieve()` becomes a two-line composition instead of a conditional branch. Single-query callers (the `if __name__ == "__main__"` smoke test, anything not doing multi-query) are unaffected — same inputs, same outputs.

### Verification
Added [tests/test_multiquery_rerank_order.py](tests/test_multiquery_rerank_order.py) with a fake `retrieval_manager` exposing *both* the old and new interfaces, so the same double works for the red and green runs:
- **Before the fix:** `retrieve_calls == ["q1", "q2", "q3"]` (the old per-sub-query path was used) and `rerank_calls` was empty — failed on the first assertion.
- **After the fix:** `retrieve_candidates_calls == ["q1", "q2", "q3"]`, `retrieve_calls == []`, and `rerank_calls` has exactly **one** entry containing all `3 × 5 = 15` merged candidates (not a pre-truncated 9).

### Explain it simply (interview answer)
Imagine asking four friends to each separately go pick "the best 3 apples" from a shared bin, then just dumping all twelve picks together and calling that your final answer — instead of asking all four to bring back a handful of *candidates* each, pooling everyone's candidates into one pile, and *then* picking the best 3 from the whole pile. The first way, a genuinely great apple that one friend almost picked (but had two even better ones in hand already) never even makes it into the room. The second way, every apple gets a fair, single, head-to-head comparison against every other apple before anything gets thrown away.

My retrieval code generates a few different phrasings of the user's question (to catch results a single phrasing might miss), searches for each one, and was — for each phrasing separately — already narrowing down to "the best 3" *before* combining the results from all the phrasings. That meant the final 3-12 results were never actually compared against each other properly, and the expensive "which result is really the best" model ran four times instead of once.

**The fix:** let each phrasing bring back its full set of candidates, unsorted-by-quality, pool everything together, remove duplicates, and *then* run the "pick the best" comparison exactly once over the complete pool.

**How I proved it:** I wrote a test that hands the code 3 fake phrasings, each capable of returning 5 fake documents (15 total). I checked two things: did the "final pick" step get called once or four times, and how many documents did it see when it ran? Before the fix, my fake "old" retrieval method (which simulates the bug — pre-shrinking to 3 before any merge) is what got called, and the "final pick" step never ran at all. After the fix, the final pick step ran exactly once, and it saw all 15 candidates, not a pre-shrunk 9.

---

## BUG-4 & BUG-5: BM25 keyword index was per-process and overwrite-only

**Status:** Fixed 2026-06-24

### Symptom
[retrieval.py](src/components/retrieval.py) `update_bm25_index(documents)` was called once per upload with that upload's chunks, and:
- (**BUG-5**) it *replaced* `self._bm25_docs` wholesale — uploading a second file dropped the first file's keyword coverage entirely. Even within a single, never-restarted process, BM25 only ever covered the most recently ingested document.
- (**BUG-4**) it only ever ran in the one process that happened to handle a given upload's HTTP request — after a restart, on a different `uvicorn --workers N` process, or simply querying a document uploaded in an earlier session, the new process's `RetrievalManager` starts with `_bm25_retriever = None` and nothing ever populates it (since no upload event happens to run on it specifically), so hybrid search silently and permanently falls back to dense-only.

### Root Cause
The index was modeled as "whatever the most recent upload, on this process, handed me" instead of "everything currently in this namespace" — the former is neither accumulative nor shared across processes; the latter (which is what Pinecone itself already correctly tracks) is both.

### Fix
1. [retrieval.py](src/components/retrieval.py) — replaced `update_bm25_index(documents)` with:
   - `invalidate_bm25_index()` — marks the index stale (just sets a flag, no rebuild work).
   - `_ensure_bm25_index()` — lazily rebuilds it, *from Pinecone*, the first time it's needed after being marked stale. Fetches everything currently in the namespace via `vectorstore.similarity_search(query="", k=10_000, filter=None)` — the same "dummy query, large k" pattern `delete_document_by_filename` already uses elsewhere in this file, since the Pinecone client here has no plain "list everything" call.
   - `_hybrid_retrieve` calls `_ensure_bm25_index()` instead of just checking `self._bm25_retriever is None`.
2. [pipeline.py](src/pipeline/pipeline.py) — `ingest_file` now calls `invalidate_bm25_index()` (not `update_bm25_index(docs)`); `delete_document` now *also* calls `invalidate_bm25_index()`, which it never did before (a deleted document's chunks would otherwise linger in keyword search results indefinitely).

### Why this approach
Once BM25 is treated as a cached, lazily-rebuilt *view* of Pinecone rather than a separately-maintained, upload-event-driven cache, both bugs disappear as a side effect of the same mechanism — a fresh process has no special "cold" problem because it just rebuilds from the same shared source of truth every other process already reads from, and a second upload can't drop the first file's coverage because the rebuild always pulls *everything*, not just what changed. No new infrastructure (no Pinecone sparse vectors, no separate persistent store) — just stopped treating in-process memory as if it were the source of truth when Pinecone already was one.

This doesn't make BM25 free — the first hybrid search after a process starts (or after any ingest/delete) pays a rebuild. For the chunk volumes a per-user RAG app like this handles, that's a reasonable, bounded cost; it would need revisiting if namespaces grew into the tens of thousands of chunks.

### Verification
Added [tests/test_bm25_lifecycle.py](tests/test_bm25_lifecycle.py). Constructing a real `RetrievalManager` isn't safe here — its `__init__` builds a real `PineconeVectorStore`, which makes an actual HTTP call to Pinecone's control plane immediately (confirmed empirically: a fake API key gets a real `401 Unauthorized` from Pinecone's servers, not a harmless local no-op). Built it via `RetrievalManager.__new__(...)` instead, bypassing `__init__`, and injected a fake vectorstore — testing the real method bodies, just with the one genuinely network-bound dependency swapped out.

Inspected `_bm25_docs` directly rather than `_hybrid_retrieve`'s merged output — dense search isn't stale (it always reads current Pinecone data), so a merged-output-only test could pass for the wrong reason, with dense search quietly masking a completely broken BM25 component.

- **Before the fix:** both tests failed immediately with `AttributeError: 'RetrievalManager' object has no attribute '_ensure_bm25_index'` — the methods didn't exist yet.
- **After the fix:**
  - Uploading file1 then file2 (simulated by changing the fake vectorstore's contents and calling `invalidate_bm25_index()`) leaves BM25 covering **both** files' keywords, not just file2's.
  - A brand-new `RetrievalManager` that never had any upload event run on it directly still sees both files' content on its very first BM25 build, because it pulls from the shared fake "Pinecone," not from any process-local history.
- Full suite: 33 passed. `pyflakes` clean on every changed file (one pre-existing unrelated unused-import warning in `retrieval.py`, predates this change).

### Explain it simply (interview answer)
Picture a library where, every time a new shipment of books arrives, the front desk throws away the *entire* card catalog and makes a brand new one — but only using cards for the books in *that* shipment. Yesterday's books are still on the shelves, perfectly fine, but they've vanished from the catalog. And if the library ever closes and reopens with a different person at the front desk, that new person has no catalog at all until the next shipment happens to arrive while they're on duty.

That's what my keyword-search index was doing: rebuilding itself only from whatever was *just* uploaded, in memory, in whichever specific server process happened to handle that one upload.

**The fix:** stop treating "the most recent upload" as the source of truth, and treat the *actual database* (Pinecone, where every chunk really lives permanently) as the source of truth instead. Now, whenever the catalog might be out of date (after an upload or a deletion), I just mark it stale — and the next time anyone needs it, the code walks the actual shelves (queries Pinecone for everything in that namespace) and rebuilds the whole catalog from what's really there. A brand-new front-desk person — a freshly restarted server, or a different worker process — gets the *exact same correct catalog* the moment they need it, because they're reading from the real shelves, not from a previous person's notes.

**How I proved it:** I simulated "upload file 1, then upload file 2" and checked whether file 1's words were still searchable afterward (they weren't, pre-fix — confirmed the bug). Then I simulated "a brand new instance that never personally handled any upload" and checked whether it could immediately find content from files it never saw uploaded — proving the fresh-process/different-worker scenario specifically, not just the "two uploads in a row" scenario.
