"""Telegram bot(s) — long-polling bridges between Telegram chats and
Odysseus sessions. Supports multiple independently-configured bots (e.g.
one for day-to-day chat, one dedicated to a specific project's sessions),
with a single designated "notifier" bot for scheduled-task pushes.

Design mirrors the existing n8n/Make integration: every message is sent
through the token-authenticated `POST /api/v1/chat` loopback endpoint
(routes/webhook_routes.py:sync_chat), so a linked Telegram chat has exactly
the same privileges as any other API-token holder scoped to `chat` — no new
privilege model, no direct DB/session bypass. Model switching is the one
exception — it mutates the session row directly (mirrors
routes/session_routes.py:rename_session's model-switch branch) since the
plain chat endpoint has no notion of "change this session's model."

A Telegram chat_id is the SAME across every bot a given user talks to (it's
effectively their Telegram account id, not a per-bot conversation id), so
identity here is always the (bot_id, chat_id) pair — never chat_id alone.

Commands (handled locally, never sent to the model):
  /start <link_code>   - claim a pending link code generated in the Odysseus UI
  /new                  - start a new session, becomes this chat's active session
  /sessions             - list this user's recent sessions
  /switch <id-prefix>   - switch the active session (matches by id prefix)
  /model [name]         - show, or switch, the active session's model
  /whoami               - show the linked Odysseus username + active session
  /help                 - list commands

Anything else is forwarded as a chat message to the active session (auto-
created via /new semantics if none is set yet).
"""

import asyncio
import logging
import secrets
import uuid

import bcrypt
import httpx

from core.database import get_db_session, TelegramConfig, TelegramLink, ApiToken, SessionLocal
from src.constants import internal_api_base

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


class _RedactBotTokenFilter(logging.Filter):
    """Strips live bot tokens out of httpx's own request-logging.

    Telegram's API puts the bot token in the URL path itself
    (`/bot<token>/getUpdates`), and httpx logs the full request URL at
    INFO. Without this, every poll cycle would write the live token into
    the container's plaintext logs. Tracks a set (one process can run
    several bots at once) and reads it live so reload()/rotation doesn't
    need to touch this filter directly.
    """
    current_tokens: set = set()

    @staticmethod
    def _redact_str(s: str) -> str:
        for t in _RedactBotTokenFilter.current_tokens:
            if t and t in s:
                s = s.replace(t, "***REDACTED***")
        return s

    def filter(self, record: logging.LogRecord) -> bool:
        if not _RedactBotTokenFilter.current_tokens:
            return True
        if isinstance(record.msg, str):
            record.msg = self._redact_str(record.msg)
        if record.args:
            # httpx logs the request URL as a lazy %-arg using an httpx.URL
            # object, not a str — must str() it to check/redact, but only
            # replace the type when a token actually matched (an unrelated
            # int arg like a %d status code must stay an int or
            # `%d % "redacted"`-style formatting blows up downstream).
            def _redact_arg(a):
                s = str(a)
                redacted = self._redact_str(s)
                return redacted if redacted != s else a
            record.args = tuple(_redact_arg(a) for a in record.args)
        return True


logging.getLogger("httpx").addFilter(_RedactBotTokenFilter())
logging.getLogger("httpcore").addFilter(_RedactBotTokenFilter())

POLL_TIMEOUT = 30  # seconds; Telegram long-polls up to this before returning empty
MAX_MESSAGE_LEN = 4000  # Telegram's hard cap is 4096; leave headroom for our own text


def _make_api_token(owner: str) -> tuple[str, str]:
    """Create a chat-scoped ApiToken for owner. Returns (token_id, raw_token)."""
    raw_token = "ody_" + secrets.token_urlsafe(32)
    token_hash = bcrypt.hashpw(raw_token.encode(), bcrypt.gensalt()).decode()
    token_id = str(uuid.uuid4())[:8]
    with get_db_session() as db:
        db.add(ApiToken(
            id=token_id, owner=owner, name="Telegram bot",
            token_hash=token_hash, token_prefix=raw_token[:8],
            scopes="chat", is_active=True,
        ))
    return token_id, raw_token


class TelegramBotInstance:
    """Owns one bot's polling task. One instance per configured+enabled row
    in telegram_config; the module-level `telegram_bot` registry owns the
    dict of these keyed by bot_id."""

    def __init__(self, bot_id: str):
        self.bot_id = bot_id
        self._task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None
        self._token: str | None = None
        self._stopping = False

    async def start(self):
        cfg = self._load_config()
        if not cfg or not cfg["enabled"] or not cfg["bot_token"]:
            return
        self._token = cfg["bot_token"]
        _RedactBotTokenFilter.current_tokens.add(self._token)
        self._stopping = False
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(POLL_TIMEOUT + 10, connect=10))
        self._task = asyncio.create_task(self._poll_loop(cfg.get("last_update_id")))
        logger.info("Telegram bot '%s' polling started", self.bot_id)

    async def stop(self):
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._client:
            await self._client.aclose()
            self._client = None
        if self._token:
            _RedactBotTokenFilter.current_tokens.discard(self._token)
            self._token = None
        logger.info("Telegram bot '%s' polling stopped", self.bot_id)

    def _load_config(self) -> dict | None:
        db = SessionLocal()
        try:
            cfg = db.query(TelegramConfig).filter(TelegramConfig.id == self.bot_id).first()
            if not cfg:
                return None
            return {
                "bot_token": cfg.bot_token,
                "enabled": cfg.enabled,
                "last_update_id": cfg.last_update_id,
            }
        finally:
            db.close()

    def _save_offset(self, update_id: int):
        db = SessionLocal()
        try:
            db.query(TelegramConfig).filter(TelegramConfig.id == self.bot_id).update(
                {"last_update_id": update_id}
            )
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()

    async def _api_call(self, method: str, **params) -> dict:
        resp = await self._client.post(f"{TELEGRAM_API}/bot{self._token}/{method}", json=params)
        resp.raise_for_status()
        return resp.json()

    async def _send(self, chat_id, text: str):
        text = text[:MAX_MESSAGE_LEN]
        try:
            await self._api_call("sendMessage", chat_id=chat_id, text=text)
        except Exception as e:
            logger.warning("Telegram sendMessage failed: %s", e)

    async def _poll_loop(self, start_offset: int | None):
        offset = (start_offset or 0) + 1 if start_offset else None
        backoff = 1
        while not self._stopping:
            try:
                result = await self._api_call(
                    "getUpdates", timeout=POLL_TIMEOUT, offset=offset,
                    allowed_updates=["message"],
                )
                backoff = 1
                for update in result.get("result", []):
                    offset = update["update_id"] + 1
                    self._save_offset(update["update_id"])
                    try:
                        await self._handle_update(update)
                    except Exception as e:
                        logger.warning("Telegram update handling failed: %s", e)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Telegram getUpdates failed, backing off %ss: %s", backoff, e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _handle_update(self, update: dict):
        message = update.get("message")
        if not message or "text" not in message:
            return
        chat_id = str(message["chat"]["id"])
        text = message["text"].strip()

        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            code = parts[1].strip() if len(parts) > 1 else ""
            await self._handle_start(chat_id, code)
            return

        link = self._get_link(chat_id)
        if not link:
            await self._send(
                chat_id,
                "This chat isn't linked to an Odysseus account yet. "
                "Generate a link code for this bot from Settings → Integrations → Telegram, "
                "then send /start <code> here.",
            )
            return

        if text.startswith("/help"):
            await self._send(chat_id, (
                "/new - start a new session\n"
                "/sessions - list recent sessions\n"
                "/switch <id-prefix> - switch active session\n"
                "/model [name] - show or switch the active session's model\n"
                "/whoami - show linked account + active session\n"
            ))
        elif text.startswith("/whoami"):
            await self._send(
                chat_id,
                f"Linked as: {link['owner']}\nActive session: {link['active_session_id'] or '(none yet, /new to start)'}",
            )
        elif text.startswith("/new"):
            self._set_active_session(chat_id, None)
            await self._send(chat_id, "Started a fresh session. Send a message to begin.")
        elif text.startswith("/sessions"):
            await self._send(chat_id, self._list_sessions(link["owner"]))
        elif text.startswith("/switch"):
            parts = text.split(maxsplit=1)
            prefix = parts[1].strip() if len(parts) > 1 else ""
            await self._handle_switch(chat_id, link["owner"], prefix)
        elif text.startswith("/model"):
            parts = text.split(maxsplit=1)
            arg = parts[1].strip() if len(parts) > 1 else ""
            await self._handle_model(chat_id, link, arg)
        else:
            await self._forward_chat(chat_id, link, text)

    async def _handle_start(self, chat_id: str, code: str):
        if not code:
            await self._send(chat_id, "Send /start <link_code> using the code shown in Odysseus → Settings → Integrations → Telegram.")
            return
        with get_db_session() as db:
            row = db.query(TelegramLink).filter(
                TelegramLink.link_code == code, TelegramLink.is_active == False  # noqa: E712
            ).first()
            if not row:
                await self._send(chat_id, "That link code is invalid or already used. Generate a new one in Odysseus.")
                return
            if row.bot_id and row.bot_id != self.bot_id:
                await self._send(chat_id, "That link code was generated for a different bot — use the bot shown next to it in Odysseus.")
                return
            # Replace any existing link for THIS bot + chat (not other bots'
            # links to the same Telegram account — those stay independent).
            existing = db.query(TelegramLink).filter(
                TelegramLink.chat_id == chat_id, TelegramLink.bot_id == self.bot_id,
            ).first()
            if existing and existing.id != row.id:
                db.delete(existing)
            owner = row.owner
            token_id, raw_token = _make_api_token(owner)
            row.chat_id = chat_id
            row.bot_id = self.bot_id
            row.is_active = True
            row.link_code = None
            row.api_token_id = token_id
            row.token_encrypted = raw_token
        await self._send(chat_id, f"Linked to Odysseus as {owner}. Send /new to start a session, or just say hello.")

    def _get_link(self, chat_id: str) -> dict | None:
        db = SessionLocal()
        try:
            row = db.query(TelegramLink).filter(
                TelegramLink.chat_id == chat_id, TelegramLink.bot_id == self.bot_id,
                TelegramLink.is_active == True,  # noqa: E712
            ).first()
            if not row:
                return None
            return {
                "id": row.id, "owner": row.owner,
                "active_session_id": row.active_session_id,
                "token": row.token_encrypted,
            }
        finally:
            db.close()

    def _set_active_session(self, chat_id: str, session_id: str | None):
        db = SessionLocal()
        try:
            db.query(TelegramLink).filter(
                TelegramLink.chat_id == chat_id, TelegramLink.bot_id == self.bot_id,
            ).update({"active_session_id": session_id})
            db.commit()
        finally:
            db.close()

    def _list_sessions(self, owner: str) -> str:
        from core.database import Session as DbSession
        db = SessionLocal()
        try:
            rows = db.query(DbSession).filter(
                DbSession.owner == owner, DbSession.archived == False  # noqa: E712
            ).order_by(DbSession.last_accessed.desc()).limit(25).all()
            if not rows:
                return "No sessions yet. Send /new to start one."
            return "\n".join(f"{s.id[:8]}  {s.name}" for s in rows)
        finally:
            db.close()

    async def _handle_switch(self, chat_id: str, owner: str, prefix: str):
        if not prefix:
            await self._send(chat_id, "Usage: /switch <id-prefix> (see /sessions for ids)")
            return
        from core.database import Session as DbSession
        db = SessionLocal()
        try:
            match = db.query(DbSession).filter(
                DbSession.owner == owner, DbSession.id.startswith(prefix)
            ).first()
        finally:
            db.close()
        if not match:
            await self._send(chat_id, f"No session starting with '{prefix}'.")
            return
        self._set_active_session(chat_id, match.id)
        await self._send(chat_id, f"Switched to session {match.id[:8]} ({match.name}).")

    async def _handle_model(self, chat_id: str, link: dict, arg: str):
        from core.database import Session as DbSession

        if not arg:
            if not link["active_session_id"]:
                await self._send(chat_id, "No active session yet — /new or /model <name> starts one.")
                return
            db = SessionLocal()
            try:
                sess = db.query(DbSession).filter(DbSession.id == link["active_session_id"]).first()
            finally:
                db.close()
            await self._send(chat_id, f"Current model: {sess.model if sess else 'unknown'}")
            return

        from src.ai_interaction import _resolve_model
        try:
            url, model, headers = await asyncio.to_thread(_resolve_model, arg, owner=link["owner"])
        except ValueError as e:
            await self._send(chat_id, f"Couldn't resolve model '{arg}': {e}")
            return

        if link["active_session_id"]:
            db = SessionLocal()
            try:
                sess = db.query(DbSession).filter(DbSession.id == link["active_session_id"]).first()
                if not sess:
                    await self._send(chat_id, "Active session no longer exists — /new to start one.")
                    return
                sess.model = model
                sess.endpoint_url = url
                sess.headers = headers or {}
                db.commit()
            finally:
                db.close()
            await self._send(chat_id, f"Switched this session to {model}.")
        else:
            sid = str(uuid.uuid4())
            with get_db_session() as db:
                db.add(DbSession(
                    id=sid, name="Telegram", endpoint_url=url, model=model,
                    headers=headers or {}, owner=link["owner"],
                ))
            self._set_active_session(chat_id, sid)
            await self._send(chat_id, f"Started a new session with model {model}.")

    async def _forward_chat(self, chat_id: str, link: dict, text: str):
        token = link["token"]
        if not token:
            await self._send(chat_id, "This link is missing its API token — re-link with a fresh /start <code>.")
            return
        body = {"message": text}
        if link["active_session_id"]:
            body["session"] = link["active_session_id"]
        try:
            async with httpx.AsyncClient(timeout=125) as client:
                resp = await client.post(
                    f"{internal_api_base()}/api/v1/chat",
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                )
            if resp.status_code != 200:
                await self._send(chat_id, f"Odysseus returned an error ({resp.status_code}).")
                return
            data = resp.json()
        except Exception as e:
            logger.warning("Telegram->/api/v1/chat call failed: %s", e)
            await self._send(chat_id, "Couldn't reach Odysseus — try again shortly.")
            return

        if not link["active_session_id"] and data.get("session_id"):
            self._set_active_session(chat_id, data["session_id"])
        await self._send(chat_id, data.get("response") or "(empty response)")

    async def notify_owner(self, owner: str, text: str):
        """Push a notification to every chat linked to `owner` on THIS bot.
        Called only for the bot designated is_notifier=True — see
        TelegramBotRegistry.notify_owner."""
        if not self._client or not self._token:
            return
        db = SessionLocal()
        try:
            chat_ids = [
                row.chat_id for row in db.query(TelegramLink).filter(
                    TelegramLink.bot_id == self.bot_id, TelegramLink.owner == owner,
                    TelegramLink.is_active == True,  # noqa: E712
                ).all()
            ]
        finally:
            db.close()
        for chat_id in chat_ids:
            await self._send(chat_id, text)


class TelegramBotRegistry:
    """Owns every configured bot's TelegramBotInstance and routes
    notifications to whichever one is currently marked is_notifier."""

    def __init__(self):
        self._bots: dict[str, TelegramBotInstance] = {}

    async def start(self):
        for row in self._load_all_configs():
            if row["enabled"] and row["bot_token"]:
                inst = TelegramBotInstance(row["id"])
                await inst.start()
                self._bots[row["id"]] = inst

    async def stop(self):
        for inst in list(self._bots.values()):
            await inst.stop()
        self._bots.clear()

    async def reload(self):
        """Re-read every bot's config and restart polling for all of them.
        Simple full restart rather than diffing — bot counts are small
        (a handful at most) so the brief reconnect is not noticeable."""
        await self.stop()
        await self.start()

    @staticmethod
    def _load_all_configs() -> list[dict]:
        db = SessionLocal()
        try:
            return [
                {"id": r.id, "enabled": r.enabled, "bot_token": r.bot_token}
                for r in db.query(TelegramConfig).all()
            ]
        finally:
            db.close()

    async def notify_owner(self, owner: str, text: str):
        """Best-effort push through the single bot marked is_notifier."""
        db = SessionLocal()
        try:
            notifier = db.query(TelegramConfig).filter(TelegramConfig.is_notifier == True).first()  # noqa: E712
        finally:
            db.close()
        if not notifier:
            return
        inst = self._bots.get(notifier.id)
        if not inst:
            return
        await inst.notify_owner(owner, text)


telegram_bot = TelegramBotRegistry()
