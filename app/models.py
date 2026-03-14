from datetime import datetime
from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class JoinRequest(Base):
    __tablename__ = 'join_requests'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PaymentOrder(Base):
    __tablename__ = 'payment_orders'
    __table_args__ = (
        UniqueConstraint('order_code', name='uq_order_code'),
        UniqueConstraint('tx_hash', name='uq_tx_hash'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    order_code: Mapped[str] = mapped_column(String(32), index=True)
    coin: Mapped[str] = mapped_column(String(16), index=True)  # USDT_BEP20 / BNB / ETH / SOL
    usd_amount: Mapped[float] = mapped_column(Float)
    coin_amount: Mapped[float] = mapped_column(Float)
    destination_wallet: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(24), default='pending', index=True)
    tx_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tx_sender: Mapped[str | None] = mapped_column(String(255), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verification_notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class Membership(Base):
    __tablename__ = 'memberships'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    access_type: Mapped[str] = mapped_column(String(32), default='lifetime')
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    granted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    order_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
