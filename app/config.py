from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    telegram_bot_token: str
    telegram_webhook_secret: str
    public_webhook_url: str
    database_url: str = 'sqlite:///./shakers_vip.db'

    vip_chat_id: int
    vip_chat_title: str = 'Shakers Alpha VIP'
    support_username: str = '@badass_shakers2'

    lifetime_price_usd: float = 80.0
    quote_expiry_minutes: int = 15

    usdt_bep20_wallet: str
    bsc_wallet: str
    eth_wallet: str
    sol_wallet: str

    bsc_rpc_url: str
    usdt_bep20_contract: str
    eth_rpc_url: str
    sol_rpc_url: str = 'https://api.mainnet-beta.solana.com'

    bsc_confirmations: int = 1
    eth_confirmations: int = 1
    sol_confirmations: int = 1

    allowed_usernames_csv: str = ''

    coingecko_base_url: str = 'https://api.coingecko.com/api/v3'

    admin_ids_csv: str = ''

    @property
    def admin_ids(self) -> set[int]:
        return {int(x.strip()) for x in self.admin_ids_csv.split(',') if x.strip()}


settings = Settings()
