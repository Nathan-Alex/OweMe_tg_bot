from __future__ import annotations

from .webhook_app import create_whatsapp_app

app = create_whatsapp_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("bot.whatsapp.main:app", host="0.0.0.0", port=8000, reload=True)
