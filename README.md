# RecallKit

Persistent memory for AI agents. RecallKit is a small REST API that stores
facts, preferences and events per user in SQLite and returns them on demand, so
an agent can remember useful context between conversations.

This repository currently contains **Milestone 2**: full create, read, list,
keyword search, update and delete over SQLite, with every operation scoped to
one user. There is no authentication, semantic search, or LLM integration yet.

## Requirements

- Python 3.11 or newer (developed and tested on Python 3.14)
- Windows PowerShell (commands below), or the equivalent on macOS/Linux

No API keys are needed. Nothing in this milestone calls an external service.
Search is implemented with SQLite's built-in FTS5, with a `LIKE` fallback if
FTS5 is unavailable in your Python build.

## Setup (Windows PowerShell)

```powershell
Set-Location "D:\aiml related projects\recallkit"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

If PowerShell refuses to run `Activate.ps1` because of the execution policy,
you do not need to activate the environment at all. Call the interpreter
directly instead:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

## Run the API

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Then open <http://127.0.0.1:8000/docs> for interactive API documentation, or
check health:

```powershell
curl.exe http://127.0.0.1:8000/health
```

Expected response: `{"status":"ok","database":"ok"}`.

The SQLite file is created automatically at `data/recallkit.db` on startup.
Override the location by copying `.env.example` to `.env` and editing
`RECALLKIT_DATABASE_PATH`, or by setting that environment variable directly.
`.env` and everything under `data/` are git-ignored.

## Try it

PowerShell mangles JSON passed to `curl.exe` on the command line, so use
`Invoke-RestMethod` instead:

```powershell
$body = @{
    user_id = "user-a"
    content = "Prefers dark mode"
    kind    = "preference"
} | ConvertTo-Json

$created = Invoke-RestMethod -Method Post `
    -Uri http://127.0.0.1:8000/memories `
    -ContentType "application/json" `
    -Body $body

$created
```

The response contains an `id`. Read the memory back with that id:

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/memories/$($created.id)?user_id=user-a"
```

Reading with a different `user_id` returns `404`, exactly as if the memory did
not exist:

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/memories/$($created.id)?user_id=user-b"
```

If you prefer `curl.exe`, put the JSON in a file and pass `-d "@body.json"`;
embedding JSON directly in a PowerShell command line corrupts the quotes.

### List and search

```powershell
# Newest 20 memories for one user
Invoke-RestMethod "http://127.0.0.1:8000/memories?user_id=user-a"

# Filter by agent and kind, then page through the results
Invoke-RestMethod "http://127.0.0.1:8000/memories?user_id=user-a&agent_id=agent-1&kind=preference&limit=10&offset=0"

# Keyword search
Invoke-RestMethod "http://127.0.0.1:8000/memories?user_id=user-a&q=dark+mode"
```

The response is a page, not a bare list:

```json
{
  "items": [ { "id": "...", "content": "Prefers dark mode", "...": "..." } ],
  "total": 42,
  "limit": 20,
  "offset": 0
}
```

`total` counts every match ignoring paging, so `offset + limit < total` means
there is another page. `limit` is 1-100 (default 20) and `offset` must be zero
or greater.

Search rules:

- Every word must appear somewhere in the memory (`q=dark+mode` needs both).
- Words match as prefixes, so `q=depl` finds "Deploys".
- Case and diacritics are ignored, so `q=cafe` finds "Café".
- Only the requesting user's memories are searched.
- Results are ordered newest first, not by relevance; ranking comes later.

### Update a memory

Send only the fields you want to change:

```powershell
$patch = @{ content = "Prefers dark mode, never light." } | ConvertTo-Json
Invoke-RestMethod -Method Patch `
    -Uri "http://127.0.0.1:8000/memories/$($created.id)?user_id=user-a" `
    -ContentType "application/json" `
    -Body $patch
```

`updated_at` is refreshed on every successful update; `created_at` never
changes. An empty body, a `null` field, or an unknown field returns `422`.
The search index is updated in the same transaction, so the new text is
searchable immediately.

### Delete a memory

```powershell
Invoke-RestMethod -Method Delete `
    -Uri "http://127.0.0.1:8000/memories/$($created.id)?user_id=user-a"
```

Deletion is permanent and returns `204 No Content`. Deleting the same memory
again returns `404`. Deleting or changing another user's memory also returns
`404`, and leaves their data untouched.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Every test runs against a fresh SQLite file inside pytest's temporary
directory, so the development database is never touched. Coverage includes
creation, retrieval, listing, filters, ordering, pagination, keyword search
(both FTS5 and the LIKE fallback), search-input safety, update semantics,
deletion, and cross-user isolation.

Lint and format checks:

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

## Project layout

```
recallkit/
├── app/
│   ├── main.py                    # create_app() factory, lifespan, GET /health
│   ├── config.py                  # Settings from RECALLKIT_* env vars / .env
│   ├── db.py                      # SQLite connections + schema initialization
│   ├── models.py                  # Pydantic request/response schemas
│   ├── api/
│   │   ├── deps.py                # dependency wiring (settings, connection, service)
│   │   └── memories.py            # all /memories routes
│   ├── repositories/
│   │   ├── memories.py            # all SQL for memories
│   │   └── search.py              # safe FTS5 / LIKE query builders
│   ├── services/memory_service.py # IDs, timestamps, not-found rule
│   └── llm/                       # provider interface + offline fake (unused for now)
├── schema.sql                     # memories table, indexes, FTS5 index, triggers
├── tests/                         # pytest suite
├── requirements.txt               # pinned runtime dependencies
├── requirements-dev.txt           # pinned test dependencies
└── pyproject.toml                 # pytest and ruff configuration
```

The layers exist to keep each change small:

- **Routes** translate HTTP to Python and back. No SQL.
- **Service** holds rules that are neither: generating IDs and timestamps now,
  calling an LLM later.
- **Repository** is the only place that writes SQL. Every read filters by
  `user_id` in the `WHERE` clause, which is what makes cross-user access
  impossible rather than merely discouraged.
- **Database** hands each request its own short-lived SQLite connection.

## API

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/health` | `{"status":"ok","database":"ok"}`, or `503` if storage is broken |
| `POST` | `/memories` | Store a memory. Body: `user_id`, `content`, optional `agent_id`, optional `kind` |
| `GET` | `/memories` | List one page. Query: `user_id` (required), `agent_id`, `kind`, `q`, `limit`, `offset` |
| `GET` | `/memories/{id}?user_id=...` | Read one memory owned by that user, else `404` |
| `PATCH` | `/memories/{id}?user_id=...` | Change `content` and/or `kind`, else `404` |
| `DELETE` | `/memories/{id}?user_id=...` | Permanently delete; `204` on success, `404` otherwise |

`kind` is one of `fact`, `preference`, `event`, `task` (default `fact`).

### Why `user_id` is a query parameter

There is no authentication yet, so the caller states which user it is acting
for. That is a deliberate placeholder: when authentication arrives, `user_id`
will come from a verified token instead of the query string. The scoping rule
in the repository stays the same either way.

## Data model

```sql
CREATE TABLE memories (
    id         TEXT PRIMARY KEY,   -- uuid4 hex, generated in the service
    user_id    TEXT NOT NULL,      -- owner, always required
    agent_id   TEXT,               -- optional agent that recorded it
    content    TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'fact',
    created_at TEXT NOT NULL,      -- ISO-8601 UTC
    updated_at TEXT NOT NULL
);
```

Search uses a standalone FTS5 table plus three triggers:

```sql
CREATE VIRTUAL TABLE memories_fts USING fts5(
    memory_id UNINDEXED,
    content,
    tokenize = 'unicode61 remove_diacritics 2'
);
```

`memories_fts` stores its own copy of the content. That costs a little disk
space but keeps the schema simple: no `content=` bookkeeping, and the index is
maintained by triggers, so it cannot drift from the table even if a row is
edited by hand with the `sqlite3` shell. On startup a backfill statement
indexes any rows the index is missing, which is what makes enabling search on
an existing database safe.

### How search stays safe

User text is never concatenated into SQL. `app/repositories/search.py` splits
the query into word tokens and builds the FTS5 expression by wrapping each
token in double quotes and appending `*` for prefix matching, so characters
such as `"`, `*`, `:`, `-`, `^` and `NEAR(` are treated as literal text rather
than operators. The LIKE fallback escapes `\`, `%` and `_` and passes the
pattern as a bound parameter with `ESCAPE '\'`. Every query still has
`m.user_id = ?` in its `WHERE` clause.

## Next milestone (not implemented)

Authentication, then semantic retrieval behind the existing `LLMProvider`
interface. Listing, searching, updating and deleting are complete.
