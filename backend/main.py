"""
InstaParser Backend v2 — FastAPI + Playwright
Spustenie: uvicorn main:app --reload --port 8000
"""
import asyncio
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from playwright_parser import BrowserConfig, PlaywrightParser

logging.basicConfig(level=logging.INFO, format="%(levelname)s │ %(name)s │ %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("🚀 InstaParser backend štartuje")
    yield
    log.info("InstaParser zastavený")


app = FastAPI(title="InstaParser API v2 (Playwright)", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

frontend_path = Path(__file__).parent.parent / "frontend"
frontend_file = frontend_path / "instaparser.html"
if frontend_path.exists():
    app.mount("/assets", StaticFiles(directory=str(frontend_path)), name="frontend-assets")


@app.get("/")
def frontend_index():
    if frontend_file.exists():
        return FileResponse(str(frontend_file))
    raise HTTPException(status_code=404, detail="Frontend file 'instaparser.html' not found")


class ParseRequest(BaseModel):
    url: str
    max_posts: int = 21
    headless: bool = True
    scroll_rounds: int = 8


class ParseResponse(BaseModel):
    account: str
    posts: list[dict]
    total: int


def extract_username(url: str) -> str:
    url = url.strip().rstrip("/")
    if "instagram.com/" in url:
        username = url.split("instagram.com/")[-1].split("/")[0].split("?")[0]
    elif url.startswith("@"):
        username = url[1:]
    else:
        username = url
    username = username.lstrip("@")
    if not username or not re.match(r"^[\w.]+$", username):
        raise ValueError(f"Neplatné Instagram URL: '{url}'")
    return username


@app.post("/api/parse", response_model=ParseResponse)
async def parse_account(req: ParseRequest):
    username = extract_username(req.url)
    log.info(f"▶ Parsovanie: @{username}  max={req.max_posts}  headless={req.headless}")

    config = BrowserConfig(
        headless=req.headless,
        max_posts=req.max_posts,
        scroll_rounds=req.scroll_rounds,
    )
    parser = PlaywrightParser(config)
    base_timeout = 60 + req.max_posts * 15 + req.scroll_rounds * 8
    if not req.headless:
        base_timeout += 300
    parse_timeout = max(180, min(900, base_timeout))

    try:
        posts = await asyncio.wait_for(parser.fetch_posts(username), timeout=parse_timeout)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=f"Parsovanie prekročilo časový limit ({parse_timeout}s). Skúste menší počet príspevkov.",
        )
    except Exception as e:
        msg = str(e)
        if "Please wait a few minutes before you try again" in msg:
            raise HTTPException(status_code=429, detail=msg)
        if "Something went wrong" in msg or "0 výsledkov" in msg:
            raise HTTPException(status_code=503, detail=msg)
        log.exception("Parse zlyhal")
        raise HTTPException(status_code=500, detail=msg)

    log.info(f"✓ Hotovo: {len(posts)} príspevkov pre @{username}")
    return ParseResponse(account=username, posts=posts, total=len(posts))


@app.get("/api/proxy")
async def proxy_media(url: str = Query(...)):
    """
    Proxuje CDN médiá cez backend — pridáva Referer + hlavičky
    ktoré Instagram vyžaduje. Frontend volá /api/proxy?url=<fbcdn_url>
    namiesto priameho fetch() ktorý vracia prázdnych 12B.
    """
    if not any(domain in url for domain in ["fbcdn.net", "cdninstagram.com"]):
        raise HTTPException(status_code=400, detail="Len Instagram CDN URL-ky sú povolené")

    headers = {
        "Referer": "https://www.instagram.com/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "sec-fetch-dest": "image",
        "sec-fetch-mode": "no-cors",
        "sec-fetch-site": "cross-site",
    }

    if ".mp4" in url.lower():
        media_type = "video/mp4"
    elif ".webp" in url.lower():
        media_type = "image/webp"
    else:
        media_type = "image/jpeg"

    async def stream():
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            async with client.stream("GET", url, headers=headers) as r:
                if r.status_code != 200:
                    log.warning(f"CDN vrátil {r.status_code} pre: {url[:80]}")
                    raise HTTPException(status_code=r.status_code, detail="CDN fetch zlyhal")
                async for chunk in r.aiter_bytes(chunk_size=65536):
                    yield chunk

    return StreamingResponse(stream(), media_type=media_type)


@app.get("/api/health")
def health():
    return {"status": "ok", "engine": "playwright"}
