from .browser import router as browser_router
from .extensions import router as extensions_router
from .mcp_setup import router as mcp_setup_router
from .page import router as page_router
from .profiles import router as profiles_router
from .proxy import router as proxy_router
from .warming import router as warming_router

__all__ = [
    "browser_router",
    "extensions_router",
    "mcp_setup_router",
    "page_router",
    "profiles_router",
    "proxy_router",
    "warming_router",
]
