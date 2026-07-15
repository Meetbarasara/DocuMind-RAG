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

---

## BUG-2: `sign_out` called the admin API on the anon client instead of the service client

**Status:** Fixed 2026-06-24

### Symptom
[database.py](src/components/database.py) `sign_out()` called `self.client.auth.admin.sign_out(access_token)` — but `self.client` is built with the **anon** key; `service_client` (built with the service-role key) is a separate attribute. The call raised every time, was caught, logged, and turned into a `return False` — so logout never actually invalidated the session server-side. The JWT stayed valid until it expired naturally; only the frontend's local state got cleared, masking the failure from the user.

### Root Cause
Supabase's Admin API (`auth.admin.*`) is meant to be called with a service-role-authenticated client — that's the entire reason this class keeps `service_client` as a separate attribute from `client` in the first place. This one call used the wrong one.

I didn't take the original review's speculation about the SDK signature at face value — it suggested `admin.sign_out` might take a user id rather than a JWT in some versions. I inspected the actually-installed SDK (`supabase==2.28.3`) directly: `admin.sign_out(jwt: str, scope: SignOutScope = "global")` does take a JWT. So the bug here is purely "wrong client instance," not a signature mismatch — a one-line fix once confirmed.

### Fix
[database.py](src/components/database.py) — `self.client.auth.admin.sign_out(...)` → `self.service_client.auth.admin.sign_out(...)`. No other change needed.

### Why this approach
Trusted the installed SDK's actual signature over the general "signatures vary across versions" caveat in the original review — checking the real, pinned dependency in this repo is more reliable than guessing at version-specific behavior that may not apply here.

### Verification
Added [tests/test_sign_out.py](tests/test_sign_out.py): constructs a real `SupabaseManager` (fake URL/keys — safe, since `create_client` itself doesn't make a network call, only triggering it would), then patches `admin.sign_out` on *both* `db.client` and `db.service_client` separately — one raises (simulating "wrong client"), one records success — to see which one the real code actually calls.

- **Before the fix:** the anon client's fake was called, raised, and `sign_out()` returned `False`.
- **After the fix:** the service client's fake was called and succeeded; `sign_out()` returned `True`.

### Explain it simply (interview answer)
Some operations need "manager" permissions, not just "logged-in user" permissions — forcibly invalidating someone's session from the server side is one of them. My code had two different "badges" available: a regular employee badge (`self.client`, anon key) and a manager badge (`self.service_client`, service-role key). The logout code was trying to do a manager-only action while wearing the regular employee badge — it got rejected every time, the rejection was quietly swallowed, and the app just told the user "you're logged out" without the server ever actually invalidating anything.

**The fix:** use the manager badge for the manager-only action. One line.

**How I proved it:** I gave each badge (each client object) its own fake "sign out" function and watched which one actually got called when I ran the real code. Before the fix, the employee badge's fake got called (and "rejected" itself, just like the real one would). After the fix, the manager badge's fake got called and succeeded.

---

## BUG-7: Deleting a document relied on a ranked search, not a real listing, to find its vectors

**Status:** Fixed 2026-06-24

### Symptom
[retrieval.py](src/components/retrieval.py) `delete_document_by_filename` found the vectors to delete via `similarity_search(query="", k=10_000, filter={"filename": filename})` — embedding an empty string and asking for the top 10,000 *ranked* results. That's a search, not a listing: nothing guarantees a ranked vector search returns literally every vector matching a filter, no matter how large `k` is set. A document with enough chunks, or just unlucky ANN recall, could leave orphaned vectors in Pinecone after "deleting" a file.

### Root Cause
The existing code already correctly worked around one Pinecone limitation (serverless indexes don't support `delete(filter={...})` — documented in its own "Bug 4 fix" comment) by finding IDs via search and deleting by ID instead. But it used the wrong tool to find those IDs: a similarity search, when Pinecone actually exposes a real listing API (`index.list(prefix=...)`) for exactly this purpose — IDs that share a known prefix.

The reason it wasn't already listing by prefix: chunk IDs were built in [embeddings.py](src/components/embeddings.py) as `f"{source}::{content_hash}"`, where `source` was `metadata["source"]` — the **local temp filesystem path** used during ingestion (e.g. `tmp_uploads/report.pdf`), not the stable original filename. That's usable as an opaque unique ID, but not a clean, predictable prefix to list by later.

### Fix
1. [embeddings.py](src/components/embeddings.py) — chunk IDs now use `metadata["filename"]` (confirmed via `unstructured`'s own metadata convention to always be just the basename, matching the already-sanitized name from SEC-2 exactly) instead of `metadata["source"]`. IDs are now `f"{filename}::{content_hash}"` — a stable, predictable, listable prefix per file.
2. [retrieval.py](src/components/retrieval.py) `delete_document_by_filename` — replaced the similarity-search workaround with `self.vectorstore.index.list(prefix=f"{filename}::", namespace=...)`, which paginates through *every* vector ID with that prefix — a real, exhaustive listing, not a ranked search — then deletes by those exact IDs.

### Why this approach
`index.list(prefix=...)` is specifically designed for this exact use case (enumerate everything under a known ID prefix) and is supported on serverless, unlike filter-based delete. It only required chunk IDs to be built on something stable and prefix-friendly — switching from the volatile temp path to the already-stable, already-sanitized filename was the smallest change that made that possible, with no schema migration or new infrastructure needed.

### Verification
Added [tests/test_delete_by_filename.py](tests/test_delete_by_filename.py) with a fake Pinecone index whose `list()` deliberately yields results across **two batches** (to prove pagination is actually handled, not just a single lucky call) — built via `RetrievalManager.__new__(...)` for the same reason as BUG-4/5 (constructing the real class hits Pinecone's network immediately).

- **Before the fix:** failed with `AttributeError: 'FakeVectorStore' object has no attribute 'similarity_search'` — confirms the implementation actually changed (the fake only exposes the *new* `index.list`-based interface).
- **After the fix:** correctly collects all of a file's IDs across both fake pagination batches, deletes exactly those, and leaves a different file's vector (`other.pdf::xxx`) untouched.

### Explain it simply (interview answer)
Imagine trying to find every red car in a parking garage by doing a "most likely red cars" search and grabbing the top 10,000 guesses, instead of just walking every row and checking each car's color directly. The search approach usually works, but it's not a guarantee — it's optimized for "probably good enough," not "definitely complete." My code was using exactly that kind of search to decide which database entries to delete when a user deleted a file.

**The fix:** Pinecone (the database) has an actual "list everything whose ID starts with X" feature — a real walk-every-row listing, not a best-guess search. I just had to make sure each chunk's ID actually *starts with* its filename (it didn't before — it was built from an internal temp file path instead), then switch the delete logic to use that real listing instead of the probabilistic search.

**How I proved it:** I built a fake version of Pinecone's listing feature that deliberately returns results in two separate batches (simulating a big result needing multiple "pages"), then checked that my delete logic correctly gathered IDs from *both* batches before deleting — proving it doesn't just grab the first page and call it done.

---

## BUG-8: Citation verification reported correct citations as "unverified" when a chunk had no page number

**Status:** Fixed 2026-06-24

### Symptom
[generation.py](src/components/generation.py) `_build_context_and_sources` builds two things from the same retrieved chunks: a context label shown to the LLM (`f"Page: {meta.get('page_number', 'N/A')}"` — correctly defaults to `"N/A"`), and a `sources` list used later to verify citations (`"page": meta.get("page_number")` — no default at all). When a chunk had no page number, the LLM correctly saw and cited `"Page: N/A"` (matching what it was shown), but the sources list recorded `page: None` for that same chunk. `_verify_citations`'s lookup set then computed `str(None).lower()` → `"none"`, which doesn't match the LLM's `"n/a"` — so a citation that was *exactly correct* got reported as unverified.

### Root Cause
`dict.get(key, default)`'s default only applies when the *key is missing* — not when the key is present with value `None`. The context-label line happened to read correctly off a dict where the key really was absent (so its default kicked in); the sources-list line either didn't specify a default at all, or (in `_verify_citations`) specified one that the same absent-vs-None distinction silently bypassed.

### Fix
[generation.py](src/components/generation.py) `_build_context_and_sources` — changed `"page": meta.get("page_number")` to `"page": meta.get("page_number") or "N/A"`, which normalizes both "key absent" and "key present but falsy/None" to the same `"N/A"` the LLM is shown.

### Why this approach
Fixing it where the inconsistency is actually introduced (the sources list) rather than papering over it in `_verify_citations` keeps the *meaning* of "page" consistent everywhere downstream — `result["page"]` is now reliably either a real page number or the literal string `"N/A"`, never `None`. This is a narrower fix than the deeper issue the original review flagged (BUG-11: many chunks lose `page_number` entirely during chunking) — that's a separate, out-of-scope problem about *data loss during ingestion*; this fix only ensures the verification logic doesn't *also* introduce its own spurious mismatches on top of whatever page data does or doesn't survive ingestion.

### Verification
Added [tests/test_citation_page_defaults.py](tests/test_citation_page_defaults.py):
- **Before the fix:** a `Document` with no `page_number` in its metadata produced `sources[0]["page"] is None` instead of `"N/A"`.
- **After the fix:** the same document produces `"N/A"`, and a citation verification test confirms `[Source: report.pdf, Page: N/A]` against a source with `page: "N/A"` is correctly marked verified (score `1.0`), not flagged as a mismatch.

### Explain it simply (interview answer)
My code showed the AI assistant a label like "Page: N/A" for chunks that don't have a real page number — that part was correct. But when double-checking the assistant's citations afterward, the code that built the "what pages actually exist" list used a shortcut that quietly turned "no page number" into the value `None` instead of the text `"N/A"`. So when the assistant correctly wrote "Page: N/A" (copying exactly what it was shown), my verification step compared the text `"n/a"` against the value `none` — two different strings — and incorrectly flagged a perfectly correct citation as wrong.

**The fix:** make sure "no page number" always becomes the same `"N/A"` text everywhere, not `None` in one place and `"N/A"` in another.

**How I proved it:** fed the function a chunk with no page number and checked exactly what value it produced — `None`, not `"N/A"` — before the fix. After the fix, same input, correct `"N/A"`. Then double-checked the downstream verification logic accepts that `"N/A"` as a real match.

---

## BUG-9: `CORS_ORIGINS` was hardcoded to localhost — no way to configure it for a real deployment

**Status:** Fixed 2026-06-24

### Symptom
[config.py](src/components/config.py) `CORS_ORIGINS` was a literal hardcoded list (`["http://localhost:8501"]`) with no way to add a deployed frontend's real origin short of editing source code.

### Fix
`CORS_ORIGINS` now reads a comma-separated `CORS_ORIGINS` environment variable, falling back to the same `http://localhost:8501` default when unset. Documented the new variable in [.env.example](.env.example).

### Verification
Added [tests/test_cors_config.py](tests/test_cors_config.py):
- **Before the fix:** setting the `CORS_ORIGINS` env var had no effect at all — `Config().CORS_ORIGINS` was always `["http://localhost:8501"]` regardless.
- **After the fix:** unset still gives the same default (no regression); setting `CORS_ORIGINS=https://app.example.com, https://admin.example.com` correctly produces both origins as a trimmed list.

### Explain it simply (interview answer)
The list of websites allowed to talk to this API was baked directly into the code as just "my laptop during development." To let a real, deployed frontend talk to the API, someone would have had to edit and redeploy the backend's source code just to add an address to a list — that's a deployment config that shouldn't require a code change.

**The fix:** read that list from an environment variable instead, with the old localhost behavior as the default if nobody sets it (so nothing breaks for local development).

---

## BUG-10: A failed upload could leave orphaned data while still reporting success

**Status:** Fixed 2026-06-24

### Symptom
[documents.py](src/api/router/documents.py) `upload_document` runs four steps (storage upload → temp file write → Pinecone ingest → metadata record) with no rollback in either failure direction:
- If **ingestion** failed after the storage upload succeeded, the storage object was left behind — only the local temp file got cleaned up.
- `record_upload`'s return value was **never checked**. If it failed (the existing code already only logs a warning and returns `None` on failure), the response still said `201 Created` — but the file had no row in `user_documents`, the table the UI lists from. Net effect: a file invisible to the user, yet its storage object and Pinecone vectors still existed, still consumed quota, and could still be retrieved and answered by chat — a zombie file the user has no way to even see, let alone delete.

### Root Cause
Three independent systems (Supabase Storage, Pinecone, Supabase Postgres) with no shared transaction — a failure partway through left whichever earlier steps had already succeeded in place, with nothing undoing them.

### Fix
[documents.py](src/api/router/documents.py) `upload_document`:
1. If ingestion raises, best-effort `db.delete_file(...)` before re-raising (cleans up the otherwise-orphaned storage object).
2. `record_upload`'s return value is now checked. If falsy, best-effort rolls back **both** the storage object and the Pinecone vectors, then raises a `500` instead of returning `201`.
3. Both rollback attempts are individually wrapped in their own `try/except` — a cleanup step failing is logged but never allowed to mask or replace the original error that triggered the rollback in the first place.

### Why this approach
Went with compensating cleanup (try the operations, undo on failure) rather than the review's other suggested option — recording metadata *first* with a "processing" status — because that alternative needs a schema change (a status column) and a follow-up reconciliation job to handle ingestion finishing async, which is a bigger redesign than this specific failure mode calls for. Best-effort rollback directly closes the worse of the two outcomes the review flagged: a file that's invisible to the user but still fully live and queryable. It doesn't guarantee perfect consistency under every possible crash (e.g. the process dying mid-rollback), but neither did anything before this fix, and it removes the common case entirely.

### Verification
Added [tests/test_upload_rollback.py](tests/test_upload_rollback.py) with fakes for `db`/`pipeline` that simulate each failure point:
- **Before the fix:** simulated ingestion failure left the storage object un-deleted; simulated `record_upload` failure still returned `201 Created` with no rollback of either storage or Pinecone.
- **After the fix:** ingestion failure triggers a storage delete; `record_upload` failure triggers both a storage delete and a Pinecone delete, and the response is no longer `201`.
- A third test confirms the *successful* path triggers no rollback calls at all — the fix doesn't make normal uploads slower or touch cleanup code unnecessarily.

### Explain it simply (interview answer)
Uploading a file in this app is really four separate steps talking to three different systems: save the raw file, write a temp copy, turn it into searchable AI vectors, and record "this file exists" in a database table. Nothing tied those four steps together — if step 3 or step 4 failed, steps 1 and 2 (or 1-3) had already fully happened and just... stayed that way. Worse, step 4 failing didn't even change the response — the user was told "upload successful" while the file was actually invisible to them (the app's file list reads from that same table that never got the row).

**The fix:** if a later step fails, go back and best-effort undo the earlier steps that already succeeded — delete the stored file, delete the half-created search vectors — instead of leaving them behind as orphaned data nobody can see or clean up. And stop telling the user "success" when the step that makes the file actually *visible* to them never happened.

**How I proved it:** I simulated each failure point separately (ingestion fails; metadata-recording fails) and checked whether the earlier successful steps got cleaned up afterward. Before the fix: they didn't, in either case. After the fix: each failure triggers exactly the right cleanup, and — importantly — a fully successful upload still doesn't trigger any unnecessary cleanup calls at all.

---

## SEC-1 and SEC-5: already resolved earlier (noted here for the record)

Two items the review numbered separately turned out to already be handled by the time they were revisited:

- **SEC-1** ("`filename_filter` never applied in retrieval") was investigated when first reached and the review's own text retracts it on closer reading: the filter *is* correctly threaded through, no real leak was found. Documented in the SEC-2 entry above rather than given its own entry, since there was nothing to fix.
- **SEC-5** ("login/signup error text enables user-enumeration") was closed as a direct side effect of the SEC-4 fix — making login/signup return one fixed generic message regardless of the real failure reason closes the enumeration vector at the same time it stops leaking raw exception text. See the SEC-4 entry above.

---

## SEC-8: `print()` statements bypassed the configured logger

**Status:** Fixed 2026-06-24

### Symptom
[ingestion.py](src/components/ingestion.py) (parsing progress, chunk counts, the one real error case) and `_log_elements_analysis` in [utils.py](src/utils.py) (element-type breakdown) used `print()` instead of the app's configured logger. In production, this bypasses log levels, log rotation, and whatever output capture/aggregation the deployment relies on — it goes straight to stdout regardless of how logging is configured.

### Fix
Added a logger to `ingestion.py` (it didn't have one) and converted every `print()` in the two real, reachable methods (`process_documents`, `build_langchain_documents`) and in `_log_elements_analysis` to `logger.debug(...)` for routine progress, and `logger.error(..., exc_info=True)` for the one actual failure case. Left the `if __name__ == "__main__":` smoke-test block's prints untouched — it's a manual CLI debugging tool a developer runs directly, never reached by the running application, so it's not a "production logging" concern.

None of the converted calls log raw chunk/document content (`doc.page_content`, etc.) — they're all counts and status strings, both before and after. The one place that *does* print actual content (`doc.page_content[:120]` in the `__main__` block) was intentionally left alone for the same reason.

### Verification
Added [tests/test_ingestion_logging.py](tests/test_ingestion_logging.py):
- Two runtime tests (using pytest's `capsys`/`caplog`) drive the parts that don't need real `unstructured` internals: `_log_elements_analysis` directly (works fine with a duck-typed fake element), and `process_documents`'s error path (an unsupported extension fails before any real file parsing is attempted, so it needs no mocking). **Before the fix:** both wrote to stdout and nothing reached the log records. **After:** nothing on stdout, the expected record is in `caplog`.
- A third test does a static check (reads the source, asserts no `print(` appears before the `if __name__ ==` line) for the remaining conversions inside `build_langchain_documents` — those run through `chunk_by_title`, which expects real `unstructured` element internals that aren't worth faking just to exercise a print-vs-log change with no logic difference.

### Explain it simply (interview answer)
My code used `print()` — the same thing you'd use in a quick throwaway script — for real status messages in a server that's supposed to run unattended in production. The problem: `print()` always goes straight to the terminal/stdout, completely bypassing whatever logging setup the app actually has (log levels, log files that rotate so they don't fill up the disk, whatever monitoring tool is supposed to be watching the *logs*, not raw terminal output).

**The fix:** swap every one of those `print()` calls for the proper logger, at the right level — routine progress as `debug` (so it's there if you need it but doesn't spam normal output), the one real error case as `error` (with the actual exception attached, not just its text). Left one obviously-a-debugging-script's prints alone, since that's a tool a developer runs by hand, not something the live application ever executes.

**How I proved it:** used a pytest feature that captures anything written to stdout, then checked it was empty after running the code — and separately checked the *logger* actually received the message. Before the fix: stdout had content, the logger had nothing. After: the reverse.

---

## SEC-9: A missing service-role key silently downgraded security instead of failing to start

**Status:** Fixed 2026-06-24

### Symptom
[database.py](src/components/database.py) `SupabaseManager.__init__` fell back to the anon-key client for `service_client` if `SUPABASE_SERVICE_ROLE_KEY` was missing — logging only a warning, not stopping anything. Every storage and admin operation in the app assumes `service_client` actually has elevated, service-role access (that's the entire basis of the user-isolation model discussed in SEC-3). A missing env var would silently change that assumption — the app would *start successfully* and *look like it was working* in a materially weaker security posture, with no signal beyond a log line nobody may be watching.

### Fix
[database.py](src/components/database.py) — added the missing-service-key check right alongside the existing missing-URL/anon-key check (same `CustomException`-on-`__init__` pattern already used one line above it), and removed the fallback branch entirely — `service_client` is now unconditionally built from the service-role key.

### Why this approach
A misconfiguration that changes your security model should be loud and immediate (the app refuses to start) rather than quiet and deferred (the app starts, looks fine, and only behaves differently in ways that are hard to notice — until something that depended on elevated access fails unpredictably, or worse, silently succeeds with the wrong permissions). This mirrors the exact pattern the class already used for the URL/anon-key check one line above — not a new convention, just extending an existing one to a third required secret.

### Verification
Added [tests/test_supabase_manager_init.py](tests/test_supabase_manager_init.py):
- **Before the fix:** constructing `SupabaseManager` with an empty `SUPABASE_SERVICE_ROLE_KEY` succeeded silently (only a warning in the logs) — the test expecting a `CustomException` failed with "DID NOT RAISE."
- **After the fix:** the same construction raises `CustomException` immediately; constructing with a real (fake-but-present) service key still works normally, and `client`/`service_client` are confirmed to be two genuinely distinct client objects.

### Explain it simply (interview answer)
My app needs two different "keys" to talk to its database: a regular one and an elevated "admin" one. If the elevated key was missing from the environment, my code's old behavior was: log a warning, then quietly hand out the *regular* key instead and carry on as if nothing was wrong. The app would start up fine and seem to work — but every operation that assumed it had elevated access actually didn't, and nothing made that obvious.

**The fix:** if the elevated key is missing, refuse to start at all, with a clear error saying exactly what's missing and why. A loud failure at startup, when you're staring at the deploy logs anyway, is far easier to catch and fix than a silent downgrade that only shows up as a mysterious permission problem hours or days later.

**How I proved it:** tried constructing the database manager with that key deliberately left empty — confirmed it used to succeed anyway (the bug). Added the check, tried again — now it raises immediately, with a message that says exactly what's wrong.

---

## BUG-11: Text chunks had no fallback when a composite chunk's own page number was missing

**Status:** Fixed 2026-06-24

### Symptom
The original review claimed `chunk_by_title` "merges elements across pages, so `page_number` is frequently `None`," weakening citations (BUG-8). Before changing anything, I verified this empirically against the actually-installed `unstructured==0.22.10` by running real elements with different page numbers through `chunk_by_title` directly — and the composite chunk's own `page_number` was correctly set to the *first* original element's page, not `None`. `page_number` only came back `None` when *every* original element genuinely had no page number at all (e.g. non-paginated formats like `.txt`/`.csv`) — which is correct behavior, not a bug, since there's no real page to report. So the review's specific claim didn't hold for this codebase's actual dependency version.

That said, there's a real, narrower gap worth closing: [ingestion.py](src/components/ingestion.py)'s text-chunk branch read `page_number` straight off the chunk's own metadata with **no fallback at all** if it ended up `None` for any reason — table/image chunks already used the more defensive `_get_page_number()` helper; text chunks didn't.

### Fix
[utils.py](src/utils.py) `_get_page_number()` now falls back to the first original element (preserved via `metadata.orig_elements`, which `unstructured` populates specifically so chunked metadata can be reconstructed) that has a `page_number`, if the composite's own is missing. [ingestion.py](src/components/ingestion.py)'s text-chunk branch now uses this same helper (it previously extracted `page_number` directly via `_get_metadata_fields`, with no fallback) — bringing it in line with the table/image branches.

### Why this approach
I didn't "fix" `chunk_by_title` — I verified it wasn't actually broken in the way described, for this version. The fallback is genuine defense-in-depth: it doesn't depend on understanding every internal quirk of a third-party library's chunking algorithm or guarantee across every future version: if the composite's own page metadata is ever missing despite some original element having one, the fallback recovers it; if truly nothing has it, it still correctly returns `None`/`N/A`.

### Verification
Added [tests/test_page_number_fallback.py](tests/test_page_number_fallback.py) — duck-typed fake elements (no real `unstructured` objects needed, since `_get_page_number` only does `getattr` calls): own `page_number` present → used directly; own missing but an `orig_elements` entry has one → recovered; nothing anywhere has one → `None`. **Before the fix:** the fallback case returned `None` instead of the recoverable page number. **After:** all three cases pass.

### Explain it simply (interview answer)
Before touching any code, I tested the actual third-party library's behavior directly — fed it elements from two different pages and checked what page number it gave the merged result. Turned out it already does the sensible thing (uses the first page) — the bug report's claim about *this* library version was simply wrong. That's worth saying out loud in an interview: not every reported bug is real once you check it against the actual running code, and confirming that *before* writing a fix saves you from "fixing" something that already works.

What I found instead, while looking: one of three places that reads a page number had no safety net at all if it ever came back empty, while the other two did. So I added the same safety net there too — if the obvious place to find a page number comes up empty, check the original pre-merge pieces for one before giving up.

---

## BUG-12: A dead `chunk_id` was computed and then immediately thrown away

**Status:** Fixed 2026-06-24

### Symptom
[ingestion.py](src/components/ingestion.py) computed a `chunk_id` for every chunk via `_stable_id(...)` (a SHA-1 hash). [embeddings.py](src/components/embeddings.py) then unconditionally **overwrote** that value with its own scheme (`f"{filename}::{content_hash}"`, since the BUG-7 fix) right before upserting to Pinecone. The first `chunk_id` was computed, stored in a dict, and then discarded without ever being used for anything — confirmed via search that `_stable_id` had no other callers anywhere in the codebase.

### Fix
Removed all three `chunk_id: _stable_id(...)` assignments from `ingestion.py` (text/table/image branches), removed the now-unused `_stable_id` import, and deleted `_stable_id` itself from [utils.py](src/utils.py) (confirmed unused everywhere afterward via `pyflakes`).

### Why this approach
The review's own framing was "pick one" — embeddings.py's version is the one that's actually used (it's the upsert ID, and what `delete_document_by_filename` reads back), so there was nothing to migrate, only dead code to delete. Per the project's own convention: if something's confirmed unused, delete it rather than leaving it as confusing, never-executed scaffolding.

### Verification
Full test suite (57 tests) still passes after removal, and `pyflakes` is clean — confirming nothing else referenced the removed function or the removed dict keys.

### Explain it simply (interview answer)
Two different parts of the code each generated their own "unique ID" for the same piece of data — but only one of them was ever actually used; the other was computed and then immediately overwritten before it could do anything. It's like filling out a form in pen and then someone else stamping over it before it's ever filed. I deleted the part that gets overwritten, since keeping code that runs but never affects anything is just confusing for the next person reading it.

---

## BUG-13: `datetime.utcnow()` — deprecated and timezone-naive

**Status:** Fixed 2026-06-24

### Symptom
[database.py](src/components/database.py) `record_upload` stored `uploaded_at` via `datetime.utcnow().isoformat()`. `datetime.utcnow()` is deprecated since Python 3.12 (Python itself emits a `DeprecationWarning`) and — more practically — produces a **naive** datetime with no timezone marker, so a value like `"2026-06-24T10:30:00"` doesn't actually say it's UTC; a consumer has to know that out-of-band.

### Fix
`datetime.now(UTC).isoformat()` instead — produces a timezone-aware ISO string with an explicit `+00:00` suffix. Safe to use `datetime.UTC` (added in Python 3.11) since the project's own README states Python 3.11+ as a prerequisite.

### Verification
Added [tests/test_record_upload_timestamp.py](tests/test_record_upload_timestamp.py), capturing the row passed to the (faked) Supabase upsert call. **Before the fix:** the timestamp had no timezone suffix, and Python's own `DeprecationWarning` showed up in the test output. **After:** the timestamp correctly ends in `+00:00`, no warning.

### Explain it simply (interview answer)
A timestamp without a timezone is technically ambiguous — `"10:30:00"` could be UTC, could be the server's local time, could be anything, and whoever reads it later has to just *assume* which one. Python's older "give me the current UTC time" function actually returns a value with that ambiguity baked in (no timezone attached), which is exactly why Python deprecated it in favor of one that returns an explicit, labeled UTC time. One-line fix, but a real correctness improvement for anything that later reads or compares these timestamps.

---

## BUG-14: Chat-history trimming re-encoded the same overlapping text on every loop iteration

**Status:** Fixed 2026-06-24

### Symptom
[utils.py](src/utils.py) `format_chat_history` / `format_chat_history_async` fit recent messages into a token budget with a loop: format *all* currently-surviving messages into one string, tokenize the whole thing, and if it's still too big, drop the oldest message and repeat. The most recent message survives in that joined string until the very last iteration — so it (and every other message that hasn't been dropped yet) gets **re-tokenized from scratch on every iteration**, even though its own token count never changes between iterations.

### Fix
Added `_trim_lines_to_budget()`: tokenizes each message line exactly **once**, then trims from the front using simple integer subtraction on the precomputed counts — no re-tokenizing. Both functions now: try the full text once (unchanged behavior for the common "everything fits" case — still just one encode call), and only fall back to the per-line budget trim if that didn't fit. The async version additionally accounts for the summarized-older-messages prefix's own token cost before deciding how many recent lines fit alongside it, preserving the original priority (protect the summary, drop individual messages first).

### Why this approach
This keeps the cheap, common case (conversation fits, no trimming needed) exactly as cheap as before — one encode call — while fixing the actual waste, which only shows up once trimming is needed: previously, *every* surviving message got re-tokenized on *every* iteration of the loop; now each one is tokenized exactly once, period, regardless of how many iterations the old loop would have needed.

### Verification
Added [tests/test_chat_history_trim_efficiency.py](tests/test_chat_history_trim_efficiency.py) with a fake tokenizer that records every string it's asked to encode. Metric: how many times does the *most recent* message's content show up across all encode calls — a way to detect redundant re-encoding that doesn't depend on fragile exact-character-count math.
- **Before the fix:** the most recent message's content was encoded 6 times (once per loop iteration) for both the sync and async versions.
- **First version of this test had a bug, not the code:** the test's fake messages all used identical filler text ("x" repeated), so the "most recent message" marker matched *every* message's line, not just the real last one — produced a misleadingly-still-failing result (7 hits) right after the fix landed. Gave each fake message distinct content, reran, and got the real signal: 2 hits (the one-time full-text attempt, plus that message's own single-line encode) for both functions.

### Explain it simply (interview answer)
Imagine repeatedly asking "does this fit in the box?" by taking everything out, measuring it all again from scratch, putting back everything except the oldest item, and repeating — instead of just weighing each item once and doing subtraction. My code was doing the expensive version: every time it needed to drop the oldest message to make room, it re-measured (re-tokenized) *everything still in the box*, including items it had already measured in the previous step.

**The fix:** measure each message exactly once, then use simple arithmetic to figure out how many of the most recent ones fit, instead of re-measuring the whole shrinking pile over and over.

**How I proved it:** built a fake tokenizer that just remembers everything it's ever asked to measure, then checked how many times the newest message got remeasured. Before: 6 times. After: 2. And — small but real lesson — my first attempt at this test was itself broken (all my fake messages looked identical, so I couldn't actually tell which message was being remeasured), which I only caught because the post-fix result still looked wrong. Fixed the test's setup, then got a result that actually meant something.

---

## BUG-15: RAGAS evaluation routes blocked the event loop and had no rate limit

**Status:** Fixed 2026-06-24

### Symptom
[evaluate.py](src/api/router/evaluate.py)'s `/api/evaluate/single` and `/api/evaluate/batch` routes are `async def`, but called `EvaluationManager`'s plain, synchronous `evaluate_single`/`evaluate_batch` directly — and those make several real LLM calls each via RAGAS. Same class of problem as BUG-3: a blocking call inside an async route freezes the entire server for every other concurrent request for the whole duration of the evaluation. On top of that, neither route had any rate limit, despite being arguably the single most expensive operation exposed by the app — any authenticated user could trigger unbounded batch evaluations.

### Fix
1. Wrapped both calls in `await asyncio.to_thread(...)` so they run in a worker thread instead of on the event loop — same fix pattern as BUG-3, applied to a different blocking dependency (RAGAS instead of LangChain).
2. Added `request: Request` + `@limiter.limit(...)`: `5/minute` for `single`, a tighter `2/minute` for `batch` (its cost scales with how many rows are in the request).

### Why this approach
Didn't introduce a new "admin-only" authorization tier, which the review's "no auth-scoping" phrase could be read as suggesting — there's no existing role/permission concept anywhere else in this codebase (every route is just "authenticated or not"), and bolting one on for this single feature would be a bigger, unrequested architecture addition rather than a contained fix. Rate limiting plus moving the blocking work off the event loop directly addresses the concrete, demonstrated problems (server-wide blocking, unbounded cost) using patterns already established elsewhere in this same codebase (BUG-3, SEC-7).

### Verification
Added [tests/test_evaluate_routes.py](tests/test_evaluate_routes.py) — same concurrency-timing technique as BUG-3 (a fake `EvaluationManager` whose methods do a real blocking `time.sleep`, two requests fired concurrently via `asyncio.gather`, wall time measured), plus a rate-limit test matching SEC-7's pattern.
- **Before the fix:** both `single` and `batch` took ~0.41s for 2 concurrent calls against a 0.30s threshold (serialized, not concurrent); 10 rapid calls to `single` never returned a 429.
- **After the fix:** both finish in ~0.2s (genuinely concurrent), and a 429 shows up within the first 10 rapid calls while the very first request still succeeds normally.

### Explain it simply (interview answer)
This evaluation feature runs an AI-judging-AI process that makes several real calls to a language model per item — expensive in both time and money. My code called it the same way you'd call a quick, free function: directly, with no consideration that something else might need the server's attention at the same time. So while one evaluation ran, the *entire server* was frozen for every other user, however briefly. And worse, anyone with a valid login could trigger this expensive operation as many times as they wanted, with literally nothing stopping them.

**The fix:** run the expensive evaluation work on a separate thread instead of directly on the main "traffic cop" thread that's supposed to be juggling every request — and put a speed limit on how often any one person can trigger it, tighter for the batch version since it can process many items at once.

**How I proved it:** same trick as a similar bug I'd already found and fixed elsewhere (BUG-3) — built a fake version of the slow part that does a real, measurable pause, fired two requests at the same time, and timed it. Blocked: requests took twice as long together as one alone. Fixed: they took about the same time as just one — proving they actually ran side by side.

---

## LOW / Correctness nits — 10 small items, fixed 2026-06-24

The original review grouped these as low-severity. Same discipline as everywhere else in this file: verified each claim before acting on it rather than mechanically "fixing" all ten — two turned out to already be resolved as side effects of earlier work, and one ("inert" exception enrichment) didn't actually hold up once tested.

**1. `exception.py`'s `error_detail: sys` type hint.** Verified first: the "enriched traceback" feature is *not* actually inert in this codebase — every real call site raises `CustomException` from inside an `except:` block, where `sys.exc_info()` is genuinely populated (confirmed with a real try/except in a one-line script before touching anything). The real, narrower bug: the type hint used the `sys` *module* itself as a type, which is just wrong. Changed to `Optional[types.ModuleType]`. [tests/test_custom_exception.py](tests/test_custom_exception.py) locks in both behaviors (enriches when there's an active exception; falls back to the plain message when there isn't).

**2. `sys.path.insert` hacks** in `ingestion.py` and `frontend/app.py`. The project is `pip install -e .`'d (confirmed: `pip show Documind` shows an editable install), which makes `src`/`frontend` resolvable from anywhere regardless of CWD — the hacks were redundant. Removed both. Verified `ingestion.py`'s removal via the full test suite (which imports it extensively); verified `frontend/app.py`'s removal by actually launching `streamlit run frontend/app.py` and confirming it serves `200` with no import errors — didn't want to just reason my way to "should be fine" on a file I can't unit-test.

**3. Dead branch in `generate_stream`'s token extraction** (`chunk.get("answer", "")`). Verified with a real `StrOutputParser`-terminated chain (not just my own test fakes) that `astream()` yields `TextAccessor` objects, which **are** `str` subclasses (`issubclass(TextAccessor, str) == True`) — so `isinstance(chunk, str)` always wins, exactly as the review claimed. Removed the dead branch.

**4. `PINECONE_NAMESPACE` empty-string default.** Pinecone treats `namespace=""` as a real, literal (shared/default) namespace — a caller that forgot to pass a real one would silently read/write that shared bucket instead of failing. Added a guard in `RAGPipeline._get_retrieval_manager` (the one chokepoint every query/ingest/delete goes through) that raises `CustomException` on an empty namespace. [tests/test_namespace_guard.py](tests/test_namespace_guard.py).

**5. `list_files` vs. `user_documents` metadata table drift.** No new code — this is the same drift BUG-10 already closes (a failed `record_upload` used to leave storage/Pinecone data with no metadata row; BUG-10's rollback now cleans that up instead of letting it linger and drift).

**6. Unpinned `requirements.txt`.** Pinned all 27 top-level dependencies to the exact versions confirmed installed and exercised by every test in this entire session (`pip freeze` against the project venv, cross-referenced against each line already in the file).

**7. `setup.py`'s `get_requirements` comment-parsing bug.** It passed inline `# comment` suffixes straight through to `install_requires`, which expects clean PEP 508 specifiers, not comment-laden strings. Fixed to strip everything after `#` and skip blank lines. This became more than academic once requirements.txt also got version pins (item 6) — verified by extracting just this function via `ast` (deliberately *not* importing `setup.py` directly, since that executes `setup(...)` at module level) and confirming every parsed line passes `packaging.requirements.Requirement(...)` validation — the same check `setuptools` does internally. Then went one step further and actually ran `pip install -e . --no-deps` followed by `pip check` against the real, fixed files: clean install, "No broken requirements found."

**8. `.gitignore` hiding `pyproject.toml` and `test_pipeline.py`.** Confirmed neither file currently exists anywhere in the repo, so removing them from `.gitignore` is a zero-behavior-change fix for the future — if either is ever created, git will track it instead of silently dropping it. Left `improve.txt`/`production_audit.md` ignored; those read as personal working notes, not project config or test files, and weren't what the review flagged.

**9. "No tests exist despite a `test` CI job."** Already resolved, no code change — this entire session added 22 test files covering every bug fixed (63 tests total, all passing). CI's existing `if [ -d "tests" ] && find tests -name 'test_*.py' | grep -q .` check will now find them and actually run `pytest` instead of no-op'ing.

**10. `frontend/utils.py`'s hardcoded `API_BASE`.** Same pattern as BUG-9 (`CORS_ORIGINS`) — now reads an `API_BASE` env var, defaulting to the same `http://localhost:8000`. Verified via `importlib.reload()` in [tests/test_frontend_api_base.py](tests/test_frontend_api_base.py), and by actually launching the live Streamlit app again afterward to confirm it still starts cleanly.

### Explain it simply (interview answer)
Most of these were small enough that the interesting part isn't the fix itself, it's the *process*: for three different low-priority complaints in this list (and a few bigger ones earlier in this session), I tested whether the claimed problem was even real before changing anything — and two of them weren't, or weren't anymore. The exception-enrichment feature actually works in real usage, it was just mislabeled. The "no tests" complaint quietly fixed itself as a side effect of everything else I'd already done. That's worth remembering for an interview: a code review or bug report is a *hypothesis*, not a verdict — confirming it against the actual running code, every time, is what separates "I fixed ten things" from "I fixed the seven that were real and can tell you exactly why the other three weren't."

For the ones that *were* real: a couple were genuine one-liners (a wrong type hint, a hardcoded URL). The more interesting one was the `setup.py`/`requirements.txt` pair, because they're coupled — pinning versions without first fixing how the comments get parsed would have made the comment-parsing bug *worse*, not better. I fixed the parser first, then verified it against the real file using the exact validation library `setuptools` itself uses internally, then actually ran the real install end-to-end rather than trusting my own reasoning about whether it would work.

---

## Logical Mistake #3: BM25-only documents bypassed `SIMILARITY_THRESHOLD` entirely

**Status:** Fixed 2026-06-25

### Symptom
`_dense_retrieve` ([retrieval.py](src/components/retrieval.py)) correctly drops dense results below `SIMILARITY_THRESHOLD`. But `_hybrid_retrieve`'s Reciprocal Rank Fusion merges dense and BM25 results by **rank**, not score — a document that only BM25 surfaced contributes an RRF score purely from its position in BM25's result list, with no relevance check of any kind.

### Root Cause
Confirmed empirically against the real, installed `rank_bm25`/`langchain_community.BM25Retriever` rather than assuming: `BM25Retriever.invoke()` calls `rank_bm25`'s `get_top_n()`, which is a plain `np.argsort(scores)[::-1][:n]` — it always returns exactly `k` documents, with **no minimum-score cutoff**. I verified this directly: querying `"fox dog"` against a 3-document corpus where two documents (about physics and stocks) share zero words with the query returned BM25 scores of exactly `0.0` for those two — yet `BM25Retriever.invoke()` still returned them as if they were real matches, because the corpus only had 3 documents and `k=3` (or, in production, `k=TOP_K=5`). In this app's per-user namespaces, small document counts are the common case, not an edge case — so this isn't theoretical.

### Fix
[retrieval.py](src/components/retrieval.py) `_hybrid_retrieve` — after getting `bm25_docs`, filter out any document whose BM25 score is not greater than `0`, computed via the retriever's own `vectorizer.get_scores(...)` (the same `rank_bm25` API `get_top_n` itself uses internally, just without the cutoff `get_top_n` is missing). Matched back to documents by object identity (`id(d)`) since `BM25Retriever` returns the exact same `Document` objects it was built from, not copies.

### Why this approach
Reusing `SIMILARITY_THRESHOLD`'s numeric value (`0.50`) against BM25 scores would have been a *different* bug, not a fix — cosine similarity is roughly bounded `[0, 1]` (higher is better), while BM25 scores are unbounded and depend on corpus statistics (term frequency, document length, collection size). The two numbers aren't on comparable scales, so a single shared threshold value can't meaningfully gate both. The correct, scale-appropriate quality gate for the BM25 branch is BM25's *own* score: `> 0` means "shares at least one term with the query," `0` means no lexical relevance whatsoever. That's the minimal signal needed to close the specific gap the review described (zero-relevance documents riding through on rank alone) without inventing a new cross-scale heuristic or requiring extra network calls (e.g. fetching a dense similarity score for every BM25-only candidate, which would also slow down every hybrid query).

This intentionally does *not* require a BM25-surfaced document to also appear in dense's results — a real keyword match that dense's embeddings missed semantically is exactly the value hybrid search is supposed to add. Only documents with literally no relevance signal under *either* method are excluded.

### Verification
Added [tests/test_similarity_threshold_bypass.py](tests/test_similarity_threshold_bypass.py):
- First asserts the premise directly against the real `rank_bm25` scorer (not a mock): two unrelated documents score exactly `0.0` against the test query, one related document scores `> 0`.
- **Before the fix:** `_hybrid_retrieve("fox dog")` on a 3-doc BM25 corpus + 1 dense hit returned all 4 documents merged together — including the two zero-relevance ones.
- **After the fix:** the same call returns only the genuinely relevant 2 (the dense hit and the real BM25 keyword match); the zero-relevance documents are gone.
- Full suite: 64 passed. `pyflakes`/`ruff` clean on the changed file (one pre-existing unrelated `Optional` unused-import warning in `retrieval.py`, confirmed via `git diff` to predate this change).

### Explain it simply (interview answer)
My search had two engines: one that understands *meaning* (dense/embedding search) and one that matches *exact words* (BM25 keyword search), combined by blending their rankings together. The meaning-based engine had a sensible rule: "only show results that are genuinely similar enough" — measured on a 0-to-1 similarity scale. The keyword engine had no such rule at all.

The keyword engine's actual library always hands back exactly the number of results you ask for, ranked best-to-worst — even if the "best" ones share literally zero words with your question, because there was nothing better available in that user's small document collection. Since my code combined the two engines by blending their *rankings* (1st place, 2nd place, etc.) rather than their actual scores, a completely irrelevant document that happened to rank "3rd out of 3" in the keyword engine could still squeeze into the final results — nothing ever checked whether 3rd place was actually any good.

**The fix:** before blending the keyword engine's results in, I now ask it for the actual relevance score behind each one (a feature the library already has, it just wasn't being used) and throw out anything scoring exactly zero — no shared words, no business being in the results. I deliberately didn't reuse the *meaning* engine's 0-to-1 threshold number for this, because the two engines measure relevance in totally different, non-comparable units — that would've been swapping one bug for another.

**How I proved it:** I picked a query ("fox dog") and a tiny 3-document collection where I knew, by construction, that two of the documents shared no words with it at all. I confirmed with the real scoring library that those two genuinely scored `0.0` — then showed they still came back from my code's hybrid search anyway. After the fix, they're filtered out, while a real keyword match and a real semantic match both still come through correctly.

---

## Logical Mistake #4: Chunk dedup kept the longer chunk instead of the more relevant one

**Status:** Fixed 2026-06-25

### Symptom
`_deduplicate_chunks` ([retrieval.py](src/components/retrieval.py)) removes near-duplicate chunks (Jaccard similarity ≥ `CHUNK_DEDUP_THRESHOLD`), and always kept whichever of the pair had **more characters** — with no connection to which one was actually more relevant to the query.

### Root Cause
Dedup runs inside `retrieve_candidates()`, immediately after `_hybrid_retrieve`/`_dense_retrieve` and *before* any re-ranking (`rerank()` is a separate, later step — see the BUG-6 fix). At the point dedup runs, there's no cross-encoder relevance score attached to any `Document` yet — but there is a real, available, relevance-correlated signal being thrown away: **position in the list**. `_hybrid_retrieve` returns its merged result sorted by RRF score descending; `_dense_retrieve` returns Pinecone's own similarity-ranked results in order. Either way, `docs[0]` is retrieval's own best guess at "most relevant," `docs[1]` the next-best, and so on. Chunk *length* has no relationship to any of that — a verbose, padded duplicate could out-rank a concise, on-topic one purely by character count.

### Fix
[retrieval.py](src/components/retrieval.py) `_deduplicate_chunks` — when two chunks at indices `i < j` exceed the similarity threshold, always keep `i` and remove `j`. The inner loop only ever compares an index against *later* indices, so `i` is always the higher-ranked (or equal) one of any pair being compared — there's no longer a need for the length comparison, the `else` branch that removed `i` instead, or the `break` that existed only to stop comparing a doc that branch could mark for removal (now that `i` is never removed within its own iteration, none of that is reachable).

### Why this approach
This uses a signal that's already correct and already available — retrieval's own ranking — instead of inventing a new one or restructuring the pipeline to attach scores earlier. It doesn't change *when* dedup runs (still before re-ranking, which is intentional — re-ranking via cross-encoder is the expensive step, and deduping first avoids wasting it on near-identical text) or move length out as a signal entirely in some more complex tie-break scheme; it just stops using a signal (length) that was actively wrong in favor of one (rank) that was sitting right there, unused, the whole time.

### Verification
Added [tests/test_dedup_keeps_relevant_chunk.py](tests/test_dedup_keeps_relevant_chunk.py):
- Constructed two near-duplicate chunks (10 shared words, Jaccard ≈0.91, over the 0.85 default threshold) where the first (higher-ranked, i.e. earlier in the list) is short, and the second (lower-ranked) is the same text padded with 50 repeats of one filler word — same word *set* overlap, but more than 5x the character length.
- **Before the fix:** dedup kept the padded, lower-ranked chunk and discarded the shorter, higher-ranked one.
- **After the fix:** dedup keeps the higher-ranked chunk regardless of length.
- A second test confirms genuinely distinct chunks (no real overlap) are still left alone by both versions — the fix doesn't make dedup more or less aggressive, only changes *which* survivor it picks.
- Full suite: 66 passed. `ruff`/`pyflakes` clean on the changed file (the same pre-existing, unrelated `Optional` unused-import warning from the Logical Mistake #3 fix — confirmed via `git diff` to predate both changes).

### Explain it simply (interview answer)
Picture a search engine that finds two nearly-identical paragraphs answering your question — one is a tight, three-sentence answer; the other says the same thing but padded with filler. My code's rule for picking which one to keep, whenever it found two like this, was simply "keep whichever is longer" — treating word count as if it were a proxy for quality. It isn't. A padded, rambling version of the same answer isn't more relevant just because it's bigger.

The fix: by the time this duplicate-removal step runs, the search system has *already* ranked all the results from best to worst (that ranking happens earlier, before dedup ever sees them). So instead of measuring length, I just keep whichever of the two duplicates the search system already ranked higher — a real signal of relevance that was sitting right there the whole time, completely unused.

**How I proved it:** I built two near-duplicate paragraphs sharing the same core content, made the *higher-ranked* one short and the *lower-ranked* one artificially long (same words, repeated filler tacked on). Before the fix, the code kept the long, lower-ranked one. After the fix, it correctly keeps the short, higher-ranked one. I also checked that two genuinely different paragraphs are never merged by either version — the fix only changes which survivor gets picked when there really is a duplicate, not how aggressively duplicates get detected.

---

## Logical Mistake #7: Memory-summarization cache key could collide across different conversations

**Status:** Fixed 2026-06-25

### Symptom
`_hash_messages` ([utils.py](src/utils.py)) built its cache key from only `f"count:{len(messages)}::last:{messages[-1].get('content','')[:50]}"` — the message *count* plus the first 50 characters of the *last* message's content. Every other message's content (everything actually being summarized) was completely ignored. Two different conversations with the same number of older messages and the same (or same-prefix) last message produce an identical hash and share a cache slot — the second conversation gets served the *first* conversation's cached summary instead of its own.

### Root Cause
The function's docstring/comment describes itself as a "stable hash of a list of message dicts," but the implementation only ever looked at two cheap-to-compute proxies (length, last message prefix) instead of the actual content being hashed. Truncating the last message to 50 characters makes the collision easier still — two messages only need to *share a 50-character prefix*, not be identical, to collide.

### Fix
[utils.py](src/utils.py) `_hash_messages` — hash every message's role and full content, joined with `"\x1f"` (ASCII unit separator, chosen specifically because it's very unlikely to appear in real chat text — unlike `":"` or `"\n"`, which user messages could plausibly contain, a plain `+`/`join` without a separator could otherwise let `("ab", "c")` and `("a", "bc")` hash identically at a role/content boundary).

### Why this approach
This is exactly what the review itself suggested ("hash the full older-message content instead") and what the function's own docstring already claimed to do — the fix brings the implementation in line with its stated contract rather than introducing a new caching strategy. Hashing role+content (not just content) also means a message that's flipped from `user` to `assistant` with identical text — an edge case, but a real one — doesn't accidentally collide either.

### Verification
Added [tests/test_memory_cache_key_collision.py](tests/test_memory_cache_key_collision.py) with three tests, the last of which exercises the actual observable bug (wrong summary served), not just the hash function in isolation:
- **Before the fix:**
  - Two conversations with the same length and the same last message produced identical hashes — failed immediately.
  - End-to-end: summarizing conversation A (about quantum computing) then conversation B (about French geography) — both ending in the same last message — returned conversation **A's** cached summary text for conversation B's call, without ever invoking the (fake) LLM a second time.
- **After the fix:** the two conversations hash differently, and each correctly triggers its own summarization call, returning its own distinct summary.
- A third test (passed both before and after, included as a sanity check, not a reproduction) confirms the *same* conversation still hashes identically across calls — the fix doesn't break legitimate cache hits.
- Full suite: 69 passed. `ruff` clean on both changed files.

### Explain it simply (interview answer)
To avoid re-summarizing the same conversation history with an expensive LLM call every single time, my code cached summaries — keyed by a "fingerprint" of the conversation being summarized. The bug: that fingerprint was way too sloppy. It was built from just two things — how *many* messages there were, and the first 50 characters of the *last* one — completely ignoring everything else that was actually said. Two completely different conversations that happened to be the same length and end the same way (a very plausible coincidence — lots of chats end with something like "thanks, can you also check X") would get treated as "the same conversation" and the second one would silently receive the *first* conversation's cached summary. Wrong information, served confidently, with no error at all.

**The fix:** build the fingerprint from everything that was actually said — every message's role and full content — not just a couple of cheap shortcuts.

**How I proved it:** I built two obviously-different conversations (one about quantum computing, one about French geography) that were deliberately the same length and shared an identical last message. Before the fix, summarizing the second one returned the *first* one's cached summary verbatim — a wrong answer served with total confidence. After the fix, each conversation gets its own correctly-computed summary.

---

## Logical Mistake #8: `CHUNK_OVERLAP=500` was configured but never applied

**Status:** Fixed 2026-06-25 — review's claim partially correct, partially wrong (see below)

### Symptom
`config.py` defines `CHUNK_OVERLAP: int = 500`, and the README documents it in three places ("`[Chunking] ── overlap 500 ──▶`", "`CHUNK_OVERLAP | 500 | Overlap between chunks`", "Elements are chunked with semantic boundaries and overlap"). But `ingestion.py`'s call to `chunk_by_title` never passed it — chunks were produced with zero overlap, contradicting the documented behavior.

### Root Cause — verified, with a correction to the review's own claim
The review states: *"`chunk_by_title` doesn't take an overlap arg in this code, so the README's 'overlap 500' is not actually applied."* I checked this against the actually-installed `unstructured==0.22.10` before touching anything (same discipline as the BUG-11 investigation earlier in this file), and the **literal claim is wrong**: `chunk_by_title`'s real signature includes both `overlap: Optional[int]` and `overlap_all: Optional[bool]` parameters (confirmed via `inspect.signature`). So the function absolutely can take an overlap argument — the review's stated *mechanism* doesn't hold.

The review's **conclusion is still correct**, just for a different reason. Reading `unstructured`'s own source (`unstructured/chunking/base.py`):
```python
@cached_property
def overlap(self) -> int:
    overlap_arg = self._kwargs.get("overlap")
    return overlap_arg or 0          # <- defaults to 0 when not passed

@cached_property
def inter_chunk_overlap(self) -> int:
    overlap_all_arg = self._kwargs.get("overlap_all")
    return self.overlap if overlap_all_arg else 0   # <- 0 unless overlap_all=True
```
`overlap` defaults to `0`, and — more importantly — even a nonzero `overlap` only applies to *mid-text splitting of a single oversized element* unless `overlap_all=True` is **also** passed, in which case it additionally applies between separate "normal" chunks formed from whole elements (the common case, and the one the README's diagram/table actually describes). `ingestion.py` passed neither. I confirmed this isn't just a documentation-reading exercise by running real elements through the real `chunk_by_title` both ways: without `overlap`/`overlap_all`, consecutive chunks share zero text; with `overlap=200, overlap_all=True`, the next chunk visibly starts with the previous chunk's trailing text.

So: the review correctly identified that overlap wasn't being applied and that the README was misleading — but for the wrong reason ("the function can't do it") rather than the right one ("the function can do it, but two specific arguments were never passed").

### Fix
[ingestion.py](src/components/ingestion.py) `build_langchain_documents` — added `overlap=self.config.CHUNK_OVERLAP, overlap_all=True` to the `chunk_by_title(...)` call for text elements. (`CHUNK_SIZE=3000` vs. `CHUNK_OVERLAP=500` comfortably satisfies the library's own validation that overlap must be less than `max_characters`.) Table and image chunks are unaffected — they're built one chunk per element with no `chunk_by_title` splitting involved, so "overlap between chunks" doesn't apply to them in this codebase.

### Why this approach
This is the minimal change that makes the code match behavior the README already (correctly) documents — no redesign of chunking, no new config, just passing through a config value that already existed for exactly this purpose. `overlap_all=True` is necessary, not optional window-dressing: passing `overlap` alone would have "fixed" only the rare case of a single element large enough to need mid-text splitting, leaving the common case (most chunk boundaries come from `chunk_by_title` deciding to start a new chunk between whole elements, not from splitting one oversized element) just as overlap-free as before.

### Verification
Added [tests/test_chunk_overlap_config.py](tests/test_chunk_overlap_config.py) with two tests:
1. **Wiring test** (the actual application-bug reproduction): mocks `chunk_by_title` as imported into `src.components.ingestion` and asserts the kwargs `build_langchain_documents` calls it with.
   - **Before the fix:** `kwargs.get("overlap")` was `None` — failed.
   - **After the fix:** `overlap == 500` (`config.CHUNK_OVERLAP`) and `overlap_all is True`.
2. **Library-semantics test** (not a red/green reproduction — a direct verification of real library behavior, the same kind of check that grounded the root-cause section above): runs real elements through the real `chunk_by_title` twice, once with no overlap args and once with `overlap=200, overlap_all=True`, and confirms shared text between consecutive chunks appears only in the second case. Required bypassing `conftest.py`'s `unstructured` stub (which exists to keep network-dependent `partition_*` modules from hanging on a sandboxed box — `chunk_by_title` needs neither, confirmed by timing a real import) — done by snapshotting and removing every `unstructured*` entry from `sys.modules`, importing fresh, then restoring the exact prior state so other tests in the session are unaffected. Confirmed this restore is clean by running the full suite (not just this file) afterward.

Full suite: 71 passed. `ruff` clean on both changed files.

### Explain it simply (interview answer)
The config had a setting, `CHUNK_OVERLAP = 500`, meant to make consecutive chunks of a document share a bit of trailing/leading text — so if an answer-relevant sentence happens to land right at a chunk boundary, it still shows up in full in at least one chunk instead of getting cut in half. The README described this feature. The code defined the setting. But the actual function call that does the chunking never passed that setting along — so every chunk boundary was a clean, zero-overlap cut, the whole time.

The original code review blamed this on the third-party chunking function "not supporting" overlap at all. I checked that claim against the actual library before accepting it, the same way I've checked every other claim in this review — and it's not quite right: the function *does* support overlap, there are just two separate settings for it (one for the rare case of a single huge piece of text getting split, one for the common case of two separate, normal chunks sitting next to each other), and the code was passing neither.

**The fix:** pass both. One is the actual character count (pulled from the config value that already existed), the other is a flag that says "yes, apply that overlap between normal chunks too, not just inside huge split-up ones."

**How I proved it:** I ran real text through the real chunking function twice — once exactly like the existing code (no overlap settings) and once with both new settings added — and checked whether the start of one chunk contained the end of the previous chunk's text. Without the settings: no overlap at all, confirming the bug. With both settings: real, visible overlap, confirming the fix actually does what the README always claimed it did. Then, separately, I mocked the chunking function so I could check the exact arguments the application's own code passes to it — proving the *fix*, not just the library feature, actually wires the configured value through.

---

## Latency Optimization #5: Embedding model rebuilt on every upload instead of once

**Status:** Fixed 2026-06-25

### Symptom
`EmbeddingManager.create_vector_store` ([embeddings.py](src/components/embeddings.py)) constructed a brand-new `OpenAIEmbeddings(...)` on every single call — and since `create_vector_store` runs once per file upload (via `RAGPipeline.ingest_file`), every upload paid the cost of spinning up a fresh embeddings client (and its underlying HTTP client) even though the model name and API key never change between calls.

### Root Cause
The embedding model was constructed inline inside the method body instead of once at the object's lifetime scope. `EmbeddingManager` itself is a long-lived singleton (one instance per `RAGPipeline`, which is itself cached as a singleton via `get_pipeline()`), so there was no reason for the model it depends on to be rebuilt more often than the manager itself.

### Fix
[embeddings.py](src/components/embeddings.py) — moved the `OpenAIEmbeddings(...)` construction into `EmbeddingManager.__init__`, stored as `self._embedding_model`. `create_vector_store` now reuses it (both on the normal path and the "nothing to embed" early-exit path, which previously built its own separate local copy too).

### Why this approach
This is the same fix shape the review asked for ("construct once and reuse") with no behavior change — same model, same key, same `PineconeVectorStore` usage downstream; only *when* the embeddings client gets built changes (once, at manager-construction time, instead of once per upload).

### Verification
Added [tests/test_embedding_model_reuse.py](tests/test_embedding_model_reuse.py):
- **Before the fix:** two `create_vector_store` calls (different namespaces) constructed 2 separate `OpenAIEmbeddings` instances; a third test confirmed the "no documents" early-exit path builds yet another separate instance.
- **After the fix:** both calls reuse the same instance — count stays at 1 regardless of how many times `create_vector_store` is called, including through the early-exit path.
- Full suite: 73 passed. `ruff` clean on both changed files.

### Explain it simply (interview answer)
Every time a user uploaded a file, my code built a brand-new connection to OpenAI's embedding API from scratch — even though it was always the exact same model, with the exact same API key, talking to the exact same place. That's like getting a new phone and dialing a fresh number every single time you call the same person, instead of just keeping their contact saved.

**The fix:** build that connection once, when the manager itself is created, and reuse it for every upload after that.

**How I proved it:** I replaced the real embedding-client class with a fake one that just counts how many times it gets constructed, then called the upload-embedding method twice. Before the fix: 2 separate instances. After the fix: 1, reused both times.

---

## Latency Optimization #6: Per-namespace RetrievalManager cache had no eviction

**Status:** Fixed 2026-06-25

### Symptom
`RAGPipeline._retrieval_managers` ([pipeline.py](src/pipeline/pipeline.py)) cached one `RetrievalManager` per namespace in a plain `dict`, populated by `_get_retrieval_manager` with no eviction of any kind. `RAGPipeline` itself is a long-lived singleton (`get_pipeline()`), so this dict lives for the entire process lifetime — every distinct namespace (user) that ever queries or uploads adds one more entry that's never removed. Each entry holds a `PineconeVectorStore` client, an `OpenAIEmbeddings` client, and — once hybrid search runs for that namespace — the namespace's full BM25 corpus in RAM.

### Root Cause
The cache was written as "create once, keep forever," with no bound on how many distinct namespaces it would accumulate. That's fine for a handful of users; it's an unbounded memory leak for an app meant to serve many users over a long-running process.

### Fix
[pipeline.py](src/pipeline/pipeline.py) — switched `_retrieval_managers` from a plain `dict` to a `collections.OrderedDict`, used as a simple LRU cache:
- A cache hit calls `move_to_end(namespace)` to mark it as recently used before returning it.
- A cache miss constructs a new `RetrievalManager` as before, then — if the cache now exceeds `config.MAX_CACHED_RETRIEVAL_MANAGERS` (new config field, default `100`) — evicts the least-recently-used entry via `popitem(last=False)`.

### Why this approach
`OrderedDict.move_to_end`/`popitem(last=False)` is the standard, minimal way to build an LRU cache without adding a new dependency (Python's `functools.lru_cache` doesn't fit here — this cache is keyed by a runtime string and needs explicit, manual invalidation semantics, not a decorator over a pure function). Evicting on every insert that crosses the bound (rather than batching evictions) keeps the cache size predictable at all times. No explicit cleanup of the evicted `RetrievalManager`'s resources is needed — `PineconeVectorStore`/`OpenAIEmbeddings` are lightweight SDK client wrappers with no persistent connections that require an explicit `.close()`; dropping the last reference is sufficient for normal garbage collection.

### Verification
Added [tests/test_retrieval_manager_cache_lru.py](tests/test_retrieval_manager_cache_lru.py), constructing a real `RAGPipeline` (safe — its `__init__` doesn't hit network) with `RetrievalManager` itself faked out (constructing the real class hits Pinecone immediately, same reasoning as the BUG-4/5 and namespace-guard tests) and a small `MAX_CACHED_RETRIEVAL_MANAGERS=2` bound for a fast, deterministic test:
- **Before the fix:** `Config(MAX_CACHED_RETRIEVAL_MANAGERS=2)` raised `TypeError: unexpected keyword argument` — the bound didn't exist as a concept at all.
- **After the fix:** populating 10 distinct namespaces never grows the cache past 2 entries; touching an older entry again protects it from eviction (the *other*, untouched entry is evicted instead, not whichever happens to be oldest by insertion order); repeated lookups for the same namespace reuse the identical cached instance without evicting anything.
- Full suite: 76 passed. `ruff`/`pyflakes` clean on all three changed files (one pre-existing, unrelated `E401` multiple-imports warning elsewhere in `pipeline.py`, confirmed via `git diff` to predate this change).

### Explain it simply (interview answer)
My app kept a "phone book" mapping each user to their own dedicated search engine instance, so it didn't have to rebuild one from scratch on every single question. The problem: nothing ever removed an entry from that phone book. Every new user who ever asked a question added one more permanent entry — for an app meant to run for weeks or months serving many different users, that phone book would just keep growing forever, slowly eating more and more memory with no ceiling.

**The fix:** cap the phone book at a fixed size (100 entries by default), and when it's full and a new user shows up, kick out whichever entry hasn't been used in the longest time — the same idea your browser uses to manage cached pages, or your phone uses to manage recently-used apps.

**How I proved it:** I set the cap artificially low (2) for a fast test, then simulated 10 different users showing up one after another and checked the phone book never grew past 2 entries. I also checked the *right* entry gets kicked out — if I "use" an old entry again right before a new one arrives, that freshly-touched entry survives and a genuinely-unused one gets evicted instead, proving it's really tracking recency of use, not just insertion order.

---

## Latency Optimization #7: Blocking Supabase/pipeline calls on the event loop, everywhere — plus two test-infrastructure bugs found while proving it

**Status:** Fixed 2026-06-25

### Symptom
`SupabaseManager`'s methods (`get_current_user`, `sign_up`, `sign_in`, `sign_out`, `upload_file`, `record_upload`, `get_user_documents`, `delete_file`, `delete_document_record`) and `RAGPipeline`'s `ingest_file`/`delete_document` are all plain, synchronous, network-bound methods. Every one of them was called directly — no `await`, no thread offload — from inside an `async def` FastAPI route or dependency:
- [dependencies.py](src/api/dependencies.py) `get_current_user` — the auth dependency that runs on **every** authenticated request across the whole API (chat, documents, evaluate, `/auth/me`, logout).
- [auth.py](src/api/router/auth.py) `signup`, `login`, `logout`.
- [documents.py](src/api/router/documents.py) `upload_document` (3 sequential blocking calls: `db.upload_file` → `pipeline.ingest_file` → `db.record_upload`, plus rollback paths), `list_documents`, `delete_document` (3 sequential calls: `db.delete_file` → `db.delete_document_record` → `pipeline.delete_document`).

Same bug class as BUG-3 (sync LLM calls blocking the event loop), just in the auth/database layer. `get_current_user` is the highest-impact instance — it blocks on every single authenticated request, regardless of which route it hits.

### Root Cause
Each of these methods does a real network round-trip (to Supabase's REST/Storage/Auth APIs, or — for the pipeline methods — to OpenAI/Pinecone), but none of them are `async`, and nothing wrapped the synchronous call to give the event loop a chance to run other work during that round-trip.

### Fix
Wrapped every call site listed above in `await asyncio.to_thread(...)` — the same pattern already used for BUG-3 (LLM calls) and BUG-15 (RAGAS evaluation). The underlying methods on `SupabaseManager`/`RAGPipeline` are unchanged (still plain sync methods); only the async call sites that invoke them now offload to a worker thread instead of running inline on the event loop.

### Why this approach
Matches the precedent already established twice in this codebase for the identical problem shape — wrap at the call site with `to_thread` rather than rewriting the underlying client classes as async (which would mean reimplementing or wrapping the entire Supabase SDK, a much larger and riskier change for the same outcome). Fixed every call site exhibiting the bug, not just the two examples the review named (`db.upload_file`, `db.get_current_user`) — the review's wording reads as representative examples of one systemic problem, and leaving the other ~10 call sites with the identical defect in place would have been an inconsistent half-fix.

### Verification
Added [tests/test_blocking_supabase_calls.py](tests/test_blocking_supabase_calls.py) — 13 tests, same concurrency-timing technique as BUG-3/BUG-15 (fake `db`/`pipeline` whose methods do a real blocking `time.sleep`, two calls/requests fired concurrently via `asyncio.gather`, wall time measured).

- **Before the fix:** all 7 initial route/dependency-level tests failed, each serializing to roughly `(blocking calls per request) × 2 × delay` — e.g. `upload_document` (3 sequential blocking calls) took ~1.22s for 2 concurrent requests instead of the expected ~0.6s.
- **A genuine test blind spot, caught before trusting the green:** `upload_document` and `delete_document` each make 3 *sequential* blocking calls per request. After fixing all three call sites in each route, I reverted just **one** of the three back to a bare blocking call (no `to_thread`) to check whether the existing combined "all 3 together" timing test would catch a partial regression — **it didn't.** It still passed, because three sequential steps where 2-of-3 are non-blocking land in a similar enough ballpark to all 3 being non-blocking that the timing threshold couldn't tell them apart. Restructured the test: added parametrized tests that isolate **one call site at a time** (only the call under test actually sleeps; the other two are instant), which correctly fails when that *specific* call is reverted and correctly passes when it isn't — confirmed by repeating the same revert-and-check against the new parametrized version, which failed exactly on the reverted call and passed on the other two.
- **A second, unrelated bug found while running the full suite:** adding this test file's ~8 requests to `/api/documents/upload` made three *other*, previously-passing test files (`test_path_traversal.py`, `test_upload_rollback.py`, `test_upload_size_limit.py`) start failing with `429 Too Many Requests`. The slowapi rate limiter's in-memory storage is one object shared by the entire test session (it lives on the module-level `app`/`limiter` singletons), so any test file that fires several requests at a rate-limited route permanently consumes part of that route's quota for every other test file that runs afterward, purely due to file run order. Fixed by adding an autouse fixture to [tests/conftest.py](tests/conftest.py) that resets `limiter._storage` before and after every test in the whole suite — confirmed this doesn't interfere with the two test files that deliberately exhaust a rate limit as their own test goal (`test_rate_limiting.py`, `test_evaluate_routes.py`), since each of those is self-contained regardless of the global state before/after it runs.
- **After all of the above:** full suite passes twice in a row (89 tests, checked for timing-test flakiness specifically), and the parametrized per-call-site tests correctly distinguish a real regression from a fixed call site.
- `ruff`/`pyflakes` clean on every changed file.

### Explain it simply (interview answer)
Several places in my code needed to ask Supabase (or, for two operations, the whole document pipeline) to do something over the network — log a user in, check if their token is valid, save a file, look up their documents. All of those were written as "wait right here until the network call finishes," but written *inside* code that's supposed to be async — meaning capable of juggling many users' requests at once by stepping away while waiting on slow things. A blocking call inside async code doesn't step away; it just freezes everything else, for everyone, for as long as that one network call takes. The worst offender was the function that checks "is this user actually logged in," because that runs on *every single request* to the API — so this one blocking call was secretly serializing the whole app's traffic, not just one feature.

**The fix:** hand each of these blocking calls off to a background thread (`asyncio.to_thread`) so the main async code can step away and let other requests make progress while it waits — the same fix I'd already applied once before to a similar problem with the AI model calls.

**How I proved it:** the same trick as before — fake versions of these calls that do a real, measurable pause, fired two requests at once, and timed it. But this time I caught two extra things worth mentioning in an interview. First: for routes that make *several* of these calls back-to-back, my first timing test wasn't sensitive enough — I proved this by deliberately breaking just one of three fixed calls and watching my own test still pass, which told me the test couldn't actually tell "all fixed" apart from "two-thirds fixed." I rewrote it to isolate and test each call individually until it could. Second: adding all these new test requests to the upload endpoint accidentally used up that endpoint's shared rate-limit budget for the rest of the test run, breaking three completely unrelated, previously-passing tests — a real lesson that test isolation matters for *shared global state* like a rate limiter's counters, not just for the specific feature you're testing. I fixed that at the source (reset the counter between every test) rather than just working around it in my own file.

---

## Latency Optimization #1, #2, #8, #9: already resolved — and #3, #4: tradeoffs, not bugs

**Status:** Audited 2026-06-25 — no further code change

Same discipline as everywhere else in this file: checked the actual current state of each item against the real code before doing anything, rather than treating the review's priority-ordered list as a checklist of guaranteed-open work.

### #1 (use async LLM calls) and #2 (re-rank once over the merged pool) — already fixed
Both are literally BUG-3 and BUG-6 above, fixed earlier this session. Re-verified directly against the current [generation.py](src/components/generation.py) (`generate`/`generate_stream`/`generate_multi_queries` all use `ainvoke`/`astream`) and [retrieval.py](src/components/retrieval.py)/[pipeline.py](src/pipeline/pipeline.py) (`retrieve_candidates()` + a single `rerank()` call over the full merged pool) — both confirmed still in place, nothing to do.

### #8 (streaming benefits negated by sync `stream()`) — resolved as a side effect of BUG-3
The review's own text ties this one directly to BUG-3: "the underlying sync `stream()` negates token-level streaming benefits **until BUG-3 is fixed**." BUG-3 *is* now fixed — `generate_stream` uses `async for chunk in self.chain.astream(...)`, confirmed by reading the current file — so the `X-Accel-Buffering: no` header set in [chat.py](src/api/router/chat.py)'s `StreamingResponse` now actually delivers on its purpose: tokens stream out incrementally as the (non-blocking) LLM call produces them, instead of the whole point of streaming being undermined by a blocking iterator underneath. No separate action needed — this is the same pattern as the SEC-1/SEC-5 entry below: an item that turned out to already be resolved by other work, recorded here for the record rather than silently dropped.

### #9 (citation verification runs synchronously post-stream) — confirmed fine, exactly as the review itself already noted (✔)
Re-verified directly: in `generate_stream`, `_verify_citations(full_answer, sources)` runs *after* the `async for chunk in self.chain.astream(...)` loop has fully completed — i.e., after every token has already been sent to the client — and is pure regex/string matching with no I/O and no LLM call, so it adds a small, fixed amount of CPU time after the user has already received their complete answer, never delaying token delivery. The review marked this "fine, off critical path ✔" and that holds up against the actual code; no fix needed.

### #3 (multi-query is 3 sequential LLM hops before the first token) — partially already done, rest is a tradeoff
- "Skip rewrite when chat_history is empty" — already done (the review marks this ✔ itself): `rewrite_query` returns the original query immediately if `chat_history` is falsy, before doing any LLM call.
- "Make multi-query optional/off by default" — `USE_MULTI_QUERY` is already a config flag (default `True`), so it's already optional today. Flipping the *default* to `False` is a product tradeoff (faster time-to-first-token vs. the recall benefit of searching several query reformulations), not a bug — there's no "correct" universal default, only a latency-vs-quality call that belongs to whoever owns that tradeoff for this app. Left as-is rather than silently changing default behavior.
- "Fuse rewrite+multi-query into one call" — would mean redesigning two separate prompts (different instructions, different output parsing) into a single combined LLM call with new prompt engineering and new output handling. That's a feature redesign, not a contained fix, and risks changing answer quality in ways that need real evaluation to validate — out of scope here per the same reasoning as every other "no redesigning beyond what's described" fix this session.

### #4 (cross-encoder cost — smaller reranker or optional) — already optional, model choice is a tradeoff
`USE_RERANKING` is already a config flag (default `True`) — already satisfies "making it optional." Swapping `RERANKER_MODEL` for a smaller model is a quality/latency tradeoff (a smaller cross-encoder ranks less accurately), not a bug — same reasoning as #3's multi-query default. Left as-is.

### Explain it simply (interview answer)
Going through this list, four of the nine items were either already fixed by earlier work in this same session, or were already true exactly as the original review itself said (it had already marked a couple of these "done" or "fine" with a checkmark, and I confirmed those checkmarks still hold against the real code rather than just trusting the mark). Two more (multi-query and the re-ranker) already have an on/off switch in the config — the review's literal ask ("make it optional") is already satisfied. What's left for those two is really a product question — "is it worth trading some answer quality for faster responses by default" — not an engineering bug with one obviously correct fix. I documented the tradeoff clearly instead of silently flipping a default that changes how good the app's answers are, the same way I handled a similar judgment call earlier in this file (SEC-3) by writing up the real choice instead of guessing at someone else's priorities.

---

## A1: Hybrid-search BM25 rebuild enumerated the namespace with an empty-string vector search

> From the independent re-audit in `PROJECT_AUDIT_AND_SLIMMING_PLAN.md` (post-CODE_REVIEW.md). New finding IDs use the `A-N` prefix.

### Symptom
The BM25 keyword index is (re)built lazily on the first hybrid query after any upload or delete. To "list everything in the namespace," `RetrievalManager._ensure_bm25_index` ran a *vector similarity search with an empty query string*: `self.vectorstore.similarity_search(query="", k=10_000, filter=None)`. If that call ever failed, the surrounding `try/except` swallowed the error, set `_bm25_retriever = None`, and hybrid search silently degraded to dense-only with only a single WARNING line to show for it.

### Root Cause
This is the exact "embed an empty string and abuse a ranked top-k search to enumerate vectors" anti-pattern that **BUG-7** already removed from `delete_document_by_filename` (which switched to `index.list(prefix=...)`). It survived here. Three real defects:
1. **Silent-degradation design** — the empty-string embedding hits OpenAI on every rebuild; if it ever errors, the `except` turns hybrid off without anyone noticing, and CI can't catch it because the tests mock the vector store (the same blind spot that originally hid BUG-1).
2. **10k truncation ceiling** — `similarity_search` returns at most `k` ranked results. `k=10_000` is fine for small namespaces, but a namespace that ever exceeds 10k chunks would silently feed BM25 only the 10k "closest" to a meaningless query vector.
3. **Wasted work** — it pays for a throwaway embedding call and ranks the whole namespace against it, only to discard the ordering.

**Honest correction to the audit.** PROJECT_AUDIT_AND_SLIMMING_PLAN.md flagged this **HIGH — "hybrid search is probably silently dead"**, on the theory that OpenAI rejects empty input. I checked that empirically against the installed stack: `OpenAIEmbeddings(model="text-embedding-3-small").embed_query("")` **returns a normal 1536-dim vector — it does not raise.** So hybrid is *not* dead today for namespaces under 10k chunks. The headline severity was wrong; the underlying defects are real but this is a robustness/consistency fix, not the five-alarm fire first claimed.

### Fix
Replaced the empty-string search with a real, paginated enumeration mirroring BUG-7. New `_list_all_documents()` helper:
- `index.list(namespace=...)` — pages through *every* vector ID in the namespace (no embedding, no `k` ceiling).
- `index.fetch(ids=batch, namespace=...)` in batches of 1000 — returns each vector's stored metadata.
- Reconstructs each `Document` exactly the way `PineconeVectorStore.similarity_search` does: the chunk text lives in metadata under `_text_key` (default `"text"`), so `page_content = metadata.pop("text")`.

`_ensure_bm25_index` now calls `self._list_all_documents()`; the dirty-flag retry semantics are unchanged.

### Why this approach
`index.list()` is a listing API, not a search — it can't truncate at `k` and needs no query vector, so it removes the 10k ceiling, the wasted embedding call, and the empty-string dependency all at once. Reusing the same Pinecone primitives BUG-7 already adopted means there's one mental model for "how do we enumerate this namespace," not two.

### Verification
- **Claim check first:** confirmed `embed_query("")` returns a vector (doesn't raise), which corrected the severity (see Root Cause).
- **Red:** rewrote `tests/test_bm25_lifecycle.py` so the fake store enumerates via `index.list()`+`fetch()` and its `similarity_search` *raises*. Against the unfixed code: 3 failed, and the captured log shows the real silent fallback firing — `BM25 index rebuild failed, using dense only this query`.
- **Green:** after the fix, that file → 3 passed (incl. a new test asserting `page_content` is reconstructed and the internal `text` key is stripped from metadata).
- **Full suite:** `pytest tests/` → **90 passed**. **Lint:** `ruff --select E,F,I` + `pyflakes` on changed files → clean.

### Explain it simply (interview answer)
To do keyword search I need a list of every document in the user's bucket. The old code got that list in a sneaky way: it asked the vector database "find me the 10,000 documents most similar to *nothing*" — searching with an empty question — and used whatever came back as "the list." That's the wrong tool: a search ranks and caps results, so it could quietly miss documents if there were ever more than 10,000, and it burned a paid call building a ranking it threw away. Worse, if that weird empty search ever errored, the code just shrugged and turned keyword search off without telling anyone. The database has a proper "list everything" button — I switched to that, the same fix we'd already made for deletes. I also tested the scary version of this — "does the empty search crash and silently kill keyword search?" — and found it doesn't actually crash today, so I corrected my own earlier over-statement. It was a real design smell, just not the emergency I first called it.

---

## A2: requirements.txt pinned the deprecated `pinecone-client` while the code runs on `pinecone`

### Symptom
`requirements.txt` pinned `pinecone-client==6.0.0`, but the running code (via `langchain-pinecone`) imports and uses `pinecone==7.3.0`. Both distributions were installed in the venv at once — they share the same `pinecone/` import namespace — so a fresh `pip install -r requirements.txt` on a clean machine would pull the *old, deprecated* client alongside the package the code is actually tested against. Classic "works on my machine" / demo-day reproducibility hazard.

### Root Cause
Pinecone renamed its PyPI distribution from `pinecone-client` to `pinecone`. `langchain-pinecone==0.2.13` depends on `pinecone[asyncio]>=6.0.0,<8.0.0` (the new name), which is why `pinecone 7.3.0` was present and doing the real work. The `pinecone-client==6.0.0` line was a stale leftover pin; inspecting installed metadata, the *only* thing that depended on `pinecone-client` was this project's own requirements. The deprecated dist was pure dead weight occupying the shared namespace. The same stale pin had also been baked into the committed, auto-generated `Documind.egg-info/requires.txt` — a build artifact that shouldn't have been tracked in git at all.

### Fix
- `requirements.txt`: `pinecone-client==6.0.0` → `pinecone==7.3.0` (explicit pin at the tested version; satisfies langchain-pinecone's `>=6,<8` range).
- Untracked the generated build metadata: `git rm -r --cached Documind.egg-info/` and added `*.egg-info/` to `.gitignore`, so the stale-pin contradiction can't be re-committed and setuptools regenerates it correctly from `requirements.txt` on each install.

### Why this approach
Pinning `pinecone==7.3.0` documents exactly the version the suite is green against rather than leaving it to the resolver, and it's inside langchain-pinecone's declared range so a clean install resolves without conflict. Untracking `egg-info` (instead of hand-editing it) fixes the root cause: it's a generated artifact whose source of truth is `requirements.txt` + `setup.py`. Regenerating it by hand produced 100+ lines of line-ending and file-listing churn for one meaningful line — the correct move is to stop tracking it.

### Verification
- **Compatibility:** `langchain-pinecone 0.2.13` requires `pinecone[asyncio]<8.0.0,>=6.0.0` — `7.3.0` satisfies it (no resolver conflict on a fresh install).
- **Reverse-deps:** enumerated every installed distribution's `Requires` — the only thing pulling `pinecone-client` was this project's own (now-fixed) pin; removing it fully eliminates the deprecated dist from a clean install.
- **Parse:** ran `setup.py`'s `get_requirements` logic over the edited file — `pinecone==7.3.0` parses (inline comment stripped), no `pinecone-client` remains, 27 requirements total.
- **Artifact:** `git check-ignore Documind.egg-info/requires.txt` → ignored; the five egg-info files show as removed-from-index in `git status`.
- **Regression:** runtime already imports `pinecone 7.3.0`; full suite stays at **90 passed** (packaging-only change, no code path altered).

### Explain it simply (interview answer)
The library I use to talk to the vector database got renamed — it used to be published as "pinecone-client," now it's just "pinecone." My code already used the new one, but my install recipe still asked for the old, retired one by name, so anyone installing fresh would download both packages stacked on top of each other — a recipe for "it runs for me but breaks for you." I changed the recipe to ask for the new package at the exact version I test against. I also found the old name had snuck into an auto-generated file I'd accidentally committed, so I told git to stop tracking that file — it's the kind of thing the computer rebuilds automatically, so it never belonged in version control.


## Test hermeticity: a developer's real `.env` leaked live secrets into the suite (live Cohere calls, LangSmith traces, real Redis)

### Symptom
On a machine with a real `.env`, `tests/test_cohere_rerank.py::test_rerank_without_client_falls_back_to_retrieval_order` failed: it asserts the no-Cohere-client path keeps retrieval order `[0, 1, 2]`, but got `[4, 2, 1]` — actual rankings from the **live** Cohere API. The same leak meant `LANGSMITH_*` and `REDIS_URL` were also live during the run (real trace emission, a real cache server). It passed in CI (no `.env` there) and failed only locally — a classic environment-dependent flake.

### Root Cause
`config.py` calls `load_dotenv()` at import. Run from a **git worktree**, python-dotenv's `find_dotenv` walks *up* the directory tree and finds the parent checkout's real `.env` (`E:\Desktop\DocuMind\.env`). The test command exports fake `OPENAI`/`PINECONE`/`SUPABASE` keys — and because `load_dotenv` defaults to `override=False`, those shell values win — but it does **not** set `COHERE_API_KEY` / `LANGSMITH_*` / `REDIS_URL`, so those leak in from the real `.env`. `Config`'s dataclass field defaults read `os.getenv(...)` at import, baking the real values in. In the failing test, `_make_rm` sets `_cohere_client=None` to exercise the "no key" path, but `_get_cohere_client()` then sees a truthy `COHERE_API_KEY`, builds a *real* client, and the test makes a live network call.

### Fix
`tests/conftest.py`: at module import (conftest is imported before any test module, hence before `config`), `os.environ.setdefault(...)` the leaked secrets to inert values — `COHERE_API_KEY=""`, `LANGSMITH_API_KEY=""`, `LANGSMITH_TRACING="false"`, `REDIS_URL=""`. `setdefault` (not assignment) so an explicit command-line value still wins for an intentional live run.

### Why this approach
The root cause is leaked *environment*, so the fix neutralizes the environment at the test boundary rather than patching each test that happens to trip over it. Doing it in `conftest` top-level (not a fixture) is what makes it work: `Config` reads the keys at import time, so the values must be pinned *before* the first test module imports `config` — a fixture would run too late. `setdefault` keeps live runs opt-in. The same four-line pin also stops the suite from emitting real LangSmith traces and touching a real Redis — the same class of bug, fixed once.

### Verification
- `test_cohere_rerank.py`: **4 passed** (was 1 failing — expected `[0, 1, 2]`, got live `[4, 2, 1]`).
- Confirmed the precedence: under the test run `Config().COHERE_API_KEY` now resolves to `""` (was a real 40-char key).
- **Pre-existing, not caused by the L4 change committed alongside:** the failure reproduces on the stashed (pre-L4) tree, so it's environmental.
- Full suite: **133 passed**; the only 2 failures are the documented wall-clock `does_not_block_event_loop` flakes, which pass **13/13 in isolation**.

### Explain it simply (interview answer)
My tests are meant to run sealed off, with fake keys. But the app reads a hidden settings file (`.env`) the moment it starts, and because my test folder lives *inside* the project, the test run reached up into the main project's real settings and grabbed my actual keys. One test was checking "what happens when the reranking service isn't configured?" — but with a real key present it quietly called the real service over the internet and got a real answer, so it failed, and only on my machine (the shared CI has no settings file). I made the tests blank out those few real keys before the app reads them, so the suite is sealed again: no surprise network calls, no real tracing, no accidental hits on a real database.

### Follow-up (2026-06-28): the same leak, now via a feature *flag* (`USE_HYBRID_SEARCH`)
The Part C `pydantic-settings` migration made **every** `Config` field env-overridable (not just secrets). So once native hybrid was enabled locally by setting `USE_HYBRID_SEARCH=true` in `.env`, that flag leaked into the suite the same way the secrets had — `test_embedding_model_reuse.py` started failing with `AttributeError: '_CountingOpenAIEmbeddings' object has no attribute 'embed_documents'`, because `create_vector_store` took the hybrid `_upsert_hybrid` path (which calls `embed_documents`) instead of the dense path the fake stubs. It failed only locally (CI has no `.env`) and only after hybrid was switched on. Fix: added `("USE_HYBRID_SEARCH", "false")` to the same conftest pin loop — feature flags need sealing too, not just keys. Tests that actually exercise hybrid set `Config(USE_HYBRID_SEARCH=...)` explicitly, so they're unaffected.


## Flaky timing tests: `_DELAY` left almost no absolute margin against OS scheduling jitter

### Symptom
`tests/test_blocking_supabase_calls.py::test_get_current_user_dependency_does_not_block_event_loop` and `::test_signup_does_not_block_event_loop` flaked under load this session (failed twice, passed every time run in isolation) — a classic wall-clock timing flake, not a real concurrency regression. Both tests fire two concurrent calls that each sleep `_DELAY` and assert the *combined* wall time stays under `_DELAY * 1.5` (proof the calls overlapped instead of serializing).

### Root Cause
`_DELAY` was `0.2`s. For these two tests specifically (`sequential_calls=1`, the default — the smallest of any test in the file), the pass/fail threshold is `0.2 * 1.5 = 0.3`s, leaving only `0.1`s of *absolute* slack between "truly concurrent" (~0.2s) and the threshold. OS thread-scheduling jitter under load (CPU contention, GC pauses, antivirus, another process stealing a core) is roughly a **constant additive** overhead, not proportional to the sleep length — so a 50-100ms jitter spike, which is unremarkable on a loaded machine, is enough to blow through a 100ms absolute margin even though the code path is genuinely non-blocking. The other tests in the file (`sequential_calls=3`, e.g. upload/delete) have a 3x larger absolute margin (`0.6 * 0.5 = 0.3`s) and were never reported as flaky — consistent with this theory.

### Fix
Raised `_DELAY` from `0.2` to `0.5`. The pass/fail *formula* is unchanged (`elapsed < expected_if_concurrent * 1.5`) — only the absolute scale grows, so the same 50-100ms jitter spike is now a much smaller fraction of the margin (`0.25`s instead of `0.1`s for the `sequential_calls=1` cases).

### Why this approach
The plan named two options: a looser threshold, or a deterministic concurrency check (e.g. a shared counter proving real overlap, no wall-clock at all). Raising the absolute delay is the smaller, more surgical fix — it keeps the test's existing logic and intent exactly as documented (and as every other test in the file already does), just gives it enough room to absorb realistic jitter. A deterministic rewrite would be the more bulletproof long-term fix but is a bigger, more invasive change to test infrastructure that's working correctly today, everywhere except under load on two of its eight cases — not justified by the size of the actual problem.

### Verification
Could not reliably reproduce the flake on demand (it's specifically load-dependent, and this sandbox was idle) — confirmed instead by reasoning about the root cause precisely (the four numbers above: 0.2s delay, 1.5x multiplier, 0.1s margin, vs. 3x more margin on the non-flaky cases) and re-running the affected file **3 times back-to-back** with the fix applied (28s/33s/30s, all green) to confirm the new margin doesn't break the legitimate pass case. Full suite: unaffected (same pass count, only this file's wall time grew by ~3s total across its 8 timing assertions).

### Explain it simply (interview answer)
Two of my tests prove a fix works by racing two slow calls and checking they finished in about the time of *one* slow call, not two — if they overlap (correct), it's fast; if one blocks the other (the bug), it's twice as slow. I'd set the "slow" delay short to keep the suite quick, but that left almost no cushion: on a busy machine, ordinary OS scheduling noise (a few hundredths of a second) was sometimes enough to nudge a perfectly correct run just over my pass/fail line. I made the artificial delay longer — same test, same logic, just a bigger clock to measure against — so that ordinary noise is a much smaller fraction of the margin and stops causing false alarms.


## Compliance judge default model didn't exist on Cerebras (`llama-3.3-70b` → 404 `model_not_found`)

**Status:** Fixed 2026-07-02

### Symptom
The very first *live* run of the KYC gap-analysis engine (against the synthetic RBI + policy PDFs) extracted **0 requirements** and produced an empty gap table. Every judge call logged:
```
HTTP/1.1 404 Not Found
{'message': 'Model llama-3.3-70b does not exist or you do not have access to it.', 'code': 'model_not_found'}
```
All mock tests were green — the failure only appeared against the real API.

### Root Cause
`Config.JUDGE_MODEL` defaulted to `"llama-3.3-70b"` (chosen in the pivot plan from Cerebras's public model list). That model is **not offered on the actual Cerebras account** behind the key. Querying the OpenAI-compatible `/v1/models` endpoint with the key returned only `gpt-oss-120b`, `zai-glm-4.7`, `gemma-4-31b` — no Llama at all. So the judge factory built a `ChatOpenAI(model="llama-3.3-70b", base_url="https://api.cerebras.ai/v1")` that 404s on the first call. The engine's per-chunk `try/except` swallowed it into "extraction failed on a chunk" warnings, so nothing crashed — it just silently produced nothing.

### Fix
Changed the default to `JUDGE_MODEL = "gpt-oss-120b"` — the strongest model actually available on the account (120B, cleanest JSON in json-mode of the three, confirmed with a live probe). Also updated the pinning unit test, the `judge.py` docstring, `.env.example`, and the plan doc. `JUDGE_MODEL`/`JUDGE_PROVIDER` stay env-overridable, so swapping to `zai-glm-4.7`, `gemma-4-31b`, or a paid provider is a one-line `.env` change.

### Why this approach
A provider's model catalog is account- and time-specific, so the honest fix is to default to a model this key actually has and keep it swappable, not to hard-code another guess. `gpt-oss-120b` matches the plan's intent (route the hard judging step to a strong model). The empirical `/v1/models` list is the source of truth — guessing another Llama alias would have risked the same 404.

### Verification
- Live re-run with `gpt-oss-120b`: extracted **15 requirements** and produced a fully cited gap table (5 Covered / 6 Partial / 4 Gap), zero "Needs review" rows; every Covered/Partial row's quote matched back to a real policy chunk.
- Probed all three available models with the judge's real JSON-mode call — all returned valid JSON and the correct verdict on a sample; `gpt-oss-120b` was cleanest.
- Full suite **184 passed**; ruff + pyflakes clean on the changed files.

### Explain it simply (interview answer)
My code was set up to ask a specific AI model to be the "judge," but I'd written down a name for it that isn't available on my account — so every request bounced back "no such model." My automated tests didn't catch it because they fake the AI's reply instead of actually calling it; the problem only showed up the first time I ran it for real. I asked the service "which models *can* I use?", picked the strongest one it actually offers, and made the name easy to change later. Now the same run that produced nothing produces a full, cited compliance report.


## Compliance citation verification was chunk-level substring matching — brittle in both directions

**Status:** Fixed 2026-07-04

### Symptom
The gap table's headline feature is "your policy clause vs the RBI clause, side by side, each cited." But the evidence citation was only ever verified by a whole-*chunk* substring test, which failed two ways:
1. **False negatives (real evidence dropped):** the judge quotes a policy sentence faithfully but with a benign edit — drops an "are", fixes a typo, the PDF extractor inserted a stray space — and the exact-substring match misses. A *correct* Covered/Partial verdict then gets silently downgraded to "Needs review", eroding trust in a tool whose whole job is to be trustworthy.
2. **Imprecise citation:** even when it matched, it returned the whole retrieved chunk's page and the UI showed the model's (possibly reworded) quote — not the verbatim source clause. A chunk is ~512 tokens = many clauses, so "your clause" wasn't pinned to an actual clause.

### Root Cause
`_match_quote_to_chunk` (compliance.py) normalised whitespace/case, then tested whether the quote — **or just its first 40 characters** — was a substring of a chunk's full text, returning that chunk's `(filename, page)`. That's binary and coarse: any edit inside the quote breaks the exact match (false negative), while the 40-char prefix fallback could *pass* a quote whose divergent tail was fabricated (false positive). It also had no notion of a clause — the smallest thing it could point at was a 512-token chunk.

### Fix
Replaced it with `_verify_evidence(quote, chunks)`, which grounds the quote to a specific **clause**:
- `_split_clauses` breaks each chunk on sentence/clause punctuation and newlines (short chunks stay one clause, so nothing regresses).
- `_containment_score` = `sum(difflib matching-block sizes) / len(quote)` — "how much of the quote is contiguously present in this clause", normalised by quote length so a short quote fully inside a long clause still scores 1.0.
- The best clause across all chunks wins; a citation (clause + filename + page) is returned **only** when the score clears `EVIDENCE_MATCH_THRESHOLD = 0.8`, so an ungrounded quote still yields no citation (the hallucination guard is preserved, now score-based). The verbatim clause and the raw `evidence_score` are carried on the `Verdict` and out through the API row (`policy_clause`, `evidence_score`, `evidence_verified`).

Also extended the compliance eval with an **evidence-faithfulness** metric (`evalution.evidence_faithfulness`): of the verdicts that assert a policy clause (Covered/Partial/Conflict — Gap correctly cites nothing and is excluded), the fraction whose quote actually grounded. Wired into `run_compliance_eval`'s summary and the `--check` regression gate.

### Why this approach
A graded containment score is the right middle ground: strict enough that a quote grounded in nothing scores near zero and stays flagged, loose enough that faithful reformatting no longer triggers a spurious "Needs review". `difflib` (stdlib) keeps it keyless and deterministic — no embeddings, no extra dependency, unit-testable without any live LLM. Clause-level granularity is what makes the side-by-side honest: we now show the *actual* source sentence, not a 512-token blob or the model's paraphrase. Honest scope note: a subtly doctored number inside an otherwise-verbatim quote (e.g. "three years" where the source says "five") still scores high — catching that is a *faithfulness* concern the eval metric measures across the set, not something a per-row substring check can reliably detect; the sharpened judge prompt is what guards the Conflict direction.

### Verification
- Red first: a faithful-but-reformatted quote (drops "are") was downgraded to "Needs review" under the old code (`AssertionError: 'Needs review' == 'Covered'`); green after.
- New unit tests (keyless, mocked judge): clause splitting, verbatim → score 1.0, clause pinpointing in a multi-sentence chunk, ungrounded quote → score < threshold → no citation, best-of-several-chunks, and the empty-quote path. Plus the `evidence_faithfulness` pure metric (excludes Gaps, penalises an ungrounded Covered, 1.0 when nothing bearing) and its harness wiring.
- Existing anti-hallucination test (`test_hallucinated_quote_on_covered_is_flagged_for_review`) still green — the guard is preserved.
- Full suite **215 passed**; ruff + pyflakes clean on the changed files.

### Explain it simply (interview answer)
My compliance tool shows "here's the exact sentence in your policy, and here's the rule it satisfies." To trust that, I check the AI's quoted sentence really exists in your document. The old check demanded a *character-perfect* match against a big page-sized block of text — so if the AI quoted your policy faithfully but tidied up one word, the check failed and wrongly stamped a correct finding "needs review"; and the best it could point at was a whole page-sized blob, not the one sentence. I changed it to find the single sentence that best matches the quote and score *how much* of the quote is actually in it: a real quote (even lightly reworded) scores high and gets cited to that exact sentence; a made-up quote scores near zero and gets flagged. I also added a benchmark that measures, across a labeled set, how often the cited evidence truly grounds — so I can prove the trustworthiness number, not just claim it.

---

## "Failed to fetch" on every API call when the app is opened via the machine's LAN IP

**Status:** Fixed 2026-07-15

### Symptom
Opening the Next.js app at `http://10.200.3.54:3000` (the machine's LAN IP instead of `localhost:3000`) and clicking **Create account** — or any action that calls the backend — fails instantly with `Failed to fetch`. The same flow works when the page is opened at `http://localhost:3000`.

### Root Cause
Two independent hardcodings of "localhost", either of which kills the request:
1. **Frontend:** `lib/api.ts` defaulted `API_BASE` to `http://localhost:8000`. The page's JavaScript runs in the *browser*, so from another device "localhost" is that device (nothing listening); even on the same PC the page origin `http://10.200.3.54:3000` calling `http://localhost:8000` is a cross-origin request…
2. **Backend:** …and the CORS allowlist (`CORS_ORIGINS`) contained only `http://localhost:3000` / `:8501`. The browser blocks the response and `fetch` surfaces the generic `TypeError: Failed to fetch`, which the login form renders verbatim.

A fixed allowlist can't solve this on its own: the LAN IP is DHCP-assigned and changes.

### Fix
- `frontend-next/lib/api.ts`: when `NEXT_PUBLIC_API_BASE` is unset, derive the API base from the page's own host — `http://${window.location.hostname}:8000` — so however you reach the frontend (localhost, LAN IP, Docker host), it calls the backend on that same host. The env var still overrides for real deployments.
- `Config.CORS_ORIGIN_REGEX` (new, default `None` = off) passed as `allow_origin_regex` to Starlette's CORSMiddleware — origins matching it are allowed *in addition to* `CORS_ORIGINS`. The dev `.env` sets it to a private-range pattern (10.x / 192.168.x / 172.16-31.x), so any DHCP address works without editing config again.
- Run the backend with `--host 0.0.0.0` (or `python -m src.api.main`, which already uses `API_HOST=0.0.0.0`) so it listens on the LAN interface, not just loopback.

### Why this approach
Same-host derivation removes the whole class of "the frontend guessed the wrong backend address" bugs rather than patching one IP. The regex is opt-in and scoped to private ranges: production behavior is unchanged unless the env var is set, and a public origin can never match the documented pattern.

### Verification
- `tests/test_cors_config.py`: regex default off; env round-trip; the documented pattern fullmatches `http://10.200.3.54:3000` and `http://192.168.1.7:3000` but not a public origin (Starlette matches with `re.fullmatch`); the app actually wires `allow_origin_regex` into CORSMiddleware. 5 passed.
- Frontend `tsc --noEmit` + `next build` clean.
- Live repro was the user's screenshot (signup at `10.200.3.54:3000` → "Failed to fetch"); localhost worked before and still does.

### Explain it simply (interview answer)
The web page and the API are two separate servers. The page had the API's address written into it as "localhost" — which means "this same device". As soon as the page was opened through the computer's network address instead, the browser either looked for the API on the wrong machine or refused the call because the API's guest-list of allowed websites only contained "localhost". I made the page ask for the API on whatever host the page itself was loaded from, and taught the API to accept requests from private network addresses in dev. One fix removes every future "works on localhost, breaks on the network" surprise.

---

## API error messages could render as "[object Object]" in the UI

**Status:** Fixed 2026-07-15 (found by the new Playwright E2E suite on its first run)

### Symptom
Signing up or logging in with an email the backend rejects (e.g. `user@localhost`, or any reserved test domain) showed the literal text **"[object Object]"** under the form — no hint of what went wrong. Any form in the app could do this for the same class of response.

### Root Cause
`errorDetail()` in `frontend-next/lib/api.ts` returned `data.detail` from the response as-is. For most backend errors `detail` is a clean string — but FastAPI's automatic **validation errors (HTTP 422) put an ARRAY of error objects in `detail`** (`[{loc, msg, type}, …]`). The auth routes validate `email: EmailStr`, so an address pydantic rejects never reaches the route's own clean error message; the 422 array flowed into `new Error(...)`, was stringified, and React rendered "[object Object]".

### Fix
`detailToText()` flattens whatever shape arrives: strings pass through; validation arrays become their joined human `msg`s (e.g. "value is not a valid email address…"); any other object is JSON-stringified; empty/unparseable falls back to the caller's message. Used by the shared `errorDetail()`, so every fetch helper (auth, uploads, checks, ask, suggest) benefits.

### Verification
- E2E red first: the signup and login specs failed with the page showing `paragraph: "[object Object]"` (Playwright page snapshot). Green after — the suite is 9/9, and the signup spec now permanently asserts the error text never matches `/\[object .*Object\]/`.
- `tsc --noEmit` + full Playwright suite pass.

### Explain it simply (interview answer)
When the server rejects a form, it sometimes replies with a structured list of validation problems instead of one sentence. The page assumed "the error is always a sentence" and printed the list object directly, which JavaScript renders as the useless text "[object Object]". I added a small translator that turns whatever the server sends — sentence, list, or object — into readable text, so the user always sees the actual reason.

---

## API startup died on TLS-intercepting networks while loading an already-cached embedding model

**Status:** Fixed 2026-07-15 (hit by the user's first `npm run e2e` on a network with HTTPS scanning)

### Symptom
`uvicorn` failed at startup — lifespan crashed with `SSL: CERTIFICATE_VERIFY_FAILED (self-signed certificate in certificate chain)` retries against `huggingface.co`, ending in `RuntimeError: Cannot send a request, as the client has been closed`. The embedding model (`all-mpnet-base-v2`, ~420MB) was fully downloaded and cached on the machine; the app still refused to boot. Same machine, different network (no interception): booted fine.

### Root Cause
`HuggingFaceEmbeddings(...)` → `SentenceTransformer(...)` performs online freshness/adapter-config probes against the Hugging Face Hub **even when the model is fully cached**. On networks that intercept TLS (antivirus "HTTPS scanning", corporate/campus proxies), Python's certifi bundle doesn't trust the interceptor's certificate, the probe fails, and huggingface_hub's retry path then trips over its own closed httpx client — the escaping `RuntimeError` bypasses the "connection errors fall back to cache" guard and kills the process. The app had a hard runtime dependency on huggingface.co reachability it never needed.

### Fix
New `embeddings.load_local_embeddings(config)` — used by BOTH `EmbeddingManager` and `RetrievalManager` (previously two duplicate constructions): try `model_kwargs={"local_files_only": True}` first (pure cache load, zero network), fall back to the normal downloading load only if the cache-only attempt fails (fresh machine). conftest's stub point collapses to the one module accordingly.

### Verification
- `tests/test_embeddings_offline_first.py`: cache hit → exactly one construction with `local_files_only=True`; simulated cache miss → falls back to a downloading construction; `EmbeddingManager` goes through the loader. Full suite **272 passed**, ruff clean.
- Real-model probe on this machine: `load_local_embeddings(Config())` + `embed_query` → **1.1s, no network requests** (previously multi-second with Hub HEAD requests — so this also shaves startup latency everywhere).

### Explain it simply (interview answer)
The AI model that turns text into vectors was already saved on disk, but the library still "called home" to check for updates every time it loaded. On my Wi-Fi that check happened to be blocked by security software that inspects secure traffic, so the whole backend refused to start — over a file it already had. I changed the loader to say "use the copy on disk, ask the internet only if you don't have it." The app now starts on any network, and a little faster too.
