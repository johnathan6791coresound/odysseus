# src/ssh_manager.py
"""Shared SSH keypair + config management, used by both the "CLI
Integrations" git_ssh kind (routes/cli_integration_routes.py) and the
"SSH Connections" section (routes/ssh_connection_routes.py). Both features
write into the same ~/.ssh/config, so the config regenerator here combines
rows from both tables into one file rather than each owning it independently
(which would let one clobber the other's Host blocks)."""
import re
import subprocess
from pathlib import Path

import logging

logger = logging.getLogger(__name__)

SSH_DIR = Path.home() / ".ssh"
SSH_KEYS_DIR = SSH_DIR / "keys"
SSH_PASSWORDS_DIR = SSH_DIR / "passwords"
SSH_CONFIG_PATH = SSH_DIR / "config"
SSH_CONFIG_MARKER = "# odysseus-managed-ssh-config (auto-generated — do not edit by hand)"

NAME_RE = re.compile(r"^[A-Za-z0-9 ._-]{1,64}$")
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,253}[A-Za-z0-9])?$")

SUCCESS_BANNER_RE = re.compile(r"successfully authenticated|welcome to gitlab|hi \S+!|last login", re.IGNORECASE)


def generate_keypair(row_id: str, comment: str) -> tuple[str, str, str]:
    """Generate an ed25519 keypair under SSH_KEYS_DIR. Returns
    (key_filename, public_key_text, fingerprint)."""
    SSH_KEYS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_filename = f"{row_id}_ed25519"
    key_path = SSH_KEYS_DIR / key_filename
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-C", comment],
        capture_output=True, text=True, timeout=15, check=True,
    )
    key_path.chmod(0o600)
    pub_path = key_path.with_suffix(".pub")
    pub_path.chmod(0o644)
    public_key = pub_path.read_text(encoding="utf-8").strip()
    fingerprint = fingerprint_of(pub_path)
    return key_filename, public_key, fingerprint


def import_keypair(row_id: str, private_key_text: str) -> tuple[str, str, str]:
    """Import an existing private key (already trusted on the target
    host(s)) instead of generating a fresh one — e.g. a shared admin key
    already deployed to a fleet of servers. Derives the matching public
    key + fingerprint from it. Returns (key_filename, public_key, fingerprint)."""
    SSH_KEYS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_filename = f"{row_id}_imported"
    key_path = SSH_KEYS_DIR / key_filename
    text = private_key_text if private_key_text.endswith("\n") else private_key_text + "\n"
    key_path.write_text(text, encoding="utf-8")
    key_path.chmod(0o600)
    pub_path = key_path.with_suffix(".pub")
    try:
        proc = subprocess.run(
            ["ssh-keygen", "-y", "-f", str(key_path)],
            capture_output=True, text=True, timeout=10, check=True,
        )
        public_key = proc.stdout.strip()
        pub_path.write_text(public_key + "\n", encoding="utf-8")
        pub_path.chmod(0o644)
    except subprocess.CalledProcessError as e:
        key_path.unlink(missing_ok=True)
        raise ValueError(f"Not a valid private key: {e.stderr.strip()}")
    fingerprint = fingerprint_of(pub_path)
    return key_filename, public_key, fingerprint


def fingerprint_of(pub_key_path: Path) -> str:
    try:
        out = subprocess.run(
            ["ssh-keygen", "-lf", str(pub_key_path)],
            capture_output=True, text=True, timeout=5, check=True,
        )
        return out.stdout.strip()
    except Exception as e:
        logger.warning(f"ssh-keygen fingerprint failed: {e}")
        return ""


def delete_keypair(key_filename: str) -> None:
    key_path = SSH_KEYS_DIR / key_filename
    for p in (key_path, key_path.with_suffix(".pub")):
        try:
            p.unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"Failed removing key file {p}: {e}")


def write_password_file(row_id: str, password: str) -> str:
    """Write a chmod-600 password file for sshpass -f. Returns the filename
    (basename under SSH_PASSWORDS_DIR)."""
    SSH_PASSWORDS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    filename = f"{row_id}.pass"
    path = SSH_PASSWORDS_DIR / filename
    path.write_text(password, encoding="utf-8")
    path.chmod(0o600)
    return filename


def delete_password_file(filename: str) -> None:
    try:
        (SSH_PASSWORDS_DIR / filename).unlink(missing_ok=True)
    except Exception as e:
        logger.warning(f"Failed removing password file {filename}: {e}")


def connect_command(row) -> str:
    """The exact shell command to run against this row (git_ssh/ssh_connection
    rows are duck-typed alike: host/port/ssh_user/auth_method/key_filename).
    Surfaced in the UI and meant to be copy-pasteable by the agent."""
    auth_method = getattr(row, "auth_method", "key")
    if auth_method == "password":
        pass_path = SSH_PASSWORDS_DIR / f"{row.id}.pass"
        return f"sshpass -f {pass_path} ssh -p {row.port} {row.ssh_user}@{row.host}"
    return f"ssh {row.host}"  # relies on the generated Host block for port/user/key


def test_ssh(key_filename: str, host: str, port: int, ssh_user: str) -> tuple[str, str]:
    """Run a bounded `ssh -T` probe. Returns (status, output) where status is
    "ok" or "error". Both git hosts and plain shell hosts exit non-zero on a
    `-T` no-shell probe or may exit 0 with a shell banner — the recognizable
    banner text (or a clean non-error exit) is what signals success, not the
    exit code alone."""
    key_path = SSH_KEYS_DIR / key_filename
    try:
        proc = subprocess.run(
            [
                "ssh", "-p", str(port), "-i", str(key_path),
                "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
                "-T", f"{ssh_user}@{host}",
            ],
            capture_output=True, text=True, timeout=15,
        )
        banner = (proc.stdout + proc.stderr).strip()
        ok = proc.returncode == 0 or bool(SUCCESS_BANNER_RE.search(banner))
        return ("ok" if ok else "error"), (banner[:2000] or "(no banner)")
    except subprocess.TimeoutExpired:
        return "error", "Connection timed out"


def test_ssh_password(password_filename: str, host: str, port: int, ssh_user: str) -> tuple[str, str]:
    """Same as test_ssh but for password auth, via `sshpass -f`. No
    BatchMode/IdentitiesOnly here — those are key-auth-only options."""
    pass_path = SSH_PASSWORDS_DIR / password_filename
    try:
        proc = subprocess.run(
            [
                "sshpass", "-f", str(pass_path),
                "ssh", "-p", str(port),
                "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
                "-o", "PreferredAuthentications=password",
                "-T", f"{ssh_user}@{host}",
            ],
            capture_output=True, text=True, timeout=15,
        )
        banner = (proc.stdout + proc.stderr).strip()
        ok = proc.returncode == 0 or bool(SUCCESS_BANNER_RE.search(banner))
        return ("ok" if ok else "error"), (banner[:2000] or "(no banner)")
    except FileNotFoundError:
        return "error", "sshpass is not installed in this container"
    except subprocess.TimeoutExpired:
        return "error", "Connection timed out"


def _host_block(host: str, port: int, ssh_user: str, key_filename: str | None) -> str:
    identity_line = f"  IdentityFile {SSH_KEYS_DIR / key_filename}\n  IdentitiesOnly yes\n" if key_filename else ""
    return (
        f"\nHost {host}\n"
        f"  HostName {host}\n"
        f"  Port {port}\n"
        f"  User {ssh_user}\n"
        f"{identity_line}"
    )


def regenerate_ssh_config(db) -> None:
    """Rewrite ~/.ssh/config from every enabled row across both the
    git_ssh CliIntegration kind and the SshConnection table. Idempotent —
    safe to call after any create/update/delete on either table.

    Password-auth SshConnection rows still get a Host block (for
    HostName/Port/User) but no IdentityFile — see connect_command(), which
    is how the agent actually authenticates for those (sshpass -f)."""
    from core.database import CliIntegration, SshConnection

    SSH_DIR.mkdir(mode=0o700, exist_ok=True)
    blocks = [SSH_CONFIG_MARKER + "\n"]
    git_rows = db.query(CliIntegration).filter(
        CliIntegration.kind == "git_ssh", CliIntegration.is_enabled == True  # noqa: E712
    ).all()
    ssh_rows = db.query(SshConnection).filter(SshConnection.is_enabled == True).all()  # noqa: E712
    for row in git_rows:
        blocks.append(_host_block(row.host, row.port, row.ssh_user, row.key_filename))
    for row in ssh_rows:
        key_filename = row.key_filename if getattr(row, "auth_method", "key") == "key" else None
        blocks.append(_host_block(row.host, row.port, row.ssh_user, key_filename))
    SSH_CONFIG_PATH.write_text("".join(blocks), encoding="utf-8")
    SSH_CONFIG_PATH.chmod(0o600)
