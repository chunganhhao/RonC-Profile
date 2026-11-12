import re
from dataclasses import asdict, dataclass, field

# Browser engines a profile can run on. A profile keeps its engine for life:
# the two keep incompatible browser data in the same directory.
ENGINES = ("camoufox", "chrome")

# Preset account groups shown in the UI filter & dropdown
DEFAULT_GROUPS = ("Gmail", "Facebook", "Instagram", "TikTok", "X / Twitter", "Crypto")

WEBRTC_MODES = ("proxy", "disable")
CANVAS_MODES = ("auto", "noise", "native")


MAX_TAGS = 20
MAX_TAG_LENGTH = 64
MAX_DISPLAY_ORDER = 1_000_000

_RECOVERY_PREFIX_RE = re.compile(
    r"^(?:mail\s*recovery|recovery\s*mail|recovery|mail\s*kh[oô]i\s*ph[uụ]c)\s*[:\-]?\s*"
    r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\s*$",
    re.IGNORECASE,
)
_EMAIL_TOKEN_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def split_recovery_and_notes(
    recovery_email: str | None = None,
    notes: str | None = None,
) -> tuple[str, str]:
    """Return (recovery_email, clean_notes), extracting legacy recovery emails from notes when needed."""
    explicit_rec = (recovery_email or "").strip()
    raw_notes = (notes or "").strip()
    if explicit_rec:
        return explicit_rec, raw_notes
    if not raw_notes:
        return "", ""

    found_email = ""
    kept_lines: list[str] = []
    for raw_line in raw_notes.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not found_email:
            m_pref = _RECOVERY_PREFIX_RE.match(line)
            if m_pref:
                found_email = m_pref.group(1)
                continue
            if _EMAIL_TOKEN_RE.match(line):
                found_email = line
                continue
            tokens = line.split()
            if len(tokens) >= 2 and _EMAIL_TOKEN_RE.match(tokens[-1]):
                found_email = tokens[-1]
                rem = " ".join(tokens[:-1]).strip(" -|:,")
                if rem:
                    kept_lines.append(rem)
                continue
            if len(tokens) >= 2 and _EMAIL_TOKEN_RE.match(tokens[0]):
                found_email = tokens[0]
                rem = " ".join(tokens[1:]).strip(" -|:,")
                if rem:
                    kept_lines.append(rem)
                continue
        kept_lines.append(line)

    if found_email:
        return found_email, "\n".join(kept_lines).strip()
    return "", raw_notes


def normalize_tags(tags: list[str]) -> list[str]:
    if not isinstance(tags, list) or len(tags) > MAX_TAGS:
        raise ValueError(f"Use at most {MAX_TAGS} tags")
    result = []
    seen = set()
    for value in tags:
        if not isinstance(value, str) or len(value.strip()) > MAX_TAG_LENGTH:
            raise ValueError(f"Each tag must be text up to {MAX_TAG_LENGTH} characters")
        value = value.strip()
        if value and value.casefold() not in seen:
            result.append(value)
            seen.add(value.casefold())
    return result


def valid_display_order(value: object) -> bool:
    return value is None or (type(value) is int and 1 <= value <= MAX_DISPLAY_ORDER)


@dataclass
class Profile:
    name: str
    proxy: str | None = None
    os_type: str = "windows"
    # None = follow the proxy's exit IP (Camoufox geoip); set to pin a value.
    timezone: str | None = None
    locale: str | None = None
    engine: str = "camoufox"
    # Set only by the explicit backed-up core operation; None preserves legacy resolution.
    core_version: str | None = None
    # Group / folder tag & account credentials (optional, backward-compatible)
    group: str | None = None
    account_uid: str | None = None
    account_password: str | None = None
    two_fa_secret: str | None = None
    recovery_email: str | None = None
    notes: str | None = None
    start_url: str | None = None
    # Hardware / Anti-detect overrides (validated via Camoufox coherence)
    webrtc_mode: str = "proxy"
    canvas_mode: str = "auto"
    tags: list[str] = field(default_factory=list)
    extension_ids: list[str] = field(default_factory=list)
    display_order: int | None = None

    @property
    def effective_tags(self) -> list[str]:
        return list(self.tags) if self.tags else ([self.group] if self.group else [])

    @property
    def effective_recovery_email(self) -> str:
        return split_recovery_and_notes(self.recovery_email, self.notes)[0]

    @property
    def effective_notes(self) -> str:
        return split_recovery_and_notes(self.recovery_email, self.notes)[1]

    def to_dict(self) -> dict:
        return asdict(self)
