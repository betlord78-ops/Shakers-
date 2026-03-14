from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

from solders.signature import Signature
from solana.rpc.api import Client as SolanaClient
from web3 import HTTPProvider, Web3

from .config import settings

BEP20_TRANSFER_TOPIC = Web3.keccak(text='Transfer(address,address,uint256)').hex()
USDT_BEP20_DECIMALS = 18
ZEROX = '0x000000000000000000000000'
EVM_TX_RE = re.compile(r'0x[a-fA-F0-9]{64}')
SOL_SIG_RE = re.compile(r'[1-9A-HJ-NP-Za-km-z]{43,100}')


class VerificationError(Exception):
    pass


@dataclass
class VerificationResult:
    ok: bool
    sender: str | None = None
    notes: str | None = None


def _extract_tx_hash(raw_value: str, coin: str) -> str:
    value = raw_value.strip()
    if value.startswith('http://') or value.startswith('https://'):
        parsed = urlparse(value)
        path = parsed.path.rstrip('/')
        last_segment = path.split('/')[-1] if path else ''
        value = last_segment or value

    if coin in {'USDT_BEP20', 'BNB', 'ETH'}:
        match = EVM_TX_RE.search(value)
        if not match:
            raise VerificationError('Please send a valid transaction hash or explorer link.')
        return match.group(0)

    if coin == 'SOL':
        candidates = [value]
        candidates.extend(SOL_SIG_RE.findall(value))
        for candidate in candidates:
            try:
                Signature.from_string(candidate)
                return candidate
            except Exception:
                continue
        raise VerificationError('Please send the Solana tx signature or a Solscan link.')

    return value


class EVMVerifier:
    def __init__(self, rpc_url: str):
        self.w3 = Web3(HTTPProvider(rpc_url))

    def _require_receipt(self, tx_hash: str) -> Any:
        try:
            tx = self.w3.eth.get_transaction(tx_hash)
            receipt = self.w3.eth.get_transaction_receipt(tx_hash)
            current_block = self.w3.eth.block_number
            if receipt is None or receipt.status != 1:
                raise VerificationError('Transaction not confirmed successfully yet.')
            return tx, receipt, current_block
        except VerificationError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise VerificationError(f'Unable to read transaction: {exc}') from exc

    def verify_native(self, tx_hash: str, expected_to: str, expected_amount: float, min_confirmations: int) -> VerificationResult:
        tx, receipt, current_block = self._require_receipt(tx_hash)
        if (current_block - receipt.blockNumber + 1) < min_confirmations:
            raise VerificationError('Transaction has not reached the required confirmations yet.')
        if not tx['to'] or Web3.to_checksum_address(tx['to']) != Web3.to_checksum_address(expected_to):
            raise VerificationError('Recipient wallet does not match the configured payment wallet.')
        actual_amount = Decimal(self.w3.from_wei(tx['value'], 'ether'))
        expected = Decimal(str(expected_amount))
        if actual_amount < expected:
            raise VerificationError(f'Amount too low. Expected at least {expected}, got {actual_amount}.')
        sender = tx['from']
        return VerificationResult(ok=True, sender=sender, notes=f'Native payment verified: {actual_amount}')

    def verify_bep20_usdt(self, tx_hash: str, expected_to: str, expected_amount: float, min_confirmations: int) -> VerificationResult:
        tx, receipt, current_block = self._require_receipt(tx_hash)
        if (current_block - receipt.blockNumber + 1) < min_confirmations:
            raise VerificationError('Transaction has not reached the required confirmations yet.')

        expected_lower = expected_to.lower().replace('0x', '')
        actual_amount = Decimal('0')
        sender = tx['from']

        contract_expected = Web3.to_checksum_address(settings.usdt_bep20_contract)
        for log in receipt['logs']:
            if Web3.to_checksum_address(log['address']) != contract_expected:
                continue
            topics = log['topics']
            if not topics or topics[0].hex() != BEP20_TRANSFER_TOPIC:
                continue
            if len(topics) < 3:
                continue
            to_topic = topics[2].hex().lower()
            if not to_topic.endswith(expected_lower):
                continue
            value = int(log['data'].hex(), 16)
            actual_amount += Decimal(value) / Decimal(10**USDT_BEP20_DECIMALS)

        expected = Decimal(str(expected_amount))
        if actual_amount < expected:
            raise VerificationError(f'USDT amount too low. Expected at least {expected}, got {actual_amount}.')
        return VerificationResult(ok=True, sender=sender, notes=f'USDT BEP20 payment verified: {actual_amount}')


class SolVerifier:
    def __init__(self, rpc_url: str):
        self.client = SolanaClient(rpc_url)

    def verify_sol(self, tx_hash: str, expected_to: str, expected_amount: float, min_confirmations: int) -> VerificationResult:
        try:
            sig = Signature.from_string(tx_hash)
            response = self.client.get_transaction(sig, encoding='jsonParsed', max_supported_transaction_version=0)
            value = response.value
            if value is None:
                raise VerificationError('Transaction not found on Solana yet.')
            if value.transaction.meta is None or value.transaction.meta.err is not None:
                raise VerificationError('Transaction failed or is not finalized.')

            slot_response = self.client.get_slot()
            current_slot = slot_response.value
            if current_slot - value.slot < min_confirmations:
                raise VerificationError('Transaction has not reached the required confirmations yet.')

            message = value.transaction.transaction.message
            sender = None
            matched = Decimal('0')
            for ix in message.instructions:
                parsed = getattr(ix, 'parsed', None)
                if not parsed:
                    continue
                info = parsed.get('info', {})
                ix_type = parsed.get('type')
                if ix.program == 'system' and ix_type == 'transfer':
                    destination = info.get('destination')
                    lamports = Decimal(info.get('lamports', 0))
                    if destination == expected_to:
                        matched += lamports / Decimal(1_000_000_000)
                    sender = sender or info.get('source')

            expected = Decimal(str(expected_amount))
            if matched < expected:
                raise VerificationError(f'SOL amount too low. Expected at least {expected}, got {matched}.')
            return VerificationResult(ok=True, sender=sender, notes=f'SOL payment verified: {matched}')
        except VerificationError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise VerificationError(f'Unable to verify Solana transaction: {exc}') from exc


def verify_payment(coin: str, tx_hash: str, destination_wallet: str, expected_amount: float) -> VerificationResult:
    coin = coin.upper()
    normalized_tx_hash = _extract_tx_hash(tx_hash, coin)
    if coin == 'USDT_BEP20':
        verifier = EVMVerifier(settings.bsc_rpc_url)
        return verifier.verify_bep20_usdt(normalized_tx_hash, destination_wallet, expected_amount, settings.bsc_confirmations)
    if coin == 'BNB':
        verifier = EVMVerifier(settings.bsc_rpc_url)
        return verifier.verify_native(normalized_tx_hash, destination_wallet, expected_amount, settings.bsc_confirmations)
    if coin == 'ETH':
        verifier = EVMVerifier(settings.eth_rpc_url)
        return verifier.verify_native(normalized_tx_hash, destination_wallet, expected_amount, settings.eth_confirmations)
    if coin == 'SOL':
        verifier = SolVerifier(settings.sol_rpc_url)
        return verifier.verify_sol(normalized_tx_hash, destination_wallet, expected_amount, settings.sol_confirmations)
    raise VerificationError(f'Unsupported coin {coin}')
