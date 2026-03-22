# Photo Party

A local web app that turns any gathering into an interactive experience: project a photo slideshow on the big screen while guests play a multiplayer quiz game from their phones.

Built for **family parties, birthdays, and reunions** — designed with **cognitive accessibility** as a first-class concern.

```
+------------------+     +------------------+     +------------------+
|  TV / Projector  |     |  Phones (x30)    |     |  Laptop          |
|  /display        |     |  /play/:code     |     |  /admin          |
|  Slideshow +     |     |  Join, answer,   |     |  Create, import, |
|  quiz display    |     |  see scores      |     |  control live     |
+--------+---------+     +--------+---------+     +--------+---------+
         |                         |                        |
         +-------------------------+------------------------+
                                   |
                           +-------+-------+
                           |   FastAPI +   |
                           | Socket.IO Hub |
                           |   SQLite DB   |
                           +---------------+
```

## What It Does

**Slideshow mode** — Import photos and videos from a folder or Apple Photos. They cycle on the big screen with configurable transitions (fade, slide, Ken Burns).

**Quiz mode** — Three question types interleaved with the slideshow:
- **Shadow**: A person is silhouetted — guess who it is
- **Missing**: A person is removed from the photo — guess who's missing
- **Zoom**: A cropped detail is shown — guess what it is

Players join by scanning a QR code, pick a nickname, and answer from their phone. Speed scoring rewards faster answers (configurable). A live leaderboard tracks the action.

## Quick Start

```bash
# Clone and run
git clone https://github.com/AntoineAymer/PHOTO-APP.git
cd PHOTO-APP
python3 run.py
```

`run.py` auto-installs dependencies and starts the server on port **8080**.

Open **http://localhost:8080/admin** to get started.

### Networking (local WiFi)

For phones to connect, set your machine's local hostname:

```bash
sudo scutil --set LocalHostName photoframe
```

All devices on the same WiFi can then reach **http://photoframe.local:8080**. Apple devices resolve `.local` automatically; modern Android supports it too.

## Three Views

| View | URL | Purpose |
|------|-----|---------|
| Admin | `/admin` | Import media, create activities, control the live game |
| Display | `/display` | Fullscreen output for TV/projector |
| Player | `/play/CODE` | Phone interface — join via QR, answer questions, see scores |

## Features

- **Up to 30 players** connected via WebSocket (Socket.IO)
- **3 quiz types**: shadow (silhouette), missing (person removal), zoom (cropped detail)
- **Speed scoring**: 100 to 10 points based on answer speed (or flat 10 pts)
- **Relaxed mode**: no timer — players answer at their own pace
- **Intermission**: persistent break with tap-to-reclaim (no retyping names)
- **Crash recovery**: players and scores restored from DB on server restart
- **Apple Photos integration**: native PyObjC bridge imports albums directly
- **HEIC support**: auto-converts to JPEG for browser compatibility
- **Video support**: transcodes to MP4, serves with HTTP Range (Safari compatible)
- **AI features** (optional, requires Gemini API key):
  - Artistic silhouettes, person removal, zoom crop analysis
  - Age-based date estimation for undated photos
- **Bilingual**: English and French (switchable per activity)
- **Dark theme** based on Amplifier Desktop V3 design system

## Cognitive Accessibility

This app is designed for gatherings that include people with cognitive disabilities:

- Max 4 answer choices, large tap targets (60px+)
- Color always paired with shape/icon (never color alone)
- One clear instruction per screen in plain language
- Timer shown as progress bar (green → yellow → red) + seconds, not a ticking clock
- No elimination, no negative scoring
- Relaxed mode removes all time pressure
- Destructive actions require plain-language confirmation
- Reassuring disconnect messages (no jargon)

## Tech Stack

- **Backend**: Python 3 / FastAPI / python-socketio / SQLAlchemy async + aiosqlite
- **Database**: SQLite (`~/.photoframe/photoframe.db`)
- **Frontend**: Vanilla HTML/CSS/JS — no framework, no build step
- **Image processing**: Pillow + pillow-heif + rembg (silhouettes) + ffmpeg (video)
- **Apple Photos**: PyObjC native bridge
- **AI** (optional): Google Gemini 2.0 Flash

## Project Structure

```
run.py              # Entry point — installs deps, starts server
server.py           # FastAPI + Socket.IO, all routes + game engine
models.py           # SQLAlchemy models
media_pipeline.py   # Import, convert, thumbnail, silhouette, AI
photos_bridge.py    # Native Apple Photos via PyObjC
static/css/theme.css # Design system
templates/          # admin.html, display.html, player.html
locale/             # en.json, fr.json
```

## Configuration

| Setting | How |
|---------|-----|
| Gemini API key | Create `.env` with `GEMINI_API_KEY=your_key` |
| Admin PIN | Set in Admin → Settings |
| Family context | Admin → Settings → birth years (for AI date estimation) |

## License

MIT
