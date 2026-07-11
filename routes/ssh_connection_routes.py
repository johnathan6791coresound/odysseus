# routes/ssh_connection_routes.py
"""Generic SSH host connections — a separate section from CLI Integrations'
git_ssh kind (which is for git remotes specifically). This is for the
agent to `ssh <host>` via the `bash` tool and run commands on a remote box
(e.g. tailing logs, restarting a service). Supports either a per-row
keypair (default) or a stored password (via sshpass). Both write into
the same ~/.ssh/config — see src/ssh_manager.py.regenerate_ssh_config."""
import subprocess
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
import logging

from core.database import SshConnection, SessionLocal
from core.middleware import require_admin
from src import ssh_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ssh", tags=["ssh"])


def _safe_regen(fn, *args, label: str) -> Optional[str]:
    """Run a config-file regen after a commit that already succeeded.

    The DB row is the source of truth and is already persisted by the time
    this runs; a failure here must not surface as a bare request failure
    (which would hide that the write actually succeeded) or pass silently
    (which would leave the on-disk config stale with no signal to the admin
    until the next successful call happens to regenerate it). Returns a
    warning string for the caller to surface in its response, or None.
    """
    try:
        fn(*args)
        return None
    except Exception as e:
        logger.error(f"{label} regen failed after DB commit (row saved, on-disk config now stale): {e}")
        return f"Saved, but {label} regeneration failed: {e}"


def _row_to_dict(row: SshConnection) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "host": row.host,
        "port": row.port,
        "ssh_user": row.ssh_user,
        "auth_method": row.auth_method,
        "note": row.note,
        "public_key": row.public_key if row.auth_method == "key" else None,
        "fingerprint": row.fingerprint if row.auth_method == "key" else None,
        "connect_command": ssh_manager.connect_command(row) if row.auth_method == "password" else None,
        "is_enabled": row.is_enabled,
        "last_test_status": row.last_test_status,
        "last_test_output": row.last_test_output,
        "last_test_at": row.last_test_at.isoformat() if row.last_test_at else None,
    }


def _validate_common(body: dict) -> tuple[str, str, int, str]:
    name = (body.get("name") or "").strip()
    host = (body.get("host") or "").strip().lower()
    port = int(body.get("port") or 22)
    ssh_user = (body.get("ssh_user") or "root").strip() or "root"
    if not ssh_manager.NAME_RE.match(name):
        raise HTTPException(400, "Invalid name")
    if not ssh_manager.HOST_RE.match(host):
        raise HTTPException(400, "Invalid host")
    if not (1 <= port <= 65535):
        raise HTTPException(400, "Invalid port")
    if not ssh_manager.NAME_RE.match(ssh_user):
        raise HTTPException(400, "Invalid ssh_user")
    return name, host, port, ssh_user


def setup_ssh_connection_routes():
    @router.get("/connections")
    def list_connections(request: Request):
        require_admin(request)
        db = SessionLocal()
        try:
            rows = db.query(SshConnection).all()
            return [_row_to_dict(r) for r in rows]
        finally:
            db.close()

    @router.post("/connections")
    def create_connection(request: Request, body: dict):
        require_admin(request)
        auth_method = (body.get("auth_method") or "key").strip()
        name, host, port, ssh_user = _validate_common(body)
        note = (body.get("note") or "").strip() or None
        row_id = str(uuid.uuid4())[:8]

        if auth_method == "password":
            password = (body.get("password") or "").strip()
            if not password:
                raise HTTPException(400, "Password is required")
            key_filename = ssh_manager.write_password_file(row_id, password)
            # key_filename column doubles as the password-file basename here so
            # delete_connection has one field to clean up regardless of method —
            # NOT an SSH identity file for password rows (see ssh_manager.connect_command).
            public_key = fingerprint = None
        else:
            auth_method = "key"
            password = None
            private_key = (body.get("private_key") or "").strip()
            try:
                if private_key:
                    key_filename, public_key, fingerprint = ssh_manager.import_keypair(row_id, private_key)
                else:
                    key_filename, public_key, fingerprint = ssh_manager.generate_keypair(row_id, f"odysseus-ssh-{row_id}")
            except subprocess.CalledProcessError as e:
                raise HTTPException(500, f"Key generation failed: {e.stderr}")
            except ValueError as e:
                raise HTTPException(400, str(e))

        db = SessionLocal()
        try:
            row = SshConnection(
                id=row_id, name=name, host=host, port=port, ssh_user=ssh_user,
                auth_method=auth_method, key_filename=key_filename,
                public_key=public_key, fingerprint=fingerprint, password=password,
                note=note, is_enabled=True,
            )
            db.add(row)
            db.commit()
            warning = _safe_regen(ssh_manager.regenerate_ssh_config, db, label="SSH config")
            result = _row_to_dict(row)
            if warning:
                result["warning"] = warning
            return result
        finally:
            db.close()

    @router.post("/connections/{connection_id}/test")
    def test_connection(connection_id: str, request: Request):
        require_admin(request)
        db = SessionLocal()
        try:
            row = db.query(SshConnection).filter(SshConnection.id == connection_id).one_or_none()
            if not row:
                raise HTTPException(404, "Connection not found")
            if row.auth_method == "password":
                status, output = ssh_manager.test_ssh_password(row.key_filename, row.host, row.port, row.ssh_user)
            else:
                status, output = ssh_manager.test_ssh(row.key_filename, row.host, row.port, row.ssh_user)
            row.last_test_status = status
            row.last_test_output = output
            row.last_test_at = datetime.now(timezone.utc)
            db.commit()
            return _row_to_dict(row)
        finally:
            db.close()

    @router.patch("/connections/{connection_id}")
    def patch_connection(connection_id: str, request: Request, body: dict):
        require_admin(request)
        db = SessionLocal()
        try:
            row = db.query(SshConnection).filter(SshConnection.id == connection_id).one_or_none()
            if not row:
                raise HTTPException(404, "Connection not found")
            if "is_enabled" in body:
                row.is_enabled = bool(body["is_enabled"])
            if "note" in body:
                row.note = (body["note"] or "").strip() or None
            db.commit()
            warning = _safe_regen(ssh_manager.regenerate_ssh_config, db, label="SSH config")
            result = _row_to_dict(row)
            if warning:
                result["warning"] = warning
            return result
        finally:
            db.close()

    @router.delete("/connections/{connection_id}")
    def delete_connection(connection_id: str, request: Request):
        require_admin(request)
        db = SessionLocal()
        try:
            row = db.query(SshConnection).filter(SshConnection.id == connection_id).one_or_none()
            if not row:
                raise HTTPException(404, "Connection not found")
            if row.auth_method == "password":
                ssh_manager.delete_password_file(row.key_filename)
            else:
                ssh_manager.delete_keypair(row.key_filename)
            db.delete(row)
            db.commit()
            warning = _safe_regen(ssh_manager.regenerate_ssh_config, db, label="SSH config")
            return {"ok": True, "warning": warning} if warning else {"ok": True}
        finally:
            db.close()

    return router
