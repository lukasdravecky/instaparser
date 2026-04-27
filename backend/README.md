# InstaParser v2 — Playwright Engine

## Ako to funguje

```
Playwright spustí Chromium → otvorí profil → scrolluje →
zachytí CDN requesty (fbcdn.net / cdninstagram.com) →
pre každý príspevok otvorí stránku + klikne carousel šípky →
vráti full-size URL-ky frontendu
```

Toto je ekvivalent manuálneho: pravý klik → Inspect → Network → filtrovanie .jpg/.mp4.

---

## Setup

```bash
cd backend

# 1. Virtual environment
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 2. Závislosti
pip install -r requirements.txt

# 3. Stiahnutie Chromium (jednorazové, ~150MB)
playwright install chromium

# 4. Spustenie
uvicorn main:app --reload --port 8000 --env-file ../.env
```

Frontend: http://localhost:8000/  
Swagger docs: http://localhost:8000/docs

---

## Štruktúra

```
instaparser/
├── backend/
│   ├── main.py                ← FastAPI server
│   ├── playwright_parser.py   ← Playwright logika
│   ├── requirements.txt
│   └── README.md
└── frontend/
    └── instaparser.html
```

---

## Prihlásenie do Instagramu

Playwright používa persistentný browser profil v `backend/browser_profile/`.

- Pri prvom spustení nastav request `headless=false`.
- V otvorenom okne sa prihláste na Instagram.
- Session/cookies sa uložia a ďalšie spustenia už môžu ísť `headless=true`.

---

## Tipy

- `scroll_rounds`: viac = viac príspevkov, ale pomalšie
- `max_posts`: znížte na testovanie (`1-5`)
- Ak Instagram vráti cooldown, počkajte a skúste neskôr alebo inú sieť
