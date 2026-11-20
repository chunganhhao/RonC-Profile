from urllib.parse import quote, unquote, urlparse


def normalize_proxy(proxy_str: str) -> str:
    """Rewrite the provider format host:port:user:pass as user:pass@host:port.

    Other values keep their contents; URL schemes are normalized to lowercase.
    Passwords may contain '@' or ':'; they are percent-encoded so urlparse
    can decode them correctly.
    """
    proxy_str = proxy_str.strip()
    scheme, sep, rest = proxy_str.rpartition("://")
    if sep:
        scheme = scheme.lower()
        proxy_str = f"{scheme}{sep}{rest}"
    parts = rest.split(":", 3)
    # Detect host:port:user:pass: exactly 4 non-empty parts, port is all digits,
    # and no '@' in the host (first) part.  Check this BEFORE the '@'-in-rest
    # short-circuit so passwords containing '@' are handled correctly.
    if len(parts) == 4 and parts[1].isdigit() and all(parts) and "@" not in parts[0]:
        host, port, user, password = parts
        quoted_user = quote(user, safe="")
        quoted_pass = quote(password, safe="")
        return f"{scheme}{sep}{quoted_user}:{quoted_pass}@{host}:{port}"
    if "@" in rest:
        return proxy_str
    return proxy_str


def parse_proxy(proxy_str: str) -> dict | None:
    if not proxy_str or proxy_str == "None":
        return None
    try:
        proxy_str = normalize_proxy(proxy_str)
        if "://" not in proxy_str:
            proxy_str = "http://" + proxy_str
        p = urlparse(proxy_str)
        if not p.hostname or not p.port:
            return None
        # Preserve brackets for IPv6 addresses in the server URL
        hostname = f"[{p.hostname}]" if ":" in p.hostname else p.hostname
        cfg = {"server": f"{p.scheme}://{hostname}:{p.port}"}
        if p.username:
            cfg["username"] = unquote(p.username)
        if p.password:
            cfg["password"] = unquote(p.password)
        return cfg
    except Exception:
        return None


def mask_proxy(proxy_str: str | None) -> str:
    """Proxy as scheme://host:port, without credentials, for display."""
    cfg = parse_proxy(proxy_str or "")
    return cfg["server"] if cfg else ""


def require_proxy(proxy_str: str | None) -> dict | None:
    """Parse a network-bound proxy; malformed nonempty input must never mean direct."""
    if proxy_str in (None, "", "None"):
        return None
    from .validation import validate_proxy_format

    if not isinstance(proxy_str, str) or not validate_proxy_format(proxy_str)[0]:
        raise ValueError("Invalid proxy configuration; refusing direct connection")
    config = parse_proxy(proxy_str)
    if config is None:
        raise ValueError("Invalid proxy configuration; refusing direct connection")
    return config
