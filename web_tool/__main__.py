from __future__ import annotations

import uvicorn

from web_tool.app.config import default_host, default_port


def main() -> None:
    uvicorn.run(
        "web_tool.app.main:app",
        host=default_host(),
        port=default_port(),
        reload=False,
    )


if __name__ == "__main__":
    main()
