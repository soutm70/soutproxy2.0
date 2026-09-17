import logging
import urllib.parse
from aiohttp import web
from extractors.icelanders import IcelandersExtractor, ExtractorError

logger = logging.getLogger(__name__)


async def handle_icelanders_stream(request: web.Request) -> web.Response:
    """
    Stable IPTV-friendly endpoint for icelanders.st channels.
    Extracts a fresh signed M3U8 URL and redirects through the proxy
    with correct headers so IPTV apps never see an expired token.

    Usage: /icelanders/{slug}
    e.g.   /icelanders/fox-sports-504
    """
    slug = request.match_info.get("slug", "").strip()
    if not slug:
        return web.Response(status=400, text="Missing channel slug")

    extractor = IcelandersExtractor()
    try:
        result = await extractor.extract(slug)
    except ExtractorError as e:
        logger.error("Icelanders route error for slug %s: %s", slug, e)
        return web.Response(status=502, text=f"Extraction failed: {e}")

    stream_url = result["destination_url"]
    headers = result["request_headers"]

    encoded_url = urllib.parse.quote(stream_url, safe="")
    header_params = "".join(
        f"&h_{urllib.parse.quote(k)}={urllib.parse.quote(v)}"
        for k, v in headers.items()
    )
    proxy_url = f"/proxy/manifest.m3u8?url={encoded_url}{header_params}"

    raise web.HTTPFound(location=proxy_url)


def setup_icelanders_routes(app: web.Application):
    app.router.add_get("/icelanders/{slug}", handle_icelanders_stream)