from ...core.config import THEME_MODE

LIGHT_COLORS: dict[str, str] = {
    # PionLogin Crisp Slate & White (Light theme)
    "bg": "#F8FAFC",
    "card_bg": "#FFFFFF",
    "paper_sunk": "#F1F5F9",
    "card_border": "#E2E8F0",
    "rule_strong": "#CBD5E1",
    "border": "#E2E8F0",
    "text_main": "#0F172A",
    "text_sub": "#475569",
    "text_dim": "#64748B",
    "input_bg": "#FFFFFF",
    # Sidebar rail (PionLogin pure white sidebar)
    "sidebar": "#FFFFFF",
    "cover_deep": "#F1F5F9",
    "cover_ink": "#0F172A",
    "cover_muted": "#64748B",
    "log_bg": "#F8FAFC",
    # Action & buttons (PionLogin obsidian CTA + royal blue accent)
    "accent": "#2563EB",
    "accent_hover": "#1E293B",
    "accent_soft": "#EFF6FF",
    "ink_button": "#0F172A",
    "primary_cta_bg": "#0F172A",
    "primary_cta_fg": "#FFFFFF",
    # States & badges
    "live": "#10B981",
    "live_tint": "#E9FBF3",
    "live_ink": "#067A52",
    "card_hover": "#F8FAFC",
    "card_selected_bg": "#EFF6FF",
    "card_selected_border": "#2563EB",
    "success": "#059669",
    "error": "#DC2626",
    "delete_hover": "#B91C1C",
    "warning": "#D97706",
    "warn_tint": "#FEF3C7",
    "warn_text": "#92400E",
    "cover_line": "#E2E8F0",
    "scrollbar_bg": "#F8FAFC",
    "scrollbar_grab": "#CBD5E1",
    # PionLogin pill accents
    "proxy_pill_bg": "#EFF6FF",
    "proxy_pill_fg": "#1E40AF",
    "proxy_pill_border": "#BFDBFE",
    "status_bar_bg": "#FFFFFF",
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
    "live_tint": "#0D1F1A",
    "live_ink": "#34D399",
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
