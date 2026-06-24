# Split a Bill Telegram Bot

Minimal Telegram debt bot with exactly 3 user actions:
- `In`
- `Balance`
- `Close`

Tech stack:
- aiogram v3
- FastAPI webhook entrypoint for Vercel
- PostgreSQL (direct via psycopg)
- pydantic-settings

## How it works

1. User taps `In`.
2. Bot asks for amount (`120` or `120 USD`).
3. Bot creates a deep link like `https://t.me/<BOT_USERNAME>?start=pay_<CODE>`.
4. User taps `Forward Loan` and sends it to the person who gave the money.
5. That person opens the link and taps `Approve`.
6. Bot writes a confirmed transaction to PostgreSQL.

`Balance` shows only non-zero debts.

`Close` shows people with open debts as buttons. Tapping a person sets your mutual balances to `0`.

## Requirements

- Python 3.12+
- `pip`
- PostgreSQL 14+

## Local setup

1. Open terminal in the project:

```bash
cd /Users/coconut/Documents/projects/Split_a_bill
```

2. Create and activate a virtual environment:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

3. Install dependencies:

```bash
pip install -r requirements.txt
```

4. Create env file:

```bash
cp .env.example .env
```

5. Fill `.env`:

```env
BOT_TOKEN=<telegram-bot-token>
BOT_USERNAME=<bot_username_without_@>
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/split_bill
DEFAULT_CURRENCY=ILS
WEBHOOK_SECRET=<random-secret-string>
PUBLIC_BASE_URL=https://your-project.vercel.app
```

6. Run SQL in PostgreSQL:

- `postgres/schema.sql`
- If you already have old tables and only need request-links feature:
  - `postgres/migrations/20260306_payment_requests.sql`

7. Optional local polling mode for development:

```bash
python -m bot.main
```

## Vercel deployment

The Vercel Python function entrypoint is `api/index.py`.
It exposes:
- `POST /api/telegram` for Telegram webhooks
- `GET /api/health` for a database-backed health check

Project files added for Vercel:
- `api/index.py`
- `.python-version`

After deployment, set the Telegram webhook with:

```bash
python -m bot.setup_webhook
```

This command uses `PUBLIC_BASE_URL` and `WEBHOOK_SECRET` from your environment and registers the webhook URL `https://<your-domain>/api/telegram`.

If your Vercel storage integration injects `POSTGRES_URL` instead of `DATABASE_URL`, the app accepts that too.

## Database note

This version requires table `payment_requests` and `processed_updates` from `postgres/schema.sql`.

## Supabase database note

Supabase direct database URLs resolve to IPv6 by default. If the deployment platform is IPv4-only, use the Supabase pooler connection string from Dashboard -> Connect instead of adding a separate IP override.

For long-lived servers, use the Session pooler URL on port `5432`. For serverless or auto-scaling deployments, use the Transaction pooler URL on port `6543`:

```env
DATABASE_URL=postgres://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres
WHATSAPP_DATABASE_URL=postgres://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres
```

The psycopg connections disable prepared statements so they work with Supabase Transaction pooler.
