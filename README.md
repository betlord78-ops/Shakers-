# Shakers Alpha VIP Bot

Python Telegram payment bot for **Shakers Alpha VIP**.

It handles:
- Telegram **join requests** for a private VIP group
- **Lifetime access only**
- Fixed price of **$80**
- Payment options:
  - **USDT (BEP20)**
  - **SOL**
  - **ETH**
  - **BNB (BSC)**
- Live conversion for SOL / ETH / BNB using CoinGecko
- Transaction-hash-based verification on-chain
- Auto-approval of the user's pending Telegram join request after successful verification

## Important note

This version verifies payment **after the user submits a transaction hash**.
That still gives you automatic verification and auto-approval, but it does **not** yet include passive background wallet scanning for unmatched incoming transactions.

This is the most reliable version to deploy first because:
- it avoids false positives
- it prevents duplicate approvals
- it is easier to maintain on Railway

## Tech stack

- Python
- FastAPI
- python-telegram-bot
- SQLAlchemy
- Web3.py
- Solana RPC
- Railway-ready webhook deployment

## Telegram limitation

A Telegram bot usually cannot DM a user until that user has started the bot at least once.

Best practice:
- put the bot username in the group description
- tell users to tap **Start** on the bot before or after requesting access

## Telegram bot setup

1. Create your bot with **BotFather**.
2. Add the bot as an **admin** inside your private VIP group.
3. Give it the rights needed to approve join requests.
4. Turn on **join requests** for the invite link.
5. Set `VIP_CHAT_ID` to the private group ID.

## Railway deploy

1. Upload this project to GitHub.
2. Create a new Railway project from the repo.
3. Add all environment variables from `.env.example`.
4. Set the start command to:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

5. After Railway gives you your public URL, set:

```env
PUBLIC_WEBHOOK_URL=https://your-app-name.up.railway.app
```

6. Redeploy.

## Environment variables

### Required

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_WEBHOOK_SECRET`
- `PUBLIC_WEBHOOK_URL`
- `VIP_CHAT_ID`
- `VIP_CHAT_TITLE`
- `SUPPORT_USERNAME`
- `LIFETIME_PRICE_USD`
- `QUOTE_EXPIRY_MINUTES`
- `USDT_BEP20_WALLET`
- `BSC_WALLET`
- `ETH_WALLET`
- `SOL_WALLET`
- `BSC_RPC_URL`
- `ETH_RPC_URL`
- `SOL_RPC_URL`
- `USDT_BEP20_CONTRACT`
- `ADMIN_IDS_CSV`

### Database

- `DATABASE_URL`

SQLite works for quick testing:

```env
DATABASE_URL=sqlite:///./shakers_vip.db
```

For production on Railway, PostgreSQL is better.

## User flow

1. User taps your private join link.
2. User submits a Telegram **join request**.
3. Bot DMs the user.
4. User chooses coin.
5. Bot generates a live quote for the chosen chain.
6. User pays.
7. User taps **Submit Tx Hash**.
8. Bot verifies the transaction.
9. Bot approves the join request automatically.
10. User joins **Shakers Alpha VIP**.

## Supported chains

### USDT (BEP20)
- Verifies BEP20 Transfer logs to your configured receiving wallet.
- Uses the official BSC USDT contract from `.env`.

### BNB
- Verifies native BNB transfer on BSC.

### ETH
- Verifies native ETH transfer on Ethereum.

### SOL
- Verifies native SOL transfer on Solana.

## Admin command

```text
/paid_orders
```

Shows the latest paid orders for admin IDs listed in `ADMIN_IDS_CSV`.

## Local run

```bash
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

## What you may want next

- full passive wallet scanning without tx hash submission
- coupon codes
- manual admin approval panel
- auto-ban or removal logic for refunded users
- better admin dashboards
- PostgreSQL migrations



## Auto verification

This version includes automatic payment detection every 1 minute for pending orders on BSC, Ethereum, and Solana using the configured RPC URLs. Users can still submit a tx hash manually for instant verification.


## v6 fix

This build hardens callback handling, removes Markdown formatting issues in payment messages, and shows a clear message if a wallet variable is missing.
