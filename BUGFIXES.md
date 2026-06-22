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
