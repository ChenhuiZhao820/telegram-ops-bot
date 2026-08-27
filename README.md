# Basanite Intern

A personal execution assistant for the Basanite founders. Send a natural-language
instruction from Telegram; a Claude-powered agent loop interprets it, calls the
right tools (Airtable, Devin, Otter), and replies in the same chat.

## Architecture

```
Telegram (webhook) → Gateway (FastAPI) → task queue (Postgres table)
                                              │
                                     Worker → Orchestrator (Claude tool loop)
                                              │
                                    Adapters: airtable / devin / otter (MCP)
```

- **Gateway** (`src/gateway/`): webhook auth, whitelist, enqueue, confirmation buttons.
- **Orchestrator** (`src/orchestrator/`): Claude tool-use loop. Write tools pause
  for a Confirm/Cancel button (15-minute expiry). No intent logic in code.
- **Adapters** (`src/adapters/`): thin wrappers. A failed adapter registers zero
  tools and never takes down the others.
- **Worker** (`src/queue/worker.py`): polls the tasks table, one task at a time.
- **Execution log**: every task appends a row to `Intern Execution Log` in the
  Basanite Todos Airtable base.

## Local quickstart (~10 minutes)

Prereqs: Python 3.11+, a local Postgres (or a Render Postgres external URL).

```bash
git clone <this repo> && cd <repo>
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env                            # fill in real values
```

Load the env (e.g. `set -a; source .env; set +a` or use your IDE), then run:

```bash
# Terminal 1 — gateway
uvicorn src.gateway.main:app --port 8000

# Terminal 2 — worker
python -m src.queue.worker
```

### Local dev without a public webhook

Telegram needs a public HTTPS URL for webhooks. For local dev, use long polling
instead: delete any webhook (`curl "https://api.telegram.org/bot<TOKEN>/deleteWebhook"`)
and run this simple poller, which forwards updates to your local gateway:

```bash
python - <<'EOF'
import os, time, httpx
token, offset = os.environ["TELEGRAM_BOT_TOKEN"], 0
secret = os.environ["TELEGRAM_WEBHOOK_SECRET"]
while True:
    r = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates",
                  params={"offset": offset, "timeout": 25}, timeout=30).json()
    for u in r.get("result", []):
        offset = u["update_id"] + 1
        httpx.post("http://localhost:8000/telegram/webhook", json=u,
                   headers={"X-Telegram-Bot-Api-Secret-Token": secret})
    time.sleep(1)
EOF
```

### Tests

```bash
pytest
```

## Deployment (Render)

`render.yaml` defines the full footprint: one web service (gateway), one
background worker, one managed Postgres. Create an env group named
`intern-secrets` with every variable from `.env.example` except `DATABASE_URL`
(injected from the database automatically).

After the gateway is live, register the webhook once:

```bash
curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook?url=https://<your-render-url>/telegram/webhook&secret_token=<TELEGRAM_WEBHOOK_SECRET>"
```

## Manual setup and acceptance tests

See Section 9 of the design doc for the full manual setup checklist (BotFather,
user IDs, Airtable token/base IDs, Devin API key, Otter OAuth via
`python scripts/otter_auth.py`, Anthropic key, webhook secret) and the nine
end-to-end test cases.

## Otter notes

Otter is integrated as an MCP **client** (no REST API on non-Enterprise plans).
If `OTTER_ACCESS_TOKEN` is unset or the MCP server is unavailable on your plan,
the Otter adapter registers zero tools and everything else keeps working (the
system prompt then omits meeting recall). To (re-)authorize, run
`python scripts/otter_auth.py` on a laptop and paste the printed values into the
Render env vars.

## Environment variables

Every variable is documented with placeholders in `.env.example`. Never commit
`.env`; all secrets come from the environment.
