from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Header, HTTPException, Request
from telegram import Update


from sqlalchemy import select

from .auto_verify import auto_find_tx_hash
from .bot import build_application
from .models import JoinRequest, Membership, PaymentOrder
from .db import SessionLocal
from .verifiers import VerificationError, verify_payment
from .config import settings
from .db import init_db

logger = logging.getLogger(__name__)
app = FastAPI(title='Shakers Alpha VIP Bot')
telegram_app = build_application()
scheduler = AsyncIOScheduler()


async def process_auto_verifications() -> None:
    with SessionLocal() as session:
        pending = session.execute(select(PaymentOrder).where(PaymentOrder.status.in_(['pending', 'verifying']))).scalars().all()
        used_hashes = {o.tx_hash for o in session.execute(select(PaymentOrder).where(PaymentOrder.tx_hash.is_not(None))).scalars().all()}
        for order in pending:
            if order.expires_at < __import__('datetime').datetime.utcnow():
                order.status = 'expired'
                order.verification_notes = 'Quote expired before payment was detected'
                continue
            try:
                tx_hash = order.tx_hash or auto_find_tx_hash(order, used_hashes)
                if not tx_hash:
                    continue
                order.status = 'verifying'
                order.tx_hash = tx_hash
                result = verify_payment(order.coin, tx_hash, order.destination_wallet, order.coin_amount)
                order.status = 'paid'
                order.paid_at = __import__('datetime').datetime.utcnow()
                order.tx_sender = result.sender
                order.verification_notes = result.notes
                used_hashes.add(tx_hash)

                membership = session.execute(select(Membership).where(Membership.user_id == order.user_id)).scalar_one_or_none()
                if membership is None:
                    membership = Membership(user_id=order.user_id, chat_id=settings.vip_chat_id, access_type='lifetime', active=True, order_code=order.order_code)
                    session.add(membership)
                else:
                    membership.active = True
                    membership.chat_id = settings.vip_chat_id
                    membership.access_type = 'lifetime'
                    membership.order_code = order.order_code

                join_request = session.execute(select(JoinRequest).where(JoinRequest.user_id == order.user_id, JoinRequest.chat_id == settings.vip_chat_id)).scalar_one_or_none()
                if join_request:
                    join_request.approved = True
                    join_request.approved_at = __import__('datetime').datetime.utcnow()

                session.commit()
                try:
                    await telegram_app.bot.approve_chat_join_request(chat_id=settings.vip_chat_id, user_id=order.user_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning('Auto-verify payment approved in DB but join request approval failed for %s: %s', order.user_id, exc)
                try:
                    await telegram_app.bot.send_message(
                        chat_id=order.user_id,
                        text=f'Payment confirmed automatically. Your access to {settings.vip_chat_title} has been activated.'
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning('Could not DM user %s after auto verification: %s', order.user_id, exc)
            except VerificationError as exc:
                order.status = 'pending'
                order.verification_notes = str(exc)
                session.commit()
            except Exception as exc:  # noqa: BLE001
                logger.warning('Auto verification error for order %s: %s', order.order_code, exc)
                order.status = 'pending'
                order.verification_notes = f'Auto verification error: {exc}'
                session.commit()
        session.commit()


@app.on_event('startup')
async def startup() -> None:
    init_db()
    await telegram_app.initialize()
    await telegram_app.start()
    webhook_url = f"{settings.public_webhook_url.rstrip('/')}/telegram/webhook"
    await telegram_app.bot.set_webhook(
        url=webhook_url,
        secret_token=settings.telegram_webhook_secret,
        allowed_updates=['message', 'callback_query', 'chat_join_request'],
    )
    scheduler.add_job(process_auto_verifications, 'interval', minutes=1, max_instances=1, coalesce=True)
    scheduler.start()
    logger.info('Webhook set to %s', webhook_url)


@app.on_event('shutdown')
async def shutdown() -> None:
    try:
        scheduler.shutdown(wait=False)
    except Exception:  # noqa: BLE001
        pass
    await telegram_app.stop()
    await telegram_app.shutdown()


@app.get('/')
async def root() -> dict[str, str]:
    return {'status': 'ok', 'bot': 'Shakers Alpha VIP Bot'}


@app.post('/telegram/webhook')
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    if x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        raise HTTPException(status_code=401, detail='Invalid webhook secret')
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {'ok': True}
