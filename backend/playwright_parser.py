"""
InstaParser — Playwright engine
Princíp: spustí reálny Chromium, scrolluje profil a zachytáva CDN URL-ky
z network traffic (rovnaký prístup ako DevTools → Network tab).
"""
import asyncio
import logging
import re
from dataclasses import dataclass, field

from playwright.async_api import Page, Response, async_playwright

log = logging.getLogger(__name__)

CDN_PATTERN = re.compile(
    r"https://[a-z0-9\-]+\.(cdninstagram\.com|fbcdn\.net)/v/"
    r".+\.(jpg|jpeg|mp4|webp)",
    re.IGNORECASE,
)

MIN_SIZE_HINT = re.compile(r"[_/]([4-9]\d{2}|[1-9]\d{3})x\1")


@dataclass
class BrowserConfig:
    headless: bool = True
    slow_mo: int = 0
    scroll_pause: float = 1.0
    scroll_rounds: int = 8
    max_posts: int = 50
    post_open_pause: float = 0.5
    carousel_slide_pause: float = 0.35
    max_carousel_slides: int = 12
    user_data_dir: str = "./browser_profile"
    viewport: dict = field(default_factory=lambda: {"width": 1280, "height": 900})


class PlaywrightParser:
    def __init__(self, config: BrowserConfig | None = None):
        self.config = config or BrowserConfig()

    async def fetch_posts(self, username: str) -> list[dict]:
        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=self.config.user_data_dir,
                headless=self.config.headless,
                slow_mo=self.config.slow_mo,
                viewport=self.config.viewport,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                timezone_id="Europe/Bratislava",
                args=["--disable-blink-features=AutomationControlled"],
            )

            page = await ctx.new_page()

            await page.add_init_script(
                """
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.chrome = { runtime: {} };
            """
            )

            async def on_response(response: Response):
                url = response.url
                if not CDN_PATTERN.search(url):
                    return
                is_large = bool(MIN_SIZE_HINT.search(url)) or "1080" in url or "720" in url
                if not is_large and "profile_pic" not in url:
                    return

            page.on("response", on_response)

            profile_url = f"https://www.instagram.com/{username}/"
            log.info(f"Otváram profil: {profile_url}")
            profile_loaded = False
            for attempt in range(2):
                try:
                    await page.goto(profile_url, wait_until="domcontentloaded", timeout=30_000)
                    profile_loaded = True
                    break
                except Exception as e:
                    log.warning(f"Profilové načítanie zlyhalo (pokus {attempt + 1}/2): {e}")
                    if attempt == 0:
                        try:
                            await page.reload(wait_until="domcontentloaded", timeout=30_000)
                            profile_loaded = True
                            break
                        except Exception:
                            pass
            if not profile_loaded:
                raise RuntimeError(f"Nepodarilo sa načítať profil @{username}")
            try:
                await page.wait_for_load_state("networkidle", timeout=4_000)
            except Exception:
                pass

            if await page.locator('text="Sorry, this page"').count() > 0:
                raise ValueError(f"Profil @{username} neexistuje alebo je súkromný")

            # Čakaj na načítanie príspevkov (aspoň DOM element má existovať)
            try:
                # Skúšaj primárny selektor
                locator = page.locator('article a[href*="/p/"]').first
                await locator.wait_for(state="attached", timeout=6_000)
                log.info("Príspevky sa načítali v DOMe ✓")
            except Exception as e:
                # Ak timeout, skúšaj fallback selektor
                try:
                    locator = page.locator('a[href*="/p/"]').first
                    await locator.wait_for(state="attached", timeout=4_000)
                    log.info("Príspevky sa načítali (fallback selektor) ✓")
                except Exception as e2:
                    log.warning(f"Čakanie na príspevky pred:  {e} | fallback: {e2}")

            await _dismiss_dialogs(page)
            await _maybe_wait_for_manual_login(page, username, self.config.headless)

            if await _page_has_instagram_error(page):
                log.warning("Instagram vrátil error page, skúšam 1x reload")
                await page.reload(wait_until="domcontentloaded", timeout=15_000)
                await _dismiss_dialogs(page)
                await _maybe_wait_for_manual_login(page, username, self.config.headless)

            if await _page_has_instagram_error(page):
                raise RuntimeError(
                    "Instagram vrátil chybovú stránku ('Something went wrong'). "
                    "Skúste debug okno + ručný login/reload alebo inú sieť (mobilné dáta)."
                )

            posts_data = await self._scroll_and_capture(page, username)
            if not posts_data:
                raise RuntimeError(
                    "Nepodarilo sa načítať príspevky (0 výsledkov). "
                    "Skúste zapnúť debug okno, overiť že ste prihlásený do Instagramu, alebo znížiť max posts."
                )
            await ctx.close()

        log.info(f"Celkom zachytených: {len(posts_data)} príspevkov")
        return posts_data[: self.config.max_posts]

    async def _scroll_and_capture(self, page: Page, username: str) -> list[dict]:
        posts: dict[str, dict] = {}

        for round_i in range(self.config.scroll_rounds):
            # Skúšaj primárny selektor
            tiles = await page.locator('article a[href*="/p/"]').all()
            fallback_used = False
            
            # Ak ne, skúšaj fallback selektor
            if not tiles:
                tiles = await page.locator('a[href*="/p/"]').all()
                fallback_used = True
            
            if fallback_used:
                log.info(f"Kolo {round_i + 1}: vidím {len(tiles)} príspevkov v DOM (fallback selektor)")
            else:
                log.info(f"Kolo {round_i + 1}: vidím {len(tiles)} príspevkov v DOM")

            for tile in tiles:
                href = await tile.get_attribute("href") or ""
                shortcode_match = re.search(r"/p/([A-Za-z0-9_\-]+)/", href)
                if not shortcode_match:
                    continue
                sc = shortcode_match.group(1)
                if sc in posts:
                    continue

                img = tile.locator("img").first
                thumb = await img.get_attribute("src") if await img.count() > 0 else None

                has_video_icon = await tile.locator(
                    '[aria-label*="Video"], [aria-label*="Reel"]'
                ).count() > 0
                has_carousel = await tile.locator('[aria-label*="album"], svg[aria-label]').count() > 0

                posts[sc] = {
                    "id": sc,
                    "type": "video" if has_video_icon else ("carousel" if has_carousel else "image"),
                    "thumb": thumb or "",
                    "media": [],
                    "ts": "",
                    "url": f"https://www.instagram.com/p/{sc}/",
                }

            if len(posts) >= self.config.max_posts:
                break

            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(self.config.scroll_pause)

        results = []
        for i, (sc, post) in enumerate(list(posts.items())[: self.config.max_posts]):
            log.info(f"[{i + 1}/{min(len(posts), self.config.max_posts)}] Otvárám príspevok {sc}")
            media_items = await self._open_post_and_capture(page, sc, post["type"])
            post["media"] = media_items if media_items else _fallback_media(post)
            if not post["thumb"] and media_items:
                post["thumb"] = media_items[0]["url"]
            fname_base = f"{username}_{i + 1}"
            for j, m in enumerate(post["media"]):
                ext = "mp4" if m["type"] == "video" else "jpg"
                m["fname"] = (
                    f"{fname_base}_{j + 1}.{ext}" if len(post["media"]) > 1 else f"{fname_base}.{ext}"
                )
            results.append(post)
            await asyncio.sleep(0.25)

        return results

    async def _open_post_and_capture(self, page: Page, shortcode: str, post_type: str) -> list[dict]:
        captured_urls: list[dict] = []
        captured_media_urls = set()

        try:
            if post_type != "carousel":
                await page.goto(
                    f"https://www.instagram.com/p/{shortcode}/",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                await asyncio.sleep(self.config.post_open_pause)
                media = await self._get_active_post_media(page)
                if media and media["url"] not in captured_media_urls:
                    captured_urls.append(media)
                    captured_media_urls.add(media["url"])
            else:
                await page.goto(
                    f"https://www.instagram.com/p/{shortcode}/",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                await asyncio.sleep(self.config.post_open_pause)

                api_media = await self._extract_post_media_via_api(page, shortcode)
                for item in api_media:
                    if item["url"] not in captured_media_urls:
                        captured_urls.append(item)
                        captured_media_urls.add(item["url"])

                if api_media:
                    log.debug(f"Carousel {shortcode}: z API {len(api_media)} médií")
                    log.info(f"Carousel {shortcode}: použité API médiá ({len(captured_urls)})")
                    return captured_urls

                html_media = await self._extract_post_media_from_html(page)
                for item in html_media:
                    if item["url"] not in captured_media_urls:
                        captured_urls.append(item)
                        captured_media_urls.add(item["url"])

                misses = 0
                for index in range(1, self.config.max_carousel_slides + 1):
                    loaded = False
                    for candidate in self._carousel_slide_urls(shortcode, index):
                        try:
                            await page.goto(candidate, wait_until="domcontentloaded", timeout=15_000)
                            await asyncio.sleep(self.config.carousel_slide_pause)
                            loaded = True
                            break
                        except Exception:
                            continue

                    if not loaded:
                        misses += 1
                        if misses >= 2:
                            break
                        continue

                    media = await self._get_active_post_media(page)
                    if media and media["url"] not in captured_media_urls:
                        captured_urls.append(media)
                        captured_media_urls.add(media["url"])
                        misses = 0
                    else:
                        misses += 1
                        if misses >= 2:
                            break

        except Exception as e:
            log.warning(f"Príspevok {shortcode} sa nepodarilo otvoriť: {e}")

        log.info(f"Príspevok {shortcode}: Celkom obrázkov: {len(captured_urls)}")
        return captured_urls

    async def _extract_post_media_via_api(self, page: Page, shortcode: str) -> list[dict]:
        """Skúsi presne vytiahnuť carousel média cez IG internal API /api/v1/media/{id}/info/."""
        media_id = _shortcode_to_media_id(shortcode)
        if not media_id:
            return []

        try:
            data = await page.evaluate(
                """async (mid) => {
                    try {
                        const res = await fetch(`/api/v1/media/${mid}/info/`, {
                            credentials: 'include',
                            headers: { 'x-requested-with': 'XMLHttpRequest' }
                        });
                        if (!res.ok) return null;
                        return await res.json();
                    } catch {
                        return null;
                    }
                }""",
                media_id,
            )
            if not data or not isinstance(data, dict):
                return []

            items = data.get("items") or []
            if not items:
                return []

            root = items[0]
            out: list[dict] = []

            def best_image_url(node: dict) -> str:
                candidates = (((node or {}).get("image_versions2") or {}).get("candidates") or [])
                for candidate in candidates:
                    url = candidate.get("url")
                    if isinstance(url, str) and "fbcdn" in url:
                        return url
                return ""

            def best_video_url(node: dict) -> str:
                candidates = (node or {}).get("video_versions") or []
                for candidate in candidates:
                    url = candidate.get("url")
                    if isinstance(url, str) and "fbcdn" in url:
                        return url
                return ""

            carousel = root.get("carousel_media") or []
            if carousel:
                for node in carousel:
                    url = best_video_url(node) or best_image_url(node)
                    if not url:
                        continue
                    media_type = "video" if ".mp4" in url.lower() else "image"
                    out.append({"url": url, "type": media_type})
            else:
                url = best_video_url(root) or best_image_url(root)
                if url:
                    media_type = "video" if ".mp4" in url.lower() else "image"
                    out.append({"url": url, "type": media_type})

            return out
        except Exception:
            return []

    async def _extract_post_media_from_html(self, page: Page) -> list[dict]:
        """Z HTML payloadu postu vytiahne media URL-ky (carousel slidy) cez ig_cache_key."""
        out: list[dict] = []
        try:
            html = await page.content()
            raw_urls = set(re.findall(r'https://instagram\.[^"\']+fbcdn\.net/v/[^"\']+', html))
            for url in raw_urls:
                if "ig_cache_key=" not in url:
                    continue
                if any(x in url for x in ["profile_pic", "s100x100", "s150x150", "s32", "s64"]):
                    continue

                media_type = "video" if ".mp4" in url.lower() else "image"
                out.append({"url": url, "type": media_type})

            # Stabilné poradie podľa cache key / url
            out.sort(key=lambda x: x["url"])
        except Exception:
            return []

        return out

    async def _get_active_post_media(self, page: Page) -> dict | None:
        """Vráti práve aktívne hlavné médium príspevku (video alebo image)."""
        try:
            video_src = await page.evaluate(
                """() => {
                    const v = document.querySelector('article video');
                    if (!v) return '';
                    return v.currentSrc || v.src || '';
                }"""
            )
            if video_src and "fbcdn" in video_src:
                return {"url": video_src, "type": "video"}
        except Exception:
            pass

        try:
            imgs = await page.locator("article img").all()
            best_src = ""
            best_score = -1
            for img in imgs:
                src = await img.get_attribute("src") or ""
                if not src or "fbcdn" not in src:
                    continue
                if any(x in src for x in ["profile_pic", "s100x100", "s150x150", "s32", "s64"]):
                    continue

                box = await img.bounding_box()
                w = (box or {}).get("width", 0) or 0
                h = (box or {}).get("height", 0) or 0
                score = w * h
                if score > best_score:
                    best_score = score
                    best_src = src

            if best_src:
                return {"url": best_src, "type": "image"}
        except Exception:
            pass

        return None

    async def _capture_active_slide_media(self, page: Page, captured_urls: list, captured_media_urls: set):
        """Zachytí iba aktívny carousel slide podľa URL a najväčšieho fbcdn obrázka."""
        try:
            imgs = await page.locator("article img").all()
            best_src = None
            best_score = -1

            for img in imgs:
                try:
                    src = await img.get_attribute("src") or ""
                    if not src or "fbcdn" not in src:
                        continue
                    if any(x in src for x in ["profile_pic", "s100x100", "s150x150", "s32", "s64"]):
                        continue

                    box = await img.bounding_box()
                    w = (box or {}).get("width", 0) or 0
                    h = (box or {}).get("height", 0) or 0
                    dims = await img.evaluate("el => ({ nw: el.naturalWidth || 0, nh: el.naturalHeight || 0 })")
                    nw = dims.get("nw", 0) or 0
                    nh = dims.get("nh", 0) or 0
                    score = max(w * h, nw * nh)

                    if score > best_score:
                        best_score = score
                        best_src = src
                except Exception:
                    continue

            if not best_src:
                return

            if best_src in captured_media_urls:
                return

            media_type = "video" if ".mp4" in best_src.lower() else "image"
            captured_urls.append({"url": best_src, "type": media_type})
            captured_media_urls.add(best_src)
            log.debug(f"Aktívny slide: {best_src[:90]}...")
        except Exception as e:
            log.debug(f"Chyba pri capture aktívneho slide: {e}")
    
    async def _extract_post_images_from_dom(self, page: Page, captured_urls: list, captured_media_urls: set):
        """Backward compatible wrapper."""
        await self._capture_active_slide_media(page, captured_urls, captured_media_urls)

    async def _extract_fbcdn_from_page_content(self, page: Page, captured_urls: list, captured_media_urls: set):
        """Z page.content vytiahne všetky fbcdn URL-ky a pridá nové unique media."""
        try:
            html = await page.content()
            urls = set(re.findall(r'https://[^"\']+fbcdn[^"\']+', html))
            for url in urls:
                if any(x in url for x in ["profile_pic", "s100x100", "s150x150", "s32", "s64"]):
                    continue
                if url in captured_media_urls:
                    continue

                media_type = "video" if ".mp4" in url.lower() else "image"
                captured_urls.append({"url": url, "type": media_type})
                captured_media_urls.add(url)
                log.debug(f"PAGE content fbcdn: {url[:90]}...")
        except Exception as e:
            log.debug(f"Chyba pri extrakcii z page.content: {e}")

    def _carousel_slide_urls(self, shortcode: str, index: int) -> list[str]:
        """Skúša viaceré URL formáty pre carousel slide index."""
        base = f"https://www.instagram.com/p/{shortcode}/"
        return [
            f"{base}?img_index={index}",
            f"{base}?img-index={index}",
        ]

    async def _get_active_post_image_src(self, page: Page) -> str:
        """Vráti src aktívneho hlavného obrázka príspevku."""
        try:
            imgs = await page.locator("article img").all()
            best_src = ""
            best_score = -1
            for img in imgs:
                src = await img.get_attribute("src") or ""
                if not src or "fbcdn" not in src:
                    continue
                if any(x in src for x in ["profile_pic", "s100x100", "s150x150", "s32", "s64"]):
                    continue
                box = await img.bounding_box()
                w = (box or {}).get("width", 0) or 0
                h = (box or {}).get("height", 0) or 0
                score = w * h
                if score > best_score:
                    best_score = score
                    best_src = src
            return best_src
        except Exception:
            return ""
    
    async def _extract_dom_images(self, page: Page, captured_urls: list, captured_media_ids: set):
        """Extrahovať IBA hlavný obrázok príspevku z DOM-u (nie ikony, komentáre, atď)"""
        try:
            # Hľadaj špeciálne iba obrázky príspevku - Instagram ich renderuje v article s konkrétnym kontajnerom
            # Skúšaj primárne selectorov pre post image
            post_image_selectors = [
                'article img[role="img"]',  # Oficiálny post image
                'article img._aagu',  # Instagram CSS class
                'article img[alt*=" @"]',  # Post by user mention
                'article img:not([src*="profile_pic"])',  # Hľuadaj len veľké obrázky, ignoruj miniatúry
            ]
            
            for selector in post_image_selectors:
                try:
                    img_elements = await page.locator(selector).all()
                    if img_elements:
                        log.debug(f"Found post images with selector '{selector}': {len(img_elements)}")
                        for img in img_elements:
                            src = await img.get_attribute("src")
                            if not src or not CDN_PATTERN.search(src):
                                continue
                            
                            # Ignoruj miniatúry a veľmi malé obrázky
                            if "profile_pic" in src or "s100x100" in src or "s150x150" in src:
                                continue
                            
                            # Extrahovať media ID z URL-ky
                            match = re.search(r'_(\d{15,})', src)
                            if not match:
                                continue
                            media_id = match.group(1)
                            
                            # Ak už máme túto media, skip
                            if media_id in captured_media_ids:
                                continue
                            
                            # Check že obrázok má rozumnú veľkosť (nie tiny icon)
                            width = await img.get_attribute("width")
                            if width and int(width) < 200:
                                log.debug(f"Ignorujem malý IMG ({width}px): {src[:80]}")
                                continue
                            
                            captured_urls.append({"url": src, "type": "image"})
                            captured_media_ids.add(media_id)
                            log.debug(f"DOM post image (ID:{media_id}): {src[:80]}...")
                        
                        # Ak sme našli a spracovati príslušné obrázky, konči
                        if any(mid in captured_media_ids for mid in [m.split('_')[-2] for m in [u["url"] for u in captured_urls]]):
                            break
                except Exception as e:
                    log.debug(f"Selektor '{selector}' zlyhал: {e}")
                    continue
        except Exception as e:
            log.debug(f"Chyba pri extrakcii DOM príspevkov: {e}")


async def _dismiss_dialogs(page: Page):
    await asyncio.sleep(1.0)
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass

    for selector in [
        '[aria-label="Allow all cookies"]',
        'button:has-text("Allow")',
        'button:has-text("Accept All")',
        '[aria-label="Close"]',
        'button:has-text("Not Now")',
    ]:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=600):
                await btn.click(timeout=900, force=True, no_wait_after=True)
                await asyncio.sleep(0.2)
        except Exception:
            pass

    try:
        await page.evaluate(
            """() => {
                document.querySelectorAll('div[role="dialog"]').forEach(el => {
                    el.style.pointerEvents = 'none';
                });
            }"""
        )
    except Exception:
        pass


def _fallback_media(post: dict) -> list[dict]:
    media_type = "video" if post.get("type") == "video" else "image"
    return [{"url": post.get("thumb", ""), "type": media_type, "fname": "media.jpg"}]


def _extract_slide_key(url: str) -> str | None:
    """Vytiahne kľúč karuselu z URL (napr. img-index=1)."""
    if not url:
        return None

    patterns = [
        r"img[-_]?index(?:=|/|:)?(\d+)",
        r"image[-_]?index(?:=|/|:)?(\d+)",
        r"slide(?:=|/|:)?(\d+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, url, re.IGNORECASE)
        if m:
            return m.group(1)

    return None


def _shortcode_to_media_id(shortcode: str) -> str | None:
    """Prevod IG shortcode (base64url) na numerický media_id string."""
    if not shortcode:
        return None

    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    value = 0
    try:
        for char in shortcode:
            idx = alphabet.find(char)
            if idx < 0:
                return None
            value = (value << 6) + idx
        return str(value)
    except Exception:
        return None


async def _page_has_instagram_error(page: Page) -> bool:
    checks = [
        'text="Something went wrong"',
        'text="Sorry, something went wrong"',
        'text="Try again"',
        'input[name="username"]',
    ]
    for selector in checks:
        try:
            if await page.locator(selector).count() > 0:
                return True
        except Exception:
            pass
    return False


async def _maybe_wait_for_manual_login(page: Page, username: str, headless: bool):
    try:
        login_form = await page.locator('input[name="username"]').count() > 0
        has_error_page = await _page_has_instagram_error(page)
        if not login_form and not has_error_page:
            return

        if headless:
            raise RuntimeError(
                "Instagram vyžaduje prihlásenie/challenge. Zapnite Debug okno (headless=false) a prihláste sa manuálne."
            )

        log.info("Debug režim: môžete vyplniť login/challenge v okne (max 300s)")
        for i in range(150):
            login_visible = await page.locator('input[name="username"]').count() > 0
            if not login_visible:
                try:
                    tiles = await page.locator('article a[href*="/p/"]').count()
                    if tiles > 0:
                        log.info(f"Debug režim: profil načítaný, nájdených {tiles} príspevkov")
                        break
                except Exception:
                    pass

            if i % 15 == 14:
                try:
                    await page.goto(
                        f"https://www.instagram.com/{username}/",
                        wait_until="domcontentloaded",
                        timeout=12_000,
                    )
                    await _dismiss_dialogs(page)
                except Exception:
                    pass

            await page.wait_for_timeout(2000)

        try:
            await page.goto(f"https://www.instagram.com/{username}/", wait_until="domcontentloaded", timeout=15_000)
            await _dismiss_dialogs(page)
        except Exception:
            pass
    except Exception:
        raise
