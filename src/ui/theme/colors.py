from ...core.config import THEME_MODE

LIGHT_COLORS: dict[str, str] = {
    # Security paper and ink (Light theme)
    "bg": "#F3F4EF",
    "card_bg": "#FBFBF8",
    "paper_sunk": "#E9ECE4",
    "card_border": "#D5DACF",
    "rule_strong": "#BCC3B6",
    "border": "#D5DACF",
    "text_main": "#1E2422",
    "text_sub": "#48514C",
    "text_dim": "#66706A",
    "input_bg": "#FBFBF8",
    # Sidebar rail
    "sidebar": "#17332E",
    "cover_deep": "#102622",
    "cover_ink": "#E8EFEA",
    "cover_muted": "#A3BAB0",
    "log_bg": "#102622",
    # Action & buttons
    "accent": "#C2510F",
    "accent_hover": "#A84408",
    "accent_soft": "#F6E3D6",
    "ink_button": "#1E2422",
    "primary_cta_bg": "#C2510F",
    "primary_cta_fg": "#FFFFFF",
    # States & badges
    "live": "#10B981",
    "card_hover": "#ECEFE9",
    "card_selected_bg": "#E4EBE2",
    "card_selected_border": "#17332E",
    "success": "#1F7A50",
    "error": "#B42330",
    "delete_hover": "#951C27",
    "warning": "#8A5A00",
    "warn_tint": "#F6EEDB",
    "warn_text": "#5E3E00",
    "cover_line": "#2C524A",
    "scrollbar_bg": "#F3F4EF",
    "scrollbar_grab": "#BCC3B6",
    # PionLogin pill accents
    "proxy_pill_bg": "#E4EBF5",
    "proxy_pill_fg": "#1E40AF",
    "proxy_pill_border": "#BFDBFE",
    "status_bar_bg": "#E9ECE4",
}

DARK_COLORS: dict[str, str] = {
    # PionLogin Onyx Dark Theme
    "bg": "#0B0C0E",
    "card_bg": "#121418",
    "paper_sunk": "#181B21",
    "card_border": "#22262F",
    "rule_strong": "#2E3440",
    "border": "#22262F",
    "text_main": "#F3F4F6",
    "text_sub": "#9CA3AF",
    "text_dim": "#6B7280",
    "input_bg": "#16191F",
    # Sidebar rail (PionLogin matte obsidian)
    "sidebar": "#0E1014",
    "cover_deep": "#171A21",
    "cover_ink": "#F3F4F6",
    "cover_muted": "#9CA3AF",
    "log_bg": "#0A0C0F",
    # Action & buttons (PionLogin white CTA + vibrant accent)
    "accent": "#3B82F6",
    "accent_hover": "#2563EB",
    "accent_soft": "#1E293B",
    "ink_button": "#272B35",
    "primary_cta_bg": "#F9FAFB",
    "primary_cta_fg": "#0B0C0E",
    # States & badges
    "live": "#10B981",
    "card_hover": "#181C23",
    "card_selected_bg": "#1B2230",
    "card_selected_border": "#3B82F6",
    "success": "#10B981",
    "error": "#EF4444",
    "delete_hover": "#DC2626",
    "warning": "#F59E0B",
    "warn_tint": "#292112",
    "warn_text": "#FCD34D",
    "cover_line": "#252A34",
    "scrollbar_bg": "#0B0C0E",
    "scrollbar_grab": "#2E3440",
    # PionLogin pill accents (blue slate proxy badge)
    "proxy_pill_bg": "#1E293B",
    "proxy_pill_fg": "#93C5FD",
    "proxy_pill_border": "#334155",
    "status_bar_bg": "#08090B",
}

_CURRENT_THEME = "light" if THEME_MODE == "light" else "dark"
COLORS: dict[str, str] = dict(LIGHT_COLORS if _CURRENT_THEME == "light" else DARK_COLORS)

# Families registered in configure_page; Flutter needs one file per weight.
FONT = "Be Vietnam Pro"
FONT_MEDIUM = "Be Vietnam Pro Medium"
FONT_SEMIBOLD = "Be Vietnam Pro SemiBold"
FONT_BOLD = "Be Vietnam Pro Bold"
FONT_DATA = "JetBrains Mono"


def current_theme_mode() -> str:
    return _CURRENT_THEME


def set_theme_mode(mode: str) -> str:
    """Switch between 'dark' and 'light' in-place so all imported COLORS dicts update."""
    global _CURRENT_THEME
    normalized = "light" if str(mode).strip().lower() == "light" else "dark"
    _CURRENT_THEME = normalized
    palette = LIGHT_COLORS if normalized == "light" else DARK_COLORS
    COLORS.clear()
    COLORS.update(palette)
    return _CURRENT_THEME


def toggle_theme_mode() -> str:
    return set_theme_mode("light" if _CURRENT_THEME == "dark" else "dark")
