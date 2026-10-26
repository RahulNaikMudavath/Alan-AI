# RecallKit

Persistent memory for AI agents. RecallKit is a small REST API that stores
facts, preferences and events per user in SQLite and returns them on demand, so
an agent can remember useful context between conversations.

This repository currently contains **Milestone 3.2**: create, read, list,
keyword search, update and delete, retrieval by meaning (semantic and hybrid),
and conversation ingestion that extracts memories through a replaceable model
provider. Every operation is scoped to one user. There is no authentication,
conflict supersession, or frontend yet.

## Requirements

- Python 3.11 or newer (developed and tested on Python 3.14)
- Windows PowerShell (commands below), or the equivalent on macOS/Linux

No API keys are needed. Both model-backed features default to deterministic
offline fakes — embeddings for retrieval, and a rule-based extractor for
conversation ingestion — so the app, the CLI, and the entire test suite run with
no network access and no cost. Keyword search uses SQLite's built-in FTS5, with a
`LIKE` fallback if FTS5 is unavailable in your Python build.

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

## Embedding providers

Semantic search needs vectors, so creating or editing a memory also embeds it.
Choose the provider with `RECALLKIT_EMBEDDING_PROVIDER`:

| Value | What it does | Cost | Use it for |
| --- | --- | --- | --- |
| `fake` (default) | Hashes words into a fixed-size vector | Free, offline | Development, tests, demos |
| `openai_compatible` | Calls any `POST /v1/embeddings` endpoint | Provider's price per token | Real retrieval quality |
| `none` | No vectors; semantic search reports `degraded` | Free | Keyword-only deployments |

```powershell
# Real embeddings. The key comes from the environment, never from source.
$env:RECALLKIT_EMBEDDING_PROVIDER = "openai_compatible"
$env:RECALLKIT_EMBEDDING_API_KEY  = "<your key>"
$env:RECALLKIT_EMBEDDING_BASE_URL = "https://api.openai.com/v1"
$env:RECALLKIT_EMBEDDING_MODEL    = "text-embedding-3-small"
$env:RECALLKIT_EMBEDDING_DIMENSIONS = "1536"
```

Any compatible endpoint works: Azure OpenAI, a local Ollama or vLLM server, or
an internal gateway. Only `base_url`, `model`, and `dimensions` change. If the
provider is set to `openai_compatible` without a key, startup fails with a
message naming the missing variable.

The key is read into a `SecretStr`, sent only as an `Authorization` header, and
never logged or included in an exception message. Put it in `.env` (git-ignored)
or your shell environment. Never commit it.

### What the fake provider does and does not do

The fake is a **bag-of-words hasher**. It scores shared words (and adjacent word
pairs), so `"dark mode"` is close to `"prefers dark mode"`. It has no notion of
synonyms: `"diet"` is not close to `"vegetarian"`.

That is deliberate. It makes the plumbing deterministic and free to test:
ranking, fusion, isolation, coverage, and degradation are all verified exactly.
It proves **no semantic understanding**, and this project does not claim any.
Real retrieval quality requires a real embedding model; the limits section
below explains how little the fake can be trusted for that.

## Semantic and hybrid retrieval

`POST /memories/search` is the relevance-ranked endpoint. `GET /memories?q=`
is unchanged and remains newest-first.

```powershell
$search = @{
    user_id = "user-a"
    query   = "what does this person eat?"
    mode    = "hybrid"      # keyword | semantic | hybrid
    limit   = 5
} | ConvertTo-Json

Invoke-RestMethod -Method Post `
    -Uri http://127.0.0.1:8000/memories/search `
    -ContentType "application/json" `
    -Body $search
```

```json
{
  "mode": "hybrid",
  "query": "what does this person eat?",
  "degraded": false,
  "coverage": { "embedded": 12, "considered": 13 },
  "results": [
    {
      "memory": { "id": "...", "content": "Is vegetarian", "kind": "fact", "...": "..." },
      "score": 0.0325,
      "similarity": 0.71,
      "matched_by": ["keyword", "semantic"]
    }
  ]
}
```

How to read the response:

- **`matched_by`** says which signals found the memory: FTS5 keywords, cosine
  similarity, or both.
- **`similarity`** is the raw cosine score when the semantic signal matched, in
  `[-1, 1]`. `null` means the memory was never compared as a vector.
- **`score`** is a fused rank score, **relative within this response only**. It
  is not a probability and is not comparable between requests.
- **`degraded: true`** means semantic scoring was requested but unavailable
  (no provider, or the provider failed), so results come from keyword matching
  only. The app never silently pretends otherwise.
- **`coverage`** reports how many of the user's scoped memories have a vector
  for the active model versus how many exist. It is the honest measure of what
  semantic search could see.

Modes:

| Mode | Behaviour |
| --- | --- |
| `keyword` | FTS5 only, ordered by bm25 relevance. Prefix matching still applies. |
| `semantic` | Cosine similarity only, above `RECALLKIT_SEMANTIC_MIN_SIMILARITY`. |
| `hybrid` (default) | Reciprocal Rank Fusion of both lists, plus a recency signal. |

**Why rank fusion.** A cosine similarity is bounded and positive-ish; bm25 is
unbounded and negative. Adding them requires inventing calibration constants.
Reciprocal Rank Fusion uses only positions — `weight / (60 + rank)` per list —
so it needs no training and no tuning to behave sanely. Recency can only
reorder memories that a real signal already matched; it never invents a hit.

Every mode applies `user_id`, `agent_id`, and `kind` in the SQL `WHERE` clause,
so a filter cannot be bypassed by switching modes.

### Backfilling existing memories

Memories written before this milestone have no vectors. Index them with the
CLI, which is safe to rerun:

```powershell
.\.venv\Scripts\python.exe -m app.cli backfill-embeddings
.\.venv\Scripts\python.exe -m app.cli backfill-embeddings --user-id user-a --batch-size 25
```

It only adds vectors for the configured model where none exist. It never
overwrites an existing vector and never touches vectors belonging to a
different model, so switching models later cannot corrupt the old data. Exit
code `0` means nothing failed, `1` means at least one memory could not be
embedded, `2` means embeddings are disabled.

While a backfill is pending, semantic results are simply incomplete — `coverage`
shows exactly how incomplete.

## Conversation ingestion

`POST /conversations` accepts a conversation, asks the configured model to
extract durable facts, and stores them through the same memory service as
everything else.

```powershell
$body = @{
    user_id         = "user-a"
    conversation_id = "conv-2026-10-26-001"
    agent_id        = "support-bot"
    messages        = @(
        @{ role = "user";      content = "I'm vegetarian and I deploy on Fridays." },
        @{ role = "assistant"; content = "Noted." }
    )
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Method Post `
    -Uri http://127.0.0.1:8000/conversations `
    -ContentType "application/json" `
    -Body $body
```

```json
{
  "conversation_id": "conv-2026-10-26-001",
  "replayed": false,
  "messages_received": 2,
  "memories_created": [
    { "id": "…", "user_id": "user-a", "agent_id": "support-bot",
      "content": "I'm vegetarian", "kind": "fact", "...": "..." }
  ],
  "memories_skipped": [
    { "content": "Deploys on Fridays", "kind": "event",
      "reason": "duplicate_existing", "source_message_indexes": [0] }
  ],
  "extractor": { "provider": "fake", "model": "recallkit-rule-based-v1",
                 "prompt_version": "extract-v1" }
}
```

`source_message_indexes` are 0-based positions in the submitted conversation and
are stored in `memory_sources`, so every extracted memory can be traced back to
the message that produced it.

### Idempotency

`conversation_id` is client-supplied and scoped by `user_id`, so two users can use
the same value independently. The request payload is hashed and stored with the
receipt:

| Request | Result |
| --- | --- |
| New `conversation_id` | `201 Created`, provider called once |
| Same id, **identical** payload | `200 OK`, `replayed: true`, provider **not** called |
| Same id, **different** payload | `409 Conflict`, nothing written, provider **not** called |

A replay returns the stored receipt exactly as it was issued. If a memory has
since been edited or deleted, the receipt still describes what that ingestion
produced — it is a record, not a live view.

### Duplicate detection

Facts are compared using a documented canonical form:

1. **Unicode** NFKC, so `ＤＡＲＫ` and `DARK` are the same text.
2. **Case** folding, so `STRAßE` and `strasse` match.
3. **Punctuation** replaced by spaces, so `"Dark mode!"` and `"dark mode"` match.
4. **Whitespace** runs collapsed and trimmed, including tabs, newlines, and
   non-breaking spaces.

It deliberately does **not** remove diacritics (`café` and `cafe` stay distinct)
and it does not understand meaning (`vegetarian` and `plant-based` are different).
Known trade-offs: `C#` collides with `C` because `#` is punctuation, while `C++`
does not; `don't` becomes `don t` and differs from `dont`.

A duplicate is never stored. It is reported in `memories_skipped` with one of:

| Reason | Meaning |
| --- | --- |
| `duplicate_existing` | Same canonical content already stored for this user |
| `duplicate_in_request` | Repeated within the model's own output |
| `low_confidence` | Below `RECALLKIT_MIN_FACT_CONFIDENCE` |
| `too_long` | Longer than `RECALLKIT_MAX_FACT_CHARS` |
| `invalid_fact` | Failed per-fact validation (wrong kind, blank, out of range) |
| `limit_exceeded` | Beyond `RECALLKIT_MAX_FACTS_PER_CONVERSATION` |

### Limits and failure behaviour

| Situation | Response |
| --- | --- |
| More messages, or more characters, than configured | `422`, provider never called |
| Extraction disabled (`RECALLKIT_LLM_PROVIDER=none`) and the conversation is new | `503` |
| Provider error | `502` |
| Output is not JSON, or not the expected top-level shape | `502`, nothing written |
| One bad fact inside otherwise valid output | `201`, that fact reported as `invalid_fact` |

A stored receipt can still be replayed with extraction disabled: replaying costs
nothing and needs no provider. Only new conversations return `503`.

The conversation receipt, its memories, and its provenance are committed in one
transaction. A failure at any point leaves no partial conversation. Embeddings
are generated after that commit, best effort: a memory is never rolled back
because an embedding provider was unavailable, and search coverage shows the gap.

### Raw message storage

`RECALLKIT_STORE_MESSAGES` is `false` by default. When disabled, only extracted
facts and message indexes are written; the conversation text is not persisted.
Set it to `true` if you need the raw conversation for audit or re-extraction, and
treat the database as sensitive.

### Configuring the extractor

| Variable | Default | Purpose |
| --- | --- | --- |
| `RECALLKIT_LLM_PROVIDER` | `fake` | `fake`, `openai_compatible`, or `none` |
| `RECALLKIT_LLM_MODEL` | provider default | Recorded in every receipt |
| `RECALLKIT_LLM_BASE_URL` | `https://api.openai.com/v1` | Any compatible endpoint |
| `RECALLKIT_LLM_API_KEY` | — | Env only, never logged |
| `RECALLKIT_MAX_MESSAGES_PER_CONVERSATION` | `50` | Request-size guard |
| `RECALLKIT_MAX_MESSAGE_CHARS` | `8000` | Request-size guard |
| `RECALLKIT_MAX_CONVERSATION_CHARS` | `40000` | Request-size guard |
| `RECALLKIT_MAX_FACTS_PER_CONVERSATION` | `10` | Quality cap |
| `RECALLKIT_MAX_FACT_CHARS` | `500` | Quality cap |
| `RECALLKIT_MIN_FACT_CONFIDENCE` | `0.5` | Quality floor |

The fake extractor is a **rule-based heuristic**, not a model: it keeps
first-person sentences from user messages and labels them with a few cue words.
It exists so the pipeline runs offline and so tests can pin exact behaviour.
Real extraction quality requires `openai_compatible`.

Conversation text is untrusted input. The prompt marks it as data and instructs
the model to ignore instructions inside it, but the real protection is
downstream: only a narrow typed schema can become a memory, and nothing extracted
is ever executed.

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

Search rules for `GET /memories?q=`:

- Every word must appear somewhere in the memory (`q=dark+mode` needs both).
- Words match as prefixes, so `q=depl` finds "Deploys".
- Case and diacritics are ignored, so `q=cafe` finds "Café".
- Only the requesting user's memories are searched.
- Results stay newest first. For relevance ranking, use
  `POST /memories/search`, documented above.

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
The keyword index is updated in the same transaction, so the new text is
searchable immediately. A content change also re-embeds the memory; if the
embedding provider is down, the update still succeeds, the old vector is
deleted, and search coverage reports the gap rather than returning stale text.

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
deletion, cross-user isolation, vector maths, embedding lifecycle, backfill,
degradation, database upgrades, conversation ingestion, extraction parsing,
duplicate canonicalisation, idempotency replays and conflicts, provenance,
request limits, provider failures, and transactional rollback.

**The suite is offline by construction.** An autouse fixture in
`tests/conftest.py` replaces `socket.create_connection` with a guard that fails
the test, so any accidental outbound connection is caught rather than silently
reaching a provider. `tests/test_offline.py` proves the guard is armed and drives
both retrieval and ingestion while it is.

Deterministic tests use the fake providers. They verify plumbing and ordering,
not semantic or extraction quality — see the notes above.

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
│   ├── db.py                      # connections, FK enforcement, schema init
│   ├── models.py                  # Pydantic request/response schemas
│   ├── cli.py                     # backfill-embeddings maintenance command
│   ├── api/
│   │   ├── deps.py                # dependency wiring
│   │   ├── memories.py            # /memories CRUD routes
│   │   ├── search.py              # POST /memories/search
│   │   └── conversations.py       # POST /conversations
│   ├── repositories/
│   │   ├── memories.py            # SQL for memories, incl. bm25 ranking
│   │   ├── embeddings.py          # SQL for vectors
│   │   ├── conversations.py       # receipts, provenance, optional messages
│   │   ├── transactions.py        # BEGIN IMMEDIATE / rollback helper
│   │   └── search.py              # safe FTS5 / LIKE query builders
│   ├── retrieval/vectors.py       # float32 packing and cosine similarity
│   ├── services/
│   │   ├── memory_service.py      # IDs, timestamps, re-indexing rules
│   │   ├── embedding_index.py     # best-effort vector maintenance
│   │   ├── search_service.py      # modes, fusion, coverage, degradation
│   │   ├── canonical.py           # duplicate + payload canonicalisation
│   │   ├── extraction.py          # prompt building and output parsing
│   │   └── conversation_service.py# ingestion orchestration and atomic writes
│   └── llm/
│       ├── base.py                # LLMProvider + EmbeddingProvider Protocols
│       ├── fake.py                # offline deterministic providers
│       ├── prompt_contract.py     # prompt marker shared with the fake
│       ├── openai_compatible.py   # /v1/embeddings and /v1/chat/completions
│       └── factory.py             # provider construction from settings
├── schema.sql                     # memories, FTS5, embeddings, conversations
├── tests/                         # pytest suite (offline)
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
| `POST` | `/memories/search` | Ranked retrieval. Body: `user_id`, `query`, `mode`, optional `agent_id`, `kind`, `limit` |
| `POST` | `/conversations` | Extract memories from messages. Body: `user_id`, `conversation_id`, `messages`, optional `agent_id` |
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

Conversation ingestion adds three tables:

```sql
CREATE TABLE conversations (
    user_id          TEXT NOT NULL,
    conversation_id  TEXT NOT NULL,
    agent_id         TEXT,
    message_count    INTEGER NOT NULL,
    memories_created INTEGER NOT NULL,
    memories_skipped INTEGER NOT NULL,
    extractor        TEXT NOT NULL,
    payload_hash     TEXT NOT NULL,   -- idempotency comparison
    result_json      TEXT NOT NULL,   -- receipt returned on replay
    created_at       TEXT NOT NULL,
    PRIMARY KEY (user_id, conversation_id)
);

CREATE TABLE memory_sources (
    memory_id       TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    user_id         TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    message_indexes TEXT NOT NULL,   -- JSON array of 0-based positions
    created_at      TEXT NOT NULL,
    PRIMARY KEY (memory_id, conversation_id)
);

CREATE TABLE conversation_messages (  -- only written when explicitly enabled
    user_id         TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    position        INTEGER NOT NULL,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    PRIMARY KEY (user_id, conversation_id, position)
);
```

`(user_id, conversation_id)` is both the ownership key and the idempotency key.
`result_json` duplicates memory content on purpose: it lets a replay return the
original receipt without re-reading rows that may have changed since.

```sql
CREATE TABLE memory_embeddings (
    memory_id  TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    model      TEXT NOT NULL,   -- vectors from different models are never compared
    dimensions INTEGER NOT NULL,
    vector     BLOB NOT NULL,   -- little-endian float32, L2-normalised
    created_at TEXT NOT NULL,
    PRIMARY KEY (memory_id, model)
);
```

Vectors live in their own table so switching embedding models never touches
`memories` and never mixes incompatible vectors: retrieval only reads rows for
the configured model. The foreign key cascades, which is why
`Database.connect()` enables `PRAGMA foreign_keys` on every connection — SQLite
defaults to off. Deleting a memory therefore deletes its vectors atomically.

### How semantic search runs

1. The query text is embedded once, with the same model used at write time.
2. That user's vectors are loaded with `agent_id`/`kind` filters applied in SQL.
3. Cosine similarity is computed in Python over the candidate set (bounded by
   `RECALLKIT_SEMANTIC_MAX_CANDIDATES`), unpacking float32 blobs.
4. Candidates below `RECALLKIT_SEMANTIC_MIN_SIMILARITY` are dropped and the
   rest are ranked.

It is a brute-force scan, deliberately. At a few thousand memories per user it
takes milliseconds and needs no extra infrastructure. `numpy` or `sqlite-vec`
are the upgrade path once measurements justify them.

### How search stays safe

User text is never concatenated into SQL. `app/repositories/search.py` splits
the query into word tokens and builds the FTS5 expression by wrapping each
token in double quotes and appending `*` for prefix matching, so characters
such as `"`, `*`, `:`, `-`, `^` and `NEAR(` are treated as literal text rather
than operators. The LIKE fallback escapes `\`, `%` and `_` and passes the
pattern as a bound parameter with `ESCAPE '\'`. Every query — keyword or
vector — still has `m.user_id = ?` in its `WHERE` clause.

## Limitations

- **The fake provider has no semantic understanding.** It measures word overlap.
  Deterministic tests prove plumbing, not retrieval quality. Do not judge
  "does it understand meaning?" from a fake-provider result.
- **No evaluation harness yet.** There is no labeled dataset or recall@k report;
  that was planned for a later stage. Tuning fusion weights without it is
  guesswork.
- **Brute-force vectors.** Scanning is capped and linear in the number of
  memories per user. Past the cap, only the newest memories are considered.
- **Corrupt vectors are invisible, not fatal.** A row whose stored dimensions
  disagree with its blob is skipped and logged. `coverage.embedded` counts rows
  present, so it can overstate usable vectors if the table was damaged by hand.
- **No auth.** `user_id` is caller-asserted. Do not expose this service beyond
  localhost until authentication exists.
- **The fake extractor is a heuristic, not a model.** It keeps first-person
  sentences and guesses a kind from cue words. It proves the pipeline, not
  extraction quality.
- **No semantic near-duplicate detection.** Duplicates are exact after
  canonicalisation; paraphrases still produce separate memories. No conflict
  supersession yet either, so a changed fact and its older version coexist.
- **Stored facts can carry injection text.** A memory is plain text that a future
  prompt may read. The ingestion path ignores instructions in the conversation,
  but nothing sanitises what is stored.
- **No auth.** `user_id` is caller-asserted. Do not expose this service beyond
  localhost until authentication exists.
- **Provider failures are logged, not surfaced per write.** A failed embed
  yields a normal `201`/`200`; the gap shows up as `coverage`.
- **One model at a time.** Vectors for a superseded model stay on disk; they are
  ignored, not cleaned up.
- **Retention is manual.** Extracted memories live until deleted, and
  `RECALLKIT_STORE_MESSAGES=true` makes raw conversations persistent.
- **Corrupt vectors are invisible, not fatal.** A row whose stored dimensions
  disagree with its blob is skipped and logged. `coverage.embedded` counts rows
  present, so it can overstate usable vectors if the table was damaged by hand.

## Next milestone (not implemented)

Memory quality beyond exact duplicates: semantic near-duplicates, `subject`-based
conflict supersession, and the migration runner that a canonical key column
needs. Ingestion, extraction, and all retrieval modes are complete for now.
