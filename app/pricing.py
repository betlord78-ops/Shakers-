from __future__ import annotations

from dataclasses import dataclass
from math import ceil

import httpx

from .config import settings


@dataclass
class Quote:
    coin: str
    usd_amount: float
    coin_amount: float
    display_amount: str
    destination_wallet: str


COINGECKO_IDS = {
    'SOL': 'solana',
    'ETH': 'ethereum',
    'BNB': 'binancecoin',
}


async def fetch_usd_price(coin: str) -> float:
    if coin == 'USDT_BEP20':
        return 1.0
    coin_id = COINGECKO_IDS[coin]
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            f"{settings.coingecko_base_url}/simple/price",
            params={'ids': coin_id, 'vs_currencies': 'usd'},
        )
        response.raise_for_status()
        data = response.json()
        return float(data[coin_id]['usd'])


def _round_amount(coin: str, amount: float) -> tuple[float, str]:
    if coin == 'USDT_BEP20':
        value = round(amount, 2)
        return value, f'{value:.2f}'
    if coin == 'SOL':
        value = ceil(amount * 10000) / 10000
        return value, f'{value:.4f}'
    if coin in {'ETH', 'BNB'}:
        value = ceil(amount * 1000000) / 1000000
        return value, f'{value:.6f}'
    raise ValueError(f'Unsupported coin: {coin}')


async def build_quote(coin: str) -> Quote:
    usd_amount = settings.lifetime_price_usd
    price_usd = await fetch_usd_price(coin)
    raw_amount = usd_amount / price_usd
    coin_amount, display_amount = _round_amount(coin, raw_amount)
    destination = {
        'USDT_BEP20': settings.usdt_bep20_wallet,
        'BNB': settings.bsc_wallet,
        'ETH': settings.eth_wallet,
        'SOL': settings.sol_wallet,
    }[coin]
    return Quote(
        coin=coin,
        usd_amount=usd_amount,
        coin_amount=coin_amount,
        display_amount=display_amount,
        destination_wallet=destination,
    )
