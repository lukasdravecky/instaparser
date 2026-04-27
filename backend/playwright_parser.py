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
    max_carousel_slides: int = 4
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
            await page.goto(profile_url, wait_until="domcontentloaded", timeout=20_000)
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

        async def on_resp(response: Response):
            url = response.url
            if CDN_PATTERN.search(url) and ("1080" in url or "720" in url or "640" in url):
                media_type = "video" if ".mp4" in url.lower() else "image"
                if not any(m["url"] == url for m in captured_urls):
                    captured_urls.append({"url": url, "type": media_type})

        page.on("response", on_resp)

        try:
            await page.goto(
                f"https://www.instagram.com/p/{shortcode}/",
                wait_until="domcontentloaded",
                timeout=12_000,
            )
            await asyncio.sleep(self.config.post_open_pause)

            if post_type == "carousel":
                for _ in range(self.config.max_carousel_slides):
                    next_btn = page.locator('[aria-label="Next"], button[aria-label*="next" i]').first
                    if await next_btn.count() == 0:
                        break
                    await next_btn.click(timeout=700, force=True)
                    await asyncio.sleep(self.config.carousel_slide_pause)

        except Exception as e:
            log.warning(f"Príspevok {shortcode} sa nepodarilo otvoriť: {e}")
        finally:
            page.remove_listener("response", on_resp)

        return captured_urls


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
