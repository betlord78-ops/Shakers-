from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from time import time

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

BINANCE_SYMBOLS = {
    'SOL': 'SOLUSDT',
    'ETH': 'ETHUSDT',
    'BNB': 'BNBUSDT',
}

_PRICE_CACHE: dict[str, tuple[float, float]] = {}
_CACHE_TTL_SECONDS = 60


async def _fetch_coingecko_price(coin: str) -> float:
    coin_id = COINGECKO_IDS[coin]
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            f"{settings.coingecko_base_url}/simple/price",
            params={'ids': coin_id, 'vs_currencies': 'usd'},
            headers={'accept': 'application/json'},
        )
        response.raise_for_status()
        data = response.json()
        return float(data[coin_id]['usd'])


async def _fetch_binance_price(coin: str) -> float:
    symbol = BINANCE_SYMBOLS[coin]
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            'https://api.binance.com/api/v3/ticker/price',
            params={'symbol': symbol},
            headers={'accept': 'application/json'},
        )
        response.raise_for_status()
        data = response.json()
        return float(data['price'])


async def fetch_usd_price(coin: str) -> float:
    if coin == 'USDT_BEP20':
        return 1.0

    cached = _PRICE_CACHE.get(coin)
    now = time()
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]

    last_error = None
    for fetcher in (_fetch_coingecko_price, _fetch_binance_price):
        try:
            price = await fetcher(coin)
            _PRICE_CACHE[coin] = (now, price)
            return price
        except Exception as exc:  # noqa: BLE001
            last_error = exc

    if cached:
        return cached[1]
    raise RuntimeError('Live quote temporarily unavailable. Please try again in 1 minute.') from last_error


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
