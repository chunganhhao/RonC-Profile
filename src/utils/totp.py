"""RFC 6238 TOTP (2FA 6-digit code) generator & account pipe-format parser using Python stdlib."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import struct
import time
import urllib.parse


def clean_totp_secret(raw: str | None) -> str:
    """Extract and normalize a Base32 secret from raw text or a standard SHA1/6-digit/30s otpauth://totp URI."""
    if not raw:
        return ""
    text = raw.strip()
    if "://" in text or text.lower().startswith("otpauth:"):
        parsed = urllib.parse.urlparse(text)
        if parsed.scheme.lower() != "otpauth" or parsed.netloc.lower() != "totp":
            return ""
        qs: dict[str, list[str]] = {}
        for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
            qs.setdefault(k.lower(), []).append(v)
        if "counter" in qs:
            return ""
        if "digits" in qs and qs["digits"] != ["6"]:
            return ""
        if "period" in qs and qs["period"] != ["30"]:
            return ""
        if "algorithm" in qs and [a.upper() for a in qs["algorithm"]] != ["SHA1"]:
            return ""
        secret_list = qs.get("secret")
        if not secret_list or len(secret_list) != 1 or not secret_list[0]:
            return ""
        text = secret_list[0]
    # Remove spaces, hyphens, and Base32 padding noise
    cleaned = re.sub(r"[\s\-]+", "", text).upper().rstrip("=")
    if not cleaned or not re.fullmatch(r"[A-Z2-7]+", cleaned):
        return ""
    pad_len = (8 - len(cleaned) % 8) % 8
    try:
        base64.b32decode(cleaned + ("=" * pad_len), casefold=True)
    except Exception:
        return ""
    return cleaned


def generate_totp(
    secret: str | None,
    period: int = 30,
    digits: int = 6,
    now: float | None = None,
) -> tuple[str, int] | None:
    """Return (6-digit OTP string, seconds remaining in window) or None if secret is invalid."""
    cleaned = clean_totp_secret(secret)
    if not cleaned:
        return None
    # Pad to multiple of 8 for base64.b32decode
    pad_len = (8 - len(cleaned) % 8) % 8
    try:
        key = base64.b32decode(cleaned + ("=" * pad_len), casefold=True)
    except Exception:
        return None

    ts = int(time.time() if now is None else now)
    counter = ts // period
    remaining = period - (ts % period)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    code = str(code_int % (10**digits)).zfill(digits)
    return code, remaining


def _split_space_account_tokens(line: str) -> list[str]:
    """Split a space/tab-delimited account line 'uid pass [2fa] [note...]' into logical slots."""
    if "\t" in line:
        tab_parts = [p.strip() for p in line.strip().split("\t") if p.strip()]
        if len(tab_parts) >= 2:
            return tab_parts
    tokens = line.strip().split()
    if len(tokens) <= 2:
        return tokens
    uid, password = tokens[0], tokens[1]
    rest = tokens[2:]
    first_cand = rest[0]
    if first_cand.lower().startswith("otpauth:") or len(clean_totp_secret(first_cand)) >= 16:
        note = " ".join(rest[1:]).strip()
        return [uid, password, first_cand, note] if note else [uid, password, first_cand]
    # Check 4-character Base32 groups (e.g. "JBSW Y3DP EHPK 3PXP")
    grp: list[str] = []
    for tok in rest:
        if re.fullmatch(r"[A-Za-z2-7]{4}", tok):
            grp.append(tok)
        else:
            break
    if len(grp) >= 4 and len(clean_totp_secret("".join(grp))) >= 16:
        secret_part = " ".join(grp)
        note = " ".join(rest[len(grp) :]).strip()
        return [uid, password, secret_part, note] if note else [uid, password, secret_part]
    return [uid, password, " ".join(rest)]


def parse_account_line(line: str) -> dict[str, str]:
    """Parse common MMO account format 'uid|password|2fa_secret|note' or space-separated 'uid password 2fa_secret note'."""
    stripped = line.strip()
    if "|" in stripped:
        parts = [p.strip() for p in stripped.split("|")]
    else:
        parts = _split_space_account_tokens(stripped)
    result = {
        "account_uid": "",
        "account_password": "",
        "two_fa_secret": "",
        "notes": "",
    }
    if len(parts) >= 1:
        result["account_uid"] = parts[0]
    if len(parts) >= 2:
        result["account_password"] = parts[1]
    if len(parts) >= 3:
        # Check if part[2] is a valid 2FA secret (otpauth:// or Base32 >= 16 chars
        # without multi-word prose sentences); otherwise treat part[2:] as notes.
        raw_p2 = parts[2].strip()
        tokens = raw_p2.split()
        is_otpauth = raw_p2.lower().startswith("otpauth:")
        looks_like_totp = is_otpauth or (
            len(clean_totp_secret(raw_p2)) >= 16
            and (len(tokens) == 1 or all(len(t) == 4 for t in tokens))
        )
        if is_otpauth:
            cleaned = clean_totp_secret(raw_p2)
            result["two_fa_secret"] = cleaned if cleaned else raw_p2
            if len(parts) >= 4:
                result["notes"] = " | ".join(parts[3:])
        elif looks_like_totp and clean_totp_secret(raw_p2):
            result["two_fa_secret"] = clean_totp_secret(raw_p2)
            if len(parts) >= 4:
                result["notes"] = " | ".join(parts[3:])
        else:
            result["notes"] = " | ".join(parts[2:])
    return result
