# routes/cli_integration_routes.py
"""CLI integration management routes.

A "CLI integration" provisions credentials so agent-run shell commands (the
`bash` tool) can authenticate against an external service without any MCP
server or OAuth round-trip:
  - kind="git_ssh": a per-host ed25519 keypair the admin adds to the git
    host's account (see src/ssh_manager.py for the shared keygen/config
    logic — also used by routes/ssh_connection_routes.py's generic hosts).
  - kind="aws_cli": an access key/secret written into a named profile
    under ~/.aws/credentials + ~/.aws/config.

Private key material and AWS secrets are never returned by the API.
"""
import configparser
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
import logging

from core.database import CliIntegration, SessionLocal
from core.middleware import require_admin
from src import ssh_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cli", tags=["cli"])

AWS_DIR = Path.home() / ".aws"
AWS_CREDENTIALS_PATH = AWS_DIR / "credentials"
AWS_CONFIG_PATH = AWS_DIR / "config"

_NAME_RE = ssh_manager.NAME_RE
_HOST_RE = ssh_manager.HOST_RE


class CreateGitSshIntegration(BaseModel):
    kind: str = "git_ssh"
    name: str
    host: str
    port: int = 22
    ssh_user: str = "git"


class CreateAwsIntegration(BaseModel):
    kind: str = "aws_cli"
    name: str
    aws_access_key_id: str
    aws_secret_access_key: str
    aws_region: str = "us-east-1"


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


def _regenerate_aws_files(db) -> None:
    """Rewrite ~/.aws/credentials + ~/.aws/config from all enabled aws_cli
    rows, one profile per row (profile name = row id)."""
    AWS_DIR.mkdir(mode=0o700, exist_ok=True)
    rows = db.query(CliIntegration).filter(
        CliIntegration.kind == "aws_cli", CliIntegration.is_enabled == True  # noqa: E712
    ).all()

    creds = configparser.ConfigParser()
    cfg = configparser.ConfigParser()
    for row in rows:
        creds[row.id] = {
            "aws_access_key_id": row.aws_access_key_id or "",
            "aws_secret_access_key": row.aws_secret_access_key or "",
        }
        cfg[f"profile {row.id}"] = {"region": row.aws_region or "us-east-1"}

    with open(AWS_CREDENTIALS_PATH, "w", encoding="utf-8") as f:
        creds.write(f)
    with open(AWS_CONFIG_PATH, "w", encoding="utf-8") as f:
        cfg.write(f)
    AWS_CREDENTIALS_PATH.chmod(0o600)
    AWS_CONFIG_PATH.chmod(0o600)


def _row_to_dict(row: CliIntegration) -> dict:
    d = {
        "id": row.id,
        "name": row.name,
        "kind": row.kind,
        "is_enabled": row.is_enabled,
        "last_test_status": row.last_test_status,
        "last_test_output": row.last_test_output,
        "last_test_at": row.last_test_at.isoformat() if row.last_test_at else None,
    }
    if row.kind == "aws_cli":
        d["aws_region"] = row.aws_region
        d["aws_access_key_id_masked"] = (row.aws_access_key_id or "")[:4] + "…" if row.aws_access_key_id else None
        d["aws_profile"] = row.id
    else:
        d.update({
            "host": row.host, "port": row.port, "ssh_user": row.ssh_user,
            "public_key": row.public_key, "fingerprint": row.fingerprint,
        })
    return d


def setup_cli_integration_routes():
    @router.get("/integrations")
    def list_integrations(request: Request):
        require_admin(request)
        db = SessionLocal()
        try:
            rows = db.query(CliIntegration).all()
            return [_row_to_dict(r) for r in rows]
        finally:
            db.close()

    @router.post("/integrations")
    def create_integration(request: Request, body: dict):
        require_admin(request)
        kind = (body.get("kind") or "git_ssh").strip()
        if kind == "aws_cli":
            b = CreateAwsIntegration(**body)
            name = b.name.strip()
            if not _NAME_RE.match(name):
                raise HTTPException(400, "Invalid name")
            if not b.aws_access_key_id.strip() or not b.aws_secret_access_key.strip():
                raise HTTPException(400, "Access key and secret are required")

            db = SessionLocal()
            try:
                row = CliIntegration(
                    id=str(uuid.uuid4())[:8], name=name, kind="aws_cli",
                    host="", port=0, ssh_user="", key_filename="",
                    aws_access_key_id=b.aws_access_key_id.strip(),
                    aws_secret_access_key=b.aws_secret_access_key.strip(),
                    aws_region=b.aws_region.strip() or "us-east-1",
                    is_enabled=True,
                )
                db.add(row)
                db.commit()
                warning = _safe_regen(_regenerate_aws_files, db, label="AWS credentials file")
                result = _row_to_dict(row)
                if warning:
                    result["warning"] = warning
                return result
            finally:
                db.close()

        b = CreateGitSshIntegration(**body)
        name = b.name.strip()
        host = b.host.strip().lower()
        if not _NAME_RE.match(name):
            raise HTTPException(400, "Invalid name")
        if not _HOST_RE.match(host):
            raise HTTPException(400, "Invalid host")
        if not (1 <= b.port <= 65535):
            raise HTTPException(400, "Invalid port")
        ssh_user = b.ssh_user.strip() or "git"
        if not _NAME_RE.match(ssh_user):
            raise HTTPException(400, "Invalid ssh_user")

        row_id = str(uuid.uuid4())[:8]
        try:
            key_filename, public_key, fingerprint = ssh_manager.generate_keypair(row_id, f"odysseus-cli-{row_id}")
        except subprocess.CalledProcessError as e:
            raise HTTPException(500, f"Key generation failed: {e.stderr}")

        db = SessionLocal()
        try:
            row = CliIntegration(
                id=row_id, name=name, kind="git_ssh", host=host, port=b.port,
                ssh_user=ssh_user, key_filename=key_filename,
                public_key=public_key, fingerprint=fingerprint, is_enabled=True,
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

    @router.post("/integrations/{integration_id}/test")
    def test_integration(integration_id: str, request: Request):
        require_admin(request)
        db = SessionLocal()
        try:
            row = db.query(CliIntegration).filter(CliIntegration.id == integration_id).one_or_none()
            if not row:
                raise HTTPException(404, "Integration not found")
            if row.kind == "aws_cli":
                try:
                    proc = subprocess.run(
                        ["aws", "sts", "get-caller-identity", "--profile", row.id],
                        capture_output=True, text=True, timeout=15,
                    )
                    output = (proc.stdout + proc.stderr).strip()
                    row.last_test_status = "ok" if proc.returncode == 0 else "error"
                    row.last_test_output = output[:2000] or "(no output)"
                except FileNotFoundError:
                    row.last_test_status = "error"
                    row.last_test_output = "aws CLI is not installed in this container"
                except subprocess.TimeoutExpired:
                    row.last_test_status = "error"
                    row.last_test_output = "Request timed out"
            else:
                status, output = ssh_manager.test_ssh(row.key_filename, row.host, row.port, row.ssh_user)
                row.last_test_status = status
                row.last_test_output = output
            row.last_test_at = datetime.now(timezone.utc)
            db.commit()
            return _row_to_dict(row)
        finally:
            db.close()

    @router.patch("/integrations/{integration_id}")
    def patch_integration(integration_id: str, request: Request, body: dict):
        require_admin(request)
        db = SessionLocal()
        try:
            row = db.query(CliIntegration).filter(CliIntegration.id == integration_id).one_or_none()
            if not row:
                raise HTTPException(404, "Integration not found")
            if "is_enabled" in body:
                row.is_enabled = bool(body["is_enabled"])
            db.commit()
            if row.kind == "aws_cli":
                warning = _safe_regen(_regenerate_aws_files, db, label="AWS credentials file")
            else:
                warning = _safe_regen(ssh_manager.regenerate_ssh_config, db, label="SSH config")
            result = _row_to_dict(row)
            if warning:
                result["warning"] = warning
            return result
        finally:
            db.close()

    @router.delete("/integrations/{integration_id}")
    def delete_integration(integration_id: str, request: Request):
        require_admin(request)
        db = SessionLocal()
        try:
            row = db.query(CliIntegration).filter(CliIntegration.id == integration_id).one_or_none()
            if not row:
                raise HTTPException(404, "Integration not found")
            kind = row.kind
            if kind == "git_ssh":
                ssh_manager.delete_keypair(row.key_filename)
            db.delete(row)
            db.commit()
            if kind == "aws_cli":
                warning = _safe_regen(_regenerate_aws_files, db, label="AWS credentials file")
            else:
                warning = _safe_regen(ssh_manager.regenerate_ssh_config, db, label="SSH config")
            return {"ok": True, "warning": warning} if warning else {"ok": True}
        finally:
            db.close()

    return router
