import logging
import time
import re
from typing import Dict, Any, Optional

from browser_pool import extract_via_browser_capture, fetch_html_via_browser

logger = logging.getLogger(__name__)


class ExtractorError(Exception):
    pass


# Manually sourced / bulk-scraped channel slug -> embed path mapping.
# The slug is the STABLE key here -- the embed domain rotates periodically
# (seen so far: icelanders.st -> hux-giants.shop -> cdx-08192.website) and
# is therefore resolved dynamically per-request rather than hardcoded.
CHANNEL_SLUGS: dict[str, str] = {
    "fox-sports-504": "fox-sports-504",
    "ae-usa": "ae-usa",
    # add more as you source them manually or via slug_extractor.py
}

# --- Manual override -------------------------------------------------------
# Set this whenever you've confirmed (e.g. via browser DevTools) the current
# live embed domain, to bypass dlhd/dlstreams auto-discovery entirely.
MANUAL_EMBED_BASE_OVERRIDE: Optional[str] = None
# ----------------------------------------------------------------------------

# dlhd.st / dlstreams.st mirror (the channel-listing site, separate from the
# embed-iframe domain above). Has already rotated once (dlhd.st ->
# dlstreams.st). Only used when MANUAL_EMBED_BASE_OVERRIDE is unset.
DLHD_BASE = "https://dlive.sx/"
PRIMARY_FOLDER = "plus"
FALLBACK_FOLDERS = ["stream", "cast", "watch", "casting", "player"]

# A small pool of known-good, non-adult channel IDs to use when probing for
# the live embed domain. Avoid single hardcoded IDs like "504" which may
# be reassigned to unrelated/placeholder channels over time.
PROBE_CHANNEL_IDS = ["44", "39", "35"]  # e.g. ESPN USA, Fox Sports 1 USA, Sky Sports Football UK

# Matches the embed domain regardless of which mirror is currently live,
# e.g. https://cdx-08192.website/embed/fox-sports-504
EMBED_RE = re.compile(r'https?://([^/"\'\s]+)/embed/([a-zA-Z0-9\-]+)', re.I)

# Headers that are safe/expected to carry over verbatim from the captured
# browser request. Confirmed via real DevTools capture of a working
# volder.timst.cfd request -- this CDN's request included Sec-Ch-Ua*
# client-hint headers and Priority alongside the usual Referer/Origin/UA,
# so those are now explicitly forwarded rather than dropped. We still
# deliberately exclude HTTP/2 pseudo-headers (":authority", ":method",
# etc.) and connection-specific headers ("host", "content-length"), since
# those are meaningless or wrong when replayed by our proxy.
_FORWARDABLE_HEADER_KEYS = {
    "user-agent", "accept", "accept-language", "accept-encoding",
    "referer", "origin", "cookie", "range", "priority",
    "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site",
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
}

# Cache of the resolved embed base (domain), refreshed periodically since
# it can rotate without notice.
_embed_base_cache: dict[str, float] = {"base": "", "expiry": 0.0}
EMBED_BASE_CACHE_TTL = 600  # seconds - re-check the live mirror periodically

# Cache of browser-captured signed URLs: slug -> (m3u8_url, headers, expiry_ts)
_signed_url_cache: dict[str, tuple[str, dict, float]] = {}
SIGNED_URL_CACHE_TTL = 240  # seconds - adjust after observing real CDN expiry

# Fallback client-hint values matching the Chrome UA used elsewhere in this
# extractor (Chrome 150 on Windows), for use when the browser capture
# didn't expose these headers directly.
_DEFAULT_CLIENT_HINTS = {
    "Sec-Ch-Ua": '"Not=A?Brand";v="99", "Chromium";v="150"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
}


def _build_playback_headers(captured_headers: dict, embed_base: str) -> dict:
    """Builds the header set sent on every proxied request (manifest AND
    each subsequent segment). Prefers the browser-captured headers
    verbatim -- since those are what actually got the CDN to serve the
    stream in Playwright -- and only fills in defaults for anything the
    capture didn't include. Confirmed via real DevTools capture that this
    CDN (volder.timst.cfd) expects Sec-Ch-Ua* client-hint headers and a
    Priority header alongside Referer/Origin; playback breaking after the
    first segment is consistent with those being dropped on subsequent
    proxied requests.
    """
    headers = {
        k: v for k, v in (captured_headers or {}).items()
        if k.lower() in _FORWARDABLE_HEADER_KEYS
    }

    headers.setdefault("Referer", f"{embed_base.rsplit('/embed', 1)[0]}/")
    headers.setdefault("Origin", embed_base.rsplit("/embed", 1)[0])
    headers.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    )
    headers.setdefault("Accept", "*/*")
    headers.setdefault("Accept-Encoding", "gzip, deflate, br, zstd")
    headers.setdefault("Accept-Language", "en-US,en;q=0.9")
    headers.setdefault("Priority", "u=1, i")
    headers.setdefault("Sec-Fetch-Dest", "empty")
    headers.setdefault("Sec-Fetch-Mode", "cors")
    headers.setdefault("Sec-Fetch-Site", "cross-site")
    for key, value in _DEFAULT_CLIENT_HINTS.items():
        headers.setdefault(key, value)

    return headers


class IcelandersExtractor:
    """Dedicated extractor for the icelanders-style embed pages.

    The embed domain is a rotating mirror (icelanders.st -> hux-giants.shop
    -> cdx-08192.website, and counting), so it is normally resolved
    dynamically from a live dlhd.st/dlstreams.st player page, fetched via
    the headless-browser pool since that site blocks plain HTTP requests
    to its inner pages. MANUAL_EMBED_BASE_OVERRIDE can pin a confirmed
    domain to bypass discovery entirely while it's being validated.

    Always uses the headless-browser capture path for the final signed
    .m3u8 URL too, since these embeds require JS execution to reveal it.
    """

    def __init__(self, request_headers: dict = None, proxies: list = None, bypass_warp: bool = False):
        self.request_headers = request_headers or {}
        self.proxies = proxies or []
        self.bypass_warp_active = bypass_warp
        self.mediaflow_endpoint = "hls_manifest_proxy"

    @staticmethod
    def _resolve_slug(channel_ref: str) -> str:
        """Accepts a known alias, a raw slug, or a full embed URL and
        returns the actual embed path segment to use."""
        match = EMBED_RE.search(channel_ref)
        if match:
            return match.group(2)
        return CHANNEL_SLUGS.get(channel_ref, channel_ref)

    async def _fetch_player_page_via_browser(self, probe_id: str) -> Optional[str]:
        """Requests candidate player subpages through the headless-browser
        pool (not aiohttp) since dlstreams.st blocks plain server-side GETs
        to inner pages. Returns the first successful HTML body, or None if
        every folder fails for this probe id."""
        referer = f"{DLHD_BASE}/watch.php?id={probe_id}"
        folders = [PRIMARY_FOLDER] + FALLBACK_FOLDERS

        for folder in folders:
            url = f"{DLHD_BASE}/{folder}/stream-{probe_id}.php"
            try:
                html = await fetch_html_via_browser(
                    url,
                    referer=referer,
                    bypass_warp=self.bypass_warp_active,
                )
                if html:
                    logger.debug("Icelanders: browser probe %s succeeded (folder=%s)", url, folder)
                    return html
                logger.debug("Icelanders: browser probe %s returned empty body", url)
            except Exception as e:
                logger.debug("Icelanders: browser probe failed for %s: %s", url, e)
                continue
        return None

    async def _discover_embed_base(self, channel_id_hint: Optional[str] = None) -> str:
        """Finds the currently-live embed domain. Uses
        MANUAL_EMBED_BASE_OVERRIDE if set; otherwise checks dlhd.st/
        dlstreams.st player pages via the headless-browser pool (plain
        HTTP requests are blocked on this site's inner pages), trying
        several folders and probe channel IDs since neither is guaranteed
        stable over time."""
        if MANUAL_EMBED_BASE_OVERRIDE:
            logger.debug("Icelanders: using MANUAL_EMBED_BASE_OVERRIDE %s", MANUAL_EMBED_BASE_OVERRIDE)
            _embed_base_cache["base"] = MANUAL_EMBED_BASE_OVERRIDE
            _embed_base_cache["expiry"] = time.time() + EMBED_BASE_CACHE_TTL
            return MANUAL_EMBED_BASE_OVERRIDE

        now = time.time()
        if _embed_base_cache["base"] and _embed_base_cache["expiry"] > now:
            return _embed_base_cache["base"]

        probe_ids = [channel_id_hint] if channel_id_hint else []
        probe_ids += [pid for pid in PROBE_CHANNEL_IDS if pid not in probe_ids]

        for probe_id in probe_ids:
            html = await self._fetch_player_page_via_browser(probe_id)
            if not html:
                continue
            match = EMBED_RE.search(html)
            if match:
                base = f"https://{match.group(1)}/embed"
                logger.info("Icelanders: resolved live embed base %s (via probe id %s)", base, probe_id)
                _embed_base_cache["base"] = base
                _embed_base_cache["expiry"] = now + EMBED_BASE_CACHE_TTL
                return base
            logger.debug("Icelanders: probe id %s loaded but no embed match found", probe_id)

        if _embed_base_cache["base"]:
            logger.warning("Icelanders: base discovery failed, reusing stale cached base %s", _embed_base_cache["base"])
            return _embed_base_cache["base"]

        raise ExtractorError(
            f"Icelanders: could not discover a live embed domain from {DLHD_BASE} "
            f"(tried probe ids: {probe_ids})"
        )

    async def extract(self, channel_ref: str, **kwargs) -> Dict[str, Any]:
        """Extracts the M3U8 URL and headers for a given channel.

        channel_ref: a key from CHANNEL_SLUGS, a raw slug matching the
        embed path (e.g. "fox-sports-504"), or a full embed URL (in which
        case both the slug and the domain hint are pulled from it).
        """
        slug = self._resolve_slug(channel_ref)

        cached = _signed_url_cache.get(slug)
        if cached and cached[2] > time.time():
            logger.debug("Icelanders: using cached URL for %s", slug)
            stream_url, captured_headers, _ = cached
            embed_base = _embed_base_cache["base"] or await self._discover_embed_base()
        else:
            embed_base = await self._discover_embed_base()
            embed_url = f"{embed_base}/{slug}"
            logger.info("Icelanders: launching browser capture for %s", embed_url)
            browser_result = await extract_via_browser_capture(
                embed_url,
                referer=embed_url,
                bypass_warp=self.bypass_warp_active,
            )
            if not browser_result:
                raise ExtractorError(f"Icelanders: browser capture failed for {embed_url}")
            stream_url = browser_result["url"]
            captured_headers = browser_result.get("headers", {})
            _signed_url_cache[slug] = (stream_url, captured_headers, time.time() + SIGNED_URL_CACHE_TTL)
            logger.info("Icelanders: captured stream URL for %s: %s", slug, stream_url)

        # Use the browser-captured headers verbatim (with safe defaults) so
        # every proxied request -- manifest AND each segment -- matches
        # what the real browser sent, rather than a reconstructed guess.
        playback_headers = _build_playback_headers(captured_headers, embed_base)

        return {
            "destination_url": stream_url,
            "request_headers": playback_headers,
            "mediaflow_endpoint": self.mediaflow_endpoint,
            "captured_manifest": None,
            "captured_manifests": {stream_url: ""},
        }

    async def close(self):
        pass  # No persistent session to close; BrowserPool is shared app-wide
