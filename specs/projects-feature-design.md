# Projects Feature — Design Specification

> **Purpose**: Design of record for the **Projects** feature — grouping chat sessions, documents, and notes under a shared goal, with knowledge shared across all sessions in the project, auto-accumulated as sessions run, plus an opt-in multi-agent collaboration layer.
> **Last updated**: 2026-07-11
> **Status**: Approved design — ready to break into implementation slices.
> **Scope**: New capability, purely **additive**. No existing table, column, route, or behavior is modified or removed.

This document specifies the data model, injection/harvest seams, attachment sharing, cross-session collaboration, and delete lifecycle. It is grounded in the current codebase (verified 2026-07-11); file:line references are anchors for the build, not guarantees against drift — re-verify before editing.

---

## 1. Overview

A **Project** is a first-class container that groups sessions/documents/notes around one goal and gives them a shared, growing body of knowledge.

- **Independent by default.** Grouping sessions into a project only passively injects the shared goal + knowledge doc into each session's context. Sessions do **not** talk to each other unless collaboration is explicitly invoked. Working sessions one at a time is the baseline experience.
- **Shared knowledge, two layers.** A human-readable knowledge doc (always injected, editable, appended-to by a background harvester) plus project-scoped vector recall (memory + RAG filtered by `project_id`).
- **Auto-accumulating.** As sessions run, a background job distills each turn into the project's knowledge doc and project-scoped facts.
- **Attachments are shared.** Every attachment sent in a project session is indexed into the project's shared knowledge, so all sessions can retrieve it.
- **Opt-in collaboration.** A lead session can decompose the goal and dispatch subtasks to parallel worker chats that coordinate via a project message bus; results synthesize back into the knowledge doc.
- **Safe deletion.** Projects soft-archive; after 30 days a retention sweep synthesizes durable knowledge into user-level memories/skills, then hard-deletes only the project record. Sessions and documents always survive.

### 1.1 Design principles

1. **Additive only.** New `projects` table + nullable `project_id` FKs. Existing `folder` grouping keeps working untouched.
2. **Reuse the existing seams.** Inject at `chat_processor.build_context_preface`; harvest at `run_post_response_tasks`; run headless agents via the proven `bg_monitor._drain_agent` / `stream_agent_loop` path.
3. **One writer per session.** There is no per-session write lock; collaboration is always message-passing between separate sessions. Never run two agents against one session.
4. **Owner scoping preserved.** Every new query keeps the existing `owner` filter; `project_id` is an additional dimension, never a replacement.

---

## 2. Data model

Storage backend: SQLite via SQLAlchemy (`core/database.py`, `data/app.db`). ChromaDB for vectors (`odysseus_rag`, `odysseus_memories`). Schema evolution: hand-written idempotent `_migrate_*` functions registered in `init_db()` (no Alembic). Precedent container table: `GalleryAlbum` (`core/database.py:358`).

### 2.1 New table: `projects`

```python
class Project(TimestampMixin, Base):
    __tablename__ = "projects"
    id           = Column(String, primary_key=True, index=True)   # uuid4 hex
    owner        = Column(String, nullable=True, index=True)
    name         = Column(String, nullable=False)
    description  = Column(Text, default="")
    goal         = Column(Text, nullable=True)      # the charter — always injected
    knowledge    = Column(Text, default="")         # readable, auto-grown doc
    archived     = Column(Boolean, default=False, index=True)
    archived_at  = Column(DateTime, nullable=True)  # set when archived; drives 30-day sweep
    # created_at / updated_at from TimestampMixin
```

`Base.metadata.create_all()` in `init_db()` creates this automatically — no migration function needed for the new table itself.

### 2.2 New columns: `project_id` FK

Add a nullable, indexed `project_id` FK (`ON DELETE SET NULL`) to:

- `sessions` (`core/database.py:103`) — primary membership
- `documents` (`core/database.py:310`) — per-project Library view
- `memories` (`core/database.py:896`) — project-scoped facts (the "Brain")
- `notes` (`core/database.py:2125`) — project to-dos / checklists (see §4.4)
- uploads metadata (`src/upload_handler.py` `save_upload`, `uploads.json`) — per-project attachment listing

`SET NULL` (not CASCADE) is deliberate: deleting a project must never destroy chat history or documents. This matches how `documents.session_id` is already handled.

**Each column needs an idempotent migration**, copied from `_migrate_add_folder_column` (`core/database.py:1392`) and **registered in `init_db()`** (`core/database.py:2308`):

```python
def _migrate_add_project_id_to_sessions():
    # PRAGMA table_info(sessions); if "project_id" not in cols:
    #   ALTER TABLE sessions ADD COLUMN project_id VARCHAR
    #   CREATE INDEX IF NOT EXISTS ix_sessions_project_id ON sessions(project_id)
```

**Cache gotcha:** `SessionManager` keeps an in-memory `sessions` cache. The existing `folder` write in `PATCH /api/session/{sid}` (`routes/session_routes.py:468`) writes DB-only and relies on `sync_session_metadata` to refresh the cache — mirror that for `project_id` or labels go stale until reload.

### 2.3 New table: `project_messages` (collaboration bus) — Phase 4

```python
class ProjectMessage(Base):
    __tablename__ = "project_messages"
    id              = Column(String, primary_key=True, index=True)
    project_id      = Column(String, ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    from_session_id = Column(String, nullable=True)
    from_name       = Column(String, nullable=True)
    to_session_id   = Column(String, nullable=True)   # null = broadcast
    kind            = Column(String, default="note")  # note | task | result | status
    content         = Column(Text)
    # created_at from a timestamp default
```

Bus messages are ephemeral collaboration state, so `CASCADE` here is fine (they have no value once the project is gone).

---

## 3. Shared knowledge — injection (Phase 2)

**Seam:** `ChatProcessor.build_context_preface` (`src/chat_processor.py:198`), which already receives `session` + `owner` and returns the `preface` messages. Resolve the project from `session.project_id` in `build_chat_context` (`routes/chat_helpers.py:626`, calls `build_context_preface` at ~line 736) and inject two blocks after the existing memory/RAG blocks (~`chat_processor.py:286`/`:314`):

1. **Goal / brief** → a trusted `{"role":"system", "content": project.goal}` block (like the preset system prompt at `chat_processor.py:235`).
2. **Knowledge doc** → an `untrusted_context_message("project knowledge", project.knowledge)` block (like RAG at `chat_processor.py:314`) — it's accumulated data, not authoritative instruction.

Do **not** inject inside `agent_loop._build_system_prompt` — that would require threading `session_id` through `stream_agent_loop` and `_build_system_prompt` signatures. The preface route needs no agent-loop signature changes.

### 3.1 Project-scoped vector recall

ChromaDB metadata is an arbitrary dict; RAG already filters on `owner` via a Chroma `where` clause (`src/rag_vector.py:352`) — extend that pattern:

- **RAG:** add `project_id` to the metadata dict in `index_personal_documents` / `add_document` (`src/rag_vector.py:518`), and change the filter to `where={"$and":[{"owner":owner},{"project_id":pid}]}`. Thread `project_id` from `build_context_preface` (`src/chat_processor.py:296`). Include `project_id` in `_generate_doc_id` (`src/rag_vector.py:43`) if the same chunk can live in multiple projects.
- **Memory:** recall already joins post-search by id (`src/chat_processor.py:146`), so filtering in `MemoryManager.load(owner=, project_id=)` (`src/memory.py:129`) is sufficient — no Chroma change strictly required. Optionally also tag memory vectors (`src/memory_vector.py:117`) for a scoped semantic pre-filter at scale.

### 3.2 Per-project Library view

Documents already carry `session_id`. With `project_id` on `Document`, filter the Library query (`routes/document_routes.py:362`, `GET /api/documents/library`) by project. Frontend: add a project selector/filter to the Documents tab in `static/js/documentLibrary.js` (fetch at `:330`); "open in session" already works via `libraryOpenInSession` → `selectSession(doc.session_id)`.

---

## 4. Auto-accumulating knowledge + attachments (Phase 3)

### 4.1 Harvest each turn

**Seam:** `run_post_response_tasks` (`routes/chat_helpers.py:1137`), which already assembles `_extraction_jobs` (memory extraction at `:1179`, skill extraction at `:1217`) and runs them idle-safe/sequentially via `_spawn_bg(_run_extraction_jobs_sequentially(...))` (`:1231`). Add a third job, gated on `sess.project_id`:

- Reuse `services/memory/memory_extractor.py::extract_and_store` (`:276`) with a `project_id` so extracted facts land project-scoped.
- Additionally distill turn outcomes into bullet points appended to `projects.knowledge` (the readable doc). Model the prompt on the goal-directed style in `src/goal_based_extractor.py` — extract only what serves the project goal.
- Keep the existing gating (every 4th message; idle-safe) to protect the KV cache.

In scope at that call site: `sess.history`, `session_id`, `full_response`, `message`, `owner`, `agent_rounds`, `agent_tool_calls`.

### 4.2 Attachment auto-indexing (net-new)

Today chat attachments are **inlined only** (`src/document_processor.py` `build_user_content`, `:395`) and never reach ChromaDB. New step: after an attachment is uploaded/extracted in a project session, call `rag_manager.add_document(chunk, {"project_id":pid, "owner":owner, "source":filename, "kind":"attachment"})`. Extraction utilities already exist (`_process_pdf`, `extract_office_text`, `_split_into_chunks`). This is the mechanism that makes an attachment "known by all sessions in the project."

### 4.3 Ingest on pull-in

When an existing session is attached to a project, run `extract_and_store` over its transcript with the `project_id` to seed the knowledge doc + project facts. Reuse the same background machinery as §4.1.

### 4.4 Project to-dos (Notes)

Odysseus has **no separate to-do model** — the `notes` table (`core/database.py:2125`) backs plain notes, checklists (`items` JSON of `{text, done}`), and reminders, and already has an "AI solve this todo → spawn agent session" flow (`agent_session_id`). Scoping notes to a project turns the checklist into the project's **shared work queue**.

- **Membership:** add `Note.project_id` (§2.2); inherit it from the session on create, so agent- and user-created notes made inside a project session are auto-stamped. Filter `list_notes` (`routes/note_routes.py:615`) and the agent tool `do_manage_notes` (`src/tools/notes.py`). A proper column (not a `session_id`-derived join) is used deliberately so a note can belong to a project independent of any originating session, and so the orchestrator can write/close todos directly.
- **Inject open todos:** in `build_context_preface` (§3), append the project's open checklist items so every session in the project sees the outstanding work, not just the goal.
- **Orchestrator work queue (Phase 5):** the lead session reads the project's open todos, dispatches each to a worker chat, and marks items done (via the existing `POST /api/notes/{id}/items/{index}/toggle` route) as workers finish. The checklist becomes the shared task board for the agent team.
- **Cost:** one nullable column + migration + inherit-on-create + two query filters + a per-project to-do panel in the UI. The reminder/dispatch machinery (`note_routes.py:139`) is project-agnostic and unchanged.

### 4.5 Explicitly not scoped (derived-only)

Recorded so the decision is deliberate, not silently dropped:

- **Gallery** (`gallery_images` / `gallery_albums`): **no `project_id`.** Images already carry `session_id` (AI-generated ones link to their originating chat), so "images from this project" is a `session → project` join. Albums are the user's cross-cutting taxonomy ("Logos", "Family") and often aren't project-bound; a fourth grouping axis is clutter for little gain. Note: `GalleryAlbum` (`core/database.py:358`) remains the *template* for the `projects` table (§2.1) — its shape is copied, its rows are not scoped.
- **Scheduled Tasks** (`scheduled_tasks`): **no `project_id`.** These are automations; the housekeeping majority (email summaries, urgency scans) have no project, so the column would be null on most rows. Derive via the task's `session_id` FK if a per-project automations view is ever needed.
- **Skills** (the Brain window's Skills tab): **stay global.** Skills are meant to be reusable across everything, and the synthesize-on-delete step (§6) already promotes a project's reusable procedures *into* global skills.

---

## 5. Cross-session collaboration (Phases 4–5, opt-in)

**Linchpin (already exists):** `stream_agent_loop` (`src/agent_loop.py:2602`) is a plain async generator with no HTTP dependency, and `bg_monitor._drain_agent` (`src/bg_monitor.py:28`) already does "given a session + a trigger message, run its agent with tools, capture prose + tool_events, save as a normal assistant turn." Busy-guard: `agent_runs.is_active(sid)` (`src/agent_runs.py:78`). Cross-session awareness convention: user-role `[Name]: …` injection (as in `static/js/group.js` + `POST /api/session/{sid}/inject_messages`).

### 5.1 Layer 0 — Project bus (Phase 4)

`project_messages` table (§2.3). Sessions post to and read from it; recent bus activity is injected into each session's context via the same preface seam (§3). This is the ambient-awareness / async-blackboard substrate.

### 5.2 Layer 1 — Directed dispatch (Phase 4)

`dispatch_to_session(target, message)` primitive:
1. Append `message` to the target's history as a cache-safe `[FromName]: …` user turn (reuse `inject_messages`).
2. Schedule a headless `_drain_agent`-style run on the target, guarded by `agent_runs.is_active` (defer + retry if busy).
3. Target processes with its full toolset; reply saves as a normal assistant turn (visible live in its chat) and posts back to the bus / originating session.

Exposed as agent tools (in `src/agent_tools/`):
- `project_broadcast(message)` — passive: post to the bus; siblings see it next turn.
- `send_to_session(target, message, wait=false)` — active: wake the target's agent. **Upgrade the existing tool** (`src/agent_tools/session_tools.py:162`), which is currently a single tool-less `llm_call_async` — swap its body to the `_drain_agent` path so the target actually uses its tools. `wait=true` = synchronous handoff (caller blocks on the result); `wait=false` = async delegation.

### 5.3 Layer 2 — Lead/worker orchestrator (Phase 5)

A dedicated asyncio orchestrator — **its own task, NOT the existing task scheduler** (which is deliberately serial `Semaphore(1)` and pauses for foreground activity; `src/task_scheduler.py:350`). Flow:
1. A coordinator/lead session decomposes the project goal.
2. It spawns worker sessions (each its own `Session`, optionally bound to a `CrewMember` persona + tool allowlist; `core/database.py:769`).
3. Workers run **in parallel** — safe because each owns its session (one writer per session).
4. Results collected off the bus, synthesized by the lead into the knowledge doc.

**Concurrency & safety:** never two agents in one session; serialize per-session via `agent_runs.is_active`; respect the shared subscription's rate limits when fanning out (parallel workers all draw on the same quota — cap concurrency and surface it in the UI).

---

## 6. Delete lifecycle

1. **Soft-archive (default).** Set `archived=true`, `archived_at=now`. Reversible anytime. Sessions/docs stay visible in Chats throughout (never hidden).
2. **Retention sweep.** A daily job (reuse `cleanup_service.py` / task scheduler) finds projects with `archived_at` older than 30 days.
3. **Synthesize before deletion.** Promote durable, still-relevant project knowledge to owner-level user memories (strip `project_id`, keep `owner`, via `memory_extractor` / `MemoryManager.add_entry`) and distill reusable procedures into skills (via the existing skill extractor). Same engine as §4.1.
4. **Hard-delete.** Remove the project row + knowledge doc + project-scoped vectors and bus messages. `project_id` FKs null out — **sessions and documents persist, un-categorized.**

---

## 7. API surface (new)

New `routes/project_routes.py`, wired in `app.py` next to `setup_session_routes` (~`app.py:665`). Model on `routes/session_routes.py`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/projects` | List projects for owner (with session/doc counts) |
| POST | `/api/project` | Create `{name, description?, goal?}` |
| GET | `/api/project/{id}` | Project detail + members + knowledge doc |
| PATCH | `/api/project/{id}` | Update name/description/goal/knowledge |
| POST | `/api/project/{id}/archive` · `/unarchive` | Soft-archive toggle |
| DELETE | `/api/project/{id}` | Hard-delete (normally invoked by the sweep, not the UI) |
| POST | `/api/project/{id}/session/{sid}` | Attach session (triggers ingest, §4.3) |
| DELETE | `/api/project/{id}/session/{sid}` | Detach session (nulls `project_id`) |
| GET/POST | `/api/project/{id}/bus` | Read/post bus messages (Phase 4) |
| POST | `/api/project/{id}/orchestrate` | Launch lead/worker run (Phase 5) |

Also: add `project_id` to the `GET /api/sessions` payload (`routes/session_routes.py:301`) and accept it in `PATCH /api/session/{sid}` (`:451`).

---

## 8. Frontend

Vanilla-JS ES modules, no framework, no build step. The single `#sidebar` swaps left/right (`static/js/sidebar-layout.js`, persisted at `Storage.KEYS.SIDEBAR_SIDE`) — a section renders on whichever side the user chose.

- **Projects nav section.** Add a `.section id="projects-section"` inside `.sidebar-inner` (`static/index.html`, above `#sessions-section`). It auto-gets collapse + drag-reorder from `static/js/section-management.js`. Copy the `.section-header-flex` + `list-item-plus-btn` header pattern from `#email-section` for a "+ project" button. Wire actions in `static/js/app.js` (~lines 1236–1266).
- **Project session list + "new session in project."** Reuse the folder machinery in `static/js/sessions.js`: `_renderSessionListImpl` group-mode rendering (`:1097`), `createSessionItem` (`:475`), `moveToFolder` (`:351`), `buildFolderSubmenu` (`:362`). "New session in project" = `createDirectChat` then stamp the project on `materializePendingSession` (`:2148`) or via a `PATCH`.
- **Per-project Library.** Filter the Documents tab in `static/js/documentLibrary.js` by project (§3.2).
- **Collaboration UI (Phases 4–5).** A project view showing the bus feed and an "orchestrate" launch control; each worker chat is watchable live (its reply streams as a normal assistant turn).

---

## 9. Phased plan

| Phase | Deliverable | Ship test |
|---|---|---|
| **1 — Foundation** | `projects` table + `project_id` FKs + migrations; manager/route/serialization support; nav Projects section reusing folder UI; soft-archive. | Create a project, group sessions, confirm they still show in Chats, delete → zero data loss. |
| **2 — Shared knowledge** | Inject goal + knowledge doc at `build_context_preface`; project-scoped memory/RAG recall; per-project Library filter. | All sessions in a project share goal + knowledge; docs scoped in Library. |
| **3 — Auto-knowledge + attachments + to-dos** | Harvester job in `run_post_response_tasks`; attachment auto-indexing; ingest-on-pull-in; retention-sweep synthesis; project-scoped Notes/to-dos (§4.4). | Knowledge grows automatically; an attachment in one session is retrievable in another; pulling a session in seeds knowledge; a project's open todos appear in its sessions' context. |
| **4 — Bus + dispatch** | `project_messages` bus + injection; `dispatch_to_session`; `project_broadcast` + upgraded `send_to_session`. | One chat broadcasts and actively wakes another to do work and reply, visible live. |
| **5 — Orchestrator** | Dedicated asyncio orchestrator; lead decomposes goal → parallel workers → synthesize; launch/watch UI. | Kick off a goal; watch parallel chats collaborate to completion; output accumulates into the knowledge doc. |

---

## 10. Open questions / decisions

- **Settled:** new `projects` table (not a `folder` overload); readable knowledge doc + vector recall; every attachment auto-indexes; soft-archive → 30-day → synthesize → hard-delete; full lead/worker orchestrator; independent sessions by default.
- **Settled (scoping scope):** project-scoped surfaces are sessions, documents, memories (the "Brain"), attachments, and **Notes/to-dos** (proper `project_id` column, §4.4). **Gallery** and **Scheduled Tasks** are intentionally *derived-only* (no column — join via `session_id`), and **Skills** stay global (§4.5).
- **Open:** on hard-delete, keep the knowledge doc as an orphaned Library document, or remove it entirely? (Leaning: remove — its durable value was already promoted to user memories/skills in §6.3.)
- **Open:** dedicated `odysseus_project_knowledge` Chroma collection vs. a `project_id` metadata field on the existing `odysseus_rag` collection. (Leaning: metadata field — simpler, no new collection; revisit only if project knowledge needs different retrieval params.)
- **Future (cheap):** per-session "exclude from project knowledge" toggle for pure-solo sessions inside a project.
- **Watch-out:** confirm the `src/memory*` vs `services/memory/*` duplication — verify which `MemoryManager` / `memory_vector` instance is actually injected (`src/app_initializer.py`) before editing extraction code.
