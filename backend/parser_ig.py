"""
InstagramParser — asyncio wrapper okolo instaloader
Dokumentácia instaloader: https://instaloader.github.io/
"""
import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import instaloader

log = logging.getLogger(__name__)

PostType = Literal["image", "video", "carousel"]


@dataclass
class ParserConfig:
    # Rate limiting — Instagram ťa banuje za rýchle requesty
    request_delay: float = field(default_factory=lambda: float(os.getenv("REQUEST_DELAY", "5.0")))  # sekundy medzi requestmi
    max_retries: int = 3
    compress_json: bool = True

    # Autentifikácia (odporúčané — zvyšuje limity)
    ig_username: str = field(default_factory=lambda: os.getenv("IG_USERNAME", ""))
    ig_password: str = field(default_factory=lambda: os.getenv("IG_PASSWORD", ""))
    session_dir: str = field(default_factory=lambda: os.getenv("SESSION_DIR", "./sessions"))


class InstagramParser:
    def __init__(self, config: ParserConfig | None = None):
        self.config = config or ParserConfig()
        self._loader: instaloader.Instaloader | None = None

    def _get_loader(self, session_file: str | None = None) -> instaloader.Instaloader:
        """Vráti (alebo vytvorí) Instaloader inštanciu so session."""
        L = instaloader.Instaloader(
            download_pictures=False,
            download_videos=False,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=self.config.compress_json,
            quiet=True,
        )
        L.context.max_connection_attempts = 1

        # 1. Pokus o načítanie existujúcej session zo súboru
        sf = session_file or self._default_session_path()
        if sf and Path(sf).exists():
            try:
                L.load_session_from_file(self.config.ig_username, sf)
                log.info(f"Session načítaná z: {sf}")
                return L
            except Exception as e:
                log.warning(f"Session load failed: {e}")

        # 2. Prihlásenie pomocou username/password z env premenných
        if self.config.ig_username and self.config.ig_password:
            try:
                L.login(self.config.ig_username, self.config.ig_password)
                Path(self.config.session_dir).mkdir(parents=True, exist_ok=True)
                L.save_session_to_file(self._default_session_path())
                log.info("Prihlásenie úspešné, session uložená")
            except instaloader.exceptions.TwoFactorAuthRequiredException:
                code = input("Zadajte 2FA kód: ").strip()
                L.two_factor_login(code)
            except instaloader.exceptions.BadCredentialsException:
                raise PermissionError("Nesprávne Instagram prihlasovacie údaje")
            except Exception as e:
                raise PermissionError(f"Instagram login zlyhal: {e}")
        else:
            log.info("Anonymný prístup (bez prihlásenia) — obmedzené limity")

        return L

    def _default_session_path(self) -> str | None:
        if not self.config.ig_username:
            return None
        return str(Path(self.config.session_dir) / f"session-{self.config.ig_username}")

    async def fetch_posts(
        self,
        username: str,
        max_posts: int = 50,
        session_file: str | None = None,
    ) -> list[dict]:
        """
        Asynchrónne fetchne príspevky daného účtu.
        Vracia list dictov kompatibilných s frontend formátom.
        """
        # Instaloader je synchrónny — spúšťame v thread executor
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            self._fetch_posts_sync,
            username,
            max_posts,
            session_file,
        )

    def _fetch_posts_sync(
        self,
        username: str,
        max_posts: int,
        session_file: str | None,
    ) -> list[dict]:
        L = self._get_loader(session_file)
        results = []

        try:
            profile = instaloader.Profile.from_username(L.context, username)
        except instaloader.exceptions.ProfileNotExistsException:
            raise ValueError(f"Profil '@{username}' neexistuje")
        except instaloader.exceptions.LoginRequiredException:
            raise PermissionError("Tento profil je súkromný. Prihlásenie je povinné.")

        log.info(f"Načítavam príspevky pre @{username} (max={max_posts})")

        try:
            for i, post in enumerate(profile.get_posts()):
                if i >= max_posts:
                    break

                try:
                    post_data = self._serialize_post(post, username, i)
                    results.append(post_data)
                    log.debug(f"  [{i+1}/{max_posts}] {post.shortcode} ({post_data['type']})")
                except Exception as e:
                    log.warning(f"Preskočený príspevok {i}: {e}")
                    continue

                # Rate limiting — nekradnite na Instagrame
                time.sleep(self.config.request_delay)
        except instaloader.exceptions.ConnectionException as e:
            msg = str(e)
            if "Please wait a few minutes before you try again" in msg:
                raise RuntimeError(
                    "Instagram dočasne blokuje požiadavky z tejto siete/IP (cooldown). "
                    "Počkajte 30-60 minút bez ďalších pokusov alebo skúste inú sieť (napr. mobilné dáta)."
                ) from e
            raise

        log.info(f"Hotovo: {len(results)} príspevkov")
        return results

    def _serialize_post(self, post: instaloader.Post, username: str, idx: int) -> dict:
        """Konvertuje instaloader.Post na frontend-kompatibilný dict."""

        # Určenie typu príspevku
        if post.typename == "GraphSidecar":
            post_type: PostType = "carousel"
        elif post.is_video:
            post_type = "video"
        else:
            post_type = "image"

        # Thumbnail — Instagram thumbnail URL
        thumb = post.url  # display_url je lepšia ak je dostupná

        # Médiá
        media_items = []
        if post_type == "carousel":
            for j, node in enumerate(post.get_sidecar_nodes()):
                media_items.append({
                    "url": node.video_url if node.is_video else node.display_url,
                    "type": "video" if node.is_video else "image",
                    "fname": f"{username}_{idx+1}_{j+1}.{'mp4' if node.is_video else 'jpg'}",
                })
        else:
            url = post.video_url if post.is_video else post.url
            ext = "mp4" if post.is_video else "jpg"
            media_items.append({
                "url": url,
                "type": "video" if post.is_video else "image",
                "fname": f"{username}_{idx+1}.{ext}",
            })

        return {
            "id": post.shortcode,
            "type": post_type,
            "thumb": thumb,
            "media": media_items,
            "ts": post.date_local.strftime("%d.%m.%Y"),
            "likes": post.likes,
            "caption": (post.caption or "")[:200],
            "url": f"https://www.instagram.com/p/{post.shortcode}/",
        }
