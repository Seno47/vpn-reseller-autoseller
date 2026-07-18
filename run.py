from __future__ import annotations

import uvicorn

from reseller_autoseller.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "reseller_autoseller.app:create_app",
        factory=True,
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.log_level.lower(),
        # Marketplace notification authentication tokens live in URL paths
        # because the upstream APIs cannot attach a custom header. Uvicorn's
        # default access log would persist those credentials verbatim.
        access_log=False,
    )


if __name__ == "__main__":
    main()
