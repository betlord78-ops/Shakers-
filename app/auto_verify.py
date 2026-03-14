from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

from solders.signature import Signature
from solana.rpc.api import Client as SolanaClient
from web3 import HTTPProvider, Web3

from .config import settings
from .models import PaymentOrder

BEP20_TRANSFER_TOPIC = Web3.keccak(text='Transfer(address,address,uint256)').hex()
USDT_BEP20_DECIMALS = 18


class AutoVerifyError(Exception):
    pass


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class EvmAutoScanner:
    def __init__(self, rpc_url: str):
        self.w3 = Web3(HTTPProvider(rpc_url, request_kwargs={'timeout': 20}))

    def _range_for_order(self, order: PaymentOrder) -> tuple[int, int]:
        latest = self.w3.eth.block_number
        age_seconds = max(60, int((datetime.utcnow() - order.created_at).total_seconds()) + 180)
        if order.coin in {'BNB', 'USDT_BEP20'}:
            approx_block_time = 3
            pad = 120
        else:
            approx_block_time = 12
            pad = 60
        blocks_back = max(pad, age_seconds // approx_block_time + pad)
        return max(0, latest - blocks_back), latest

    def _is_order_window(self, block_ts: int, order: PaymentOrder) -> bool:
        ts = datetime.fromtimestamp(block_ts, tz=timezone.utc)
        return _utc(order.created_at) <= ts <= _utc(order.expires_at)

    def find_native_payment(self, order: PaymentOrder, exclude_hashes: Iterable[str]) -> str | None:
        exclude = {h.lower() for h in exclude_hashes if h}
        start, end = self._range_for_order(order)
        expected_to = Web3.to_checksum_address(order.destination_wallet)
        expected = Decimal(str(order.coin_amount))

        for block_num in range(end, start - 1, -1):
            block = self.w3.eth.get_block(block_num, full_transactions=True)
            if not self._is_order_window(block['timestamp'], order):
                continue
            for tx in block['transactions']:
                tx_hash = tx['hash'].hex().lower()
                if tx_hash in exclude:
                    continue
                to = tx.get('to')
                if not to:
                    continue
                try:
                    if Web3.to_checksum_address(to) != expected_to:
                        continue
                except Exception:
                    continue
                amount = Decimal(self.w3.from_wei(tx['value'], 'ether'))
                if amount >= expected:
                    return tx['hash'].hex()
        return None

    def find_usdt_payment(self, order: PaymentOrder, exclude_hashes: Iterable[str]) -> str | None:
        exclude = {h.lower() for h in exclude_hashes if h}
        start, end = self._range_for_order(order)
        destination_topic = '0x' + ('0' * 24) + order.destination_wallet.lower().replace('0x', '')
        logs = self.w3.eth.get_logs({
            'fromBlock': start,
            'toBlock': end,
            'address': Web3.to_checksum_address(settings.usdt_bep20_contract),
            'topics': [BEP20_TRANSFER_TOPIC, None, destination_topic],
        })
        expected = Decimal(str(order.coin_amount))
        for log in reversed(logs):
            tx_hash = log['transactionHash'].hex().lower()
            if tx_hash in exclude:
                continue
            amount = Decimal(int(log['data'].hex(), 16)) / Decimal(10**USDT_BEP20_DECIMALS)
            if amount < expected:
                continue
            block = self.w3.eth.get_block(log['blockNumber'])
            if not self._is_order_window(block['timestamp'], order):
                continue
            return log['transactionHash'].hex()
        return None


class SolAutoScanner:
    def __init__(self, rpc_url: str):
        self.client = SolanaClient(rpc_url)

    def find_payment(self, order: PaymentOrder, exclude_hashes: Iterable[str]) -> str | None:
        exclude = {h for h in exclude_hashes if h}
        expected = Decimal(str(order.coin_amount))
        sigs = self.client.get_signatures_for_address(order.destination_wallet, limit=25)
        if not sigs.value:
            return None
        for siginfo in sigs.value:
            sig = str(siginfo.signature)
            if sig in exclude:
                continue
            if siginfo.err is not None:
                continue
            if siginfo.block_time is None:
                continue
            ts = datetime.fromtimestamp(siginfo.block_time, tz=timezone.utc)
            if not (_utc(order.created_at) <= ts <= _utc(order.expires_at)):
                continue
            tx = self.client.get_transaction(Signature.from_string(sig), encoding='jsonParsed', max_supported_transaction_version=0)
            value = tx.value
            if value is None or value.transaction.meta is None or value.transaction.meta.err is not None:
                continue
            message = value.transaction.transaction.message
            matched = Decimal('0')
            for ix in message.instructions:
                parsed = getattr(ix, 'parsed', None)
                if not parsed:
                    continue
                info = parsed.get('info', {})
                if ix.program == 'system' and parsed.get('type') == 'transfer' and info.get('destination') == order.destination_wallet:
                    matched += Decimal(info.get('lamports', 0)) / Decimal(1_000_000_000)
            if matched >= expected:
                return sig
        return None


def auto_find_tx_hash(order: PaymentOrder, exclude_hashes: Iterable[str]) -> str | None:
    if order.coin == 'USDT_BEP20':
        return EvmAutoScanner(settings.bsc_rpc_url).find_usdt_payment(order, exclude_hashes)
    if order.coin == 'BNB':
        return EvmAutoScanner(settings.bsc_rpc_url).find_native_payment(order, exclude_hashes)
    if order.coin == 'ETH':
        return EvmAutoScanner(settings.eth_rpc_url).find_native_payment(order, exclude_hashes)
    if order.coin == 'SOL':
        return SolAutoScanner(settings.sol_rpc_url).find_payment(order, exclude_hashes)
    raise AutoVerifyError(f'Unsupported coin: {order.coin}')
