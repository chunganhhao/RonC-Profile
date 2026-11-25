"""Remote control of a running profile's pages.

runner.py starts `serve()` next to the browser. It listens on 127.0.0.1 on a
random port, requires a random per-launch token, and announces itself on
stdout as "CONTROL:<port>:<token>" so the launcher can forward API calls.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import contextvars
import math
import re
import secrets
import time
from typing import Any
from urllib.parse import unquote, urlparse

from aiohttp import web

try:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
except ImportError:  # pragma: no cover
    PlaywrightTimeoutError = ()  # type: ignore[assignment]

try:
    from playwright._impl._errors import TargetClosedError as PlaywrightTargetClosedError
except ImportError:  # pragma: no cover
    PlaywrightTargetClosedError = ()  # type: ignore[assignment]

ALLOWED_SCHEMES = ("http", "https", "about")
DEFAULT_CANCEL_GRACE_SECONDS = 1.5
MAX_FULL_PAGE_HEIGHT = 16_384
MAX_TEXT = 40_000
TOKEN_HEADER = "X-Control-Token"
_NON_MUTATING_TIMEOUT_ACTIONS = frozenset({"wait_for", "snapshot", "text", "tabs", "screenshot"})
_TAB_SWITCH_ACTIONS = frozenset({"tab_new", "tab_select", "tab_close"})
_REQUIRE_CURRENT: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_control_require_current", default=False
)
_TARGET_PAGE_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_control_target_page_id", default=None
)
_monotonic = time.monotonic


class ControlError(Exception):
    pass


class PartialActionError(Exception):
    """Raised when a multi-step action fails after an earlier sub-step mutated page state."""


_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):(.*)$", re.S)


def _check_url(url: str) -> str:
    url = url.strip()
    # "%66ile:" or "java%73cript:" would miss _SCHEME and get https:// prepended;
    # decode a percent-encoded scheme and check it like any other.
    colon = url.find(":")
    slash = url.find("/")
    if colon != -1 and "%" in url[:colon] and (slash == -1 or colon < slash):
        decoded_scheme = unquote(url[:colon]).lower()
        if decoded_scheme not in ALLOWED_SCHEMES:
            raise ControlError(f"URL scheme '{decoded_scheme}' is not allowed")

    match = _SCHEME.match(url)
    # "localhost:8080/x" looks like a scheme but is host:port; bare hosts get https.
    if not match or re.match(r"\d+(/|$)", match.group(2)):
        url = "https://" + url
    try:
        parsed = urlparse(url)
        host = parsed.hostname
    except ValueError as e:  # e.g. an unclosed IPv6 bracket: "[::1"
        raise ControlError(f"Malformed URL: {e}") from e
    scheme = parsed.scheme.lower()
    if scheme in ("http", "https") and not host:
        # Older Python versions parse "https://[]" without raising.
        raise ControlError("Malformed URL: no host")
    if scheme not in ALLOWED_SCHEMES:
        # file:, view-source:, chrome: etc. would let a remote caller read
        # local files or browser internals through the snapshot.
        raise ControlError(f"URL scheme '{scheme}' is not allowed")
    if scheme == "about" and url.lower() != "about:blank":
        # about:config, about:logins etc. expose or change browser internals.
        raise ControlError("Only about:blank is allowed among about: pages")
    return url


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


class PageController:
    """Executes control actions against a Playwright BrowserContext."""

    def __init__(self, context: Any) -> None:
        self._context = context
        self._current: Any = None
        self._page_ids: dict[int, tuple[Any, str]] = {}

    # --- page selection ---------------------------------------------------

    def _open_pages(self) -> list[Any]:
        return [p for p in self._context.pages if not p.is_closed()]

    def _page_id(self, page: Any) -> str:
        existing = self._page_ids.get(id(page))
        if existing is not None and existing[0] is page:
            return existing[1]
        self._page_ids = {
            k: v
            for k, v in self._page_ids.items()
            if not (callable(getattr(v[0], "is_closed", None)) and v[0].is_closed())
        }
        pid = secrets.token_hex(8)
        self._page_ids[id(page)] = (page, pid)
        return pid

    def current_page_id(self) -> str | None:
        if self._current is None or self._current.is_closed():
            return None
        return self._page_id(self._current)

    async def _page(self) -> Any:
        expected_page_id = _TARGET_PAGE_ID.get()
        if expected_page_id is not None:
            if (
                self._current is None
                or self._current.is_closed()
                or self._page_id(self._current) != expected_page_id
            ):
                raise PartialActionError("Active target tab changed or is no longer open")
            return self._current
        if _REQUIRE_CURRENT.get():
            if self._current is None or self._current.is_closed():
                raise PartialActionError("Active target tab is no longer open")
            return self._current
        pages = self._open_pages()
        if self._current is not None and not self._current.is_closed():
            return self._current
        if pages:
            self._current = pages[-1]
            return self._current
        self._current = await self._context.new_page()
        return self._current

    async def _describe(self, page: Any) -> dict[str, Any]:
        return {"url": page.url, "title": await page.title()}

    async def _describe_after_mutation(self, page: Any) -> dict[str, Any]:
        if page.is_closed():
            raise PartialActionError("Target page closed during action")
        try:
            return await self._describe(page)
        except Exception as exc:
            raise PartialActionError(f"{type(exc).__name__}: {exc}") from exc

    async def _tabs_after_mutation(self, expected_current: Any = None) -> dict[str, Any]:
        if expected_current is not None and expected_current.is_closed():
            raise PartialActionError("Target tab closed during action")
        try:
            pages = self._open_pages()
            current = expected_current if expected_current is not None else await self._page()
            tabs = [
                {"index": i, "url": p.url, "title": await p.title(), "active": p is current}
                for i, p in enumerate(pages)
            ]
        except Exception as exc:
            raise PartialActionError(f"{type(exc).__name__}: {exc}") from exc
        if expected_current is not None and expected_current.is_closed():
            raise PartialActionError("Target tab closed during action")
        return {"tabs": tabs}

    # --- actions ----------------------------------------------------------

    async def navigate(self, url: str, wait_until: str = "load") -> dict[str, Any]:
        page = await self._page()
        await page.goto(_check_url(url), wait_until=wait_until)
        return await self._describe_after_mutation(page)

    async def back(self) -> dict[str, Any]:
        page = await self._page()
        await page.go_back()
        return await self._describe_after_mutation(page)

    async def snapshot(self, max_chars: int = MAX_TEXT) -> dict[str, Any]:
        """Accessibility tree of the page: roles, names and text, compact."""
        page = await self._page()
        aria = await page.locator("body").aria_snapshot()
        aria, truncated = _truncate(aria, max_chars)
        return {**await self._describe(page), "snapshot": aria, "truncated": truncated}

    async def text(self, selector: str = "body", max_chars: int = MAX_TEXT) -> dict[str, Any]:
        page = await self._page()
        content = await page.locator(selector).first.inner_text()
        content, truncated = _truncate(content, max_chars)
        return {"text": content, "truncated": truncated}

    async def click(self, selector: str, timeout: int = 10_000) -> dict[str, Any]:
        page = await self._page()
        await page.locator(selector).first.click(timeout=timeout)
        return await self._describe_after_mutation(page)

    async def type(
        self,
        selector: str,
        text: str,
        submit: bool = False,
        clear: bool = True,
        timeout: int = 10_000,
    ) -> dict[str, Any]:
        page = await self._page()
        field = page.locator(selector).first
        if clear:
            await field.fill(text, timeout=timeout)
        else:
            await field.press_sequentially(text, timeout=timeout)
        if submit:
            try:
                await field.press("Enter")
            except Exception as exc:
                raise PartialActionError(f"{type(exc).__name__}: {exc}") from exc
        return await self._describe_after_mutation(page)

    async def press(self, key: str, selector: str | None = None) -> dict[str, Any]:
        page = await self._page()
        if selector:
            await page.locator(selector).first.press(key)
        else:
            await page.keyboard.press(key)
        return await self._describe_after_mutation(page)

    async def click_at(self, x: float, y: float, humanize: bool = False) -> dict[str, Any]:
        """Click viewport coordinates, as seen on a screenshot of the active tab."""
        page = await self._page()
        if humanize:
            from chrome_engine.stealth import human_click_at

            await human_click_at(page, x, y)
        else:
            await page.mouse.click(x, y)
        return await self._describe_after_mutation(page)

    async def keyboard_type(self, text: str, humanize: bool = False) -> dict[str, Any]:
        """Type into whatever element has focus."""
        page = await self._page()
        if humanize:
            from chrome_engine.stealth import human_type

            await human_type(page, text)
        else:
            await page.keyboard.type(text)
        return await self._describe_after_mutation(page)

    async def scroll(
        self,
        delta_y: float = 380.0,
        delta_x: float = 0.0,
        steps: int = 6,
        humanize: bool = True,
    ) -> dict[str, Any]:
        """Scroll the active page using wheel increments or smooth window.scrollBy."""
        page = await self._page()
        if humanize:
            from chrome_engine.stealth import human_scroll

            await human_scroll(page, delta_y=delta_y, delta_x=delta_x, steps=steps)
        else:
            mouse = getattr(page, "mouse", None)
            wheel_fn = getattr(mouse, "wheel", None) if mouse is not None else None
            if callable(wheel_fn):
                await wheel_fn(float(delta_x), float(delta_y))
            else:
                await page.evaluate(f"window.scrollBy({float(delta_x)}, {float(delta_y)}); true;")
        return await self._describe_after_mutation(page)

    async def wait_for(self, selector: str, timeout: int = 15_000) -> dict[str, Any]:
        page = await self._page()
        await page.locator(selector).first.wait_for(timeout=timeout)
        return await self._describe(page)

    async def screenshot(self, full_page: bool = False) -> dict[str, Any]:
        page = await self._page()
        if full_page:
            height = await page.evaluate("() => document.documentElement.scrollHeight")
            if height > MAX_FULL_PAGE_HEIGHT:
                raise ControlError(
                    f"Page is {height}px tall; full-page screenshots are limited to "
                    f"{MAX_FULL_PAGE_HEIGHT}px. Scroll and take viewport screenshots instead.",
                )
        png = await page.screenshot(full_page=full_page, type="png")
        return {**await self._describe(page), "png_base64": base64.b64encode(png).decode()}

    async def evaluate(self, script: str) -> dict[str, Any]:
        page = await self._page()
        return {"result": await page.evaluate(script)}

    async def tabs(self) -> dict[str, Any]:
        current = await self._page()
        pages = self._open_pages()
        return {
            "tabs": [
                {"index": i, "url": p.url, "title": await p.title(), "active": p is current}
                for i, p in enumerate(pages)
            ],
        }

    async def tab_new(self, url: str | None = None) -> dict[str, Any]:
        checked = _check_url(url) if url else None
        target = await self._context.new_page()
        self._current = target
        if checked:
            try:
                await target.goto(checked)
            except Exception as exc:
                raise PartialActionError(f"{type(exc).__name__}: {exc}") from exc
        return await self._tabs_after_mutation(expected_current=target)

    async def tab_select(self, index: int) -> dict[str, Any]:
        pages = self._open_pages()
        if not 0 <= index < len(pages):
            raise ControlError(f"No tab at index {index}")
        target = pages[index]
        try:
            await target.bring_to_front()
        except Exception as exc:
            if target.is_closed():
                self._current = target
                raise PartialActionError(f"{type(exc).__name__}: {exc}") from exc
            raise
        self._current = target
        return await self._tabs_after_mutation(expected_current=target)

    async def tab_close(self, index: int) -> dict[str, Any]:
        pages = self._open_pages()
        if not 0 <= index < len(pages):
            raise ControlError(f"No tab at index {index}")
        if len(pages) == 1:
            # Closing the last tab closes the profile's browser.
            raise ControlError("Cannot close the last tab; stop the profile instead")
        await pages[index].close()
        self._current = None
        return await self._tabs_after_mutation()

    ACTIONS = (
        "navigate",
        "back",
        "snapshot",
        "text",
        "click",
        "type",
        "press",
        "click_at",
        "keyboard_type",
        "scroll",
        "wait_for",
        "screenshot",
        "evaluate",
        "tabs",
        "tab_new",
        "tab_select",
        "tab_close",
    )

    async def dispatch(
        self,
        action: str,
        params: dict[str, Any],
        *,
        require_current: bool = False,
        target_page_id: str | None = None,
    ) -> Any:
        if action not in self.ACTIONS:
            raise ControlError(f"Unknown action '{action}'")
        strict = require_current and action not in _TAB_SWITCH_ACTIONS
        pinned = (
            target_page_id
            if (target_page_id is not None and action not in _TAB_SWITCH_ACTIONS)
            else None
        )
        token = _REQUIRE_CURRENT.set(strict)
        pid_token = _TARGET_PAGE_ID.set(pinned)
        try:
            return await getattr(self, action)(**params)
        except TypeError as e:
            raise ControlError(f"Invalid parameters for '{action}': {e}") from e
        finally:
            _TARGET_PAGE_ID.reset(pid_token)
            _REQUIRE_CURRENT.reset(token)


def build_app(controller: PageController, token: str) -> web.Application:
    action_lock = asyncio.Lock()

    def _release_on_done(done_task: asyncio.Task[Any]) -> None:
        if not done_task.cancelled():
            with contextlib.suppress(BaseException):
                done_task.exception()
        if action_lock.locked():
            action_lock.release()

    async def handle(request: web.Request) -> web.Response:
        if not secrets.compare_digest(request.headers.get(TOKEN_HEADER, ""), token):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception as e:
            return web.json_response({"ok": False, "error": f"Invalid JSON: {e}"}, status=400)
        if not isinstance(body, dict):
            return web.json_response(
                {"ok": False, "error": "Request body must be a JSON object"}, status=400
            )
        raw_require_current = body.get("require_current", False)
        if not isinstance(raw_require_current, bool):
            return web.json_response(
                {"ok": False, "error": "require_current must be a boolean"}, status=400
            )
        raw_target_page_id = body.get("target_page_id")
        if raw_target_page_id is not None and (
            not isinstance(raw_target_page_id, str) or not raw_target_page_id.strip()
        ):
            return web.json_response(
                {"ok": False, "error": "target_page_id must be a non-empty string"}, status=400
            )

        raw_timeout_ms = body.get("timeout_ms")
        timeout_s: float | None = None
        if raw_timeout_ms is not None:
            if isinstance(raw_timeout_ms, bool) or not isinstance(raw_timeout_ms, (int, float)):
                return web.json_response(
                    {"ok": False, "error": "timeout_ms must be a positive number"}, status=400
                )
            try:
                timeout_val = float(raw_timeout_ms)
            except OverflowError:
                return web.json_response(
                    {"ok": False, "error": "timeout_ms is out of range"}, status=400
                )
            if not math.isfinite(timeout_val) or timeout_val <= 0:
                return web.json_response(
                    {"ok": False, "error": "timeout_ms must be a positive number"}, status=400
                )
            timeout_s = timeout_val / 1000.0
        deadline = _monotonic() + timeout_s if timeout_s is not None else None

        if timeout_s is not None:
            try:
                await asyncio.wait_for(action_lock.acquire(), timeout=timeout_s)
            except asyncio.TimeoutError:
                return web.json_response(
                    {
                        "ok": False,
                        "error": "TimeoutError: Previous browser action is still running",
                        "error_code": "action_busy",
                        "unknown_state": False,
                        "settled": False,
                    },
                    status=504,
                )
        else:
            await action_lock.acquire()

        lock_handed_off = False
        task: asyncio.Task[Any] | None = None
        try:
            action = body.get("action", "")
            params = body.get("params") or {}
            remaining: float | None = None
            if deadline is not None:
                remaining = deadline - _monotonic()
                if remaining <= 0:
                    return web.json_response(
                        {
                            "ok": False,
                            "error": f"TimeoutError: Deadline expired before '{action}' started",
                            "error_code": "deadline_expired",
                            "unknown_state": False,
                            "settled": True,
                        },
                        status=504,
                    )
            coro = (
                controller.dispatch(
                    action,
                    params,
                    require_current=raw_require_current,
                    target_page_id=raw_target_page_id,
                )
                if (raw_require_current or raw_target_page_id is not None)
                else controller.dispatch(action, params)
            )
            task = asyncio.create_task(coro)
            if remaining is not None:
                done, _ = await asyncio.wait({task}, timeout=remaining)
                if not done:
                    task.cancel()
                    grace = min(DEFAULT_CANCEL_GRACE_SECONDS, max(0.15, (timeout_s or 1.0) * 0.25))
                    done_after, _ = await asyncio.wait({task}, timeout=grace)
                    settled = bool(done_after)
                    if settled:
                        if not task.cancelled():
                            with contextlib.suppress(BaseException):
                                task.exception()
                    else:
                        # Keep action_lock held until the uninterruptible action finishes
                        # so subsequent commands cannot interleave with its late side effects.
                        lock_handed_off = True
                        task.add_done_callback(_release_on_done)
                    return web.json_response(
                        {
                            "ok": False,
                            "error": f"TimeoutError: Action '{action}' timed out",
                            "error_code": "state_uncertain",
                            "unknown_state": True,
                            "settled": settled,
                        },
                        status=504,
                    )
            result = await task
        except ControlError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        except Exception as e:  # Playwright timeouts, navigation errors, JS errors
            message = " ".join(str(e).split())
            if "Call log:" in message:
                message = message.split("Call log:", 1)[0].strip()
            current = getattr(controller, "_current", None)
            page_closed = bool(
                current is not None
                and callable(getattr(current, "is_closed", None))
                and current.is_closed()
            )
            is_partial = isinstance(e, PartialActionError)
            is_timeout = isinstance(e, (TimeoutError, PlaywrightTimeoutError))
            is_target_closed = isinstance(e, PlaywrightTargetClosedError) or page_closed
            unknown_state = (
                is_partial
                or is_target_closed
                or (is_timeout and action not in _NON_MUTATING_TIMEOUT_ACTIONS)
            )
            error_code = (
                "state_uncertain"
                if unknown_state
                else ("action_timeout" if is_timeout else "action_error")
            )
            err_body: dict[str, Any] = {
                "ok": False,
                "error": f"{type(e).__name__}: {message[:500]}",
                "error_code": error_code,
                "unknown_state": unknown_state,
            }
            err_page_id = controller.current_page_id() if not unknown_state else None
            if err_page_id is not None:
                err_body["page_id"] = err_page_id
            return web.json_response(err_body, status=422)
        finally:
            if not lock_handed_off:
                if task is not None and not task.done():
                    task.cancel()
                    task.add_done_callback(_release_on_done)
                elif action_lock.locked():
                    action_lock.release()
        return web.json_response(
            {"ok": True, "result": result, "page_id": controller.current_page_id()}
        )

    app = web.Application(client_max_size=4 * 1024 * 1024)
    app.router.add_post("/action", handle)
    return app


async def serve(context: Any) -> tuple[web.AppRunner, int, str]:
    """Start the control server for a context. Returns (runner, port, token)."""
    token = secrets.token_urlsafe(24)
    runner = web.AppRunner(build_app(PageController(context), token), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port, token
