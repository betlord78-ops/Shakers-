from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timedelta
from html import escape

from sqlalchemy import desc, select, update as sa_update
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ChatJoinRequestHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import settings
from .db import SessionLocal
from .models import JoinRequest, Membership, PaymentOrder
from .pricing import build_quote
from .verifiers import VerificationError, verify_payment

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

COIN_LABELS = {
    "USDT_BEP20": "USDT (BEP20)",
    "BNB": "BNB (BSC)",
    "ETH": "ETH (Ethereum)",
    "SOL": "SOL (Solana)",
}

TEXT_TO_COIN = {
    "USDT (BEP20)": "USDT_BEP20",
    "SOL": "SOL",
    "ETH": "ETH",
    "BNB": "BNB",
}


def _wallet_for_coin(coin: str) -> str:
    return {
        "USDT_BEP20": settings.usdt_bep20_wallet,
        "BNB": settings.bsc_wallet,
        "ETH": settings.eth_wallet,
        "SOL": settings.sol_wallet,
    }[coin]


def _payment_text(coin: str, order_code: str, amount_text: str, wallet: str) -> str:
    return (
        f"<b>{escape(settings.vip_chat_title)}</b>\n"
        "Access: <b>Lifetime</b>\n"
        f"USD value: <b>${settings.lifetime_price_usd:.0f}</b>\n"
        f"Payment method: <b>{escape(COIN_LABELS[coin])}</b>\n"
        f"Amount to pay: <b>{escape(amount_text)}</b>\n"
        f"Wallet: <code>{escape(wallet)}</code>\n"
        f"Order ID: <code>{escape(order_code)}</code>\n"
        f"Quote expires: <b>{settings.quote_expiry_minutes} minutes</b>\n\n"
        "After payment, send your tx hash here for instant verification.\n"
        "The bot also keeps auto-checking your payment in the background."
    )


def payment_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["USDT (BEP20)"],
            ["SOL", "ETH"],
            ["BNB"],
            ["My Access Status"],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def payment_actions_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["Refresh Price", "My Access Status"],
            ["Choose Payment Method"],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        f"Welcome to <b>{escape(settings.vip_chat_title)}</b>\n\n"
        "This bot handles lifetime VIP access payments.\n"
        f"Price: <b>${settings.lifetime_price_usd:.0f}</b>\n"
        "Access: <b>Lifetime</b>\n\n"
        "Submit your join request, then choose a payment method below."
    )
    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=payment_menu_keyboard(),
    )


async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    jr = update.chat_join_request
    if jr is None or jr.chat.id != settings.vip_chat_id:
        return

    with SessionLocal() as session:
        row = session.execute(
            select(JoinRequest).where(
                JoinRequest.user_id == jr.from_user.id,
                JoinRequest.chat_id == jr.chat.id,
            )
        ).scalar_one_or_none()
        if row is None:
            row = JoinRequest(
                user_id=jr.from_user.id,
                username=jr.from_user.username,
                chat_id=jr.chat.id,
                requested_at=datetime.utcnow(),
                approved=False,
            )
            session.add(row)
        else:
            row.username = jr.from_user.username
            row.requested_at = datetime.utcnow()
            row.approved = False
            row.approved_at = None
        session.commit()

    text = (
        f"Your join request for <b>{escape(settings.vip_chat_title)}</b> is pending.\n\n"
        f"To unlock <b>lifetime access</b>, pay <b>${settings.lifetime_price_usd:.0f}</b> using one of the supported coins below."
    )
    try:
        await context.bot.send_message(
            chat_id=jr.from_user.id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=payment_menu_keyboard(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to DM user %s after join request: %s", jr.from_user.id, exc)


async def create_payment_order(target_message, context: ContextTypes.DEFAULT_TYPE, user_id: int, coin: str) -> None:
    wallet = _wallet_for_coin(coin).strip()
    if not wallet:
        await target_message.reply_text(
            f"{COIN_LABELS.get(coin, coin)} is not configured yet. Add the wallet in Railway variables and try again.",
            reply_markup=payment_menu_keyboard(),
        )
        return

    try:
        quote = await build_quote(coin)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Quote build failed for %s: %s", coin, exc)
        if coin == "USDT_BEP20":
            await target_message.reply_text(
                "USDT is fixed at $80, but the payment wallet is unavailable right now. Check your bot variables.",
                reply_markup=payment_menu_keyboard(),
            )
        else:
            await target_message.reply_text(
                "Unable to fetch a live quote right now. Please try again in a minute.",
                reply_markup=payment_menu_keyboard(),
            )
        return

    order_code = secrets.token_hex(4).upper()
    expires_at = datetime.utcnow() + timedelta(minutes=settings.quote_expiry_minutes)
    with SessionLocal() as session:
        session.execute(
            sa_update(PaymentOrder)
            .where(PaymentOrder.user_id == user_id, PaymentOrder.status == "pending")
            .values(status="expired", verification_notes="Superseded by new quote")
        )
        order = PaymentOrder(
            user_id=user_id,
            order_code=order_code,
            coin=coin,
            usd_amount=quote.usd_amount,
            coin_amount=quote.coin_amount,
            destination_wallet=quote.destination_wallet,
            expires_at=expires_at,
            status="pending",
        )
        session.add(order)
        session.commit()

    context.user_data["last_coin"] = coin
    context.user_data["awaiting_tx_hash"] = True
    await target_message.reply_text(
        _payment_text(coin, order_code, quote.display_amount, quote.destination_wallet),
        parse_mode=ParseMode.HTML,
        reply_markup=payment_actions_keyboard(),
        disable_web_page_preview=True,
    )


async def send_status(user_id: int, target_message) -> None:
    with SessionLocal() as session:
        membership = session.execute(
            select(Membership).where(Membership.user_id == user_id, Membership.active.is_(True))
        ).scalar_one_or_none()
        order = session.execute(
            select(PaymentOrder)
            .where(PaymentOrder.user_id == user_id)
            .order_by(desc(PaymentOrder.created_at))
        ).scalar_one_or_none()

    if membership:
        await target_message.reply_text(
            f"Access status: ACTIVE\nGroup: {settings.vip_chat_title}\nType: Lifetime",
            reply_markup=payment_actions_keyboard(),
        )
        return

    if order is None:
        await target_message.reply_text(
            "No payment order found yet. Choose a payment method to begin.",
            reply_markup=payment_menu_keyboard(),
        )
        return

    await target_message.reply_text(
        f"Latest order: {order.order_code}\n"
        f"Coin: {COIN_LABELS.get(order.coin, order.coin)}\n"
        f"Status: {order.status.upper()}\n"
        f"Amount: {order.coin_amount}\n\n"
        "If you already paid, send the tx hash here. The bot is also checking automatically.",
        reply_markup=payment_actions_keyboard(),
    )


async def tx_hash_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tx_hash = (update.effective_message.text or "").strip()
    user_id = update.effective_user.id

    with SessionLocal() as session:
        order = session.execute(
            select(PaymentOrder)
            .where(PaymentOrder.user_id == user_id, PaymentOrder.status == "pending")
            .order_by(desc(PaymentOrder.created_at))
        ).scalar_one_or_none()

        if order is None:
            await update.effective_message.reply_text(
                "No active payment order found. Choose a payment method first.",
                reply_markup=payment_menu_keyboard(),
            )
            return

        if order.expires_at < datetime.utcnow():
            order.status = "expired"
            order.verification_notes = "Order expired before tx submission"
            session.commit()
            await update.effective_message.reply_text(
                "That quote has expired. Tap Refresh Price or choose a coin again.",
                reply_markup=payment_actions_keyboard(),
            )
            return

        order.status = "verifying"
        order.tx_hash = tx_hash
        order_id = order.id
        order_coin = order.coin
        order_wallet = order.destination_wallet
        order_amount = order.coin_amount
        session.commit()

    await update.effective_message.reply_text(
        "Checking your transaction now...",
        reply_markup=payment_actions_keyboard(),
        disable_web_page_preview=True,
    )

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(verify_payment, order_coin, tx_hash, order_wallet, order_amount),
            timeout=25,
        )
    except asyncio.TimeoutError:
        with SessionLocal() as session:
            current = session.execute(select(PaymentOrder).where(PaymentOrder.id == order_id)).scalar_one_or_none()
            if current:
                current.status = "pending"
                current.verification_notes = "Verification timed out"
                session.commit()
        await update.effective_message.reply_text(
            "Verification timed out. Please send the tx hash again in a moment.",
            reply_markup=payment_actions_keyboard(),
        )
        return
    except VerificationError as exc:
        with SessionLocal() as session:
            current = session.execute(select(PaymentOrder).where(PaymentOrder.id == order_id)).scalar_one_or_none()
            if current:
                current.status = "pending"
                current.verification_notes = str(exc)
                session.commit()
        await update.effective_message.reply_text(
            f"Payment not verified yet.\n\nReason: {exc}",
            reply_markup=payment_actions_keyboard(),
        )
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected verification error for user %s: %s", user_id, exc)
        with SessionLocal() as session:
            current = session.execute(select(PaymentOrder).where(PaymentOrder.id == order_id)).scalar_one_or_none()
            if current:
                current.status = "pending"
                current.verification_notes = f"Unexpected verification error: {exc}"
                session.commit()
        await update.effective_message.reply_text(
            "Unexpected verification error. Please try again in a moment.",
            reply_markup=payment_actions_keyboard(),
        )
        return

    with SessionLocal() as session:
        current = session.execute(select(PaymentOrder).where(PaymentOrder.id == order_id)).scalar_one_or_none()
        if current is None:
            await update.effective_message.reply_text(
                "Order no longer exists. Choose a payment method again.",
                reply_markup=payment_menu_keyboard(),
            )
            return

        current.status = "paid"
        current.paid_at = datetime.utcnow()
        current.tx_sender = result.sender
        current.verification_notes = result.notes

        membership = session.execute(
            select(Membership).where(Membership.user_id == user_id)
        ).scalar_one_or_none()
        if membership is None:
            membership = Membership(
                user_id=user_id,
                chat_id=settings.vip_chat_id,
                access_type="lifetime",
                active=True,
                order_code=current.order_code,
            )
            session.add(membership)
        else:
            membership.active = True
            membership.chat_id = settings.vip_chat_id
            membership.access_type = "lifetime"
            membership.order_code = current.order_code

        join_request = session.execute(
            select(JoinRequest).where(
                JoinRequest.user_id == user_id,
                JoinRequest.chat_id == settings.vip_chat_id,
            )
        ).scalar_one_or_none()
        if join_request:
            join_request.approved = True
            join_request.approved_at = datetime.utcnow()

        session.commit()

    try:
        await context.bot.approve_chat_join_request(chat_id=settings.vip_chat_id, user_id=user_id)
        approved_msg = (
            "Payment confirmed.\n\n"
            f"Your access to {settings.vip_chat_title} has been activated and your join request has been approved."
        )
        await update.effective_message.reply_text(approved_msg, reply_markup=ReplyKeyboardRemove())
    except Exception as exc:  # noqa: BLE001
        logger.exception("Verified but failed to approve join request: %s", exc)
        await update.effective_message.reply_text(
            "Payment verified successfully, but I could not approve the join request automatically. Please contact support immediately.",
            reply_markup=payment_actions_keyboard(),
        )


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.effective_message.text or "").strip()
    if text in TEXT_TO_COIN:
        await create_payment_order(update.effective_message, context, update.effective_user.id, TEXT_TO_COIN[text])
        return
    if text == "Choose Payment Method":
        context.user_data["awaiting_tx_hash"] = False
        await update.effective_message.reply_text(
            "Choose your payment method:",
            reply_markup=payment_menu_keyboard(),
        )
        return
    if text == "Refresh Price":
        coin = context.user_data.get("last_coin")
        if not coin:
            await update.effective_message.reply_text(
                "Choose a payment method first.",
                reply_markup=payment_menu_keyboard(),
            )
            return
        await create_payment_order(update.effective_message, context, update.effective_user.id, coin)
        return
    if text == "My Access Status":
        await send_status(update.effective_user.id, update.effective_message)
        return
    if text.startswith("/"):
        return
    await tx_hash_received(update, context)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_status(update.effective_user.id, update.effective_message)


async def admin_paid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in settings.admin_ids:
        await update.effective_message.reply_text("Not allowed.")
        return
    with SessionLocal() as session:
        orders = session.execute(
            select(PaymentOrder)
            .where(PaymentOrder.status == "paid")
            .order_by(desc(PaymentOrder.paid_at))
            .limit(20)
        ).scalars().all()
    if not orders:
        await update.effective_message.reply_text("No paid orders yet.")
        return
    lines = ["Latest paid orders:"]
    for o in orders:
        lines.append(f"- {o.order_code} | user {o.user_id} | {o.coin} {o.coin_amount} | {o.paid_at}")
    await update.effective_message.reply_text("\n".join(lines))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled bot error: %s", context.error)


def build_application() -> Application:
    application = Application.builder().token(settings.telegram_bot_token).updater(None).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("paid_orders", admin_paid))
    application.add_handler(ChatJoinRequestHandler(on_join_request))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    application.add_error_handler(error_handler)
    return application
