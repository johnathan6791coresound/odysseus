"""Telegram bot configuration + chat-link management — /api/telegram/*.

The bot itself (long-polling, command handling) lives in src/telegram_bot.py.
This module only exposes the admin/user-facing HTTP surface:
  - admin registers/edits/removes bots (each with its own token), and marks
    at most one of them as the notifier bot for scheduled-task pushes
  - a signed-in user generates a short-lived link code for a specific bot,
    then sends `/start <code>` to that bot in Telegram to bind the chat to
    their account (a user may link several bots, each to a different chat)
"""

import secrets
import uuid

from fastapi import APIRouter, HTTPException, Request

from core.database import get_db_session, TelegramConfig, TelegramLink, ApiToken
from core.middleware import require_admin
from src.auth_helpers import get_current_user

MAX_NAME_LEN = 60


def setup_telegram_routes(telegram_bot=None) -> APIRouter:
    router = APIRouter(prefix="/api/telegram", tags=["telegram"])

    def _bot_out(cfg: TelegramConfig) -> dict:
        return {
            "id": cfg.id,
            "name": cfg.name or "Telegram Bot",
            "configured": bool(cfg.bot_token),
            "enabled": bool(cfg.enabled),
            "is_notifier": bool(cfg.is_notifier),
        }

    @router.get("/bots")
    def list_bots(request: Request):
        # Login-only, not admin-only: any user needs this list to pick which
        # bot to link their account to. _bot_out never exposes the token.
        if not get_current_user(request):
            raise HTTPException(401, "Login required")
        with get_db_session() as db:
            return [_bot_out(c) for c in db.query(TelegramConfig).order_by(TelegramConfig.created_at).all()]

    @router.post("/bots")
    async def create_bot(request: Request):
        require_admin(request)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        name = (payload.get("name") or "Telegram Bot").strip()[:MAX_NAME_LEN] or "Telegram Bot"
        bot_token = (payload.get("bot_token") or "").strip()
        if not bot_token:
            raise HTTPException(400, "bot_token is required")
        make_notifier = bool(payload.get("is_notifier"))

        with get_db_session() as db:
            if make_notifier:
                db.query(TelegramConfig).update({"is_notifier": False})
            cfg = TelegramConfig(
                id=str(uuid.uuid4()), name=name, bot_token=bot_token,
                enabled=bool(payload.get("enabled", True)), is_notifier=make_notifier,
            )
            db.add(cfg)
            result = _bot_out(cfg)

        if telegram_bot is not None:
            await telegram_bot.reload()
        return result

    @router.patch("/bots/{bot_id}")
    async def update_bot(request: Request, bot_id: str):
        require_admin(request)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        with get_db_session() as db:
            cfg = db.query(TelegramConfig).filter(TelegramConfig.id == bot_id).first()
            if not cfg:
                raise HTTPException(404, "Bot not found")
            if isinstance(payload.get("name"), str) and payload["name"].strip():
                cfg.name = payload["name"].strip()[:MAX_NAME_LEN]
            bot_token = payload.get("bot_token")
            if isinstance(bot_token, str) and bot_token.strip():
                cfg.bot_token = bot_token.strip()
                cfg.last_update_id = None  # fresh token → fresh polling cursor
            if isinstance(payload.get("enabled"), bool):
                cfg.enabled = payload["enabled"]
            if payload.get("is_notifier") is True:
                db.query(TelegramConfig).filter(TelegramConfig.id != bot_id).update({"is_notifier": False})
                cfg.is_notifier = True
            elif payload.get("is_notifier") is False:
                cfg.is_notifier = False
            result = _bot_out(cfg)

        if telegram_bot is not None:
            await telegram_bot.reload()
        return result

    @router.delete("/bots/{bot_id}")
    async def delete_bot(request: Request, bot_id: str):
        require_admin(request)
        with get_db_session() as db:
            cfg = db.query(TelegramConfig).filter(TelegramConfig.id == bot_id).first()
            if not cfg:
                raise HTTPException(404, "Bot not found")
            links = db.query(TelegramLink).filter(TelegramLink.bot_id == bot_id).all()
            for link in links:
                if link.api_token_id:
                    db.query(ApiToken).filter(ApiToken.id == link.api_token_id).delete()
            db.delete(cfg)  # cascades to its TelegramLink rows

        if telegram_bot is not None:
            await telegram_bot.reload()
        return {"status": "deleted"}

    @router.post("/link")
    async def create_link_code(request: Request):
        """Generate a one-time code for a specific bot; the user pastes it
        into that bot on Telegram as `/start <code>`."""
        owner = get_current_user(request)
        if not owner:
            raise HTTPException(401, "Login required")
        bot_id = request.query_params.get("bot_id")
        if not bot_id:
            try:
                payload = await request.json()
            except Exception:
                payload = {}
            bot_id = (payload or {}).get("bot_id") if isinstance(payload, dict) else None
        if not bot_id:
            raise HTTPException(400, "bot_id is required")

        code = secrets.token_urlsafe(9)  # short enough to type/paste comfortably
        with get_db_session() as db:
            bot = db.query(TelegramConfig).filter(TelegramConfig.id == bot_id).first()
            if not bot:
                raise HTTPException(404, "Bot not found")
            # One pending code per (owner, bot) at a time — replace any stale one.
            db.query(TelegramLink).filter(
                TelegramLink.owner == owner, TelegramLink.bot_id == bot_id,
                TelegramLink.is_active == False,  # noqa: E712
            ).delete(synchronize_session=False)
            row = TelegramLink(
                id=str(uuid.uuid4()),
                chat_id=f"pending:{uuid.uuid4()}",  # placeholder until claimed
                bot_id=bot_id,
                owner=owner,
                link_code=code,
                is_active=False,
            )
            db.add(row)
        return {"link_code": code}

    @router.get("/status")
    def link_status(request: Request):
        owner = get_current_user(request)
        if not owner:
            raise HTTPException(401, "Login required")
        with get_db_session() as db:
            links = db.query(TelegramLink).filter(
                TelegramLink.owner == owner, TelegramLink.is_active == True  # noqa: E712
            ).all()
            return {
                "linked_chats": [
                    {
                        "id": l.id,
                        "bot_id": l.bot_id,
                        "bot_name": (l.bot.name if l.bot else None) or "Telegram Bot",
                        "active_session_id": l.active_session_id,
                    }
                    for l in links
                ]
            }

    @router.delete("/link/{link_id}")
    def unlink(request: Request, link_id: str):
        owner = get_current_user(request)
        if not owner:
            raise HTTPException(401, "Login required")
        with get_db_session() as db:
            row = db.query(TelegramLink).filter(TelegramLink.id == link_id).first()
            if not row or row.owner != owner:
                raise HTTPException(404, "Link not found")
            if row.api_token_id:
                db.query(ApiToken).filter(ApiToken.id == row.api_token_id).delete()
            db.delete(row)
        return {"status": "unlinked"}

    return router
