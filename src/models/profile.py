from dataclasses import asdict, dataclass

# Browser engines a profile can run on. A profile keeps its engine for life:
# the two keep incompatible browser data in the same directory.
ENGINES = ("camoufox", "chrome")

# Preset account groups shown in the UI filter & dropdown
DEFAULT_GROUPS = ("Gmail", "Facebook", "Instagram", "TikTok", "X / Twitter", "Crypto")

WEBRTC_MODES = ("proxy", "disable")
CANVAS_MODES = ("auto", "noise", "native")


@dataclass
class Profile:
    name: str
    proxy: str | None = None
    os_type: str = "windows"
    # None = follow the proxy's exit IP (Camoufox geoip); set to pin a value.
    timezone: str | None = None
    locale: str | None = None
    engine: str = "camoufox"
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

    def to_dict(self) -> dict:
        return asdict(self)
