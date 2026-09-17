import logging
import asyncio
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

AD_BLOCK_DOMAINS = [
    "tabretwicht.com", "effectivecpmnetwork.com", "cleverwebserver.com",
    "adsboosters.xyz", "histats.com", "aclib", "cobnutscopsole.com",
]

WARP_PROXY = "socks5://127.0.0.1:1080"


def _normalize_proxy_scheme(proxy_url: str) -> str:
    """Playwright's proxy server format doesn't accept 'socks5h://' style
    schemes (curl-style remote-DNS marker); normalize to plain socks5."""
    if proxy_url.startswith("socks5h://"):
        return "socks5://" + proxy_url[len("socks5h://"):]
    if proxy_url.startswith("socks4h://"):
        return "socks4://" + proxy_url[len("socks4h://"):]
    return proxy_url


class BrowserPool:
    """Maintains a single persistent headless Chromium instance for the
    lifetime of the app, avoiding per-request browser launch overhead.
    Only lightweight browser contexts are created/destroyed per extraction.
    """
    _playwright = None
    _browser = None
    _lock = asyncio.Lock()

    @classmethod
    async def get_browser(cls):
        async with cls._lock:
            if cls._browser is None or not cls._browser.is_connected():
                logger.info("BrowserPool: launching persistent Chromium instance")
                cls._playwright = await async_playwright().start()
                cls._browser = await cls._playwright.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-gpu",
                        "--disable-extensions",
                        "--disable-background-networking",
                        "--disable-default-apps",
                        "--disable-sync",
                        "--metrics-recording-only",
                        "--mute-audio",
                    ],
                )
        return cls._browser

    @classmethod
    async def close(cls):
        if cls._browser:
            try:
                await cls._browser.close()
            except Exception:
                pass
            cls._browser = None
        if cls._playwright:
            try:
                await cls._playwright.stop()
            except Exception:
                pass
            cls._playwright = None


async def extract_via_browser_capture(
    iframe_url: str,
    referer: str,
    timeout: int = 12,
    bypass_warp: bool = False,
    proxy: str = None,
    disable_ssl: bool = True,
) -> dict | None:
    """Loads iframe_url in a shared headless browser context and captures
    the first .m3u8 network request (URL + request headers).
    """
    browser = await BrowserPool.get_browser()
    captured: dict = {}
    done = asyncio.Event()

    effective_proxy = proxy
    if not effective_proxy and not bypass_warp:
        effective_proxy = WARP_PROXY

    context_kwargs = dict(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        ignore_https_errors=disable_ssl,
        extra_http_headers={"Referer": referer},
    )

    if effective_proxy:
        normalized = _normalize_proxy_scheme(effective_proxy)
        context_kwargs["proxy"] = {"server": normalized}
        logger.debug("BrowserPool: using proxy %s for %s", normalized, iframe_url)
    else:
        logger.debug("BrowserPool: connecting directly (no proxy) for %s", iframe_url)

    context = await browser.new_context(**context_kwargs)
    page = await context.new_page()

    async def route_filter(route):
        req = route.request
        if req.resource_type in ("image", "font", "stylesheet", "media"):
            await route.abort()
            return
        if any(d in req.url for d in AD_BLOCK_DOMAINS):
            await route.abort()
            return
        await route.continue_()

    await page.route("**/*", route_filter)

    def on_request(request):
        if ".m3u8" in request.url and not captured:
            captured["url"] = request.url
            captured["headers"] = dict(request.headers)
            done.set()

    page.on("request", on_request)

    try:
        await page.goto(iframe_url, wait_until="commit", timeout=timeout * 1000)
        await asyncio.wait_for(done.wait(), timeout=timeout)
    except (asyncio.TimeoutError, Exception) as e:
        logger.debug("BrowserPool: capture failed for %s: %s", iframe_url, e)
    finally:
        await context.close()

    return captured if captured.get("url") else None


async def fetch_html_via_browser(
    url: str,
    referer: str = None,
    timeout: int = 12,
    bypass_warp: bool = False,
    proxy: str = None,
    disable_ssl: bool = True,
    wait_until: str = "domcontentloaded",
) -> str | None:
    """Loads url in a shared headless browser context and returns the
    rendered HTML, without waiting for any specific network request.

    Use this (rather than a plain aiohttp GET) for pages protected by
    bot-detection that blocks stateless server-side requests but allows
    a real browser through -- e.g. discovering the current embed domain
    from a dlhd.st/dlstreams.st player page.
    """
    browser = await BrowserPool.get_browser()

    effective_proxy = proxy
    if not effective_proxy and not bypass_warp:
        effective_proxy = WARP_PROXY

    context_kwargs = dict(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        ignore_https_errors=disable_ssl,
        extra_http_headers={"Referer": referer} if referer else {},
    )

    if effective_proxy:
        normalized = _normalize_proxy_scheme(effective_proxy)
        context_kwargs["proxy"] = {"server": normalized}
        logger.debug("BrowserPool: using proxy %s for %s", normalized, url)
    else:
        logger.debug("BrowserPool: connecting directly (no proxy) for %s", url)

    context = await browser.new_context(**context_kwargs)
    page = await context.new_page()

    async def route_filter(route):
        req = route.request
        if req.resource_type in ("image", "font", "stylesheet", "media"):
            await route.abort()
            return
        if any(d in req.url for d in AD_BLOCK_DOMAINS):
            await route.abort()
            return
        await route.continue_()

    await page.route("**/*", route_filter)

    html = None
    try:
        await page.goto(url, wait_until=wait_until, timeout=timeout * 1000)
        html = await page.content()
    except Exception as e:
        logger.debug("BrowserPool: fetch_html_via_browser failed for %s: %s", url, e)
        html = None
    finally:
        await context.close()

    return html
