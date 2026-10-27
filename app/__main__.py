"""Start with python -m app; .env controls APP_HOST, APP_PORT and WEB_CONCURRENCY."""

import uvicorn

from app.core.config import get_settings


def main():
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        workers=settings.web_concurrency,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
