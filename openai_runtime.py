#!/usr/bin/env python3
from __future__ import annotations

import os
from typing import Any


def resolve_openai_runtime_credentials() -> dict[str, Any]:
    """Resolve public-safe OpenAI API credentials.

    The public project intentionally reads only OPENAI_API_KEY from the
    environment or secrets.env. It does not inspect local app auth files.
    """
    api_key = str(os.environ.get("OPENAI_API_KEY") or "").strip()
    if api_key:
        return {
            "auth_mode": "api_key",
            "source": "env:OPENAI_API_KEY",
            "api_key": api_key,
            "base_url": "https://api.openai.com/v1",
            "default_headers": {},
        }

    return {
        "auth_mode": "none",
        "source": "unavailable",
        "api_key": "",
        "base_url": "https://api.openai.com/v1",
        "default_headers": {},
    }
