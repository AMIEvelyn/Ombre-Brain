#!/usr/bin/env python3
# ============================================================
# SessionStart Hook: auto-breath + dreaming on session start
# 对话开始钩子：自动浮现记忆 + 触发 dreaming
#
# On SessionStart, this script calls the Ombre Brain MCP server's
# breath-hook and dream-hook endpoints, printing results to stdout
# so Claude sees them as session context.
#
# Sequence: breath → dream → feel
# 顺序：呼吸浮现 → 做梦消化 → 读取 feel
#
# Config:
#   OMBRE_HOOK_URL  — override the server URL (default: http://localhost:8000)
#   OMBRE_HOOK_SKIP — set to "1" to disable the hook temporarily
# ============================================================

import os
import sys
import urllib.parse
import urllib.request
import urllib.error

def main():
    # Allow disabling the hook via env var
    if os.environ.get("OMBRE_HOOK_SKIP") == "1":
        sys.exit(0)

    base_url = os.environ.get("OMBRE_HOOK_URL", "http://localhost:8000").rstrip("/")
    # Hook endpoints are token-gated on public deployments; pass it through when set.
    token = os.environ.get("OMBRE_HOOK_TOKEN", "").strip()

    # --- Step 1: Breath — surface unresolved memories ---
    _call_endpoint(base_url, "/breath-hook", token)

    # --- Step 2: Dream — digest recent memories ---
    _call_endpoint(base_url, "/dream-hook", token)


def _call_endpoint(base_url, path, token=""):
    url = f"{base_url}{path}"
    if token:
        url = f"{url}?token={urllib.parse.quote(token)}"
    req = urllib.request.Request(
        url,
        headers={"Accept": "text/plain"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            raw = response.read().decode("utf-8")
            output = raw.strip()
            if output:
                print(output)
    except (urllib.error.URLError, OSError):
        pass
    except Exception:
        pass


if __name__ == "__main__":
    main()
