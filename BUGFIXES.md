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
