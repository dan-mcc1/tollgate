"""The page at the root of the hostname.

It is the only route a person rather than an SDK is expected to open, so what matters is
that it answers without any of the gateway's dependencies, and that it stays static: no
tenant, no database, and nothing fetched from anywhere else when a browser renders it.
"""

import re

import httpx

from tollgate.main import app


async def browser() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://tollgate")


async def test_landing_page_needs_no_dependencies() -> None:
    # No lifespan, so no database, no Redis and no upstream: the page must still answer.
    async with await browser() as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>Tollgate</title>" in response.text


async def test_landing_page_is_self_contained() -> None:
    """Nothing loaded from a third party.

    A visitor to the gateway should not be making requests to anyone else because of it,
    and a page that pulled a font or a script from a CDN would also be one more thing that
    can break the only human-readable thing this host serves.
    """
    async with await browser() as client:
        page = (await client.get("/")).text

    # Links a reader can follow are fine and there are plenty; what may not appear is
    # anything the browser fetches on its own - a src, or a <link> to a stylesheet or font.
    assert re.findall(r'\ssrc="(?!data:)[^"]*"', page) == []
    assert re.findall(r"<link[^>]+href=\"(?!data:)[^\"]*\"", page) == []
    assert "<script" not in page.lower()


async def test_landing_page_carries_no_secrets() -> None:
    """It is public, so it may only say what the repository already says."""
    async with await browser() as client:
        page = (await client.get("/")).text

    assert "tg_..." in page  # the example key is a placeholder, and looks like one
    assert not re.search(r"\bAIza[0-9A-Za-z_-]{10,}", page)  # a real Google API key
    assert not re.search(r"\btg_[0-9A-Za-z_-]{10,}", page)  # a real tenant key
