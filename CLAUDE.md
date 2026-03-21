# PHOTO-APP — Claude Code Project Instructions

## What This Is
Local web app: digital photo frame + interactive multiplayer "Shadow Game".
Party game where players on phones guess who is behind a silhouette. Crowdpurr-style.
**Users include people with cognitive disabilities — accessibility is ALWAYS non-negotiable.**

## How To Run
```bash
cd ~/Desktop/PHOTO-APP
python3 run.py
# Server starts on https://photoframe.local:8080
```

## Networking
- **Hostname**: `photoframe.local` (Bonjour/mDNS — set via `scutil --set LocalHostName photoframe`)
- **HTTPS**: Locally-trusted cert via `mkcert` in `certs/` directory
- **Single entry point**: `https://photoframe.local:8080/admin` — works on any WiFi, no IP needed
- All Apple devices resolve `.local` automatically. Android supports mDNS on modern versions.
- QR codes and join URLs always use `photoframe.local`, never raw IPs

## Three Views
- `/admin` — Admin panel (laptop): create experiences, import media, control game
- `/display` — Fullscreen display (TV/projector): slideshow + game + leaderboard
- `/play/<CODE>` — Player phone: join via QR, answer questions, see score

## Tech Stack
- Python 3.14 / FastAPI / python-socketio / SQLAlchemy async + aiosqlite
- SQLite DB at `~/.photoframe/photoframe.db`
- Frontend: Vanilla HTML/CSS/JS (NO framework, NO build step)
- Image: Pillow + pillow-heif (HEIC) + rembg (silhouettes) + ffmpeg (video)
- Apple Photos: PyObjC native bridge (`photos_bridge.py`)
- i18n: JSON locale files in `locale/` (en.json, fr.json)
- Gemini 2.0 Flash: people analysis, age estimation, artistic silhouettes, person removal

## Key Files
| File | Purpose |
|------|---------|
| `run.py` | Entry point, auto-installs deps, HTTPS setup |
| `server.py` | FastAPI + Socket.IO, all routes + game engine |
| `models.py` | SQLAlchemy models |
| `media_pipeline.py` | Import, convert, thumbnail, silhouette, Gemini AI |
| `photos_bridge.py` | Native Apple Photos via PyObjC |
| `static/css/theme.css` | Design system (Amplifier Desktop V3) |
| `templates/*.html` | Admin, display, player, base |
| `locale/*.json` | EN + FR translations |
| `certs/` | mkcert HTTPS certificates (photoframe.local) |
| `SPEC-BLUEPRINT-V1.0.md` | Full specification (67 requirements) |

## Apple Photos
- Target album: **"Flo"**
- Requires Photos access for Terminal: System Settings > Privacy > Photos
- PyObjC bridge in `photos_bridge.py` — uses PHPhotoLibrary natively
- Falls back to AppleScript if PyObjC unavailable

## Design System
Adapted from **Amplifier Desktop V3** (`/Users/antoineaymer/Desktop/AI-FOR-QET/amplifier-desktop-v3/`).
Dark theme. See `static/css/theme.css` for values.
- Fonts: Ubuntu (headings), Verdana (body)
- Colors: #0f172a body, #1e293b surface, #0070AD accent, #12ABDB light accent
- 4px spacing grid, consistent border-radius
- **Player page and Admin page MUST use the same design system** — same tokens, same components, same dark theme. No separate styling.

## Cognitive Accessibility Rules (NEVER VIOLATE)
1. Max 4 answer choices, min 60px tall buttons, high contrast
2. Color is ALWAYS paired with icon/text (never color alone)
3. One clear instruction per screen in plain language
4. Timer = progress bar (green/yellow/red) + seconds number, NOT a ticking clock
5. No elimination, no negative scoring
6. Relaxed mode toggle: removes all time pressure
7. Destructive actions require plain-language confirmation
8. Disconnection message: reassuring, no jargon

## Game Mechanics
- Up to 30 players, single session, speed scoring (100 to 10 pts)
- FRAME slides (passive) + GAME slides (silhouette MCQ) interleaved
- Silhouette: rembg segments person, fills black, keeps background
- Quiz flow: silhouette shown first → timer/answers → reveal swaps to original photo → admin clicks Next
- WebSocket sync via Socket.IO rooms

## Player Join Flow
- QR scan → `/play/CODE` → nickname screen (code screen skipped) → join → waiting
- No code entry needed when joining via QR — maximum simplicity

## Specification
Full spec in `SPEC-BLUEPRINT-V1.0.md` — follows Amplifier Specification Engine V1.0 protocol
from `/Users/antoineaymer/Desktop/AI-FOR-QET/amplifier-desktop-v3/AMPLIFIER-SPECIFICATION-ENGINE-V1.0.md`

## Known Fixes
- `socketio.ASGIApp(sio, other_asgi_app=app)` — NOT `other_app` (python-socketio 5.16 API)
- Use `127.0.0.1` not `localhost` for local dev (macOS DNS delay)
- Video serving needs HTTP Range support (206 Partial Content) for Safari
