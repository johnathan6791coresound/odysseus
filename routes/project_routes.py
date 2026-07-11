# routes/project_routes.py
"""Projects API — group chat sessions (and later documents, memories, notes,
attachments) under one shared goal.

Phase 1: project CRUD, soft-archive, and session membership (attach/detach +
member listing). Deleting a project never destroys chat data — member sessions
are detached (project_id nulled, the app-layer ON DELETE SET NULL) and survive
in the Chats list. Knowledge injection, auto-harvest, and the retention sweep
land in later phases. See specs/projects-feature-design.md.
"""

import uuid
import logging
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from sqlalchemy import func

from core.database import SessionLocal, Project as DbProject, Session as DbSession, utcnow_naive
from src.auth_helpers import require_user, owner_filter

logger = logging.getLogger(__name__)


def setup_project_routes(session_manager=None):
    """Build the projects router. ``session_manager`` (optional) lets membership
    changes keep the in-memory session cache in sync so Phase 2's injection seam
    (which reads ``session.project_id``) sees changes without a reload."""
    router = APIRouter(prefix="/api", tags=["projects"])

    def _owned_project(db, pid: str, owner: Optional[str]) -> DbProject:
        """Fetch a project the caller owns, or 404. Mirrors owner scoping used
        across the app so one user can never see or mutate another's project."""
        q = owner_filter(db.query(DbProject).filter(DbProject.id == pid), DbProject, owner)
        proj = q.first()
        if proj is None:
            raise HTTPException(404, "Project not found")
        return proj

    def _session_counts(db, owner: Optional[str]) -> dict:
        """{project_id: member session count} in one grouped query."""
        q = owner_filter(
            db.query(DbSession.project_id, func.count(DbSession.id))
            .filter(DbSession.project_id != None),  # noqa: E711
            DbSession, owner,
        ).group_by(DbSession.project_id)
        return {row[0]: row[1] for row in q.all()}

    def _sync_cache_project_cleared(pid: str):
        """Clear project_id on any cached in-memory sessions for a removed project."""
        if session_manager is None:
            return
        for s in getattr(session_manager, "sessions", {}).values():
            if getattr(s, "project_id", None) == pid:
                try:
                    s.project_id = None
                except Exception:
                    pass

    def _sync_cache_session_project(sid: str, new_pid: Optional[str]):
        if session_manager is None:
            return
        s = getattr(session_manager, "sessions", {}).get(sid)
        if s is not None:
            try:
                s.project_id = new_pid
            except Exception:
                pass

    # ------------------------------------------------------------------ list
    @router.get("/projects")
    def list_projects(request: Request, include_archived: bool = False):
        owner = require_user(request) or None
        db = SessionLocal()
        try:
            q = owner_filter(db.query(DbProject), DbProject, owner)
            if not include_archived:
                q = q.filter(DbProject.archived == False)  # noqa: E712
            q = q.order_by(DbProject.updated_at.desc())
            counts = _session_counts(db, owner)
            out = []
            for p in q.all():
                d = p.to_dict()
                d["session_count"] = counts.get(p.id, 0)
                out.append(d)
            return out
        finally:
            db.close()

    # ---------------------------------------------------------------- create
    @router.post("/project")
    def create_project(
        request: Request,
        name: str = Form(...),
        description: str = Form(""),
        goal: str = Form(None),
    ):
        owner = require_user(request) or None
        name = (name or "").strip()
        if not name:
            raise HTTPException(400, "Project name is required")
        db = SessionLocal()
        try:
            proj = DbProject(
                id=uuid.uuid4().hex,
                owner=owner,
                name=name,
                description=description or "",
                goal=(goal or None),
                knowledge="",
                archived=False,
            )
            db.add(proj)
            db.commit()
            db.refresh(proj)
            d = proj.to_dict()
            d["session_count"] = 0
            return d
        finally:
            db.close()

    # ------------------------------------------------------------------ read
    @router.get("/project/{pid}")
    def get_project(request: Request, pid: str):
        owner = require_user(request) or None
        db = SessionLocal()
        try:
            proj = _owned_project(db, pid, owner)
            d = proj.to_dict()
            d["session_count"] = _session_counts(db, owner).get(pid, 0)
            return d
        finally:
            db.close()

    # ---------------------------------------------------------------- update
    @router.patch("/project/{pid}")
    def update_project(
        request: Request, pid: str,
        name: str = Form(None), description: str = Form(None),
        goal: str = Form(None), knowledge: str = Form(None),
    ):
        owner = require_user(request) or None
        db = SessionLocal()
        try:
            proj = _owned_project(db, pid, owner)
            if name is not None:
                nm = name.strip()
                if not nm:
                    raise HTTPException(400, "Project name cannot be empty")
                proj.name = nm
            if description is not None:
                proj.description = description or ""
            if goal is not None:
                proj.goal = goal or None
            if knowledge is not None:
                proj.knowledge = knowledge or ""
            proj.updated_at = utcnow_naive()
            db.commit()
            db.refresh(proj)
            d = proj.to_dict()
            d["session_count"] = _session_counts(db, owner).get(pid, 0)
            return d
        finally:
            db.close()

    # --------------------------------------------------------- archive toggle
    @router.post("/project/{pid}/archive")
    def archive_project(request: Request, pid: str):
        owner = require_user(request) or None
        db = SessionLocal()
        try:
            proj = _owned_project(db, pid, owner)
            proj.archived = True
            proj.archived_at = utcnow_naive()  # starts the 30-day retention clock (Phase 3)
            proj.updated_at = utcnow_naive()
            db.commit()
            return {"id": pid, "archived": True, "archived_at": proj.archived_at.isoformat()}
        finally:
            db.close()

    @router.post("/project/{pid}/unarchive")
    def unarchive_project(request: Request, pid: str):
        owner = require_user(request) or None
        db = SessionLocal()
        try:
            proj = _owned_project(db, pid, owner)
            proj.archived = False
            proj.archived_at = None
            proj.updated_at = utcnow_naive()
            db.commit()
            return {"id": pid, "archived": False}
        finally:
            db.close()

    # ---------------------------------------------------------------- delete
    @router.delete("/project/{pid}")
    def delete_project(request: Request, pid: str):
        """Hard-delete a project. Member sessions/documents are NOT deleted —
        their project_id is nulled (app-layer ON DELETE SET NULL) so they remain
        in the Chats list. Phase 3 adds synthesize-to-memory before this runs."""
        owner = require_user(request) or None
        db = SessionLocal()
        try:
            _owned_project(db, pid, owner)  # 404 if not owned
            detached = db.query(DbSession).filter(DbSession.project_id == pid).update(
                {DbSession.project_id: None}, synchronize_session=False
            )
            db.query(DbProject).filter(DbProject.id == pid).delete(synchronize_session=False)
            db.commit()
            _sync_cache_project_cleared(pid)
            return {"id": pid, "deleted": True, "sessions_detached": detached}
        finally:
            db.close()

    # ------------------------------------------------------ member sessions
    @router.get("/project/{pid}/sessions")
    def list_project_sessions(request: Request, pid: str):
        owner = require_user(request) or None
        db = SessionLocal()
        try:
            _owned_project(db, pid, owner)  # ownership check
            q = owner_filter(
                db.query(DbSession).filter(DbSession.project_id == pid), DbSession, owner
            ).order_by(DbSession.last_message_at.desc().nullslast())
            return [
                {"id": s.id, "name": s.name, "model": s.model, "archived": s.archived,
                 "message_count": s.message_count or 0,
                 "last_message_at": s.last_message_at.isoformat() if s.last_message_at else None}
                for s in q.all()
            ]
        finally:
            db.close()

    @router.post("/project/{pid}/session/{sid}")
    def attach_session(request: Request, pid: str, sid: str):
        owner = require_user(request) or None
        db = SessionLocal()
        try:
            _owned_project(db, pid, owner)
            sess = owner_filter(
                db.query(DbSession).filter(DbSession.id == sid), DbSession, owner
            ).first()
            if sess is None:
                raise HTTPException(404, "Session not found")
            sess.project_id = pid
            sess.updated_at = utcnow_naive()
            db.commit()
            _sync_cache_session_project(sid, pid)
            return {"project_id": pid, "session_id": sid, "attached": True}
        finally:
            db.close()

    @router.delete("/project/{pid}/session/{sid}")
    def detach_session(request: Request, pid: str, sid: str):
        owner = require_user(request) or None
        db = SessionLocal()
        try:
            sess = owner_filter(
                db.query(DbSession).filter(DbSession.id == sid, DbSession.project_id == pid),
                DbSession, owner,
            ).first()
            if sess is None:
                raise HTTPException(404, "Session not in this project")
            sess.project_id = None
            sess.updated_at = utcnow_naive()
            db.commit()
            _sync_cache_session_project(sid, None)
            return {"project_id": pid, "session_id": sid, "detached": True}
        finally:
            db.close()

    return router
