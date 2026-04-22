import os, json, httpx, pathlib
from dotenv import load_dotenv
load_dotenv()

BASE = "http://localhost:8000"
EMAIL = "meetbarasara9@gmail.com"
PASSWORD = os.getenv("TEST_PASSWORD") or input(f"Password for {EMAIL}: ").strip()

# 1. Login
print("=== Step 1: Login ===")
r = httpx.post(f"{BASE}/api/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=15)
if r.status_code != 200:
    print("Login FAILED:", r.text)
    exit(1)
data = r.json()
token = data["access_token"]
print(f"Login OK  |  user_id: {data['user_id']}")
headers = {"Authorization": f"Bearer {token}"}

# 2. Upload a small text file
print("\n=== Step 2: Upload ===")
content = b"DocuMind is an AI-powered document intelligence platform. It uses RAG (Retrieval Augmented Generation) to answer questions about your uploaded documents accurately with source citations."
tmp = pathlib.Path("tmp_test_doc.txt")
tmp.write_bytes(content)

with open(tmp, "rb") as f:
    r2 = httpx.post(
        f"{BASE}/api/documents/upload",
        headers=headers,
        files={"file": ("test_doc.txt", f, "text/plain")},
        timeout=120,
    )
tmp.unlink(missing_ok=True)

print(f"Upload status: {r2.status_code}")
print(f"Upload body:\n{json.dumps(r2.json(), indent=2)}")

if r2.status_code != 201:
    exit(1)

# 3. List documents
print("\n=== Step 3: List documents ===")
r3 = httpx.get(f"{BASE}/api/documents/", headers=headers, timeout=15)
print(f"List status: {r3.status_code}")
docs = r3.json().get("documents", [])
print(f"Documents in DB: {len(docs)}")
for d in docs:
    print(f"  - {d['filename']}  ({d['file_type']}, {d['size_bytes']} bytes)")

# 4. RAG query
print("\n=== Step 4: RAG Query ===")
r4 = httpx.post(
    f"{BASE}/api/chat/query",
    headers=headers,
    json={"question": "What is DocuMind?", "chat_history": []},
    timeout=60,
)
print(f"Query status: {r4.status_code}")
if r4.status_code == 200:
    result = r4.json()
    print(f"Answer: {result['answer']}")
    print(f"Sources used: {result['num_sources_used']}")
else:
    print(r4.text)

print("\n=== ALL DONE ===")
