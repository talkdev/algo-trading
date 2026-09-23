"""
upstox_token.py

Fully automates daily Upstox access-token generation using TOTP-based 2FA
via the `upstox-totp` package (mobile -> password -> TOTP -> PIN -> OAuth).

Credentials are read from env.txt next to this script (same file the trading
engine uses). Required keys:

   UPSTOX_USERNAME=9876543210          # 10-digit Upstox mobile number
   UPSTOX_PASSWORD=your-login-password
   UPSTOX_PIN_CODE=123456              # Upstox PIN
   UPSTOX_TOTP_SECRET=JBSWY3DPEHPK3PXP # base32 secret from TOTP QR setup
   UPSTOX_CLIENT_ID=your-api-key       # or UPSTOX_API_KEY=
   UPSTOX_CLIENT_SECRET=your-api-secret  # or UPSTOX_API_SECRET=
   UPSTOX_REDIRECT_URI=https://your-redirect-uri

Setup
-----
1. pip install upstox-totp
2. Fill the keys above in env.txt (inline # comments are stripped)
3. python upstox_token.py
4. Schedule daily a few minutes after 03:30 IST (token expiry)

Writes the fresh token to token.json AND updates UPSTOX_ACCESS_TOKEN in
env.txt so main.py / core.load_config pick it up without a manual paste.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from upstox_totp import ConfigurationError, UpstoxTOTP

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / "env.txt"
TOKEN_FILE = BASE_DIR / "token.json"
LOG_FILE = BASE_DIR / "upstox_token.log"
IST = timezone(timedelta(hours=5, minutes=30))

REQUIRED = (
    "UPSTOX_USERNAME",
    "UPSTOX_PASSWORD",
    "UPSTOX_PIN_CODE",
    "UPSTOX_TOTP_SECRET",
    "UPSTOX_CLIENT_ID",
    "UPSTOX_CLIENT_SECRET",
    "UPSTOX_REDIRECT_URI",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("upstox_token")


def load_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    """Load key=value from env.txt; strip blanks, quotes, and inline comments."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        # Drop inline comments (VALUE  # comment) — not part of the secret.
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        if key:
            env[key] = value
    return env


def resolve_upstox_creds(env: dict[str, str]) -> dict[str, str]:
    """Map env.txt keys to upstox-totp fields; accept API_KEY aliases."""
    client_id = (env.get("UPSTOX_CLIENT_ID") or env.get("UPSTOX_API_KEY") or "").strip()
    client_secret = (
        env.get("UPSTOX_CLIENT_SECRET") or env.get("UPSTOX_API_SECRET") or ""
    ).strip()
    creds = {
        "UPSTOX_USERNAME": (env.get("UPSTOX_USERNAME") or "").strip(),
        "UPSTOX_PASSWORD": (env.get("UPSTOX_PASSWORD") or "").strip(),
        "UPSTOX_PIN_CODE": (env.get("UPSTOX_PIN_CODE") or "").strip(),
        "UPSTOX_TOTP_SECRET": (env.get("UPSTOX_TOTP_SECRET") or "").strip(),
        "UPSTOX_CLIENT_ID": client_id,
        "UPSTOX_CLIENT_SECRET": client_secret,
        "UPSTOX_REDIRECT_URI": (env.get("UPSTOX_REDIRECT_URI") or "").strip(),
    }
    return creds


def validate_creds(creds: dict[str, str]) -> None:
    missing = [k for k in REQUIRED if not creds.get(k)]
    if missing:
        raise ConfigurationError(
            "Missing required Upstox credentials in env.txt: "
            + ", ".join(missing)
            + f"\nEdit {ENV_FILE}"
        )
    user = creds["UPSTOX_USERNAME"]
    if not (user.isdigit() and len(user) == 10):
        raise ConfigurationError(
            f"UPSTOX_USERNAME must be a 10-digit mobile number, got {user!r}"
        )
    pwd = creds["UPSTOX_PASSWORD"]
    if pwd.lower() in ("your-login-password", "changeme", "password", "xxx"):
        raise ConfigurationError(
            "UPSTOX_PASSWORD in env.txt is still a placeholder — set your real login password"
        )
    secret = creds["UPSTOX_TOTP_SECRET"].replace(" ", "").upper()
    if not re.fullmatch(r"[A-Z2-7]+=*", secret):
        raise ConfigurationError(
            "UPSTOX_TOTP_SECRET does not look like a base32 TOTP secret"
        )
    creds["UPSTOX_TOTP_SECRET"] = secret


def build_client(creds: dict[str, str]) -> UpstoxTOTP:
    """Construct client with raw PIN (library base64-encodes once at submit).

    Do NOT use UpstoxTOTP.from_env_file — it pre-encodes the PIN and the API
    layer encodes again (double-encoding → PIN rejection).
    """
    # Also export to process env so any nested dotenv reads stay consistent.
    for key, val in creds.items():
        os.environ[key] = val

    return UpstoxTOTP(
        username=creds["UPSTOX_USERNAME"],
        password=creds["UPSTOX_PASSWORD"],
        pin_code=creds["UPSTOX_PIN_CODE"],
        totp_secret=creds["UPSTOX_TOTP_SECRET"],
        client_id=creds["UPSTOX_CLIENT_ID"],
        client_secret=creds["UPSTOX_CLIENT_SECRET"],
        redirect_uri=creds["UPSTOX_REDIRECT_URI"],
        debug=os.environ.get("UPSTOX_DEBUG", "false").lower()
        in ("true", "1", "yes", "on"),
    )


def update_env_access_token(token: str, path: Path = ENV_FILE) -> None:
    """Upsert UPSTOX_ACCESS_TOKEN= in env.txt for the trading engine."""
    if not path.exists():
        path.write_text(f"UPSTOX_ACCESS_TOKEN={token}\n", encoding="utf-8")
        return
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    key = "UPSTOX_ACCESS_TOKEN"
    found = False
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        k = stripped.split("=", 1)[0].strip()
        if k == key:
            nl = "\n" if line.endswith("\n") else ""
            out.append(f"{key}={token}{nl}")
            found = True
        else:
            out.append(line)
    if not found:
        if out and not out[-1].endswith("\n"):
            out[-1] = out[-1] + "\n"
        out.append(f"\n# refreshed by upstox_token.py\n{key}={token}\n")
    path.write_text("".join(out), encoding="utf-8")
    log.info("Updated %s in %s", key, path.name)


def generate_and_save_token() -> str:
    env = load_env_file(ENV_FILE)
    if not env:
        raise FileNotFoundError(
            f"No credentials found — create {ENV_FILE} with UPSTOX_* keys"
        )
    creds = resolve_upstox_creds(env)
    validate_creds(creds)

    try:
        upx = build_client(creds)
    except ConfigurationError:
        raise
    except Exception as exc:
        raise ConfigurationError(
            f"Failed to build Upstox client from env.txt: {exc}"
        ) from exc

    response = upx.app_token.get_access_token()

    if not (response.success and response.data):
        log.error("Token generation failed: %s", response)
        raise RuntimeError(f"Upstox token generation failed: {response}")

    access_token = response.data.access_token
    data = {
        "access_token": access_token,
        "user_id": response.data.user_id,
        "user_name": response.data.user_name,
        "email": response.data.email,
        "generated_at_ist": datetime.now(IST).isoformat(),
    }

    TOKEN_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        TOKEN_FILE.chmod(0o600)
    except OSError:
        pass  # Windows / non-POSIX FS may not support chmod

    update_env_access_token(access_token, ENV_FILE)

    log.info("Access token refreshed successfully for user %s", data["user_id"])
    return access_token


def load_cached_token() -> str | None:
    """Read today's token if it's already been generated (IST-aware)."""
    if not TOKEN_FILE.exists():
        return None
    data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    generated_at = datetime.fromisoformat(data["generated_at_ist"])
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=IST)
    now_ist = datetime.now(IST)
    # Upstox tokens expire ~3:30 AM IST daily — stale if generated before
    # today's 3:30 AM IST cutoff while we are already past that cutoff.
    cutoff_today = now_ist.replace(hour=3, minute=30, second=0, microsecond=0)
    if now_ist >= cutoff_today and generated_at < cutoff_today:
        return None
    return data.get("access_token")


if __name__ == "__main__":
    try:
        token = generate_and_save_token()
        # Print only a short prefix — full token lands in env.txt / token.json
        print(f"Access token OK ({len(token)} chars) → {ENV_FILE.name} + {TOKEN_FILE.name}")
    except Exception:
        log.exception("Failed to refresh Upstox access token")
        sys.exit(1)

# ---------------------------------------------------------------------------
# Scheduling (pick one)
# ---------------------------------------------------------------------------
#
# Linux/macOS — cron (runs daily at 3:35 AM IST; adjust for your server's TZ):
#   35 3 * * * /usr/bin/python3 /path/to/upstox_token.py >> /path/to/cron.log 2>&1
#
# Windows — Task Scheduler:
#   schtasks /create /tn "UpstoxTokenRefresh" /tr "python C:\path\to\upstox_token.py" /sc daily /st 03:35
#
# Cloud (if your server isn't guaranteed to be running at 3:35 AM):
#   Use a scheduled cloud function (AWS Lambda + EventBridge, GCP Cloud
#   Scheduler + Cloud Function, etc.) instead of relying on your own machine
#   being awake — market-open automation shouldn't depend on your laptop
#   being on.
