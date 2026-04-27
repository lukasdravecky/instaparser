"""
Pridaj tieto importy a endpoint do main.py
"""

# --- IMPORTY (pridaj k existujúcim) ---
import httpx
from fastapi import Query
from fastapi.responses import StreamingResponse

# --- ENDPOINT (pridaj do main.py) ---

@app.get("/api/proxy")
async def proxy_media(url: str = Query(...)):
    """
    Proxuje CDN médiá cez backend — pridáva Referer + Accept headers
    ktoré Instagram vyžaduje. Frontend volá /api/proxy?url=<fbcdn_url>
    namiesto priameho fetchu.
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

    async def stream():
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            async with client.stream("GET", url, headers=headers) as r:
                if r.status_code != 200:
                    raise HTTPException(status_code=r.status_code, detail="CDN fetch zlyhal")
                async for chunk in r.aiter_bytes(chunk_size=65536):
                    yield chunk

    # Zisti content-type podľa URL
    if ".mp4" in url.lower():
        media_type = "video/mp4"
    elif ".webp" in url.lower():
        media_type = "image/webp"
    else:
        media_type = "image/jpeg"

    return StreamingResponse(stream(), media_type=media_type)
