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
    notes: str | None = None
    start_url: str | None = None
    # Hardware / Anti-detect overrides (validated via Camoufox coherence)
    webrtc_mode: str = "proxy"
    canvas_mode: str = "auto"
    tags: list[str] = field(default_factory=list)
    display_order: int | None = None

    @property
    def effective_tags(self) -> list[str]:
        return list(self.tags) if self.tags else ([self.group] if self.group else [])

    def to_dict(self) -> dict:
        return asdict(self)
