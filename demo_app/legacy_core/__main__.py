"""Entry point for ``uv run legacy-core`` — serves LegacyCore on localhost:8000."""

from __future__ import annotations

import uvicorn


def main() -> None:
    uvicorn.run("legacy_core.app:app", host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
