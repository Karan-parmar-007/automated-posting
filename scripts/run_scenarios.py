"""Live end-to-end scenarios against real platforms (creates REAL public posts).

Each scenario starts a temporary server (via pm2) with some credentials deliberately
broken through environment variables (which override `.env`), sends one request,
and saves request + response + server log lines to docs/test-runs/.

Usage:  python scripts/run_scenarios.py [scenario-name ...]
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "test-runs"
PORT = 8007
BASE = f"http://127.0.0.1:{PORT}"
PM2_NAME = "automated-posting-scenario"

SCENARIOS: list[tuple[str, str, dict[str, str], str]] = [
    # (name, description, env overrides, message)
    (
        "01-all-success",
        "All 5 platforms configured correctly",
        {},
        "Launch week is here: our new analytics dashboard helps small teams track growth in real time. https://example.com/launch",
    ),
    (
        "02-fail-mastodon",
        "Mastodon: invalid access token",
        {"MASTODON_ACCESS_TOKEN": "invalid-token-for-testing"},
        "Free webinar this Friday on building fast APIs with Python and FastAPI. Save your seat: https://example.com/webinar",
    ),
    (
        "03-fail-bluesky",
        "Bluesky: wrong app password",
        {"BLUESKY_APP_PASSWORD": "aaaa-bbbb-cccc-dddd"},
        "We just published a practical guide to automating your social media workflow. Read it here https://example.com/guide",
    ),
    (
        "04-fail-telegram",
        "Telegram: revoked/invalid bot token",
        {"TELEGRAM_BOT_TOKEN": "1234567890:AAInvalidTokenForTestingPurposes00000"},
        "Our customer support is now available 24/7 through live chat. Try it today at https://example.com/support",
    ),
    (
        "05-fail-github",
        "GitHub Gist: bad credentials",
        {"GITHUB_TOKEN": "github_pat_invalid_for_testing"},
        "New case study: how a local bakery doubled online orders in three months. Full story https://example.com/case-study",
    ),
    (
        "06-fail-devto",
        "DEV Community: invalid API key",
        {"DEVTO_API_KEY": "invalid-devto-key"},
        "Tips for writing clean Python: small functions, clear names and good tests. More at https://example.com/python-tips",
    ),
    (
        "07-fail-two-at-once",
        "Mastodon server unreachable (network error) + Telegram channel does not exist; "
        "Gemini key broken so the LLM falls back to local Ollama",
        {
            "MASTODON_BASE_URL": "https://mastodon.invalid",
            "TELEGRAM_CHANNEL": "@this_channel_does_not_exist_9876",
            "GEMINI_API_KEY": "invalid-gemini-key",
        },
        "Spring sale: 30 percent off all annual plans until the end of the month. Grab the deal https://example.com/sale",
    ),
    (
        "08-all-fail",
        "Every platform misconfigured -> success: false (nothing is posted)",
        {
            "MASTODON_ACCESS_TOKEN": "bad",
            "BLUESKY_APP_PASSWORD": "aaaa-bbbb-cccc-dddd",
            "TELEGRAM_BOT_TOKEN": "1234567890:AAInvalidTokenForTestingPurposes00000",
            "GITHUB_TOKEN": "github_pat_invalid",
            "DEVTO_API_KEY": "",
        },
        "This message should not appear anywhere because every platform is misconfigured https://example.com/none",
    ),
]


def pm2(*args: str) -> None:
    subprocess.run(["pm2", *args], cwd=ROOT, capture_output=True, check=False)


def start_server(env: dict[str, str]) -> None:
    pm2("delete", PM2_NAME)
    assignments = " ".join(f"{k}='{v}'" for k, v in env.items())
    cmd = f"env {assignments} .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port {PORT}"
    pm2("start", cmd, "--name", PM2_NAME, "--cwd", str(ROOT), "--no-autorestart")
    for _ in range(40):
        try:
            if httpx.get(f"{BASE}/health", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.5)
    raise RuntimeError("scenario server did not start")


def app_log_lines() -> list[str]:
    log = Path.home() / ".pm2" / "logs" / f"{PM2_NAME}-error.log"
    if not log.exists():
        return []
    return [line.rstrip() for line in log.read_text().splitlines() if " app." in line]


def main(selected: list[str]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, description, env, message in SCENARIOS:
        if selected and name not in selected:
            continue
        print(f"\n=== {name}: {description}")
        (Path.home() / ".pm2" / "logs" / f"{PM2_NAME}-error.log").unlink(missing_ok=True)
        start_server(env)
        try:
            resp = httpx.post(f"{BASE}/posting", json={"message": message}, timeout=120)
            time.sleep(0.5)  # let log lines flush
            record = {
                "scenario": name,
                "description": description,
                "broken_settings": sorted(env),  # names only, never values
                "request": {"method": "POST", "url": "/posting", "body": {"message": message}},
                "response": {"status_code": resp.status_code, "body": resp.json()},
                "server_log": app_log_lines(),
            }
            (OUT / f"{name}.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
            body = resp.json()
            print(f"HTTP {resp.status_code} success={body.get('success')}")
            for r in body.get("results", []):
                print(
                    f"  {r['platform']:<14} {r['status']:<8} public={r['public_access']!s:<5} "
                    f"{r['posted_url'] or ''} {r['error'] or ''}"
                )
        finally:
            pm2("delete", PM2_NAME)


if __name__ == "__main__":
    main(sys.argv[1:])
