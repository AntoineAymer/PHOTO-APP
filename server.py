"""PhotoFrame & Shadow Game — Main Server."""

import os
import json
import time
import secrets
import hashlib
import asyncio
import subprocess
import socket
from pathlib import Path
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import socketio
from sqlalchemy import select, func, delete, Integer
from sqlalchemy.orm import selectinload
from dotenv import load_dotenv

from models import (
    get_engine, get_session_maker, init_db,
    Media, Experience, Slide, Room, Player, PlayerAnswer, Settings,
    SlideType, MediaType, GameState, DB_DIR
)
from media_pipeline import (
    scan_folder, import_media_file, generate_thumbnail,
    generate_silhouette, get_exif_date, convert_heic_to_jpeg,
    list_apple_photos_albums, import_apple_photos_album,
    estimate_photo_year_gemini, analyze_people_gemini, remove_person_gemini,
    silhouette_person_gemini, silhouette_person_local,
    analyze_zoom_gemini, generate_zoom_crop,
    estimate_year_from_ages_gemini,
    get_gemini_usage,
    load_prompts, save_prompts, _DEFAULT_PROMPTS,
)
try:
    from photos_bridge import (
        request_photos_access, list_albums as native_list_albums,
        get_album_assets, export_album_to_cache, export_asset_to_cache,
        PHOTOS_CACHE_DIR
    )
    NATIVE_PHOTOS = True
except ImportError:
    NATIVE_PHOTOS = False

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ── Directories ──────────────────────────────────────────────────────────────
WORK_DIR = os.path.expanduser("~/.photoframe")
THUMB_DIR = os.path.join(WORK_DIR, "thumbnails")
SILHOUETTE_DIR = os.path.join(WORK_DIR, "silhouettes")
WEB_MEDIA_DIR = os.path.join(WORK_DIR, "web_media")
APPLE_IMPORT_DIR = os.path.join(WORK_DIR, "apple_imports")
UPLOAD_DIR = os.path.join(WORK_DIR, "uploads")
for d in [WORK_DIR, THUMB_DIR, SILHOUETTE_DIR, WEB_MEDIA_DIR, APPLE_IMPORT_DIR, UPLOAD_DIR]:
    os.makedirs(d, exist_ok=True)

# ── Locale ───────────────────────────────────────────────────────────────────
LOCALE_DIR = os.path.join(os.path.dirname(__file__), "locale")

def load_locale(lang="en"):
    # Sanitize lang to prevent path traversal
    import re as _re
    if not _re.match(r'^[a-z]{2}$', lang):
        lang = "en"
    path = os.path.join(LOCALE_DIR, f"{lang}.json")
    if not os.path.exists(path):
        path = os.path.join(LOCALE_DIR, "en.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

# ── Server hostname ───────────────────────────────────────────────────────────
def get_server_host():
    """Get hostname for QR codes and join URLs.
    Priority: SERVER_HOST env > photoframe.local (if resolves) > LAN IP."""
    import subprocess
    env_host = os.environ.get("SERVER_HOST")
    if env_host:
        return env_host
    # Check if photoframe.local resolves (mDNS)
    try:
        import socket as _sock
        _sock.getaddrinfo("photoframe.local", None, _sock.AF_INET, _sock.SOCK_STREAM)
        return "photoframe.local"
    except Exception:
        pass
    for iface in ("en0", "en1"):
        try:
            ip = subprocess.check_output(
                ["/usr/sbin/ipconfig", "getifaddr", iface], stderr=subprocess.DEVNULL
            ).decode().strip()
            if ip:
                return ip
        except Exception:
            continue
    return "127.0.0.1"

SERVER_PORT = int(os.environ.get("PORT", 8080))
SERVER_HOST = get_server_host()
SERVER_PROTO = "http"  # DO NOT change to https — self-signed certs break mobile phones

# ── Database ─────────────────────────────────────────────────────────────────
engine = get_engine()
SessionLocal = get_session_maker(engine)

async def get_db():
    async with SessionLocal() as session:
        yield session

# ── Socket.IO ────────────────────────────────────────────────────────────────
sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins="*",  # Local network app — allow all origins
)

# ── Active game state (in-memory for speed) ──────────────────────────────────
active_rooms = {}  # room_code -> { state, experience, slides, current_index, question_start_time, timer_task, answers }
admin_sids = set()  # Socket IDs that have joined as admin
_bg_tasks = set()  # prevent GC of fire-and-forget tasks

# ── FastAPI Lifespan ─────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db(engine)
    # Ensure default settings exist
    async with SessionLocal() as session:
        result = await session.execute(select(Settings))
        if not result.scalar_one_or_none():
            session.add(Settings(language="en"))
            await session.commit()
    # Restore non-finished rooms from DB
    async with SessionLocal() as session:
        result = await session.execute(
            select(Room).where(Room.state != GameState.FINISHED)
        )
        db_rooms = result.scalars().all()
        for db_room in db_rooms:
            try:
                await _restore_room(session, db_room)
            except Exception as e:
                print(f"  Could not restore room {db_room.code}: {e}")
        if db_rooms:
            print(f"  Restored {len(active_rooms)} active room(s)")
    print(f"\n  PhotoFrame running at:")
    print(f"    Admin:   {SERVER_PROTO}://{SERVER_HOST}:{SERVER_PORT}/admin")
    print(f"    Display: {SERVER_PROTO}://{SERVER_HOST}:{SERVER_PORT}/display")
    print(f"    Players: {SERVER_PROTO}://{SERVER_HOST}:{SERVER_PORT}/play\n")
    yield
    await engine.dispose()

# ── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(title="PhotoFrame", lifespan=lifespan)
socket_app = socketio.ASGIApp(sio, other_asgi_app=app)

# Static files
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


# ══════════════════════════════════════════════════════════════════════════════
# HTML ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/health")
async def health_check():
    """Health endpoint for monitoring. Returns active rooms and player counts."""
    rooms_info = []
    for code, room in active_rooms.items():
        rooms_info.append({
            "code": code, "state": room["state"],
            "players": len(room["players"]),
            "slide": room["current_slide_index"],
        })
    return {"status": "ok", "rooms": rooms_info}

@app.get("/")
async def index():
    from starlette.responses import RedirectResponse
    return RedirectResponse(url="/admin")

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    return templates.TemplateResponse("admin.html", {
        "request": request,
        "server_ip": SERVER_HOST,
        "server_port": SERVER_PORT,
    })

@app.get("/display", response_class=HTMLResponse)
async def display_page(request: Request):
    return templates.TemplateResponse("display.html", {
        "request": request,
        "server_ip": SERVER_HOST,
        "server_port": SERVER_PORT,
    })

@app.get("/play", response_class=HTMLResponse)
@app.get("/play/{room_code}", response_class=HTMLResponse)
async def player_page(request: Request, room_code: str = ""):
    return templates.TemplateResponse("player.html", {
        "request": request,
        "room_code": room_code,
        "server_ip": SERVER_HOST,
        "server_port": SERVER_PORT,
    })


# ── Sync I/O helpers (for asyncio.to_thread) ─────────────────────────────────
def _write_bytes(path: str, data: bytes):
    with open(path, "wb") as f:
        f.write(data)

def _update_env_file(env_path: str, key: str):
    lines = []
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            lines = [l for l in f.readlines() if not l.startswith("GEMINI_API_KEY=")]
    lines.append(f"GEMINI_API_KEY={key}\n")
    with open(env_path, "w") as f:
        f.writelines(lines)

# ══════════════════════════════════════════════════════════════════════════════
# API ROUTES — MEDIA
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/locale/{lang}")
async def get_locale(lang: str):
    return load_locale(lang)

@app.get("/api/media")
async def list_media(request: Request, db=Depends(get_db)):
    # Pagination: ?page=1&per_page=50 (default: all for backward compat)
    params = request.query_params
    page = int(params.get("page", 0))
    per_page = int(params.get("per_page", 50))

    if page > 0:
        # Paginated mode
        total_result = await db.execute(select(func.count(Media.id)))
        total = total_result.scalar()
        offset = (page - 1) * per_page
        result = await db.execute(
            select(Media).order_by(Media.imported_at.desc()).offset(offset).limit(per_page)
        )
        media_list = result.scalars().all()
        return {
            "items": [_media_to_dict(m) for m in media_list],
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page,
        }
    else:
        # Legacy: return flat list (used by media picker) — cap at 5000
        result = await db.execute(select(Media).order_by(Media.imported_at.desc()).limit(5000))
        media_list = result.scalars().all()
        return [_media_to_dict(m) for m in media_list]

def _media_to_dict(m):
    return {
        "id": m.id, "filename": m.filename, "media_type": m.media_type.value,
        "format": m.format, "width": m.width, "height": m.height,
        "duration": m.duration, "exif_date": m.exif_date.isoformat() if m.exif_date else None,
        "source_folder": m.source_folder, "category": m.category,
        "thumbnail_url": f"/api/media/{m.id}/thumbnail",
        "url": f"/api/media/{m.id}/file",
    }

@app.get("/api/media/{media_id}/file")
async def serve_media_file(media_id: int, request: Request, db=Depends(get_db)):
    media = await db.get(Media, media_id)
    if not media:
        raise HTTPException(404, "Media not found")
    # Serve web-compatible version if available, otherwise original
    path = os.path.realpath(media.web_path or media.file_path)
    if not os.path.exists(path):
        raise HTTPException(404, "File not found on disk")
    # Prevent serving files outside expected directories
    _allowed_roots = [WORK_DIR, APPLE_IMPORT_DIR]
    if NATIVE_PHOTOS:
        _allowed_roots.append(os.path.realpath(PHOTOS_CACHE_DIR))
    if not any(path.startswith(os.path.realpath(r)) for r in _allowed_roots) and \
       not path.startswith(os.path.expanduser("~/")):
        raise HTTPException(403, "Access denied")
    # Set correct media type for videos
    ext = Path(path).suffix.lower()
    media_type_map = {
        ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
        ".m4v": "video/x-m4v", ".avi": "video/x-msvideo", ".mkv": "video/x-matroska",
    }
    content_type = media_type_map.get(ext)
    # Video files need Range request support for seeking and Safari playback
    if content_type and content_type.startswith("video/"):
        return _serve_range(request, path, content_type)
    if content_type:
        return FileResponse(path, media_type=content_type)
    return FileResponse(path)

def _serve_range(request: Request, path: str, content_type: str):
    """Serve a file with HTTP Range support (required for video playback)."""
    from starlette.responses import Response, StreamingResponse
    file_size = os.path.getsize(path)
    range_header = request.headers.get("range")

    if not range_header:
        # No range requested — serve full file with Accept-Ranges header
        return FileResponse(path, media_type=content_type,
                            headers={"Accept-Ranges": "bytes",
                                     "Content-Length": str(file_size)})

    # Parse "bytes=start-end"
    try:
        range_spec = range_header.replace("bytes=", "").strip()
        parts = range_spec.split("-")
        start = int(parts[0]) if parts[0] else 0
        end = int(parts[1]) if parts[1] else file_size - 1
    except (ValueError, IndexError):
        start, end = 0, file_size - 1

    end = min(end, file_size - 1)
    length = end - start + 1

    def file_stream():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        file_stream(),
        status_code=206,
        media_type=content_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
        },
    )

@app.get("/api/media/{media_id}/thumbnail")
async def serve_thumbnail(media_id: int, db=Depends(get_db)):
    media = await db.get(Media, media_id)
    if not media:
        raise HTTPException(404, "Media not found")
    if media.thumbnail_path and os.path.exists(media.thumbnail_path):
        return FileResponse(media.thumbnail_path)
    # For videos without a thumbnail, try to generate one now
    if media.media_type == MediaType.VIDEO:
        path = media.web_path or media.file_path
        if os.path.exists(path):
            thumb = generate_thumbnail(path, THUMB_DIR, "video")
            if thumb:
                media.thumbnail_path = thumb
                await db.commit()
                return FileResponse(thumb)
    raise HTTPException(404, "Thumbnail not found")

_video_task_status = {"running": False, "progress": "", "done": False, "result": None}

@app.post("/api/media/reprocess-videos")
async def api_reprocess_videos():
    """Launch background task to re-generate video thumbnails and transcode."""
    if _video_task_status["running"]:
        return {"status": "already_running", "progress": _video_task_status["progress"]}
    _video_task_status.update(running=True, done=False, progress="Starting...", result=None)
    task = asyncio.create_task(_reprocess_videos_bg())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return {"status": "started"}

@app.get("/api/media/reprocess-videos/status")
async def api_reprocess_videos_status():
    return _video_task_status

async def _reprocess_videos_bg():
    """Background video reprocessing task."""
    from media_pipeline import transcode_video
    try:
        async with SessionLocal() as db:
            result = await db.execute(select(Media).where(Media.media_type == MediaType.VIDEO))
            videos = result.scalars().all()
            total = len(videos)
            fixed_thumbs = 0
            fixed_transcode = 0
            for i, v in enumerate(videos):
                _video_task_status["progress"] = f"Processing {i+1}/{total}: {v.filename}"
                # Emit progress via Socket.IO to admin
                await sio.emit("video_reprocess_progress", {
                    "current": i + 1, "total": total, "filename": v.filename,
                }, room="admin_broadcast")
                path = v.file_path
                if not os.path.exists(path):
                    continue
                # Generate thumbnail if missing
                if not v.thumbnail_path or not os.path.exists(v.thumbnail_path):
                    thumb = await asyncio.to_thread(generate_thumbnail, path, THUMB_DIR, "video")
                    if thumb:
                        v.thumbnail_path = thumb
                        fixed_thumbs += 1
                # Transcode if needed and missing
                ext = Path(path).suffix.lower()
                if ext not in (".mp4", ".webm") and (not v.web_path or not os.path.exists(v.web_path)):
                    web = await asyncio.to_thread(transcode_video, path, WEB_MEDIA_DIR)
                    if web:
                        v.web_path = web
                        fixed_transcode += 1
            await db.commit()
            summary = {"videos": total, "thumbnails_fixed": fixed_thumbs, "transcoded": fixed_transcode}
            _video_task_status.update(running=False, done=True, progress="Complete", result=summary)
            await sio.emit("video_reprocess_done", summary, room="admin_broadcast")
    except Exception as e:
        _video_task_status.update(running=False, done=True, progress=f"Error: {e}", result=None)
        await sio.emit("video_reprocess_done", {"error": str(e)}, room="admin_broadcast")

@app.post("/api/media/scan-folder")
async def api_scan_folder(request: Request, db=Depends(get_db)):
    data = await request.json()
    folder_path = os.path.realpath(data.get("path", ""))
    if not os.path.isdir(folder_path):
        raise HTTPException(400, f"Folder not found: {folder_path}")
    # Block scanning sensitive system directories
    _blocked = ["/etc", "/var", "/usr", "/bin", "/sbin", "/System", "/Library",
                os.path.expanduser("~/.ssh"), os.path.expanduser("~/.gnupg")]
    if any(folder_path == b or folder_path.startswith(b + "/") for b in _blocked):
        raise HTTPException(400, "Cannot scan system directories")

    files = scan_folder(folder_path)
    imported = 0
    for file_path in files:
        # Skip if already imported
        existing = await db.execute(select(Media).where(Media.file_path == file_path))
        if existing.scalar_one_or_none():
            continue
        media = await import_media_file(file_path, folder_path, THUMB_DIR, WEB_MEDIA_DIR)
        if media:
            db.add(media)
            imported += 1
    await db.commit()
    return {"imported": imported, "total_found": len(files)}

@app.post("/api/media/upload")
async def api_upload_media(files: list[UploadFile] = File(...), db=Depends(get_db)):
    """Upload photos/videos directly from the browser."""
    from media_pipeline import ALL_EXTENSIONS
    imported = 0
    skipped = 0
    for f in files:
        ext = os.path.splitext(f.filename or "")[1].lower()
        if ext not in ALL_EXTENSIONS:
            skipped += 1
            continue
        safe_name = f.filename.replace("/", "_").replace("\\", "_")
        dest = os.path.join(UPLOAD_DIR, safe_name)
        # Avoid overwriting: add suffix if file exists
        base, extension = os.path.splitext(dest)
        counter = 1
        while os.path.exists(dest):
            dest = f"{base}_{counter}{extension}"
            counter += 1
        content = await f.read()
        await asyncio.to_thread(_write_bytes, dest, content)
        # Skip if already in DB by file path
        existing = await db.execute(select(Media).where(Media.file_path == dest))
        if existing.scalar_one_or_none():
            skipped += 1
            continue
        media = await import_media_file(dest, UPLOAD_DIR, THUMB_DIR, WEB_MEDIA_DIR)
        if media:
            db.add(media)
            imported += 1
        else:
            skipped += 1
    await db.commit()
    return {"imported": imported, "skipped": skipped, "total": len(files)}

@app.get("/api/media/apple-albums")
async def api_apple_albums():
    try:
        if NATIVE_PHOTOS:
            albums = native_list_albums()
        else:
            albums = list_apple_photos_albums()
        return {"albums": albums, "native": NATIVE_PHOTOS}
    except Exception as e:
        err = str(e)
        # Detect permission-related errors and flag them
        is_permission = any(kw in err.lower() for kw in ["denied", "permission", "authorization", "access", "not authorized"])
        raise HTTPException(400, {"message": err, "permission_error": is_permission})

@app.post("/api/media/apple-import")
async def api_apple_import(request: Request, db=Depends(get_db)):
    data = await request.json()
    album_name = data.get("album", "")
    try:
        if NATIVE_PHOTOS:
            exported_files = export_album_to_cache(album_name)
            source_dir = PHOTOS_CACHE_DIR
        else:
            exported_files = import_apple_photos_album(album_name, APPLE_IMPORT_DIR)
            source_dir = APPLE_IMPORT_DIR
        imported = 0
        for file_path in exported_files:
            existing = await db.execute(select(Media).where(Media.file_path == file_path))
            if existing.scalar_one_or_none():
                continue
            media = await import_media_file(file_path, source_dir, THUMB_DIR, WEB_MEDIA_DIR)
            if media:
                db.add(media)
                imported += 1
        await db.commit()
        return {"imported": imported, "total_exported": len(exported_files)}
    except Exception as e:
        raise HTTPException(400, str(e))

@app.get("/api/media/apple-albums/{album_name}/assets")
async def api_album_assets(album_name: str):
    """List assets in an album without exporting (native only)."""
    if not NATIVE_PHOTOS:
        raise HTTPException(400, "Native Photos not available")
    try:
        assets = get_album_assets(album_name)
        return {"album": album_name, "assets": assets, "count": len(assets)}
    except Exception as e:
        raise HTTPException(400, str(e))

@app.put("/api/media/{media_id}/category")
async def update_media_category(media_id: int, request: Request, db=Depends(get_db)):
    data = await request.json()
    media = await db.get(Media, media_id)
    if not media:
        raise HTTPException(404)
    media.category = data.get("category", "")
    await db.commit()
    return {"ok": True}

@app.post("/api/media/{media_id}/analyze-people")
async def analyze_people(media_id: int, db=Depends(get_db)):
    """Identify people in a photo. Uses Gemini if available, local rembg fallback otherwise."""
    media = await db.get(Media, media_id)
    if not media:
        raise HTTPException(404)
    source_path = media.web_path or media.file_path
    if not os.path.exists(source_path):
        raise HTTPException(404, "Media file not found")

    # Try Gemini first (provides descriptions + positions)
    if os.environ.get("GEMINI_API_KEY"):
        result = await asyncio.to_thread(analyze_people_gemini, source_path)
        if result:
            return result

    # Local fallback: detect people count via rembg connected components
    result = await asyncio.to_thread(_analyze_people_local, source_path)
    if result:
        return result
    raise HTTPException(500, "Could not detect people in photo")


def _analyze_people_local(source_path: str) -> dict | None:
    """Fast local people detection via OpenCV face detection. Falls back to rembg.
    OpenCV: ~100ms. rembg fallback: ~2s."""
    import time
    t0 = time.time()

    # Try OpenCV face detection first (much faster)
    result = _detect_faces_opencv(source_path)
    if result:
        print(f"[LOCAL ANALYZE] OpenCV face detection: {(time.time()-t0)*1000:.0f}ms, {len(result['people'])} people")
        return result

    # Fall back to rembg segmentation (slower but catches non-frontal faces)
    print("[LOCAL ANALYZE] OpenCV found no faces, falling back to rembg...")
    result = _detect_people_rembg(source_path)
    if result:
        print(f"[LOCAL ANALYZE] rembg fallback: {(time.time()-t0)*1000:.0f}ms, {len(result['people'])} people")
    return result


def _detect_faces_opencv(source_path: str) -> dict | None:
    """Detect faces using OpenCV Haar cascades. ~100ms, no GPU needed."""
    try:
        import cv2
        import numpy as np
        from PIL import Image, ImageOps

        img = ImageOps.exif_transpose(Image.open(source_path)).convert("RGB")
        w, h = img.size

        # Resize for speed
        max_dim = 480
        ratio = max_dim / max(w, h)
        small = img.resize((int(w * ratio), int(h * ratio)))
        arr = np.array(small)
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        sh, sw = gray.shape

        # Frontal face detection
        face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(20, 20))

        # Profile face detection (both directions)
        profile_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml")
        profiles_l = profile_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(20, 20))
        profiles_r = profile_cascade.detectMultiScale(cv2.flip(gray, 1), scaleFactor=1.1, minNeighbors=5, minSize=(20, 20))

        all_boxes = []
        for (x, y, bw, bh) in (list(faces) + list(profiles_l)):
            all_boxes.append((x, y, bw, bh))
        for (x, y, bw, bh) in profiles_r:
            all_boxes.append((sw - x - bw, y, bw, bh))  # flip back

        if not all_boxes:
            return None

        # Non-maximum suppression: merge overlapping detections
        merged = []
        used = set()
        for i, (x1, y1, w1, h1) in enumerate(all_boxes):
            if i in used:
                continue
            cx1, cy1 = x1 + w1/2, y1 + h1/2
            group = [(x1, y1, w1, h1)]
            for j, (x2, y2, w2, h2) in enumerate(all_boxes):
                if j <= i or j in used:
                    continue
                cx2, cy2 = x2 + w2/2, y2 + h2/2
                dist = ((cx1 - cx2)**2 + (cy1 - cy2)**2)**0.5
                if dist < max(w1, w2) * 0.6:
                    group.append((x2, y2, w2, h2))
                    used.add(j)
            used.add(i)
            # Use largest box from group
            best = max(group, key=lambda b: b[2] * b[3])
            merged.append(best)

        # Sort left to right
        merged.sort(key=lambda b: b[0] + b[2]/2)

        people = []
        positions = []
        for i, (x, y, bw, bh) in enumerate(merged):
            cx = (x + bw/2) / sw * 100
            cy = (y + bh/2) / sh * 100
            people.append(f"Person {i+1}")
            positions.append({"center_x": round(cx, 1), "center_y": round(cy, 1)})

        return {
            "people": people,
            "positions": positions,
            "quiz": {
                "question": "Who is behind the silhouette?",
                "correct": people[0],
                "wrong": [f"Person {i+2}" for i in range(min(2, len(people) - 1))] or ["Someone else", "Nobody"],
            },
            "source": "local_opencv",
        }
    except ImportError:
        return None  # OpenCV not installed
    except Exception as e:
        print(f"[OPENCV] Failed: {e}")
        return None


def _detect_people_rembg(source_path: str) -> dict | None:
    """Detect people using rembg segmentation. Slower (~2s) but catches all poses."""
    try:
        from rembg import remove
        from PIL import Image, ImageOps
        import numpy as np
        from scipy import ndimage

        img = ImageOps.exif_transpose(Image.open(source_path)).convert("RGBA")
        # Resize for speed — we only need positions, not full-res masks
        max_dim = 512
        if max(img.size) > max_dim:
            ratio = max_dim / max(img.size)
            img = img.resize((int(img.width * ratio), int(img.height * ratio)))
        w, h = img.size

        fg = remove(img)
        full_mask = np.array(fg)[:, :, 3] > 128

        if full_mask.sum() / full_mask.size < 0.01:
            return None

        labeled, num_features = ndimage.label(full_mask)
        component_sizes = ndimage.sum(full_mask, labeled, range(1, num_features + 1))
        min_size = max(component_sizes) * 0.05

        people = []
        positions = []
        for i in range(1, num_features + 1):
            if component_sizes[i - 1] < min_size:
                continue
            ys, xs = np.where(labeled == i)
            cx = float(xs.mean() / w * 100)
            cy = float(ys.mean() / h * 100)
            positions.append({"center_x": round(cx, 1), "center_y": round(cy, 1)})

        positions.sort(key=lambda p: p["center_x"])
        people = [f"Person {i+1}" for i in range(len(positions))]

        if not people:
            return None

        return {
            "people": people,
            "positions": positions,
            "quiz": {
                "question": "Who is behind the silhouette?",
                "correct": people[0],
                "wrong": [f"Person {i+2}" for i in range(min(2, len(people) - 1))] or ["Someone else", "Nobody"],
            },
            "source": "local_rembg",
        }
    except Exception as e:
        print(f"[REMBG ANALYZE] Failed: {e}")
        return None


@app.post("/api/media/{media_id}/fix-date")
async def fix_media_date(media_id: int, db=Depends(get_db)):
    """Re-read EXIF date from file. Falls back to Gemini if no EXIF and key is available."""
    media = await db.get(Media, media_id)
    if not media:
        raise HTTPException(404)
    source_path = media.web_path or media.file_path
    if not os.path.exists(source_path):
        raise HTTPException(404, "Media file not found")

    # First, try to re-read actual EXIF from the file (most reliable)
    real_exif = await asyncio.to_thread(get_exif_date, media.file_path)
    if real_exif:
        old_year = media.exif_date.strftime("%Y") if media.exif_date else None
        changed = media.exif_date != real_exif
        media.exif_date = real_exif
        await db.commit()
        return {"new_date": real_exif.isoformat(), "old_year": old_year, "changed": changed, "source": "exif"}

    # No EXIF — fall back to Gemini year estimation (if key available)
    if not os.environ.get("GEMINI_API_KEY"):
        return {"new_date": None, "old_year": media.exif_date.strftime("%Y") if media.exif_date else None, "changed": False, "source": "none"}
    old_year = media.exif_date.strftime("%Y") if media.exif_date else None
    year = await asyncio.to_thread(estimate_photo_year_gemini, source_path)
    if year:
        new_date = datetime(year, 6, 15)
        changed = old_year != str(year)
        media.exif_date = new_date
        await db.commit()
        return {"new_date": new_date.isoformat(), "old_year": old_year, "changed": changed, "source": "gemini"}
    return {"new_date": None, "old_year": old_year, "changed": False}

@app.post("/api/media/repair-dates")
async def repair_all_dates(db=Depends(get_db)):
    """Re-read EXIF dates from all media files and fix corrupted DB entries."""
    result = await db.execute(select(Media))
    all_media = result.scalars().all()
    fixed = 0
    for media in all_media:
        if not os.path.exists(media.file_path):
            continue
        real_exif = get_exif_date(media.file_path)
        if real_exif and media.exif_date != real_exif:
            media.exif_date = real_exif
            fixed += 1
    await db.commit()
    return {"total": len(all_media), "fixed": fixed}

@app.post("/api/media/{media_id}/analyze-zoom")
async def analyze_zoom(media_id: int, db=Depends(get_db)):
    """Use Gemini to find an interesting detail for a Zoom In quiz."""
    if not os.environ.get("GEMINI_API_KEY"):
        raise HTTPException(400, "AI features need a Gemini key. Add it in Settings.")
    media = await db.get(Media, media_id)
    if not media:
        raise HTTPException(404)
    source_path = media.web_path or media.file_path
    if not os.path.exists(source_path):
        raise HTTPException(404, "Media file not found")
    result = await asyncio.to_thread(analyze_zoom_gemini, source_path)
    if not result:
        raise HTTPException(500, "Could not find a detail — try a different image")
    return result


@app.post("/api/slides/{slide_id}/make-quiz")
async def make_quiz_from_slide(slide_id: int, request: Request, db=Depends(get_db)):
    """Convert a frame slide into a game slide with quiz type (shadow/missing/zoom).
    Expects JSON: {quiz_type, correct, wrong: [w1, w2], person_to_remove, person_index, use_gemini, bbox}"""
    # Gemini key only required for missing/zoom or when use_gemini explicitly requested
    data = await request.json()
    use_gemini = data.get("use_gemini", False)
    quiz_type = data.get("quiz_type", "shadow")
    if quiz_type in ("missing", "zoom") and not os.environ.get("GEMINI_API_KEY"):
        raise HTTPException(400, "Gemini API key required for missing/zoom quiz")
    if use_gemini and not os.environ.get("GEMINI_API_KEY"):
        raise HTTPException(400, "AI features need a Gemini key. Add it in Settings.")

    correct = data.get("correct", "").strip()
    wrong = [w.strip() for w in data.get("wrong", []) if isinstance(w, str) and w.strip()]
    if not correct:
        raise HTTPException(400, "Need a correct answer")

    slide = await db.get(Slide, slide_id)
    if not slide:
        raise HTTPException(404)
    media = await db.get(Media, slide.media_id)
    if not media:
        raise HTTPException(404)

    source_path = media.web_path or media.file_path
    if not os.path.exists(source_path):
        raise HTTPException(404, "Media file not found")

    # Build answers list: 1 correct + 0-2 wrong (dynamic based on people count)
    import random
    answers = [{"text": correct, "is_correct": True}]
    for w in wrong[:2]:
        answers.append({"text": w, "is_correct": False})
    random.shuffle(answers)
    letter_keys = ["a", "b", "c", "d"]
    correct_letter = letter_keys[next(i for i, a in enumerate(answers) if a["is_correct"])]

    # Validate quiz_type
    if quiz_type not in ("shadow", "missing", "zoom"):
        raise HTTPException(400, f"Invalid quiz_type: {quiz_type}")

    # Update slide to game type
    slide.slide_type = SlideType.GAME
    slide.quiz_type = quiz_type
    slide.answer_a = answers[0]["text"] if len(answers) > 0 else None
    slide.answer_b = answers[1]["text"] if len(answers) > 1 else None
    slide.answer_c = answers[2]["text"] if len(answers) > 2 else None
    slide.answer_d = None
    slide.correct_answer = correct_letter
    await db.commit()

    # Generate the edited image in background
    _silhouette_tasks[slide_id] = {"status": "generating", "warning": None}

    async def _generate():
        try:
            if quiz_type == "zoom":
                bbox = data.get("bbox") or {"x": 25, "y": 25, "w": 25, "h": 25}
                path = await asyncio.to_thread(
                    generate_zoom_crop, source_path, bbox, SILHOUETTE_DIR, slide_id
                )
            elif quiz_type == "missing":
                person_desc = data.get("person_to_remove", correct)
                path = await asyncio.to_thread(
                    remove_person_gemini, source_path, person_desc, SILHOUETTE_DIR, slide_id
                )
            else:  # shadow
                person_desc = data.get("person_to_remove", "")
                person_idx = data.get("person_index", 0)
                print(f"[SHADOW] person_index={person_idx}, use_gemini={use_gemini}, desc='{person_desc[:80]}'")

                if use_gemini and person_desc:
                    # Gemini mode: send description for AI-based silhouette
                    path = await asyncio.to_thread(
                        silhouette_person_gemini, source_path, person_desc, SILHOUETTE_DIR, slide_id
                    )
                else:
                    # Local mode (default): rembg + connected components + Voronoi
                    positions = data.get("positions", None)
                    path = await asyncio.to_thread(
                        silhouette_person_local, source_path, person_idx, SILHOUETTE_DIR, slide_id, positions
                    )

            if not path:
                # Rollback slide to FRAME — broken quiz should never reach players
                async with SessionLocal() as sdb:
                    s = await sdb.get(Slide, slide_id)
                    if s:
                        s.slide_type = SlideType.FRAME
                        s.quiz_type = None
                        await sdb.commit()
                _silhouette_tasks[slide_id] = {"status": "error", "warning": "Generation failed — no image returned"}
                return
            async with SessionLocal() as sdb:
                s = await sdb.get(Slide, slide_id)
                if s:
                    s.silhouette_path = path
                    await sdb.commit()
            _silhouette_tasks[slide_id] = {"status": "done", "warning": None}
        except ValueError as e:
            if "no_person_detected" in str(e):
                _silhouette_tasks[slide_id] = {"status": "done", "warning": "no_person_detected"}
            else:
                async with SessionLocal() as sdb:
                    s = await sdb.get(Slide, slide_id)
                    if s:
                        s.slide_type = SlideType.FRAME; s.quiz_type = None
                        await sdb.commit()
                _silhouette_tasks[slide_id] = {"status": "error", "warning": str(e)}
        except Exception as e:
            async with SessionLocal() as sdb:
                s = await sdb.get(Slide, slide_id)
                if s:
                    s.slide_type = SlideType.FRAME; s.quiz_type = None
                    await sdb.commit()
            _silhouette_tasks[slide_id] = {"status": "error", "warning": str(e)}

    task = asyncio.create_task(_generate())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return {"ok": True, "slide_id": slide_id, "correct_answer": correct_letter, "generating": True}


@app.post("/api/slides/{slide_id}/remove-quiz")
async def remove_quiz_from_slide(slide_id: int, db=Depends(get_db)):
    """Revert a game slide back to a frame slide."""
    slide = await db.get(Slide, slide_id)
    if not slide:
        raise HTTPException(404)
    slide.slide_type = SlideType.FRAME
    slide.quiz_type = None
    slide.answer_a = None
    slide.answer_b = None
    slide.answer_c = None
    slide.answer_d = None
    slide.correct_answer = None
    # Clean up silhouette file
    if slide.silhouette_path and os.path.exists(slide.silhouette_path):
        os.remove(slide.silhouette_path)
    slide.silhouette_path = None
    await db.commit()
    return {"ok": True}


# ── Duplicate Detection ──

@app.get("/api/media/duplicates")
async def find_duplicates(db=Depends(get_db)):
    """Find perceptual duplicate images using dHash. Returns groups of similar items."""
    result = await db.execute(select(Media).order_by(Media.id))
    all_media = result.scalars().all()

    def compute_duplicates():
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            pass
        from PIL import Image

        def dhash(image, size=12):
            img = image.convert('L').resize((size + 1, size), Image.LANCZOS)
            pixels = list(img.getdata())
            w = size + 1
            bits = []
            for row in range(size):
                for col in range(size):
                    bits.append(1 if pixels[row * w + col + 1] > pixels[row * w + col] else 0)
            return int(''.join(str(b) for b in bits), 2)

        def hamming(a, b):
            return bin(a ^ b).count('1')

        # Hash all images
        hashed = []
        for m in all_media:
            if m.media_type != 'IMAGE':
                continue
            try:
                img = Image.open(m.file_path)
                h = dhash(img)
                hashed.append((m, h))
            except Exception:
                continue

        # Also handle videos: compare by duration + thumbnail hash
        for m in all_media:
            if m.media_type != 'VIDEO' or not m.thumbnail_path or not os.path.exists(m.thumbnail_path):
                continue
            try:
                img = Image.open(m.thumbnail_path)
                h = dhash(img)
                hashed.append((m, h))
            except Exception:
                continue

        # Find groups with hamming distance <= 6
        used = set()
        groups = []
        for i in range(len(hashed)):
            if hashed[i][0].id in used:
                continue
            group = [hashed[i]]
            for j in range(i + 1, len(hashed)):
                if hashed[j][0].id in used:
                    continue
                if hamming(hashed[i][1], hashed[j][1]) <= 6:
                    group.append(hashed[j])
                    used.add(hashed[j][0].id)
            if len(group) > 1:
                groups.append(group)
                used.add(hashed[i][0].id)

        result_groups = []
        for group in groups:
            items = []
            for m, h in group:
                sz = os.path.getsize(m.file_path) if os.path.exists(m.file_path) else 0
                items.append({
                    "id": m.id, "filename": m.filename,
                    "media_type": m.media_type.lower() if m.media_type else "image",
                    "exif_date": m.exif_date, "file_size": sz,
                    "thumbnail_url": f"/api/media/{m.id}/thumbnail",
                })
            # Sort: prefer item with date, then larger file (better quality)
            items.sort(key=lambda x: (x["exif_date"] is not None, x["file_size"]), reverse=True)
            result_groups.append({"keep": items[0], "remove": items[1:]})
        return result_groups

    groups = await asyncio.to_thread(compute_duplicates)
    return {"groups": groups, "total_duplicates": sum(len(g["remove"]) for g in groups)}


@app.post("/api/media/duplicates/remove")
async def remove_duplicates(request: Request, db=Depends(get_db)):
    """Remove duplicate media items by ID list."""
    data = await request.json()
    ids_to_remove = data.get("ids", [])
    if not ids_to_remove:
        return {"removed": 0}

    removed = 0
    for mid in ids_to_remove:
        media = await db.get(Media, mid)
        if not media:
            continue
        # Remove from any slides first
        slides = (await db.execute(select(Slide).where(Slide.media_id == mid))).scalars().all()
        for s in slides:
            await db.delete(s)
        # Remove physical files
        for path in [media.file_path, media.thumbnail_path, media.web_path, media.silhouette_path]:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        await db.delete(media)
        removed += 1

    await db.commit()
    return {"removed": removed}


# ══════════════════════════════════════════════════════════════════════════════
# API ROUTES — EXPERIENCES
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/experiences")
async def list_experiences(db=Depends(get_db)):
    result = await db.execute(
        select(Experience).order_by(Experience.created_at.desc())
    )
    exps = result.scalars().all()
    return [{"id": e.id, "name": e.name, "language": e.language,
             "created_at": e.created_at.isoformat()} for e in exps]

@app.post("/api/experiences")
async def create_experience(request: Request, db=Depends(get_db)):
    data = await request.json()
    exp = Experience(
        name=data.get("name", "New Experience"),
        language=data.get("language", "en"),
        default_image_duration=data.get("default_image_duration", 8),
        max_video_duration=data.get("max_video_duration", 60),
        transition_effect=data.get("transition_effect", "fade"),
        default_question_timer=data.get("default_question_timer", 15),
        relaxed_mode=data.get("relaxed_mode", False),
        show_leaderboard_between=data.get("show_leaderboard_between", True),
        sound_enabled=data.get("sound_enabled", True),
    )
    db.add(exp)
    await db.commit()
    await db.refresh(exp)
    return {"id": exp.id, "name": exp.name}

@app.get("/api/experiences/{exp_id}")
async def get_experience(exp_id: int, db=Depends(get_db)):
    exp = await db.get(Experience, exp_id)
    if not exp:
        raise HTTPException(404)
    result = await db.execute(
        select(Slide).where(Slide.experience_id == exp_id)
        .options(selectinload(Slide.media))
        .order_by(Slide.position)
    )
    slides = result.scalars().all()
    return {
        "id": exp.id, "name": exp.name, "language": exp.language,
        "default_image_duration": exp.default_image_duration,
        "max_video_duration": exp.max_video_duration,
        "transition_effect": exp.transition_effect,
        "default_question_timer": exp.default_question_timer,
        "quiz_intro_duration": exp.quiz_intro_duration if exp.quiz_intro_duration is not None else 3,
        "relaxed_mode": exp.relaxed_mode,
        "show_leaderboard_between": exp.show_leaderboard_between,
        "sound_enabled": exp.sound_enabled,
        "player_display_mode": exp.player_display_mode or "question_and_choices",
        "speed_scoring": exp.speed_scoring if exp.speed_scoring is not None else True,
        "max_points": exp.max_points or 100,
        "min_points": exp.min_points if exp.min_points is not None else 10,
        "wrong_points": exp.wrong_points or 0,
        "slides": [{
            "id": s.id, "position": s.position, "slide_type": s.slide_type.value, "quiz_type": s.quiz_type,
            "media_id": s.media_id,
            "media_url": f"/api/media/{s.media_id}/file",
            "media_thumbnail": f"/api/media/{s.media_id}/thumbnail",
            "media_filename": s.media.filename if s.media else "",
            "media_type": s.media.media_type.value if s.media else "",
            "exif_date": s.media.exif_date.isoformat() if s.media and s.media.exif_date else None,
            "duration": s.media.duration if s.media else None,
            "display_duration": s.display_duration,
            "silhouette_url": f"/api/slides/{s.id}/silhouette" if s.silhouette_path else None,
            "question_timer": s.question_timer,
            "answer_a": s.answer_a, "answer_b": s.answer_b,
            "answer_c": s.answer_c, "answer_d": s.answer_d,
            "correct_answer": s.correct_answer,
        } for s in slides]
    }

@app.put("/api/experiences/{exp_id}")
async def update_experience(exp_id: int, request: Request, db=Depends(get_db)):
    data = await request.json()
    exp = await db.get(Experience, exp_id)
    if not exp:
        raise HTTPException(404)
    for key in ["name", "language", "default_image_duration", "max_video_duration",
                "transition_effect", "default_question_timer", "quiz_intro_duration",
                "relaxed_mode", "show_leaderboard_between", "sound_enabled",
                "leaderboard_duration", "speed_scoring", "max_points", "min_points",
                "wrong_points", "player_display_mode"]:
        if key in data:
            setattr(exp, key, data[key])
    await db.commit()
    # Also update any active room using this experience
    for code, room in active_rooms.items():
        if room.get("experience_id") == exp_id:
            for key in data:
                if key in room.get("experience", {}):
                    room["experience"][key] = data[key]
    return {"ok": True}

@app.delete("/api/experiences/{exp_id}")
async def delete_experience(exp_id: int, db=Depends(get_db)):
    exp = await db.get(Experience, exp_id)
    if not exp:
        raise HTTPException(404)
    await db.execute(delete(Slide).where(Slide.experience_id == exp_id))
    await db.delete(exp)
    await db.commit()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# API ROUTES — SLIDES
# ══════════════════════════════════════════════════════════════════════════════

_silhouette_tasks = {}  # slide_id -> {"status": "generating"|"done"|"error", "warning": str|None}

async def _run_silhouette_task(slide_id: int, source_path: str, mode: str):
    """Generate silhouette in background, update DB and task status."""
    try:
        sil_path = await asyncio.to_thread(
            generate_silhouette, source_path, SILHOUETTE_DIR, slide_id, mode
        )
        async with SessionLocal() as sdb:
            s = await sdb.get(Slide, slide_id)
            if s and sil_path:
                s.silhouette_path = sil_path
                await sdb.commit()
        _silhouette_tasks[slide_id] = {"status": "done", "warning": None}
    except ValueError as e:
        if "no_person_detected" in str(e):
            _silhouette_tasks[slide_id] = {"status": "done", "warning": "no_person_detected"}
        else:
            _silhouette_tasks[slide_id] = {"status": "error", "warning": str(e)}
    except Exception as e:
        _silhouette_tasks[slide_id] = {"status": "error", "warning": str(e)}

@app.get("/api/slides/{slide_id}/silhouette-status")
async def silhouette_status(slide_id: int):
    task = _silhouette_tasks.get(slide_id)
    if not task:
        return {"status": "unknown"}
    return task

@app.post("/api/experiences/{exp_id}/slides")
async def add_slide(exp_id: int, request: Request, db=Depends(get_db)):
    data = await request.json()
    # Get next position
    result = await db.execute(
        select(func.max(Slide.position)).where(Slide.experience_id == exp_id)
    )
    max_pos = result.scalar() or 0

    slide = Slide(
        experience_id=exp_id,
        position=max_pos + 1,
        slide_type=SlideType(data.get("slide_type", "frame")),
        media_id=data["media_id"],
        display_duration=data.get("display_duration"),
        question_timer=data.get("question_timer"),
        answer_a=data.get("answer_a"),
        answer_b=data.get("answer_b"),
        answer_c=data.get("answer_c"),
        answer_d=data.get("answer_d"),
        correct_answer=data.get("correct_answer"),
    )
    db.add(slide)
    await db.commit()
    await db.refresh(slide)

    # Auto-generate silhouette for game slides (background)
    if slide.slide_type == SlideType.GAME:
        media = await db.get(Media, slide.media_id)
        if media:
            source_path = media.web_path or media.file_path
            sil_mode = data.get("silhouette_mode", "local")
            slide_id = slide.id
            _silhouette_tasks[slide_id] = {"status": "generating", "warning": None}
            task = asyncio.create_task(_run_silhouette_task(slide_id, source_path, sil_mode))
            _bg_tasks.add(task)
            task.add_done_callback(_bg_tasks.discard)
            return {"id": slide.id, "position": slide.position, "generating": True}

    return {"id": slide.id, "position": slide.position, "generating": False}

@app.put("/api/slides/{slide_id}")
async def update_slide(slide_id: int, request: Request, db=Depends(get_db)):
    data = await request.json()
    slide = await db.get(Slide, slide_id)
    if not slide:
        raise HTTPException(404)
    for key in ["position", "slide_type", "media_id", "display_duration",
                "question_timer", "answer_a", "answer_b", "answer_c",
                "answer_d", "correct_answer"]:
        if key in data:
            val = data[key]
            if key == "slide_type":
                val = SlideType(val)
            setattr(slide, key, val)
    await db.commit()
    return {"ok": True}

@app.delete("/api/slides/{slide_id}")
async def delete_slide(slide_id: int, db=Depends(get_db)):
    slide = await db.get(Slide, slide_id)
    if not slide:
        raise HTTPException(404)
    await db.delete(slide)
    await db.commit()
    return {"ok": True}

@app.post("/api/slides/{slide_id}/generate-silhouette")
async def api_generate_silhouette(slide_id: int, request: Request, db=Depends(get_db)):
    slide = await db.get(Slide, slide_id)
    if not slide:
        raise HTTPException(404)
    media = await db.get(Media, slide.media_id)
    if not media:
        raise HTTPException(404)
    source_path = media.web_path or media.file_path
    try:
        data = await request.json()
    except Exception:
        data = {}
    mode = data.get("mode", "local")
    if mode == "gemini" and not os.environ.get("GEMINI_API_KEY"):
        raise HTTPException(400, "AI features need a Gemini key. Add it in Settings.")

    _silhouette_tasks[slide_id] = {"status": "generating", "warning": None}
    task = asyncio.create_task(_run_silhouette_task(slide_id, source_path, mode))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return {"ok": True, "generating": True}

@app.get("/api/slides/{slide_id}/silhouette")
async def serve_silhouette(slide_id: int, db=Depends(get_db)):
    slide = await db.get(Slide, slide_id)
    if not slide or not slide.silhouette_path or not os.path.exists(slide.silhouette_path):
        raise HTTPException(404)
    return FileResponse(slide.silhouette_path)

@app.post("/api/experiences/{exp_id}/reorder")
async def reorder_slides(exp_id: int, request: Request, db=Depends(get_db)):
    data = await request.json()
    slide_ids = data.get("slide_ids", [])
    for i, sid in enumerate(slide_ids):
        slide = await db.get(Slide, sid)
        if slide and slide.experience_id == exp_id:
            slide.position = i + 1
    await db.commit()
    return {"ok": True}

@app.post("/api/experiences/{exp_id}/bulk-remove")
async def bulk_remove_slides(exp_id: int, request: Request, db=Depends(get_db)):
    """Remove multiple slides from an experience (not from media library)."""
    data = await request.json()
    slide_ids = data.get("slide_ids", [])
    removed = 0
    for sid in slide_ids:
        slide = await db.get(Slide, sid)
        if slide and slide.experience_id == exp_id:
            await db.delete(slide)
            removed += 1
    await db.commit()
    return {"removed": removed}

@app.post("/api/experiences/{exp_id}/bulk-import")
async def bulk_import_slides(exp_id: int, request: Request, db=Depends(get_db)):
    """Import media as FRAME slides, skipping any already in the experience."""
    data = await request.json()
    media_ids = data.get("media_ids", [])
    # Find media IDs already in this experience
    existing = await db.execute(
        select(Slide.media_id).where(Slide.experience_id == exp_id)
    )
    existing_ids = {row[0] for row in existing.fetchall()}
    new_ids = [mid for mid in media_ids if mid not in existing_ids]
    result = await db.execute(
        select(func.max(Slide.position)).where(Slide.experience_id == exp_id)
    )
    pos = (result.scalar() or 0)
    for mid in new_ids:
        pos += 1
        slide = Slide(
            experience_id=exp_id, position=pos,
            slide_type=SlideType.FRAME, media_id=mid,
        )
        db.add(slide)
    await db.commit()
    return {"imported": len(new_ids), "skipped": len(media_ids) - len(new_ids)}

import re

def _extract_date_from_filename(filename: str) -> datetime | None:
    """Extract a date from common filename patterns as fallback when EXIF is missing."""
    name = Path(filename).stem
    # Pattern 1: YYYY-MM-DD or YYYY_MM_DD or YYYYMMDD
    m = re.search(r'(20\d{2})[-_]?(0[1-9]|1[0-2])[-_]?(0[1-9]|[12]\d|3[01])', name)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    # Pattern 2: Just a year (e.g., "vacation_2015", "photo 2008")
    m = re.search(r'(?:^|[^0-9])(19[5-9]\d|20[0-2]\d)(?:[^0-9]|$)', name)
    if m:
        return datetime(int(m.group(1)), 6, 15)  # Mid-year estimate
    return None

def _get_sort_date(media) -> datetime:
    """Get best available date for sorting: EXIF > filename heuristic > file mtime > max."""
    if media.exif_date:
        return media.exif_date
    fname_date = _extract_date_from_filename(media.filename)
    if fname_date:
        return fname_date
    return datetime.max

@app.post("/api/experiences/{exp_id}/sort-by-date")
async def sort_slides_by_date(exp_id: int, request: Request, db=Depends(get_db)):
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    use_ai = body.get("use_ai", False)

    result = await db.execute(
        select(Slide).where(Slide.experience_id == exp_id)
        .options(selectinload(Slide.media))
        .order_by(Slide.position)
    )
    slides = result.scalars().all()
    frame_slides = [s for s in slides if s.slide_type == SlideType.FRAME]
    game_slides = [s for s in slides if s.slide_type == SlideType.GAME]

    # For AI-assisted sorting, estimate dates for undated photos via Gemini
    ai_dates = {}  # media_id -> datetime
    ai_count = 0
    if use_ai and os.environ.get("GEMINI_API_KEY"):
        for s in frame_slides:
            if not s.media.exif_date and not _extract_date_from_filename(s.media.filename):
                source = s.media.web_path or s.media.file_path
                if os.path.exists(source):
                    year = await asyncio.to_thread(estimate_photo_year_gemini, source)
                    if year:
                        ai_dates[s.media.id] = datetime(year, 6, 15)
                        ai_count += 1

    def get_date(s):
        if s.media.exif_date:
            return s.media.exif_date
        fname = _extract_date_from_filename(s.media.filename)
        if fname:
            return fname
        if s.media.id in ai_dates:
            return ai_dates[s.media.id]
        return datetime.max

    frame_slides.sort(key=get_date)
    # Merge back: frames sorted, games keep relative position
    all_sorted = []
    fi, gi = 0, 0
    for s in slides:
        if s.slide_type == SlideType.FRAME:
            all_sorted.append(frame_slides[fi])
            fi += 1
        else:
            all_sorted.append(game_slides[gi])
            gi += 1
    for i, s in enumerate(all_sorted):
        s.position = i + 1
    await db.commit()
    return {"ok": True, "ai_estimated": ai_count}


# ══════════════════════════════════════════════════════════════════════════════
# API ROUTES — SETTINGS & AUTH
# ══════════════════════════════════════════════════════════════════════════════

def hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.encode()).hexdigest()

@app.post("/api/auth/set-pin")
async def set_pin(request: Request, db=Depends(get_db)):
    data = await request.json()
    pin = data.get("pin", "")
    if not pin or len(pin) < 4 or len(pin) > 6 or not pin.isdigit():
        raise HTTPException(400, "PIN must be 4-6 digits")
    result = await db.execute(select(Settings))
    settings = result.scalar_one_or_none()
    if not settings:
        settings = Settings()
        db.add(settings)
    settings.admin_pin = hash_pin(pin)
    await db.commit()
    return {"ok": True}

@app.post("/api/auth/verify-pin")
async def verify_pin(request: Request, db=Depends(get_db)):
    data = await request.json()
    pin = data.get("pin", "")
    result = await db.execute(select(Settings))
    settings = result.scalar_one_or_none()
    if not settings or not settings.admin_pin:
        return {"valid": True, "needs_setup": True}
    if settings.admin_pin == hash_pin(pin):
        return {"valid": True, "needs_setup": False}
    return {"valid": False, "needs_setup": False}

@app.get("/api/settings")
async def get_settings(db=Depends(get_db)):
    result = await db.execute(select(Settings))
    s = result.scalar_one_or_none()
    return {
        "language": s.language if s else "en",
        "has_pin": bool(s and s.admin_pin),
        "gemini_configured": bool(os.environ.get("GEMINI_API_KEY")),
        "family_context": s.family_context if s else "",
    }

@app.put("/api/settings/language")
async def set_language(request: Request, db=Depends(get_db)):
    import re as _re
    data = await request.json()
    lang = data.get("language", "en")
    if not _re.match(r'^[a-z]{2}$', lang):
        lang = "en"
    result = await db.execute(select(Settings))
    s = result.scalar_one_or_none()
    if not s:
        s = Settings(language=lang)
        db.add(s)
    else:
        s.language = lang
    await db.commit()
    return {"language": lang}

@app.put("/api/settings/family-context")
async def set_family_context(request: Request, db=Depends(get_db)):
    data = await request.json()
    ctx = data.get("family_context", "")
    result = await db.execute(select(Settings))
    s = result.scalar_one_or_none()
    if not s:
        s = Settings(family_context=ctx)
        db.add(s)
    else:
        s.family_context = ctx
    await db.commit()
    return {"ok": True}

@app.put("/api/settings/gemini-key")
async def set_gemini_key(request: Request):
    data = await request.json()
    key = data.get("key", "").strip()
    if not key:
        raise HTTPException(400, "Key cannot be empty")
    # Write to .env file and set in current process
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    await asyncio.to_thread(_update_env_file, env_path, key)
    os.environ["GEMINI_API_KEY"] = key
    return {"ok": True}

@app.get("/api/settings/gemini-usage")
async def api_gemini_usage():
    return get_gemini_usage()

@app.get("/api/settings/prompts")
async def api_get_prompts():
    """Get all configurable Gemini prompts."""
    prompts = load_prompts()
    return {"prompts": prompts, "defaults": _DEFAULT_PROMPTS}

@app.put("/api/settings/prompts")
async def api_save_prompts(request: Request):
    """Save configurable Gemini prompts."""
    data = await request.json()
    prompts = data.get("prompts", {})
    save_prompts(prompts)
    return {"ok": True}

@app.delete("/api/media/all")
async def delete_all_media(db=Depends(get_db)):
    """Delete all media and their files from the library."""
    result = await db.execute(select(Media))
    all_media = result.scalars().all()
    for m in all_media:
        for p in [m.file_path, m.web_path, m.thumbnail_path]:
            if p and os.path.exists(p):
                try: os.remove(p)
                except: pass
        await db.delete(m)
    await db.commit()
    return {"ok": True, "deleted": len(all_media)}

@app.post("/api/reset-database")
async def reset_database():
    """Delete the database file and reinitialize. DESTRUCTIVE."""
    db_path = os.path.join(DB_DIR, "photoframe.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    # Reinitialize
    engine = get_engine()
    await init_db(engine)
    return {"ok": True}

# Track batch estimation progress
_estimate_dates_status = {"running": False, "processed": 0, "total": 0, "results": []}

@app.post("/api/media/estimate-dates")
async def estimate_dates_batch(request: Request, db=Depends(get_db)):
    """Batch-estimate dates for all undated images using Gemini age-based analysis."""
    global _estimate_dates_status
    if _estimate_dates_status["running"]:
        return {"status": "already_running", "processed": _estimate_dates_status["processed"],
                "total": _estimate_dates_status["total"]}

    if not os.environ.get("GEMINI_API_KEY"):
        raise HTTPException(400, "AI features need a Gemini key. Add it in Settings.")

    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    family_context = body.get("family_context", "")

    # Save family context
    result = await db.execute(select(Settings))
    s = result.scalar_one_or_none()
    if s:
        s.family_context = family_context
    else:
        s = Settings(family_context=family_context)
        db.add(s)
    await db.commit()

    # Get all undated images
    result = await db.execute(
        select(Media).where(Media.exif_date.is_(None), Media.media_type == MediaType.IMAGE)
    )
    undated = result.scalars().all()

    if not undated:
        return {"status": "done", "total": 0, "estimated": 0}

    _estimate_dates_status = {"running": True, "processed": 0, "total": len(undated),
                              "results": []}

    async def _run():
        estimated = 0
        for media in undated:
            source = media.web_path or media.file_path
            if not os.path.exists(source):
                _estimate_dates_status["processed"] += 1
                continue
            try:
                result = await asyncio.to_thread(
                    estimate_year_from_ages_gemini, source, family_context
                )
                if result and result.get("year"):
                    year = result["year"]
                    # Update in a fresh session
                    async with SessionLocal() as sess:
                        m = await sess.get(Media, media.id)
                        if m and not m.exif_date:
                            m.exif_date = datetime(year, 6, 15)
                            m.category = m.category or str(year)
                            await sess.commit()
                    estimated += 1
                    _estimate_dates_status["results"].append({
                        "id": media.id, "filename": media.filename,
                        "year": year, "confidence": result.get("confidence", "?"),
                        "reasoning": result.get("reasoning", "")[:100],
                    })
            except Exception as e:
                print(f"Estimate failed for {media.filename}: {e}")
            _estimate_dates_status["processed"] += 1
            # Small delay to avoid API rate limits
            await asyncio.sleep(0.5)
        _estimate_dates_status["running"] = False
        _estimate_dates_status["estimated"] = estimated
        print(f"Batch estimation done: {estimated}/{len(undated)} dated")

    task = asyncio.create_task(_run())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return {"status": "started", "total": len(undated)}

@app.get("/api/media/estimate-dates/status")
async def estimate_dates_status():
    return _estimate_dates_status


# ══════════════════════════════════════════════════════════════════════════════
# API ROUTES — ROOM / GAME
# ══════════════════════════════════════════════════════════════════════════════

def generate_room_code():
    """5-char uppercase, no ambiguous chars (0/O, 1/I/L)."""
    chars = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(chars) for _ in range(5))

async def _sync_room_to_db(code):
    """Persist room state + slide index to DB."""
    room = active_rooms.get(code)
    if not room:
        return
    try:
        async with SessionLocal() as db:
            db_room = await db.get(Room, room["room_id"])
            if db_room:
                db_room.state = GameState(room["state"]) if room["state"] in [e.value for e in GameState] else db_room.state
                db_room.current_slide_index = room["current_slide_index"]
                db_room.is_locked = room.get("is_locked", False)
                await db.commit()
    except Exception:
        pass  # Non-critical — room still works in memory


async def _restore_room(session, db_room):
    """Rebuild an active_rooms entry from a DB Room record."""
    exp = await session.get(Experience, db_room.experience_id)
    if not exp:
        return
    result = await session.execute(
        select(Slide).where(Slide.experience_id == exp.id)
        .options(selectinload(Slide.media))
        .order_by(Slide.position)
    )
    slides = result.scalars().all()
    if not slides:
        return

    import qrcode, io, base64
    join_url = f"{SERVER_PROTO}://{SERVER_HOST}:{SERVER_PORT}/play/{db_room.code}"
    qr = qrcode.make(join_url, box_size=10, border=2)
    buf = io.BytesIO()
    qr.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode()

    # Restore players with scores from DB
    p_result = await session.execute(
        select(Player).where(Player.room_id == db_room.id)
    )
    db_players = p_result.scalars().all()
    # Store as "disconnected" placeholders — players rejoin by name to reclaim
    restored_players = {}
    for p in db_players:
        fake_sid = f"_restored_{p.id}"
        restored_players[fake_sid] = {
            "id": p.id,
            "nickname": p.nickname,
            "score": p.total_score or 0,
            "connected": False,
            "disconnected_at": 0,  # always allow reconnect for restored players
        }

    active_rooms[db_room.code] = {
        "room_id": db_room.id,
        "experience_id": exp.id,
        "experience": {
            "name": exp.name, "language": exp.language,
            "default_image_duration": exp.default_image_duration,
            "max_video_duration": exp.max_video_duration,
            "transition_effect": exp.transition_effect,
            "default_question_timer": exp.default_question_timer,
            "relaxed_mode": exp.relaxed_mode,
            "show_leaderboard_between": exp.show_leaderboard_between,
            "sound_enabled": exp.sound_enabled,
            "leaderboard_duration": exp.leaderboard_duration,
            "quiz_intro_duration": exp.quiz_intro_duration if exp.quiz_intro_duration is not None else 3,
            "player_display_mode": exp.player_display_mode or "question_and_choices",
            "speed_scoring": exp.speed_scoring if exp.speed_scoring is not None else True,
            "max_points": exp.max_points or 100,
            "min_points": exp.min_points if exp.min_points is not None else 10,
            "wrong_points": exp.wrong_points or 0,
        },
        "slides": [{
            "id": s.id, "position": s.position, "slide_type": s.slide_type.value, "quiz_type": s.quiz_type,
            "media_id": s.media_id,
            "media_url": f"/api/media/{s.media_id}/file",
            "media_type": s.media.media_type.value if s.media else "image",
            "display_duration": s.display_duration or exp.default_image_duration,
            "silhouette_url": f"/api/slides/{s.id}/silhouette" if s.silhouette_path else None,
            "question_timer": s.question_timer or exp.default_question_timer,
            "answer_a": s.answer_a, "answer_b": s.answer_b,
            "answer_c": s.answer_c, "answer_d": s.answer_d,
            "correct_answer": s.correct_answer,
        } for s in slides],
        "state": db_room.state.value if db_room.state else "lobby",
        "current_slide_index": db_room.current_slide_index or 0,
        "players": restored_players,
        "is_locked": db_room.is_locked or False,
        "question_start_time": None,
        "timer_task": None,
        "answers": {},
        "qr_b64": qr_b64,
        "join_url": join_url,
    }


@app.get("/api/rooms")
async def list_rooms():
    """List all active rooms for admin management."""
    rooms = []
    for code, room in active_rooms.items():
        rooms.append({
            "code": code,
            "state": room["state"],
            "experience_name": room["experience"]["name"],
            "player_count": len(room["players"]),
            "total_slides": len(room["slides"]),
            "current_slide_index": room["current_slide_index"],
            "is_locked": room["is_locked"],
        })
    return rooms


@app.get("/api/rooms/history")
async def rooms_history(experience_id: int = None, db=Depends(get_db)):
    """List past (finished) game sessions with scores."""
    query = select(Room).where(Room.state == GameState.FINISHED)
    if experience_id:
        query = query.where(Room.experience_id == experience_id)
    result = await db.execute(
        query.order_by(Room.created_at.desc()).limit(20)
    )
    rooms = result.scalars().all()
    history = []
    for r in rooms:
        # Get experience name
        exp = await db.get(Experience, r.experience_id)
        # Get players and scores
        p_result = await db.execute(
            select(Player).where(Player.room_id == r.id).order_by(Player.total_score.desc())
        )
        players = p_result.scalars().all()
        # Count answers for this room's players
        player_ids = [p.id for p in players]
        ans_count = 0
        correct_count = 0
        if player_ids:
            ac = await db.execute(
                select(func.count(PlayerAnswer.id), func.sum(PlayerAnswer.is_correct.cast(Integer)))
                .where(PlayerAnswer.player_id.in_(player_ids))
            )
            row = ac.one()
            ans_count = row[0] or 0
            correct_count = row[1] or 0
        history.append({
            "code": r.code,
            "experience_name": exp.name if exp else "?",
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "slide_count": r.current_slide_index or 0,
            "players": [{"nickname": p.nickname, "score": p.total_score} for p in players],
            "total_answers": ans_count,
            "correct_answers": correct_count,
        })
    return history


@app.delete("/api/rooms/bulk-delete")
async def bulk_delete_rooms(experience_id: int = None, empty_only: bool = False,
                            with_players_only: bool = False, db=Depends(get_db)):
    """Bulk delete rooms. Deletes all states (not just FINISHED) so crashed sessions get cleaned up."""
    query = select(Room)
    if experience_id:
        query = query.where(Room.experience_id == experience_id)
    result = await db.execute(query)
    rooms = result.scalars().all()
    deleted = 0
    for room in rooms:
        # Skip rooms that are actively in-memory (playing right now)
        if room.code in active_rooms and active_rooms[room.code].get("state") == "playing":
            continue
        p_result = await db.execute(select(Player).where(Player.room_id == room.id))
        players = p_result.scalars().all()
        if empty_only and len(players) > 0:
            continue
        if with_players_only and len(players) == 0:
            continue
        for p in players:
            await db.execute(delete(PlayerAnswer).where(PlayerAnswer.player_id == p.id))
        await db.execute(delete(Player).where(Player.room_id == room.id))
        await db.delete(room)
        deleted += 1
    await db.commit()
    return {"ok": True, "deleted": deleted}

@app.delete("/api/rooms/{code}")
async def delete_room(code: str, db=Depends(get_db)):
    """Kill a room — remove from memory and mark finished in DB."""
    room = active_rooms.pop(code, None)
    if room:
        if room.get("timer_task"):
            room["timer_task"].cancel()
        # Notify everyone
        await sio.emit("game_finished", {"leaderboard": []}, room=f"display_{code}")
        await sio.emit("game_finished", {"leaderboard": []}, room=f"admin_{code}")
        for psid in list(room["players"].keys()):
            await sio.emit("game_finished", {"leaderboard": [], "your_rank": 0, "your_score": 0}, to=psid)
    # Mark finished in DB
    result = await db.execute(select(Room).where(Room.code == code))
    db_room = result.scalar_one_or_none()
    if db_room:
        db_room.state = GameState.FINISHED
        await db.commit()
    return {"ok": True}


@app.delete("/api/rooms/{code}/history")
async def delete_room_history(code: str, db=Depends(get_db)):
    """Permanently delete a finished room and all its player data."""
    result = await db.execute(select(Room).where(Room.code == code))
    db_room = result.scalar_one_or_none()
    if not db_room:
        raise HTTPException(404, "Room not found")
    p_result = await db.execute(select(Player).where(Player.room_id == db_room.id))
    players = p_result.scalars().all()
    for p in players:
        await db.execute(delete(PlayerAnswer).where(PlayerAnswer.player_id == p.id))
    await db.execute(delete(Player).where(Player.room_id == db_room.id))
    await db.delete(db_room)
    await db.commit()
    return {"ok": True}


@app.post("/api/test-player")
async def add_test_player(request: Request, db=Depends(get_db)):
    """Add a fake test player to an active room (for admin testing)."""
    data = await request.json()
    code = data.get("code", "").upper()
    nickname = data.get("nickname", "Test")
    room = active_rooms.get(code)
    if not room:
        raise HTTPException(404, "Room not found")
    if len(room["players"]) >= 30:
        raise HTTPException(400, "Room is full")
    # Check name uniqueness
    existing_names = {p["nickname"].lower() for p in room["players"].values()}
    if nickname.lower() in existing_names:
        raise HTTPException(409, "Name already taken")
    # Create player in DB
    result = await db.execute(select(Room).where(Room.code == code))
    db_room = result.scalar_one_or_none()
    if not db_room:
        raise HTTPException(404, "Room not found in DB")
    player = Player(room_id=db_room.id, nickname=nickname, total_score=0)
    db.add(player)
    await db.commit()
    await db.refresh(player)
    # Add to in-memory room with a fake sid
    fake_sid = f"test_{player.id}_{secrets.token_hex(4)}"
    room["players"][fake_sid] = {
        "id": player.id,
        "nickname": nickname,
        "score": 0,
        "connected": True,
        "answered": False,
    }
    # Notify everyone
    connected_names = [p["nickname"] for p in room["players"].values() if p["connected"]]
    await sio.emit("player_joined", {
        "nickname": nickname,
        "player_count": len(connected_names),
        "players": connected_names,
    }, room=f"display_{code}")
    await sio.emit("player_joined", {
        "nickname": nickname,
        "player_count": len(connected_names),
        "players": connected_names,
    }, room=f"admin_{code}")
    await sio.emit("player_list", get_player_list(code), room=f"admin_{code}")
    return {"ok": True, "nickname": nickname, "id": player.id}


@app.post("/api/rooms")
async def create_room(request: Request, db=Depends(get_db)):
    data = await request.json()
    exp_id = data.get("experience_id")
    exp = await db.get(Experience, exp_id)
    if not exp:
        raise HTTPException(404, "Experience not found")

    custom_code = data.get("code", "").strip().upper()
    if custom_code:
        # Validate: 3-20 chars, alphanumeric only
        import re
        if not re.match(r'^[A-Z0-9]{3,8}$', custom_code):
            raise HTTPException(400, "Room code must be 3-8 alphanumeric characters (letters and numbers only)")
        # Check uniqueness
        existing = await db.execute(select(Room).where(Room.code == custom_code, Room.state != GameState.FINISHED))
        if existing.scalar_one_or_none():
            raise HTTPException(409, "This room code is already in use")
        code = custom_code
    else:
        code = generate_room_code()
    room = Room(code=code, experience_id=exp_id, state=GameState.LOBBY)
    db.add(room)
    await db.commit()
    await db.refresh(room)

    # Load experience data into memory
    result = await db.execute(
        select(Slide).where(Slide.experience_id == exp_id)
        .options(selectinload(Slide.media))
        .order_by(Slide.position)
    )
    slides = result.scalars().all()

    import qrcode
    import io
    import base64
    join_url = f"{SERVER_PROTO}://{SERVER_HOST}:{SERVER_PORT}/play/{code}"
    qr = qrcode.make(join_url, box_size=10, border=2)
    buf = io.BytesIO()
    qr.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode()

    active_rooms[code] = {
        "room_id": room.id,
        "experience_id": exp_id,
        "experience": {
            "name": exp.name,
            "language": exp.language,
            "default_image_duration": exp.default_image_duration,
            "max_video_duration": exp.max_video_duration,
            "transition_effect": exp.transition_effect,
            "default_question_timer": exp.default_question_timer,
            "relaxed_mode": exp.relaxed_mode,
            "show_leaderboard_between": exp.show_leaderboard_between,
            "sound_enabled": exp.sound_enabled,
            "leaderboard_duration": exp.leaderboard_duration,
            "quiz_intro_duration": exp.quiz_intro_duration if exp.quiz_intro_duration is not None else 3,
            "player_display_mode": exp.player_display_mode or "question_and_choices",
            "speed_scoring": exp.speed_scoring if exp.speed_scoring is not None else True,
            "max_points": exp.max_points or 100,
            "min_points": exp.min_points if exp.min_points is not None else 10,
            "wrong_points": exp.wrong_points or 0,
        },
        "slides": [{
            "id": s.id, "position": s.position, "slide_type": s.slide_type.value, "quiz_type": s.quiz_type,
            "media_id": s.media_id,
            "media_url": f"/api/media/{s.media_id}/file",
            "media_type": s.media.media_type.value if s.media else "image",
            "display_duration": s.display_duration or exp.default_image_duration,
            "silhouette_url": f"/api/slides/{s.id}/silhouette" if s.silhouette_path else None,
            "question_timer": s.question_timer or exp.default_question_timer,
            "answer_a": s.answer_a, "answer_b": s.answer_b,
            "answer_c": s.answer_c, "answer_d": s.answer_d,
            "correct_answer": s.correct_answer,
        } for s in slides],
        "state": "lobby",
        "current_slide_index": 0,
        "players": {},  # sid -> {id, nickname, score, connected}
        "is_locked": False,
        "question_start_time": None,
        "timer_task": None,
        "answers": {},  # slide_id -> {player_id: {answer, time, points}}
        "qr_b64": qr_b64,
        "join_url": join_url,
    }

    return {"code": code, "room_id": room.id, "qr_b64": qr_b64, "join_url": join_url, "experience_name": exp.name}

@app.get("/api/rooms/{code}")
async def get_room_info(code: str):
    room = active_rooms.get(code)
    if not room:
        raise HTTPException(404, "Room not found")
    return {
        "code": code,
        "state": room["state"],
        "is_locked": room["is_locked"],
        "player_count": len(room["players"]),
        "players": [
            {"id": p["id"], "nickname": p["nickname"],
             "score": p["score"], "connected": p["connected"]}
            for p in room["players"].values()
        ],
        "experience_name": room["experience"]["name"],
        "total_slides": len(room["slides"]),
        "current_slide_index": room["current_slide_index"],
        "qr_b64": room["qr_b64"],
        "join_url": room["join_url"],
    }


# ══════════════════════════════════════════════════════════════════════════════
# SOCKET.IO — REAL-TIME GAME ENGINE
# ══════════════════════════════════════════════════════════════════════════════

@sio.event
async def connect(sid, environ):
    pass

@sio.event
async def disconnect(sid):
    admin_sids.discard(sid)
    # Find which room this player was in
    for code, room in active_rooms.items():
        for psid, player in room["players"].items():
            if psid == sid:
                player["connected"] = False
                player["disconnected_at"] = time.time()
                await sio.emit("player_disconnected", {
                    "player_id": player["id"],
                    "nickname": player["nickname"]
                }, room=f"admin_{code}")
                await sio.emit("player_list", get_player_list(code), room=f"admin_{code}")
                connected_names = [p["nickname"] for p in room["players"].values() if p["connected"]]
                await sio.emit("player_update", {
                    "player_count": len(connected_names),
                    "players": connected_names,
                }, room=f"display_{code}")
                return

def get_player_list(code):
    room = active_rooms.get(code)
    if not room:
        return []
    # Include per-player answered status for current slide
    current_slide_id = None
    idx = room.get("current_slide_index", 0)
    if idx < len(room.get("slides", [])):
        current_slide_id = room["slides"][idx].get("id")
    current_answers = room.get("answers", {}).get(current_slide_id, {}) if current_slide_id else {}
    players = []
    for p in room["players"].values():
        answered_info = current_answers.get(p["id"])
        players.append({
            "id": p["id"], "nickname": p["nickname"],
            "score": p["score"], "connected": p["connected"],
            "answered": answered_info is not None,
            "was_correct": answered_info["is_correct"] if answered_info else None,
        })
    # Sort by score descending
    players.sort(key=lambda x: x["score"], reverse=True)
    return players

def get_current_slide_data(code):
    room = active_rooms.get(code)
    if not room:
        return None
    idx = room["current_slide_index"]
    if idx >= len(room["slides"]):
        return None
    slide = room["slides"][idx]
    return {
        **slide,
        "index": idx,
        "total": len(room["slides"]),
        "correct_answer": None,  # Don't send to players
        "_relaxed": bool(room.get("experience", {}).get("relaxed_mode")),
    }

def get_current_slide_admin(code):
    room = active_rooms.get(code)
    if not room:
        return None
    idx = room["current_slide_index"]
    if idx >= len(room["slides"]):
        return None
    slide = room["slides"][idx]
    return {
        **slide,
        "index": idx,
        "total": len(room["slides"]),
    }

def get_current_slide_player(code):
    """Get slide data filtered by player_display_mode."""
    room = active_rooms.get(code)
    if not room:
        return None
    idx = room["current_slide_index"]
    if idx >= len(room["slides"]):
        return None
    slide = room["slides"][idx]
    mode = room.get("experience", {}).get("player_display_mode", "question_and_choices")

    player_slide = {
        "slide_type": slide["slide_type"],
        "index": idx,
        "total": len(room["slides"]),
        "correct_answer": None,
        "_relaxed": bool(room.get("experience", {}).get("relaxed_mode")),
        "_display_mode": mode,
    }

    if slide["slide_type"] == "game":
        # Always send answer choices
        player_slide["answer_a"] = slide.get("answer_a")
        player_slide["answer_b"] = slide.get("answer_b")
        player_slide["answer_c"] = slide.get("answer_c")
        player_slide["answer_d"] = slide.get("answer_d")
        player_slide["question_timer"] = slide.get("question_timer")

        if mode in ("question_and_choices", "full"):
            player_slide["question_text"] = slide.get("question_text")
        if mode == "full":
            player_slide["media_url"] = slide.get("silhouette_url") or slide.get("media_url")
            player_slide["media_thumbnail"] = f"/api/media/{slide['media_id']}/thumbnail" if slide.get("media_id") else None
    else:
        # Frame slide
        if mode == "full":
            player_slide["media_url"] = slide.get("media_url")
            player_slide["media_thumbnail"] = f"/api/media/{slide['media_id']}/thumbnail" if slide.get("media_id") else None
        player_slide["display_duration"] = slide.get("display_duration")

    return player_slide

# ── Admin events ─────────────────────────────────────────────────────────────

def is_admin(sid):
    """Check if a socket ID is an authenticated admin."""
    return sid in admin_sids

@sio.event
async def admin_join(sid, data):
    code = data.get("code")
    if code not in active_rooms:
        await sio.emit("error", {"message": "Room not found"}, to=sid)
        return
    admin_sids.add(sid)
    await sio.enter_room(sid, f"admin_{code}")
    await sio.enter_room(sid, f"display_{code}")
    await sio.enter_room(sid, "admin_broadcast")
    room = active_rooms[code]
    await sio.emit("room_state", {
        "state": room["state"],
        "players": get_player_list(code),
        "current_slide": get_current_slide_admin(code),
        "is_locked": room["is_locked"],
        "experience": room["experience"],
        "qr_b64": room["qr_b64"],
        "join_url": room["join_url"],
    }, to=sid)

@sio.event
async def display_join(sid, data):
    code = data.get("code")
    if code not in active_rooms:
        await sio.emit("error", {"message": "Room not found"}, to=sid)
        return
    await sio.enter_room(sid, f"display_{code}")
    room = active_rooms[code]
    await sio.emit("room_state", {
        "state": room["state"],
        "current_slide": get_current_slide_data(code),
        "qr_b64": room["qr_b64"],
        "join_url": room["join_url"],
        "experience": room["experience"],
        "player_count": len(room["players"]),
        "players": [p["nickname"] for p in room["players"].values()],
    }, to=sid)

@sio.event
async def lock_room(sid, data):
    if not is_admin(sid): return
    code = data.get("code")
    room = active_rooms.get(code)
    if not room:
        return
    room["is_locked"] = True
    await sio.emit("room_locked", {}, room=f"admin_{code}")
    await sio.emit("room_locked", {}, room=f"player_{code}")

@sio.event
async def unlock_room(sid, data):
    if not is_admin(sid): return
    code = data.get("code")
    room = active_rooms.get(code)
    if not room:
        return
    room["is_locked"] = False
    await sio.emit("room_unlocked", {}, room=f"admin_{code}")

@sio.event
async def start_experience(sid, data):
    if not is_admin(sid): return
    code = data.get("code")
    room = active_rooms.get(code)
    if not room:
        return
    room["state"] = "playing"
    room["current_slide_index"] = 0
    slide = get_current_slide_data(code)
    admin_slide = get_current_slide_admin(code)

    await sio.emit("game_started", {
        "slide": slide,
        "experience": room["experience"],
    }, room=f"display_{code}")

    await sio.emit("game_started", {
        "slide": admin_slide,
        "experience": room["experience"],
    }, room=f"admin_{code}")

    player_slide = get_current_slide_player(code)
    await sio.emit("game_started", {
        "slide": player_slide,
    }, room=f"player_{code}")

    if slide and slide["slide_type"] == "game":
        await start_question_timer(code)
    elif slide and slide["slide_type"] == "frame":
        duration = slide.get("display_duration") or room["experience"].get("default_image_duration", 8)
        await start_frame_timer(code, duration)

async def start_question_timer(code):
    room = active_rooms.get(code)
    if not room:
        return
    idx = room["current_slide_index"]
    if idx < 0 or idx >= len(room["slides"]):
        return
    slide = room["slides"][idx]
    if room["experience"].get("relaxed_mode"):
        room["question_start_time"] = time.time()
        return  # No timer in relaxed mode

    timer_duration = slide.get("question_timer") or room["experience"]["default_question_timer"]
    room["question_start_time"] = time.time()
    deadline = int((room["question_start_time"] + timer_duration) * 1000)  # unix ms

    timer_payload = {"duration": timer_duration, "deadline": deadline}
    await sio.emit("timer_start", timer_payload, room=f"display_{code}")
    await sio.emit("timer_start", timer_payload, room=f"player_{code}")
    await sio.emit("timer_start", timer_payload, room=f"admin_{code}")

    # Auto-reveal when timer expires
    async def timer_expired():
        await asyncio.sleep(timer_duration)
        if active_rooms.get(code) and room["state"] == "playing":
            current_slide = room["slides"][room["current_slide_index"]]
            if current_slide["slide_type"] == "game":
                await do_reveal(code)

    if room.get("timer_task"):
        room["timer_task"].cancel()
    room["timer_task"] = asyncio.create_task(timer_expired())

@sio.event
async def display_video_ended(sid, data):
    """Auto-advance when a video finishes playing on display."""
    code = data.get("code")
    room = active_rooms.get(code)
    if not room or room["state"] != "playing":
        return
    slide = room["slides"][room["current_slide_index"]]
    # Only auto-advance for frame (video) slides, not game slides
    if slide.get("slide_type") == "frame" and slide.get("media_type") == "video":
        await advance_slide(code, 1)

@sio.event
async def admin_next(sid, data):
    if not is_admin(sid): return
    code = data.get("code")
    await advance_slide(code, 1)

@sio.event
async def admin_prev(sid, data):
    if not is_admin(sid): return
    code = data.get("code")
    await advance_slide(code, -1)

async def advance_slide(code, direction):
    room = active_rooms.get(code)
    if not room:
        return
    if room.get("timer_task"):
        room["timer_task"].cancel()
        room["timer_task"] = None

    new_idx = room["current_slide_index"] + direction
    if new_idx < 0:
        new_idx = 0
    if new_idx >= len(room["slides"]):
        # Experience finished
        room["state"] = "finished"
        leaderboard = get_leaderboard(code)
        await sio.emit("game_finished", {"leaderboard": leaderboard}, room=f"display_{code}")
        await sio.emit("game_finished", {"leaderboard": leaderboard}, room=f"admin_{code}")
        # Send personal results to each player
        for psid, player in room["players"].items():
            rank = next((i+1 for i, lb in enumerate(leaderboard) if lb["id"] == player["id"]), len(leaderboard))
            await sio.emit("game_finished", {
                "leaderboard": leaderboard,
                "your_rank": rank,
                "your_score": player["score"],
            }, to=psid)
        return

    room["current_slide_index"] = new_idx
    room["state"] = "playing"
    task = asyncio.create_task(_sync_room_to_db(code))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    slide = get_current_slide_data(code)
    admin_slide = get_current_slide_admin(code)
    player_slide = get_current_slide_player(code)

    # Quiz intro screen before game slides
    if slide and slide["slide_type"] == "game":
        intro_dur = room.get("experience", {}).get("quiz_intro_duration", 3)
        if intro_dur > 0:
            room["intro_start_time"] = time.time()
            intro_data = {
                "quiz_type": slide.get("quiz_type") or "shadow",
                "duration": intro_dur,
                "question_index": sum(1 for s in room["slides"][:new_idx + 1] if s["slide_type"] == "game"),
            }
            await sio.emit("quiz_intro", intro_data, room=f"display_{code}")
            await sio.emit("quiz_intro", intro_data, room=f"player_{code}")
            await sio.emit("slide_changed", {"slide": admin_slide}, room=f"admin_{code}")
            await sio.emit("player_list", get_player_list(code), room=f"admin_{code}")

            async def _after_intro():
                await asyncio.sleep(intro_dur)
                if active_rooms.get(code) is not room:
                    return
                if room["current_slide_index"] != new_idx:
                    return
                room.pop("intro_start_time", None)
                await sio.emit("slide_changed", {"slide": slide}, room=f"display_{code}")
                await sio.emit("slide_changed", {"slide": player_slide}, room=f"player_{code}")
                await start_question_timer(code)

            room["timer_task"] = asyncio.create_task(_after_intro())
            return

    await sio.emit("slide_changed", {"slide": slide}, room=f"display_{code}")
    await sio.emit("slide_changed", {"slide": admin_slide}, room=f"admin_{code}")
    await sio.emit("slide_changed", {"slide": player_slide}, room=f"player_{code}")
    await sio.emit("player_list", get_player_list(code), room=f"admin_{code}")

    if slide and slide["slide_type"] == "frame":
        duration = slide.get("display_duration") or room.get("experience", {}).get("default_image_duration", 8)
        await start_frame_timer(code, duration)

async def start_frame_timer(code, duration):
    """Auto-advance frame slide after duration seconds. Send timer to admin."""
    room = active_rooms.get(code)
    if not room:
        return
    import time as _time
    deadline = int((_time.time() + duration) * 1000)
    await sio.emit("frame_timer", {"duration": duration, "deadline": deadline}, room=f"admin_{code}")
    await sio.emit("frame_timer", {"duration": duration, "deadline": deadline}, room=f"display_{code}")

    async def auto_advance():
        await asyncio.sleep(duration)
        # Only advance if still on the same slide
        if active_rooms.get(code) is room and room["state"] == "playing":
            await advance_slide(code, 1)

    room["timer_task"] = asyncio.create_task(auto_advance())

@sio.event
async def admin_end_game(sid, data):
    """End the game immediately, show final leaderboard."""
    if not is_admin(sid): return
    code = data.get("code")
    room = active_rooms.get(code)
    if not room:
        return
    if room.get("timer_task"):
        room["timer_task"].cancel()
        room["timer_task"] = None
    room["state"] = "finished"
    leaderboard = get_leaderboard(code)
    await sio.emit("game_finished", {"leaderboard": leaderboard}, room=f"display_{code}")
    await sio.emit("game_finished", {"leaderboard": leaderboard}, room=f"admin_{code}")
    for psid, player in room["players"].items():
        rank = next((i+1 for i, lb in enumerate(leaderboard) if lb["id"] == player["id"]), len(leaderboard))
        await sio.emit("game_finished", {
            "leaderboard": leaderboard,
            "your_rank": rank,
            "your_score": player["score"],
        }, to=psid)
    # Mark finished in DB
    async with SessionLocal() as db:
        result = await db.execute(select(Room).where(Room.code == code))
        db_room = result.scalar_one_or_none()
        if db_room:
            db_room.state = GameState.FINISHED
            await db.commit()
    # Remove from active_rooms
    active_rooms.pop(code, None)

@sio.event
async def admin_reveal(sid, data):
    if not is_admin(sid): return
    code = data.get("code")
    await do_reveal(code)

async def do_reveal(code):
    room = active_rooms.get(code)
    if not room:
        return
    if room.get("timer_task"):
        room["timer_task"].cancel()
        room["timer_task"] = None

    room["state"] = "revealing"
    slide = room["slides"][room["current_slide_index"]]
    slide_id = slide["id"]
    answers = room["answers"].get(slide_id, {})

    # Calculate stats
    total = len(room["players"])
    answered = len(answers)
    correct_count = sum(1 for a in answers.values() if a["is_correct"])
    answer_counts = {"a": 0, "b": 0, "c": 0, "d": 0}
    for a in answers.values():
        if a["answer"] in answer_counts:
            answer_counts[a["answer"]] += 1

    reveal_data = {
        "correct_answer": slide["correct_answer"],
        "media_url": slide["media_url"],
        "stats": {
            "total": total,
            "answered": answered,
            "correct": correct_count,
            "answer_counts": answer_counts,
        }
    }

    await sio.emit("reveal", reveal_data, room=f"display_{code}")
    await sio.emit("reveal", reveal_data, room=f"admin_{code}")
    await sio.emit("player_list", get_player_list(code), room=f"admin_{code}")

    # Send personal results to each player
    for psid, player in room["players"].items():
        player_answer = answers.get(player["id"])
        await sio.emit("reveal", {
            **reveal_data,
            "your_answer": player_answer["answer"] if player_answer else None,
            "your_correct": player_answer["is_correct"] if player_answer else False,
            "your_points": player_answer["points"] if player_answer else 0,
            "your_total_score": player["score"],
        }, to=psid)

@sio.event
async def admin_pause(sid, data):
    if not is_admin(sid): return
    code = data.get("code")
    room = active_rooms.get(code)
    if not room:
        return
    room["state"] = "paused"
    if room.get("timer_task"):
        room["timer_task"].cancel()
        room["timer_task"] = None
    # Save remaining timer time for resume
    if room.get("intro_start_time"):
        # Paused during quiz intro
        elapsed = time.time() - room["intro_start_time"]
        intro_dur = room["experience"].get("quiz_intro_duration", 3)
        room["paused_intro_remaining"] = max(0, intro_dur - elapsed)
    elif room.get("question_start_time") and not room["experience"].get("relaxed_mode"):
        elapsed = time.time() - room["question_start_time"]
        idx = room["current_slide_index"]
        if idx < len(room["slides"]):
            slide = room["slides"][idx]
            timer_duration = slide.get("question_timer") or room["experience"]["default_question_timer"]
            room["paused_remaining"] = max(0, timer_duration - elapsed)
    await sio.emit("paused", {}, room=f"display_{code}")
    await sio.emit("paused", {}, room=f"player_{code}")
    await sio.emit("paused", {}, room=f"admin_{code}")

@sio.event
async def admin_resume(sid, data):
    if not is_admin(sid): return
    code = data.get("code")
    room = active_rooms.get(code)
    if not room:
        return
    room["state"] = "playing"
    # Paused during quiz intro — resume the intro countdown
    intro_remaining = room.pop("paused_intro_remaining", None)
    if intro_remaining is not None and intro_remaining > 0:
        idx = room["current_slide_index"]
        slide = get_current_slide_data(code)
        player_slide = get_current_slide_player(code)
        room["intro_start_time"] = time.time()

        async def _resume_intro():
            await asyncio.sleep(intro_remaining)
            if active_rooms.get(code) is not room or room["state"] != "playing":
                return
            if room["current_slide_index"] != idx:
                return
            room.pop("intro_start_time", None)
            await sio.emit("slide_changed", {"slide": slide}, room=f"display_{code}")
            await sio.emit("slide_changed", {"slide": player_slide}, room=f"player_{code}")
            await start_question_timer(code)

        if room.get("timer_task"):
            room["timer_task"].cancel()
        room["timer_task"] = asyncio.create_task(_resume_intro())
        await sio.emit("resumed", {}, room=f"display_{code}")
        await sio.emit("resumed", {}, room=f"player_{code}")
        return

    # Restart timer with remaining time if we paused during a question
    remaining = room.pop("paused_remaining", None)
    if remaining and remaining > 0:
        idx = room["current_slide_index"]
        if idx < len(room["slides"]) and room["slides"][idx]["slide_type"] == "game":
            room["question_start_time"] = time.time()
            deadline = int((room["question_start_time"] + remaining) * 1000)
            tp = {"duration": remaining, "deadline": deadline}
            await sio.emit("timer_start", tp, room=f"display_{code}")
            await sio.emit("timer_start", tp, room=f"player_{code}")
            await sio.emit("timer_start", tp, room=f"admin_{code}")

            async def timer_expired():
                await asyncio.sleep(remaining)
                if active_rooms.get(code) and room["state"] == "playing":
                    current_slide = room["slides"][room["current_slide_index"]]
                    if current_slide["slide_type"] == "game":
                        await do_reveal(code)

            if room.get("timer_task"):
                room["timer_task"].cancel()
            room["timer_task"] = asyncio.create_task(timer_expired())
    await sio.emit("resumed", {}, room=f"display_{code}")
    await sio.emit("resumed", {}, room=f"player_{code}")
    await sio.emit("resumed", {}, room=f"admin_{code}")

@sio.event
async def show_leaderboard(sid, data):
    if not is_admin(sid): return
    code = data.get("code")
    room = active_rooms.get(code)
    if not room:
        return
    # Save remaining timer for restore on dismiss
    if room.get("question_start_time") and not room["experience"].get("relaxed_mode"):
        elapsed = time.time() - room["question_start_time"]
        idx = room["current_slide_index"]
        if idx < len(room["slides"]):
            slide = room["slides"][idx]
            timer_duration = slide.get("question_timer") or room["experience"]["default_question_timer"]
            room["leaderboard_remaining"] = max(0, timer_duration - elapsed)
    room["pre_leaderboard_state"] = room.get("state", "playing")
    # Cancel any existing timer
    if room.get("timer_task"):
        room["timer_task"].cancel()
        room["timer_task"] = None

    leaderboard = get_leaderboard(code)
    duration = room.get("experience", {}).get("leaderboard_duration") or 5
    await sio.emit("show_leaderboard", {
        "leaderboard": leaderboard, "duration": duration,
    }, room=f"display_{code}")
    await sio.emit("show_leaderboard", {
        "leaderboard": leaderboard, "duration": duration,
    }, room=f"admin_{code}")
    for psid, player in room["players"].items():
        rank = next((i+1 for i, lb in enumerate(leaderboard) if lb["id"] == player["id"]), len(leaderboard))
        await sio.emit("show_leaderboard", {
            "leaderboard": leaderboard,
            "your_rank": rank,
            "duration": duration,
        }, to=psid)

    # Auto-advance after leaderboard_duration
    async def leaderboard_auto_advance():
        await asyncio.sleep(duration)
        if active_rooms.get(code) and active_rooms[code].get("state") == "playing":
            await advance_slide(code, 1)

    room["timer_task"] = asyncio.create_task(leaderboard_auto_advance())

@sio.event
async def dismiss_leaderboard(sid, data):
    """Dismiss the leaderboard overlay and resume the current slide."""
    if not is_admin(sid): return
    code = data.get("code")
    room = active_rooms.get(code)
    if not room:
        return
    # Cancel auto-advance from leaderboard
    if room.get("timer_task"):
        room["timer_task"].cancel()
        room["timer_task"] = None
    room["state"] = room.pop("pre_leaderboard_state", "playing")
    # Re-emit current slide to all screens so they go back to what was showing
    idx = room["current_slide_index"]
    if idx < len(room["slides"]):
        slide = room["slides"][idx]
        await sio.emit("slide_changed", {"slide": slide, "index": idx, "total": len(room["slides"])}, room=f"display_{code}")
        await sio.emit("slide_changed", {"slide": slide, "index": idx, "total": len(room["slides"])}, room=f"player_{code}")
        await sio.emit("slide_changed", {"slide": slide, "index": idx, "total": len(room["slides"])}, room=f"admin_{code}")
    # Restore timer if there was remaining time
    remaining = room.pop("leaderboard_remaining", None)
    if remaining and remaining > 0 and room["state"] == "playing":
        if idx < len(room["slides"]) and room["slides"][idx]["slide_type"] == "game":
            room["question_start_time"] = time.time()
            deadline = int((room["question_start_time"] + remaining) * 1000)
            tp = {"duration": remaining, "deadline": deadline}
            await sio.emit("timer_start", tp, room=f"display_{code}")
            await sio.emit("timer_start", tp, room=f"player_{code}")
            await sio.emit("timer_start", tp, room=f"admin_{code}")

            async def timer_expired():
                await asyncio.sleep(remaining)
                if active_rooms.get(code) and room["state"] == "playing":
                    current_slide = room["slides"][room["current_slide_index"]]
                    if current_slide["slide_type"] == "game":
                        await do_reveal(code)

            room["timer_task"] = asyncio.create_task(timer_expired())
    await sio.emit("dismissed_leaderboard", {}, room=f"admin_{code}")

@sio.event
async def toggle_info_bar(sid, data):
    """Toggle the persistent info bar on the display."""
    if not is_admin(sid): return
    code = data.get("code")
    visible = data.get("visible", True)
    await sio.emit("info_bar_toggle", {"visible": visible}, room=f"display_{code}")

@sio.event
async def toggle_phone_timer(sid, data):
    """Toggle the timer visibility on player phones."""
    if not is_admin(sid): return
    code = data.get("code")
    visible = data.get("visible", True)
    await sio.emit("phone_timer_toggle", {"visible": visible}, room=f"player_{code}")

@sio.event
async def remove_player(sid, data):
    if not is_admin(sid): return
    code = data.get("code")
    player_id = data.get("player_id")
    room = active_rooms.get(code)
    if not room:
        return
    for psid, player in list(room["players"].items()):
        if player["id"] == player_id:
            await sio.emit("kicked", {}, to=psid)
            del room["players"][psid]
            break
    await sio.emit("player_list", get_player_list(code), room=f"admin_{code}")
    connected_names = [p["nickname"] for p in room["players"].values() if p["connected"]]
    await sio.emit("player_update", {
        "player_count": len(connected_names),
        "players": connected_names,
    }, room=f"display_{code}")

def get_leaderboard(code):
    room = active_rooms.get(code)
    if not room:
        return []
    players = sorted(room["players"].values(), key=lambda p: p["score"], reverse=True)
    return [{"id": p["id"], "nickname": p["nickname"], "score": p["score"]} for p in players]


# ── Player events ────────────────────────────────────────────────────────────

@sio.event
async def player_join(sid, data):
    code = data.get("code", "").upper().strip()
    nickname = data.get("nickname", "").strip()[:20]

    if not nickname or len(nickname) < 2:
        await sio.emit("join_error", {"message": "name_too_short"}, to=sid)
        return

    room = active_rooms.get(code)
    if not room:
        await sio.emit("join_error", {"message": "room_not_found"}, to=sid)
        return

    if room["is_locked"]:
        await sio.emit("join_error", {"message": "room_locked"}, to=sid)
        return

    if len(room["players"]) >= 30:
        await sio.emit("join_error", {"message": "room_full"}, to=sid)
        return

    # HTML-escape nickname
    import html
    nickname = html.escape(nickname)

    # Check for duplicate nickname among connected players
    for psid, player in room["players"].items():
        if player["nickname"] == nickname and player["connected"]:
            await sio.emit("join_error", {"message": "name_taken"}, to=sid)
            return

    # Check for reconnection (restored players have disconnected_at=0, always allow)
    for psid, player in list(room["players"].items()):
        if player["nickname"] == nickname and not player["connected"]:
            dc_time = player.get("disconnected_at", 0)
            if dc_time == 0 or time.time() - dc_time < 300:
                # Reconnect
                del room["players"][psid]
                player["connected"] = True
                room["players"][sid] = player
                await sio.enter_room(sid, f"player_{code}")
                # Check if player already answered current question
                idx = room["current_slide_index"]
                slide_id = room["slides"][idx]["id"] if idx < len(room["slides"]) else None
                already_answered = bool(slide_id and room.get("answers", {}).get(slide_id, {}).get(player["id"]))

                await sio.emit("join_success", {
                    "player_id": player["id"],
                    "nickname": nickname,
                    "score": player["score"],
                    "reconnected": True,
                    "state": room["state"],
                    "slide": get_current_slide_player(code) if room["state"] == "playing" else None,
                    "already_answered": already_answered,
                }, to=sid)
                # Send timer with remaining time if mid-question
                if room["state"] == "playing" and room.get("question_start_time"):
                    idx = room["current_slide_index"]
                    if idx < len(room["slides"]) and room["slides"][idx]["slide_type"] == "game":
                        if not room["experience"].get("relaxed_mode"):
                            slide = room["slides"][idx]
                            timer_dur = slide.get("question_timer") or room["experience"]["default_question_timer"]
                            elapsed = time.time() - room["question_start_time"]
                            remaining = max(0, timer_dur - elapsed)
                            if remaining > 0:
                                dl = int((room["question_start_time"] + timer_dur) * 1000)
                                await sio.emit("timer_start", {"duration": remaining, "deadline": dl}, to=sid)
                await sio.emit("player_list", get_player_list(code), room=f"admin_{code}")
                return

    # New player — use DB-generated ID to avoid collisions
    async with SessionLocal() as db:
        db_player = Player(room_id=room["room_id"], nickname=nickname, sid=sid)
        db.add(db_player)
        await db.commit()
        await db.refresh(db_player)
        player_id = db_player.id

    room["players"][sid] = {
        "id": player_id,
        "nickname": nickname,
        "score": 0,
        "connected": True,
    }

    await sio.enter_room(sid, f"player_{code}")
    await sio.emit("join_success", {
        "player_id": player_id,
        "nickname": nickname,
        "score": 0,
        "reconnected": False,
        "state": room["state"],
    }, to=sid)

    # Notify admin and display
    await sio.emit("player_joined", {
        "player_id": player_id,
        "nickname": nickname,
        "player_count": len(room["players"]),
    }, room=f"admin_{code}")
    await sio.emit("player_joined", {
        "nickname": nickname,
        "player_count": len(room["players"]),
        "players": [p["nickname"] for p in room["players"].values()],
    }, room=f"display_{code}")
    await sio.emit("player_list", get_player_list(code), room=f"admin_{code}")

@sio.event
async def submit_answer(sid, data):
    code = data.get("code")
    answer = data.get("answer", "").lower().strip()

    if answer not in ("a", "b", "c", "d"):
        return

    room = active_rooms.get(code)
    if not room or room["state"] != "playing":
        return

    player = room["players"].get(sid)
    if not player:
        return

    idx = room["current_slide_index"]
    if idx >= len(room["slides"]):
        return
    slide = room["slides"][idx]
    if slide["slide_type"] != "game":
        return

    # Reject answers during reveal phase
    if room.get("state") == "revealing":
        return

    slide_id = slide["id"]
    if slide_id not in room["answers"]:
        room["answers"][slide_id] = {}

    # Already answered?
    if player["id"] in room["answers"][slide_id]:
        return

    time_taken = time.time() - (room["question_start_time"] or time.time())
    # Clamp time_taken to timer duration to prevent negative scores from late arrivals
    timer_duration_check = slide.get("question_timer") or room["experience"]["default_question_timer"]
    if not room["experience"].get("relaxed_mode") and time_taken > timer_duration_check:
        time_taken = timer_duration_check
    is_correct = answer == slide["correct_answer"]
    print(f"  [SCORE] Player '{player['nickname']}': answered='{answer}', correct='{slide['correct_answer']}', match={is_correct}, time={time_taken:.1f}s")

    # Scoring — uses experience settings
    exp = room["experience"]
    timer_duration = slide.get("question_timer") or exp["default_question_timer"]
    speed_ratio = min(1.0, time_taken / timer_duration) if timer_duration > 0 else 1.0
    max_pts = exp.get("max_points", 100)
    min_pts = exp.get("min_points", 10)
    wrong_pts = exp.get("wrong_points", 0)
    use_speed = exp.get("speed_scoring", True)

    if exp.get("relaxed_mode"):
        points = max_pts if is_correct else wrong_pts
    elif is_correct:
        if use_speed:
            points = max(min_pts, max_pts - int((max_pts - min_pts) * speed_ratio))
        else:
            points = max_pts
    else:
        points = wrong_pts
    print(f"  [SCORE] => points={points} (max={max_pts}, min={min_pts}, speed_ratio={speed_ratio:.2f}, relaxed={exp.get('relaxed_mode')})")

    room["answers"][slide_id][player["id"]] = {
        "answer": answer,
        "time": time_taken,
        "points": points,
        "is_correct": is_correct,
    }
    player["score"] += points

    # Save to DB
    async with SessionLocal() as db:
        db_answer = PlayerAnswer(
            player_id=player["id"], slide_id=slide_id,
            answer=answer, is_correct=is_correct,
            time_taken=time_taken, points_earned=points,
        )
        db.add(db_answer)
        # Update player total score
        db_player = await db.get(Player, player["id"])
        if db_player:
            db_player.total_score = player["score"]
        await db.commit()

    await sio.emit("answer_submitted", {"player_id": player["id"]}, to=sid)

    # Notify admin of live stats
    answers = room["answers"].get(slide_id, {})
    answer_counts = {"a": 0, "b": 0, "c": 0, "d": 0}
    for a in answers.values():
        if a["answer"] in answer_counts:
            answer_counts[a["answer"]] += 1

    await sio.emit("live_stats", {
        "answered": len(answers),
        "total": len(room["players"]),
        "answer_counts": answer_counts,
    }, room=f"admin_{code}")

    # Update admin scoreboard with latest scores
    await sio.emit("player_list", get_player_list(code), room=f"admin_{code}")

    # Auto-reveal if all players answered
    if len(answers) >= len(room["players"]):
        await do_reveal(code)


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(socket_app, host="0.0.0.0", port=SERVER_PORT, log_level="info")
