from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta

from sqlalchemy import desc, select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    ChatJoinRequestHandler,
    filters,
)

from .config import settings
from .db import SessionLocal
from .models import JoinRequest, Membership, PaymentOrder
from .pricing import build_quote
from .verifiers import VerificationError, verify_payment

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

WAITING_TX_HASH = 1
COIN_LABELS = {
    'USDT_BEP20': 'USDT (BEP20)',
    'BNB': 'BNB (BSC)',
    'ETH': 'ETH (Ethereum)',
    'SOL': 'SOL (Solana)',
}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        f"Welcome to *{settings.vip_chat_title}*\n\n"
        "This bot handles lifetime VIP access payments.\n"
        f"Price: *${settings.lifetime_price_usd:.0f}*\n"
        "Access: *Lifetime*\n\n"
        "Tap the button below to choose your payment method after you submit your Telegram join request."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton('Choose Payment Method', callback_data='menu:pay')],
        [InlineKeyboardButton('Support', url=f"https://t.me/{settings.support_username.lstrip('@')}")],
    ])
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)


async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    jr = update.chat_join_request
    if jr is None:
        return
    if jr.chat.id != settings.vip_chat_id:
        return

    with SessionLocal() as session:
        row = session.execute(select(JoinRequest).where(JoinRequest.user_id == jr.from_user.id, JoinRequest.chat_id == jr.chat.id)).scalar_one_or_none()
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
        f"Your join request for *{settings.vip_chat_title}* is pending.\n\n"
        f"To unlock *lifetime access*, pay *${settings.lifetime_price_usd:.0f}* using one of the supported coins below."
    )
    try:
        await context.bot.send_message(
            chat_id=jr.from_user.id,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=payment_menu_keyboard(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning('Failed to DM user %s after join request: %s', jr.from_user.id, exc)


async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ''

    if data == 'menu:pay':
        await query.message.reply_text('Choose your payment method:', reply_markup=payment_menu_keyboard())
        return ConversationHandler.END

    if data.startswith('coin:'):
        coin = data.split(':', 1)[1]
        try:
            quote = await build_quote(coin)
        except Exception as exc:  # noqa: BLE001
            await query.message.reply_text(f'Unable to fetch a live quote right now. Please try again in a minute.\n\nError: {exc}')
            return ConversationHandler.END

        order_code = secrets.token_hex(4).upper()
        expires_at = datetime.utcnow() + timedelta(minutes=settings.quote_expiry_minutes)
        with SessionLocal() as session:
            session.execute(
                PaymentOrder.__table__.update()
                .where(PaymentOrder.user_id == query.from_user.id, PaymentOrder.status == 'pending')
                .values(status='expired', verification_notes='Superseded by new quote')
            )
            order = PaymentOrder(
                user_id=query.from_user.id,
                order_code=order_code,
                coin=coin,
                usd_amount=quote.usd_amount,
                coin_amount=quote.coin_amount,
                destination_wallet=quote.destination_wallet,
                expires_at=expires_at,
                status='pending',
            )
            session.add(order)
            session.commit()

        text = (
            f"*{settings.vip_chat_title}*\n"
            "Access: *Lifetime*\n"
            f"USD value: *${quote.usd_amount:.0f}*\n"
            f"Payment method: *{COIN_LABELS[coin]}*\n"
            f"Amount to pay: *{quote.display_amount}*\n"
            f"Wallet: `{quote.destination_wallet}`\n"
            f"Order ID: `{order_code}`\n"
            f"Quote expires: *{settings.quote_expiry_minutes} minutes*\n\n"
            "The bot will auto-check your payment in the background. You can also tap *Submit Tx Hash* for instant manual verification."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton('Submit Tx Hash', callback_data='action:submit_tx')],
            [InlineKeyboardButton('Refresh Price', callback_data=f'coin:{coin}')],
            [InlineKeyboardButton('Choose Another Coin', callback_data='menu:pay')],
            [InlineKeyboardButton('Check Payment Now', callback_data='action:status')],
        ])
        await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return ConversationHandler.END

    if data == 'action:submit_tx':
        await query.message.reply_text('Send your transaction hash for verification.')
        return WAITING_TX_HASH

    if data == 'action:status':
        await send_status(query.from_user.id, query.message, context)
        return ConversationHandler.END

    return ConversationHandler.END


async def tx_hash_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tx_hash = (update.effective_message.text or '').strip()
    user_id = update.effective_user.id

    with SessionLocal() as session:
        order = session.execute(
            select(PaymentOrder)
            .where(PaymentOrder.user_id == user_id, PaymentOrder.status == 'pending')
            .order_by(desc(PaymentOrder.created_at))
        ).scalar_one_or_none()

        if order is None:
            await update.effective_message.reply_text('No active payment order found. Tap /start and create a new payment quote.')
            return ConversationHandler.END

        if order.expires_at < datetime.utcnow():
            order.status = 'expired'
            order.verification_notes = 'Order expired before tx submission'
            session.commit()
            await update.effective_message.reply_text('That quote has expired. Tap /start and generate a fresh quote.')
            return ConversationHandler.END

        order.status = 'verifying'
        order.tx_hash = tx_hash
        session.commit()

        try:
            result = verify_payment(order.coin, tx_hash, order.destination_wallet, order.coin_amount)
        except VerificationError as exc:
            order.status = 'pending'
            order.verification_notes = str(exc)
            session.commit()
            await update.effective_message.reply_text(f'Payment not verified yet.\n\nReason: {exc}')
            return ConversationHandler.END
        except Exception as exc:  # noqa: BLE001
            order.status = 'pending'
            order.verification_notes = f'Unexpected verification error: {exc}'
            session.commit()
            await update.effective_message.reply_text(f'Unexpected verification error.\n\n{exc}')
            return ConversationHandler.END

        order.status = 'paid'
        order.paid_at = datetime.utcnow()
        order.tx_sender = result.sender
        order.verification_notes = result.notes

        membership = session.execute(select(Membership).where(Membership.user_id == user_id)).scalar_one_or_none()
        if membership is None:
            membership = Membership(user_id=user_id, chat_id=settings.vip_chat_id, access_type='lifetime', active=True, order_code=order.order_code)
            session.add(membership)
        else:
            membership.active = True
            membership.chat_id = settings.vip_chat_id
            membership.access_type = 'lifetime'
            membership.order_code = order.order_code

        join_request = session.execute(
            select(JoinRequest).where(JoinRequest.user_id == user_id, JoinRequest.chat_id == settings.vip_chat_id)
        ).scalar_one_or_none()
        if join_request:
            join_request.approved = True
            join_request.approved_at = datetime.utcnow()

        session.commit()

    try:
        await context.bot.approve_chat_join_request(chat_id=settings.vip_chat_id, user_id=user_id)
        approved_msg = (
            f"Payment confirmed.\n\n"
            f"Your access to *{settings.vip_chat_title}* has been activated and your join request has been approved."
        )
        await update.effective_message.reply_text(approved_msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as exc:  # noqa: BLE001
        logger.exception('Verified but failed to approve join request: %s', exc)
        await update.effective_message.reply_text(
            'Payment verified successfully, but I could not approve the join request automatically. Please contact support immediately.'
        )
    return ConversationHandler.END


async def send_status(user_id: int, target_message, context: ContextTypes.DEFAULT_TYPE) -> None:
    with SessionLocal() as session:
        membership = session.execute(select(Membership).where(Membership.user_id == user_id, Membership.active.is_(True))).scalar_one_or_none()
        order = session.execute(
            select(PaymentOrder).where(PaymentOrder.user_id == user_id).order_by(desc(PaymentOrder.created_at))
        ).scalar_one_or_none()

    if membership:
        await target_message.reply_text(f'Access status: ACTIVE\nGroup: {settings.vip_chat_title}\nType: Lifetime')
        return

    if order is None:
        await target_message.reply_text('No payment order found yet. Tap /start to begin.')
        return

    await target_message.reply_text(
        f'Latest order: {order.order_code}\nCoin: {COIN_LABELS.get(order.coin, order.coin)}\nStatus: {order.status.upper()}\nAmount: {order.coin_amount}\n\nIf you already paid, the bot is still checking automatically. You can also submit the tx hash manually.'
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_status(update.effective_user.id, update.effective_message, context)


async def admin_paid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in settings.admin_ids:
        await update.effective_message.reply_text('Not allowed.')
        return
    with SessionLocal() as session:
        orders = session.execute(
            select(PaymentOrder).where(PaymentOrder.status == 'paid').order_by(desc(PaymentOrder.paid_at)).limit(20)
        ).scalars().all()
    if not orders:
        await update.effective_message.reply_text('No paid orders yet.')
        return
    lines = ['Latest paid orders:']
    for o in orders:
        lines.append(f'- {o.order_code} | user {o.user_id} | {o.coin} {o.coin_amount} | {o.paid_at}')
    await update.effective_message.reply_text('\n'.join(lines))


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text('Cancelled.')
    return ConversationHandler.END


def payment_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('USDT (BEP20)', callback_data='coin:USDT_BEP20')],
        [InlineKeyboardButton('SOL', callback_data='coin:SOL')],
        [InlineKeyboardButton('ETH', callback_data='coin:ETH')],
        [InlineKeyboardButton('BNB', callback_data='coin:BNB')],
        [InlineKeyboardButton('My Access Status', callback_data='action:status')],
    ])


def build_application() -> Application:
    application = Application.builder().token(settings.telegram_bot_token).updater(None).build()
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(menu_handler, pattern=r'^(menu:pay|coin:|action:submit_tx|action:status)')],
        states={
            WAITING_TX_HASH: [MessageHandler(filters.TEXT & ~filters.COMMAND, tx_hash_received)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        per_chat=True,
        per_user=True,
    )
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('status', status_command))
    application.add_handler(CommandHandler('paid_orders', admin_paid))
    application.add_handler(ChatJoinRequestHandler(on_join_request))
    application.add_handler(conv)
    return application
