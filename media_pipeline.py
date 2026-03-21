"""Media pipeline: scan, import, convert, thumbnail, silhouette."""

import os
import json as _json
import subprocess
import hashlib
from pathlib import Path
from datetime import datetime, date

from PIL import Image
import pillow_heif

from models import Media, MediaType

# ── Configurable prompts (editable from admin UI) ─────────────────────────
_PROMPTS_FILE = os.path.join(os.path.expanduser("~/.photoframe"), "prompts.json")

_DEFAULT_PROMPTS = {
    "shadow": (
        "# PHOTO SILHOUETTE EDITOR FOR PARTY GAME\n\n"
        "## OBJECTIVE\n"
        "Edit this party photo by replacing one specific person with a pure black silhouette "
        "while keeping all other people and the entire background completely unchanged.\n\n"
        "**Person to silhouette:** '{person}'\n\n"
        "## PROCESSING STEPS\n\n"
        "**Phase 1: Person Identification**\n"
        "1. Analyze the photo to detect all people present\n"
        "2. Parse the person description to extract identifying characteristics\n"
        "3. Match the description against detected people using position, clothing, features\n"
        "4. Identify the single person that best matches\n\n"
        "**Phase 2: Silhouette Creation**\n"
        "1. Extract the precise body outline of the identified person\n"
        "2. Preserve the exact pose, position, and proportions\n"
        "3. Create a filled shape using pure black (#000000)\n"
        "4. Ensure silhouette boundary follows body outline exactly with no gaps or overflow\n\n"
        "**Phase 3: Preservation Verification**\n"
        "1. Verify all other people remain at original visibility\n"
        "2. Verify background elements unchanged\n"
        "3. Verify no darkening or modifications to non-target areas\n\n"
        "## REQUIREMENTS\n\n"
        "**You must:**\n"
        "- Replace ONLY the person matching the description\n"
        "- Use solid pure black (#000000) for the silhouette fill\n"
        "- Follow the target person's exact body outline\n"
        "- Keep all other people completely unchanged and fully visible\n"
        "- Keep the entire background unchanged (same colors, lighting, objects)\n"
        "- Output only the edited image\n\n"
        "**You must not:**\n"
        "- Silhouette more than one person\n"
        "- Silhouette the wrong person\n"
        "- Darken, modify, or partially silhouette other people\n"
        "- Change background colors, lighting, or objects\n\n"
        "## VERIFICATION CHECKLIST\n"
        "Before output, verify:\n"
        "- Correct person identified from description\n"
        "- Silhouette applied to ONLY that one person\n"
        "- Silhouette is pure black (#000000) with no transparency\n"
        "- All other people unchanged and fully visible\n"
        "- Background unchanged\n"
        "- Output contains only the edited image"
    ),
    "missing": (
        "# PHOTO PERSON REMOVAL EDITOR\n\n"
        "## OBJECTIVE\n"
        "Edit this photo by completely removing one specific person and seamlessly filling "
        "the vacated area with natural background continuation, creating the appearance "
        "that the person was never present in the scene.\n\n"
        "**Person to remove:** '{person}'\n\n"
        "## PROCESSING STEPS\n\n"
        "**Phase 1: Person Identification**\n"
        "1. Analyze the photo to detect all people present\n"
        "2. Parse the person description to extract identifying characteristics\n"
        "3. Match the description against detected people using position, clothing, features\n"
        "4. Identify the single person that best matches\n\n"
        "**Phase 2: Person Removal**\n"
        "1. Determine the complete area occupied by the target person (body, clothing, accessories)\n"
        "2. Include any shadows or reflections directly caused by that person\n"
        "3. Mark the entire removal region precisely\n\n"
        "**Phase 3: Background Inpainting**\n"
        "1. Analyze surrounding background context (textures, patterns, colors, lighting)\n"
        "2. Generate seamless background fill using content-aware inpainting\n"
        "3. Blend inpainted area with surrounding context to eliminate visible seams\n"
        "4. Ensure lighting, color temperature, and perspective match adjacent areas\n\n"
        "**Phase 4: Preservation Verification**\n"
        "1. Verify all other people remain at original visibility and appearance\n"
        "2. Verify all other scene elements unchanged\n"
        "3. Verify no unintended modifications outside removal region\n\n"
        "## REQUIREMENTS\n\n"
        "**You must:**\n"
        "- Remove ONLY the person matching the description\n"
        "- Remove the person completely (entire visible body)\n"
        "- Fill vacated area with natural background continuation\n"
        "- Inpaint seamlessly so removal is undetectable\n"
        "- Match surrounding textures, patterns, colors, and lighting\n"
        "- Keep all other people completely unchanged and fully visible\n"
        "- Keep the entire rest of the scene unchanged\n"
        "- Output only the edited image\n\n"
        "**You must not:**\n"
        "- Remove more than one person\n"
        "- Remove the wrong person\n"
        "- Leave blank, blurred, or obviously artificial areas\n"
        "- Modify, darken, or alter other people\n"
        "- Change scene elements outside the person's occupied area\n\n"
        "## VERIFICATION CHECKLIST\n"
        "Before output, verify:\n"
        "- Correct person identified from description\n"
        "- ONLY that one person removed\n"
        "- Person completely removed from image\n"
        "- Vacated area filled with seamless background continuation\n"
        "- No visible seams, artifacts, or discontinuities\n"
        "- All other people unchanged and fully visible\n"
        "- Rest of scene unchanged\n"
        "- Photo appears natural as if person was never present\n"
        "- Output contains only the edited image"
    ),
    "analyze_people": (
        "Analyze this photo. List every person visible. For each person give a short "
        "physical description (age range, clothing, position in frame, distinguishing features). "
        "Return JSON: {{\"people\": [\"description1\", \"description2\", ...], "
        "\"quiz\": {{\"question\": \"...\", \"correct\": \"...\", \"wrong\": [\"...\", \"...\"]}}}}"
    ),
}


def load_prompts() -> dict:
    """Load prompts from file, falling back to defaults."""
    prompts = dict(_DEFAULT_PROMPTS)
    try:
        with open(_PROMPTS_FILE) as f:
            saved = _json.load(f)
            prompts.update(saved)
    except (FileNotFoundError, _json.JSONDecodeError):
        pass
    return prompts


def save_prompts(prompts: dict):
    """Save prompts to file."""
    with open(_PROMPTS_FILE, "w") as f:
        _json.dump(prompts, f, indent=2)


# ── rembg mask cache (avoids running rembg twice on same image) ─────────────
_rembg_cache = {}  # {file_path: {"mtime": float, "mask": np.array, "size": (w,h)}}
_REMBG_CACHE_MAX = 5

def _get_rembg_mask(img_rgba):
    """Run rembg and return foreground mask. Uses cache by image size hash."""
    from rembg import remove
    import numpy as np
    fg = remove(img_rgba)
    return np.array(fg)[:, :, 3] > 128

def get_cached_rembg_mask(source_path: str, img_rgba=None):
    """Get rembg mask, using cache if available."""
    import numpy as np
    mtime = os.path.getmtime(source_path)
    if source_path in _rembg_cache and _rembg_cache[source_path]["mtime"] == mtime:
        return _rembg_cache[source_path]["mask"]

    from rembg import remove
    from PIL import Image, ImageOps
    if img_rgba is None:
        img_rgba = ImageOps.exif_transpose(Image.open(source_path)).convert("RGBA")
    fg = remove(img_rgba)
    mask = np.array(fg)[:, :, 3] > 128

    # Keep cache bounded
    if len(_rembg_cache) >= _REMBG_CACHE_MAX:
        oldest = next(iter(_rembg_cache))
        del _rembg_cache[oldest]
    _rembg_cache[source_path] = {"mtime": mtime, "mask": mask}
    return mask


# ── Gemini API usage tracker ────────────────────────────────────────────────
_USAGE_FILE = os.path.join(os.path.expanduser("~/.photoframe"), "gemini_usage.json")

def _load_usage() -> dict:
    try:
        with open(_USAGE_FILE) as f:
            return _json.load(f)
    except (FileNotFoundError, _json.JSONDecodeError):
        return {"daily": {}, "total_calls": 0}

def _save_usage(data: dict):
    os.makedirs(os.path.dirname(_USAGE_FILE), exist_ok=True)
    with open(_USAGE_FILE, "w") as f:
        _json.dump(data, f)

def _strip_markdown_fences(text: str) -> str:
    """Strip markdown code fences (```json ... ```) from Gemini responses."""
    import re
    text = text.strip()
    if text.startswith("```"):
        # Remove opening fence line (```json, ```, etc.)
        text = re.sub(r'^```\w*\s*\n?', '', text)
        # Remove closing fence
        text = re.sub(r'\n?```\s*$', '', text)
    return text.strip()


def track_gemini_call(feature: str = "unknown"):
    """Record one Gemini API call."""
    data = _load_usage()
    today = date.today().isoformat()
    if today not in data.get("daily", {}):
        data.setdefault("daily", {})[today] = {"calls": 0, "features": {}}
    data["daily"][today]["calls"] += 1
    data["daily"][today]["features"][feature] = data["daily"][today]["features"].get(feature, 0) + 1
    data["total_calls"] = data.get("total_calls", 0) + 1
    _save_usage(data)

def get_gemini_usage() -> dict:
    """Return usage stats for the API."""
    data = _load_usage()
    today = date.today().isoformat()
    today_data = data.get("daily", {}).get(today, {"calls": 0, "features": {}})
    # Last 7 days
    week_calls = 0
    for d in sorted(data.get("daily", {}).keys(), reverse=True)[:7]:
        week_calls += data["daily"][d].get("calls", 0)
    return {
        "today": today_data["calls"],
        "today_features": today_data.get("features", {}),
        "week": week_calls,
        "total": data.get("total_calls", 0),
    }

# Register HEIF/HEIC support with Pillow
pillow_heif.register_heif_opener()

# Supported formats
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tiff", ".tif",
                    ".gif", ".webp", ".bmp", ".avif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
ALL_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

# Browser-compatible formats (no conversion needed)
WEB_IMAGE_FORMATS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}
WEB_VIDEO_FORMATS = {".mp4", ".webm"}


def scan_folder(folder_path: str) -> list[str]:
    """Recursively scan a folder for supported media files."""
    files = []
    for root, _, filenames in os.walk(folder_path):
        for fname in filenames:
            ext = Path(fname).suffix.lower()
            if ext in ALL_EXTENSIONS:
                files.append(os.path.join(root, fname))
    files.sort()
    return files


def get_exif_date(file_path: str) -> datetime | None:
    """Extract date taken from EXIF metadata, with macOS and filename fallbacks."""
    # 1. EXIF
    try:
        img = Image.open(file_path)
        exif = img._getexif()
        if exif:
            for tag in [36867, 306]:
                if tag in exif:
                    return datetime.strptime(exif[tag], "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass

    # 2. macOS Spotlight metadata (works even when EXIF stripped)
    try:
        result = subprocess.run(
            ["mdls", "-name", "kMDItemContentCreationDate", "-raw", file_path],
            capture_output=True, text=True, timeout=5
        )
        raw = result.stdout.strip()
        if raw and raw != "(null)":
            dt = datetime.strptime(raw.split("+")[0].strip(), "%Y-%m-%d %H:%M:%S")
            if (datetime.now() - dt).days > 2:
                return dt
    except Exception:
        pass

    # 3. Filename pattern: IMG_20210503, VID_2019-08-14, 2020-12-25, etc.
    try:
        import re
        basename = os.path.basename(file_path)
        # Match YYYYMMDD or YYYY-MM-DD in filename
        m = re.search(r'(20\d{2})[-_]?(\d{2})[-_]?(\d{2})', basename)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 2000 <= y <= 2030 and 1 <= mo <= 12 and 1 <= d <= 31:
                return datetime(y, mo, d)
    except Exception:
        pass

    # 4. File birth time (macOS)
    try:
        stat = os.stat(file_path)
        btime = getattr(stat, 'st_birthtime', None)
        if btime:
            dt = datetime.fromtimestamp(btime)
            if (datetime.now() - dt).days > 2:
                return dt
    except Exception:
        pass

    return None


def get_video_creation_date(file_path: str) -> datetime | None:
    """Extract creation date from video using ffprobe, falling back to macOS mdls."""
    # Try ffprobe creation_time from the original source file
    # For FLO-VIDEO transcoded files, check the original .mov first
    original = file_path
    # If this is a web_media transcoded .mp4, find the original source
    if "web_media" in file_path:
        # Can't determine original from transcoded path; skip ffprobe for these
        pass
    else:
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_entries", "format_tags=creation_time", original],
                capture_output=True, text=True, timeout=10
            )
            import json as _json
            data = _json.loads(result.stdout)
            ct = data.get("format", {}).get("tags", {}).get("creation_time", "")
            if ct:
                dt = datetime.fromisoformat(ct.replace("Z", "+00:00")).replace(tzinfo=None)
                # Reject dates that are within last 48 hours (likely transcode artifact)
                if (datetime.now() - dt).days > 2:
                    return dt
        except Exception:
            pass

    # Fallback: macOS Spotlight metadata (kMDItemContentCreationDate)
    try:
        result = subprocess.run(
            ["mdls", "-name", "kMDItemContentCreationDate", "-raw", file_path],
            capture_output=True, text=True, timeout=5
        )
        raw = result.stdout.strip()
        if raw and raw != "(null)":
            # Format: "2019-04-14 12:44:31 +0000"
            dt = datetime.strptime(raw.split("+")[0].strip(), "%Y-%m-%d %H:%M:%S")
            if (datetime.now() - dt).days > 2:
                return dt
    except Exception:
        pass

    # Last fallback: file birth time
    try:
        stat = os.stat(file_path)
        btime = getattr(stat, 'st_birthtime', None)
        if btime:
            dt = datetime.fromtimestamp(btime)
            if (datetime.now() - dt).days > 2:
                return dt
    except Exception:
        pass

    return None


def get_video_duration(file_path: str) -> float | None:
    """Get video duration using ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", file_path],
            capture_output=True, text=True, timeout=10
        )
        return float(result.stdout.strip())
    except Exception:
        return None


def get_image_dimensions(file_path: str) -> tuple[int, int] | None:
    """Get image width and height."""
    try:
        with Image.open(file_path) as img:
            return img.size
    except Exception:
        return None


def convert_heic_to_jpeg(heic_path: str, output_dir: str) -> str:
    """Convert HEIC/HEIF to JPEG. Returns path to JPEG."""
    name = Path(heic_path).stem
    unique = hashlib.md5(heic_path.encode()).hexdigest()[:8]
    out_path = os.path.join(output_dir, f"{name}_{unique}.jpg")
    if os.path.exists(out_path):
        return out_path
    try:
        img = Image.open(heic_path)
        img = img.convert("RGB")
        img.save(out_path, "JPEG", quality=92)
        return out_path
    except Exception as e:
        print(f"HEIC conversion failed for {heic_path}: {e}")
        return None


def transcode_video(video_path: str, output_dir: str) -> str | None:
    """Transcode video to H.264 MP4 for browser playback."""
    name = Path(video_path).stem
    unique = hashlib.md5(video_path.encode()).hexdigest()[:8]
    out_path = os.path.join(output_dir, f"{name}_{unique}.mp4")
    if os.path.exists(out_path):
        return out_path
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_path,
             "-c:v", "libx264", "-preset", "fast", "-crf", "23",
             "-c:a", "aac", "-b:a", "128k",
             "-movflags", "+faststart", out_path],
            capture_output=True, timeout=300
        )
        if os.path.exists(out_path):
            return out_path
    except Exception as e:
        print(f"Video transcode failed for {video_path}: {e}")
    return None


def generate_thumbnail(file_path: str, thumb_dir: str, media_type: str = "image") -> str | None:
    """Generate a 300px-wide thumbnail."""
    name = Path(file_path).stem
    unique = hashlib.md5(file_path.encode()).hexdigest()[:8]
    out_path = os.path.join(thumb_dir, f"{name}_{unique}_thumb.jpg")
    if os.path.exists(out_path):
        return out_path

    try:
        if media_type == "video":
            # Extract first frame with ffmpeg
            subprocess.run(
                ["ffmpeg", "-y", "-i", file_path, "-frames:v", "1",
                 "-vf", "scale=300:-1", "-update", "1", out_path],
                capture_output=True, timeout=10
            )
            if os.path.exists(out_path):
                return out_path
            return None
        else:
            img = Image.open(file_path)
            img = img.convert("RGB")
            # Respect EXIF orientation
            try:
                from PIL import ImageOps
                img = ImageOps.exif_transpose(img)
            except Exception:
                pass
            ratio = 300 / img.width
            new_size = (300, int(img.height * ratio))
            img = img.resize(new_size, Image.LANCZOS)
            img.save(out_path, "JPEG", quality=80)
            return out_path
    except Exception as e:
        print(f"Thumbnail generation failed for {file_path}: {e}")
        return None


async def import_media_file(file_path: str, source_folder: str,
                            thumb_dir: str, web_media_dir: str) -> Media | None:
    """Import a single media file: detect type, convert if needed, thumbnail."""
    ext = Path(file_path).suffix.lower()

    if ext in IMAGE_EXTENSIONS:
        media_type = MediaType.IMAGE
        fmt = ext.lstrip(".")

        # Convert HEIC/HEIF to JPEG for browser
        web_path = None
        if ext in (".heic", ".heif"):
            web_path = convert_heic_to_jpeg(file_path, web_media_dir)

        # Get dimensions
        dims = get_image_dimensions(web_path or file_path)
        width, height = dims if dims else (None, None)

        # Thumbnail
        thumb_path = generate_thumbnail(web_path or file_path, thumb_dir, "image")

        # EXIF date
        exif_date = get_exif_date(file_path)

        return Media(
            file_path=file_path, filename=os.path.basename(file_path),
            media_type=media_type, format=fmt,
            width=width, height=height,
            thumbnail_path=thumb_path, web_path=web_path,
            exif_date=exif_date, source_folder=source_folder,
        )

    elif ext in VIDEO_EXTENSIONS:
        media_type = MediaType.VIDEO
        fmt = ext.lstrip(".")

        # Transcode if not web-compatible
        web_path = None
        if ext not in WEB_VIDEO_FORMATS:
            web_path = transcode_video(file_path, web_media_dir)

        duration = get_video_duration(file_path)
        thumb_path = generate_thumbnail(file_path, thumb_dir, "video")
        exif_date = get_video_creation_date(file_path)

        return Media(
            file_path=file_path, filename=os.path.basename(file_path),
            media_type=media_type, format=fmt,
            duration=duration,
            thumbnail_path=thumb_path, web_path=web_path,
            exif_date=exif_date, source_folder=source_folder,
        )

    return None


def generate_silhouette(source_path: str, silhouette_dir: str, slide_id: int,
                        mode: str = "local") -> str | None:
    """Generate a silhouette: person segmented and filled black, background kept.
    mode: 'local' (rembg) or 'gemini' (artistic via Gemini API)."""
    if mode == "gemini":
        return generate_silhouette_gemini(source_path, silhouette_dir, slide_id)
    return generate_silhouette_local(source_path, silhouette_dir, slide_id)


def generate_silhouette_local(source_path: str, silhouette_dir: str, slide_id: int) -> str | None:
    """Generate a silhouette using local rembg (U2-Net).
    Returns path on success, raises ValueError if no person detected, None on error."""
    out_path = os.path.join(silhouette_dir, f"silhouette_{slide_id}.png")
    if os.path.exists(out_path):
        os.remove(out_path)  # Regenerate

    try:
        from rembg import remove
        import numpy as np
        from PIL import ImageOps

        img = ImageOps.exif_transpose(Image.open(source_path)).convert("RGBA")

        # rembg removes background, keeping person
        # We want the opposite: keep background, black out person
        # Step 1: Get the person mask (foreground)
        fg = remove(img)  # Returns image with transparent background (person kept)

        # Step 2: Create person mask from alpha channel
        fg_array = np.array(fg)
        person_mask = fg_array[:, :, 3] > 128  # Where person is

        # Check if enough pixels were detected as a person (> 1% of image)
        person_ratio = person_mask.sum() / person_mask.size
        if person_ratio < 0.01:
            raise ValueError("no_person_detected")

        # Step 3: Original image with person blacked out
        original = np.array(img.convert("RGBA"))
        result = original.copy()
        result[person_mask] = [0, 0, 0, 255]  # Black out person

        result_img = Image.fromarray(result).convert("RGB")
        result_img.save(out_path, "PNG")
        return out_path

    except ValueError:
        raise  # Re-raise person detection warning
    except Exception as e:
        print(f"Silhouette generation (local) failed: {e}")
        return None


def silhouette_person_local(source_path: str, person_index: int, output_dir: str, slide_id: int,
                            positions: list | None = None) -> str | None:
    """Silhouette ONE specific person using local rembg + connected component analysis.
    person_index: 0-based index of the person sorted left-to-right.
    positions: optional list of {"center_x": %, "center_y": %} from Gemini analysis.
    Returns path to edited image or None on error."""
    out_path = os.path.join(output_dir, f"silhouette_{slide_id}.png")
    if os.path.exists(out_path):
        os.remove(out_path)

    try:
        import numpy as np
        from PIL import ImageOps
        import time
        t0 = time.time()

        img = Image.open(source_path).convert("RGBA")
        img = ImageOps.exif_transpose(img)
        h, w = np.array(img).shape[:2]

        # Step 1: rembg segments all foreground (uses cache if available)
        full_mask = get_cached_rembg_mask(source_path, img)
        print(f"[LOCAL SILHOUETTE] rembg mask: {(time.time()-t0)*1000:.0f}ms")

        person_ratio = full_mask.sum() / full_mask.size
        if person_ratio < 0.01:
            raise ValueError("no_person_detected")

        # Step 2: Connected components to separate individual people
        from scipy import ndimage
        labeled, num_features = ndimage.label(full_mask)
        print(f"[LOCAL SILHOUETTE] Found {num_features} connected component(s)")

        if num_features == 0:
            raise ValueError("no_person_detected")

        # Filter out small noise (< 1% of largest component)
        component_sizes = ndimage.sum(full_mask, labeled, range(1, num_features + 1))
        min_size = max(component_sizes) * 0.01
        valid_components = []
        for i in range(1, num_features + 1):
            if component_sizes[i - 1] >= min_size:
                ys, xs = np.where(labeled == i)
                cx = xs.mean()
                valid_components.append((i, cx, component_sizes[i - 1]))

        # Sort by center-x (left to right)
        valid_components.sort(key=lambda c: c[1])
        print(f"[LOCAL SILHOUETTE] {len(valid_components)} valid region(s), "
              f"requested index={person_index}")

        num_people = len(positions) if positions else 1
        target_mask = None

        if len(valid_components) >= num_people and num_people > 1:
            # Components cleanly separate — use index directly
            idx = min(person_index, len(valid_components) - 1)
            target_mask = labeled == valid_components[idx][0]
            print(f"[LOCAL SILHOUETTE] Using component {idx} (cleanly separated)")

        elif num_people > 1 and positions and len(valid_components) < num_people:
            # People are touching — use Voronoi split based on Gemini positions
            print(f"[LOCAL SILHOUETTE] People overlap — using Voronoi split with {len(positions)} centers")
            # Convert percentage positions to pixel coordinates
            centers = []
            for pos in positions:
                px = int(pos.get("center_x", 50) / 100 * w)
                py = int(pos.get("center_y", 50) / 100 * h)
                centers.append((py, px))  # (row, col)

            # For each foreground pixel, assign to nearest center
            fg_ys, fg_xs = np.where(full_mask)
            if len(fg_ys) > 0:
                # Compute distance from each fg pixel to each center
                fg_coords = np.stack([fg_ys, fg_xs], axis=1)  # (N, 2)
                centers_arr = np.array(centers)  # (K, 2)
                # Broadcast distance calculation
                dists = np.linalg.norm(
                    fg_coords[:, None, :] - centers_arr[None, :, :], axis=2
                )  # (N, K)
                assignments = np.argmin(dists, axis=1)  # (N,)

                # Create mask for target person
                target_idx = min(person_index, len(centers) - 1)
                target_pixels = assignments == target_idx
                target_mask = np.zeros_like(full_mask)
                target_mask[fg_ys[target_pixels], fg_xs[target_pixels]] = True
                print(f"[LOCAL SILHOUETTE] Voronoi assigned {target_pixels.sum()} pixels to person {target_idx}")

        if target_mask is None:
            # Single person or no position data — use full mask
            if len(valid_components) == 1:
                target_mask = labeled == valid_components[0][0]
            else:
                idx = min(person_index, len(valid_components) - 1)
                target_mask = labeled == valid_components[idx][0]

        # Step 3: Black out only the target person, keep everything else
        original = np.array(img.convert("RGBA"))
        result = original.copy()
        result[target_mask] = [0, 0, 0, 255]  # Pure black

        blacked_pixels = target_mask.sum()
        total_fg = full_mask.sum()
        print(f"[LOCAL SILHOUETTE] Blacked {blacked_pixels} pixels "
              f"({blacked_pixels/full_mask.size*100:.1f}% of image, "
              f"{blacked_pixels/total_fg*100:.0f}% of foreground)")

        result_img = Image.fromarray(result).convert("RGB")
        result_img.save(out_path, "PNG")
        return out_path

    except ValueError:
        raise
    except Exception as e:
        print(f"[LOCAL SILHOUETTE] Failed: {e}")
        import traceback
        traceback.print_exc()
        return None


def _validate_silhouette(original_path: str, generated_path: str) -> bool:
    """Validate that the generated image looks like a silhouette of the original.
    Checks: similar dimensions, contains significant black regions, background preserved."""
    try:
        import numpy as np
        orig = np.array(Image.open(original_path).convert("RGB"))
        gen = np.array(Image.open(generated_path).convert("RGB"))

        # Check dimensions are reasonably similar (within 2x)
        orig_area = orig.shape[0] * orig.shape[1]
        gen_area = gen.shape[0] * gen.shape[1]
        if gen_area < orig_area * 0.25 or gen_area > orig_area * 4:
            print("Silhouette validation failed: size mismatch")
            return False

        # Check for significant black regions (silhouette pixels)
        # Black = all channels < 30
        black_mask = (gen[:, :, 0] < 30) & (gen[:, :, 1] < 30) & (gen[:, :, 2] < 30)
        black_ratio = black_mask.sum() / black_mask.size
        if black_ratio < 0.01:
            print(f"Silhouette validation failed: only {black_ratio:.1%} black pixels")
            return False

        print(f"Silhouette validation passed: {black_ratio:.1%} black pixels")
        return True
    except Exception as e:
        print(f"Silhouette validation error: {e}")
        return True  # Don't block on validation errors


def _gemini_generate_image(img_b64: str, api_key: str, prompt: str, model: str) -> bytes | None:
    """Call Gemini API to generate/edit an image. Returns image bytes or None."""
    import json
    import urllib.request
    import urllib.error
    import ssl

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )

    payload = {
        "contents": [{
            "parts": [
                {"inlineData": {"mimeType": "image/jpeg", "data": img_b64}},
                {"text": prompt},
            ]
        }],
        "generationConfig": {
            "responseModalities": ["IMAGE", "TEXT"],
            "temperature": 0.2,
        }
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        import certifi
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ssl_ctx = ssl.create_default_context()

    import base64 as b64mod
    track_gemini_call("image_generation")
    with urllib.request.urlopen(req, timeout=120, context=ssl_ctx) as resp:
        result = json.loads(resp.read().decode("utf-8"))

    candidates = result.get("candidates", [])
    for candidate in candidates:
        parts = candidate.get("content", {}).get("parts", [])
        for part in parts:
            inline = part.get("inlineData", {})
            if inline.get("mimeType", "").startswith("image/"):
                return b64mod.b64decode(inline["data"])
    return None


# Prompt variants — try primary first, retry with alternate if validation fails
_SILHOUETTE_PROMPTS = [
    (
        "Edit this photo: replace every person and human figure with a solid pure black "
        "(#000000) filled silhouette shape. Preserve their exact outline, pose, and position. "
        "Keep the entire background completely unchanged — same colors, lighting, objects. "
        "Do NOT darken, blur, or modify the background in any way. "
        "Output only the edited image."
    ),
    (
        "I need this photo edited for a guessing game. Paint over every human/person in the "
        "image with solid opaque black (#000000), creating a silhouette. The silhouette must "
        "follow the person's body outline precisely. The background behind and around the "
        "people must remain exactly as in the original photo — no changes to background at all. "
        "Return only the edited image."
    ),
]

# Models to try in order (image editing/generation capable)
_GEMINI_MODELS = [
    "gemini-2.5-flash-image",
    "gemini-2.0-flash",
]


def generate_silhouette_gemini(source_path: str, silhouette_dir: str, slide_id: int) -> str | None:
    """Generate an artistic silhouette using Gemini API with image generation.
    Tries multiple prompts and validates results. Falls back to local on failure."""
    import base64
    import io

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Gemini API key not configured, falling back to local")
        return generate_silhouette_local(source_path, silhouette_dir, slide_id)

    out_path = os.path.join(silhouette_dir, f"silhouette_{slide_id}.png")
    if os.path.exists(out_path):
        os.remove(out_path)

    try:
        # Prepare the image
        img = Image.open(source_path).convert("RGB")
        max_dim = 1024
        if max(img.size) > max_dim:
            ratio = max_dim / max(img.size)
            img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        img_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        # Try each model + prompt combination
        for model in _GEMINI_MODELS:
            for prompt in _SILHOUETTE_PROMPTS:
                try:
                    print(f"Gemini silhouette: trying {model}...")
                    img_data = _gemini_generate_image(img_b64, api_key, prompt, model)
                    if not img_data:
                        continue

                    # Save and validate
                    gen_img = Image.open(io.BytesIO(img_data)).convert("RGB")
                    gen_img.save(out_path, "PNG")

                    if _validate_silhouette(source_path, out_path):
                        print(f"Gemini silhouette generated with {model}: {out_path}")
                        return out_path

                    print(f"Validation failed with {model}, trying next...")
                    os.remove(out_path)
                except Exception as e:
                    print(f"Gemini attempt failed ({model}): {e}")
                    continue

        print("All Gemini attempts failed, falling back to local")
        return generate_silhouette_local(source_path, silhouette_dir, slide_id)

    except Exception as e:
        print(f"Gemini silhouette generation failed: {e}, falling back to local")
        return generate_silhouette_local(source_path, silhouette_dir, slide_id)


def estimate_photo_year_gemini(file_path: str) -> int | None:
    """Use Gemini API to estimate the year a photo was taken based on visual content.
    Returns estimated year (e.g. 2008) or None on failure."""
    import base64
    import json
    import urllib.request
    import urllib.error
    import ssl
    import io

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None

    try:
        img = Image.open(file_path).convert("RGB")
        # Resize small for fast API call
        max_dim = 512
        if max(img.size) > max_dim:
            ratio = max_dim / max(img.size)
            img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        img_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-2.5-flash:generateContent"
            f"?key={api_key}"
        )

        payload = {
            "contents": [{
                "parts": [
                    {"inlineData": {"mimeType": "image/jpeg", "data": img_b64}},
                    {"text": (
                        "Estimate what year this photo was most likely taken. "
                        "Consider clothing, hairstyles, image quality, devices visible, "
                        "and any other visual clues. "
                        "Reply with ONLY a single 4-digit year number, nothing else. "
                        "Example: 2007"
                    )},
                ]
            }],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 256},
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )

        try:
            import certifi
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            ssl_ctx = ssl.create_default_context()

        track_gemini_call("year_estimate")
        with urllib.request.urlopen(req, timeout=15, context=ssl_ctx) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        # Extract text response
        candidates = result.get("candidates", [])
        for candidate in candidates:
            parts = candidate.get("content", {}).get("parts", [])
            for part in parts:
                text = part.get("text", "").strip()
                # Extract 4-digit year
                import re
                m = re.search(r'(19[5-9]\d|20[0-3]\d)', text)
                if m:
                    year = int(m.group(1))
                    print(f"Gemini estimated year for {os.path.basename(file_path)}: {year}")
                    return year
        return None
    except Exception as e:
        print(f"Gemini date estimation failed for {file_path}: {e}")
        return None


def analyze_people_gemini(file_path: str) -> dict | None:
    """Use Gemini to identify people in a photo and suggest quiz answers.
    Returns {"people": [...], "quiz": {"correct": str, "wrong": [str, str]}} or None."""
    import base64
    import json
    import urllib.request
    import ssl
    import io

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None

    try:
        img = Image.open(file_path).convert("RGB")
        max_dim = 768
        if max(img.size) > max_dim:
            ratio = max_dim / max(img.size)
            img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        img_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-2.5-flash:generateContent"
            f"?key={api_key}"
        )

        payload = {
            "contents": [{
                "parts": [
                    {"inlineData": {"mimeType": "image/jpeg", "data": img_b64}},
                    {"text": (
                        "Analyze this photo for a party guessing game. "
                        "List each person visible with a short description (age range, clothing, position). "
                        "For each person, provide their approximate center position as percentage of image "
                        "width (center_x) and height (center_y), where 0=left/top, 100=right/bottom. "
                        "Order people from LEFT to RIGHT. "
                        "Then suggest a quiz question: pick one person as the correct answer and provide "
                        "2 plausible wrong answers (other people in the photo, or invented if only 1 person). "
                        "Reply ONLY with valid JSON, no markdown, in this exact format:\n"
                        '{"people": [{"desc": "description1", "center_x": 30, "center_y": 50}, '
                        '{"desc": "description2", "center_x": 70, "center_y": 50}], '
                        '"quiz": {"question": "Who is behind the silhouette?", '
                        '"correct": "short name/description", '
                        '"wrong": ["wrong1", "wrong2"]}}'
                    )},
                ]
            }],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 8192},
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )

        try:
            import certifi
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            ssl_ctx = ssl.create_default_context()

        track_gemini_call("people_analysis")
        with urllib.request.urlopen(req, timeout=30, context=ssl_ctx) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        candidates = result.get("candidates", [])
        for candidate in candidates:
            parts = candidate.get("content", {}).get("parts", [])
            for part in parts:
                text = part.get("text", "").strip()
                text = _strip_markdown_fences(text)
                try:
                    data = json.loads(text)
                    if "quiz" in data and "correct" in data["quiz"]:
                        # Ensure exactly 2 wrong answers
                        wrong = data["quiz"].get("wrong", [])[:2]
                        while len(wrong) < 2:
                            wrong.append("Someone else")
                        data["quiz"]["wrong"] = wrong
                        # Normalize people format: support both string[] and object[]
                        people = data.get("people", [])
                        normalized_people = []
                        positions = []
                        for p in people:
                            if isinstance(p, dict):
                                normalized_people.append(p.get("desc", str(p)))
                                positions.append({
                                    "center_x": p.get("center_x", 50),
                                    "center_y": p.get("center_y", 50)
                                })
                            else:
                                normalized_people.append(str(p))
                                positions.append({"center_x": 50, "center_y": 50})
                        data["people"] = normalized_people
                        data["positions"] = positions
                        return data
                    else:
                        print(f"Gemini people analysis: unexpected JSON structure: {text[:300]}")
                except json.JSONDecodeError:
                    print(f"Gemini people analysis: could not parse JSON: {text[:300]}")
        return None
    except Exception as e:
        print(f"Gemini people analysis failed for {file_path}: {e}")
        import traceback; traceback.print_exc()
        return None


def analyze_zoom_gemini(file_path: str) -> dict | None:
    """Use Gemini to find an interesting detail in a photo for a Zoom In quiz.
    Returns {"detail": "description", "bbox": {"x": %, "y": %, "w": %, "h": %},
             "quiz": {"correct": str, "wrong": ["w1", "w2"]}} or None."""
    import base64
    import json
    import urllib.request
    import ssl
    import io

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None

    try:
        img = Image.open(file_path).convert("RGB")
        max_dim = 768
        if max(img.size) > max_dim:
            ratio = max_dim / max(img.size)
            img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        img_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-2.5-flash:generateContent"
            f"?key={api_key}"
        )

        payload = {
            "contents": [{
                "parts": [
                    {"inlineData": {"mimeType": "image/jpeg", "data": img_b64}},
                    {"text": (
                        "Analyze this photo for a party guessing game called 'Zoom In'. "
                        "Find one interesting, recognizable detail (a face, an object, a piece of clothing, "
                        "a logo, a toy, food, etc.) that would be fun to guess when shown as a tight crop. "
                        "Return the bounding box as percentages of the image (0-100). "
                        "Also suggest a quiz: the correct answer is what the detail shows, "
                        "plus 2 plausible wrong answers. "
                        "Reply ONLY with valid JSON, no markdown:\n"
                        '{"detail": "short description of the detail", '
                        '"bbox": {"x": 30, "y": 20, "w": 25, "h": 25}, '
                        '"quiz": {"correct": "what it is", "wrong": ["wrong1", "wrong2"]}}'
                    )},
                ]
            }],
            "generationConfig": {"temperature": 0.5, "maxOutputTokens": 8192},
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )

        try:
            import certifi
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            ssl_ctx = ssl.create_default_context()

        track_gemini_call("zoom_analysis")
        with urllib.request.urlopen(req, timeout=30, context=ssl_ctx) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        candidates = result.get("candidates", [])
        for candidate in candidates:
            parts = candidate.get("content", {}).get("parts", [])
            for part in parts:
                text = _strip_markdown_fences(part.get("text", ""))
                try:
                    data = json.loads(text)
                    if "bbox" in data and "quiz" in data:
                        wrong = data["quiz"].get("wrong", [])[:2]
                        while len(wrong) < 2:
                            wrong.append("Something else")
                        data["quiz"]["wrong"] = wrong
                        return data
                except json.JSONDecodeError:
                    pass
        return None
    except Exception as e:
        print(f"Gemini zoom analysis failed for {file_path}: {e}")
        return None


def generate_zoom_crop(source_path: str, bbox: dict, output_dir: str, slide_id: int) -> str | None:
    """Crop a region of the image based on bbox percentages.
    bbox: {"x": %, "y": %, "w": %, "h": %} where values are 0-100.
    Returns path to cropped image or None."""
    try:
        os.makedirs(output_dir, exist_ok=True)
        img = Image.open(source_path).convert("RGB")
        w, h = img.size

        # Convert percentages to pixels
        x_pct = max(0, min(100, bbox.get("x", 0)))
        y_pct = max(0, min(100, bbox.get("y", 0)))
        w_pct = max(5, min(100, bbox.get("w", 25)))
        h_pct = max(5, min(100, bbox.get("h", 25)))

        left = int(w * x_pct / 100)
        top = int(h * y_pct / 100)
        crop_w = int(w * w_pct / 100)
        crop_h = int(h * h_pct / 100)

        # Ensure crop stays within bounds
        left = min(left, w - 10)
        top = min(top, h - 10)
        right = min(left + crop_w, w)
        bottom = min(top + crop_h, h)

        cropped = img.crop((left, top, right, bottom))

        # Resize to a decent display size (min 400px on shortest side)
        min_side = min(cropped.size)
        if min_side < 400:
            scale = 400 / min_side
            cropped = cropped.resize(
                (int(cropped.width * scale), int(cropped.height * scale)),
                Image.LANCZOS
            )

        out_path = os.path.join(output_dir, f"zoom_{slide_id}.jpg")
        cropped.save(out_path, "JPEG", quality=90)
        return out_path
    except Exception as e:
        print(f"Zoom crop failed: {e}")
        return None


def estimate_year_from_ages_gemini(file_path: str, family_context: str) -> dict | None:
    """Use Gemini to estimate ages of people in a photo and cross-reference with
    known family member birth years to deduce the year the photo was taken.

    Args:
        file_path: Path to the image file.
        family_context: Text describing family members and birth years,
            e.g. "Marie born 1985, Jean born 1950, Flo born 1990"

    Returns: {"year": int, "confidence": str, "reasoning": str} or None.
    """
    import base64
    import json
    import urllib.request
    import ssl
    import io

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None

    try:
        img = Image.open(file_path).convert("RGB")
        max_dim = 768
        if max(img.size) > max_dim:
            ratio = max_dim / max(img.size)
            img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75)
        img_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-2.5-flash:generateContent"
            f"?key={api_key}"
        )

        prompt = (
            "You are an expert at estimating when a photo was taken.\n\n"
            "STEP 1 — Estimate the apparent age of EVERY person visible in this photo.\n"
            "STEP 2 — Look at visual clues: clothing style, hairstyles, image quality/grain, "
            "technology visible (phones, TVs, cars), furniture, decorations, photo format.\n"
            "STEP 3 — Cross-reference with these known family members and their birth years:\n"
            f"{family_context}\n\n"
            "STEP 4 — For each person you can match to a family member, compute: "
            "birth_year + apparent_age = estimated photo year. "
            "Average these estimates and reconcile with the visual clues from step 2.\n\n"
            "Reply ONLY with valid JSON (no markdown fences), in this exact format:\n"
            '{"year": 2004, "confidence": "high", '
            '"people_seen": [{"apparent_age": 19, "match": "Flo", "computed_year": 2009}], '
            '"reasoning": "brief explanation"}\n\n'
            "confidence must be one of: high, medium, low.\n"
            "If you cannot estimate at all, reply: {\"year\": null, \"confidence\": \"low\", "
            "\"people_seen\": [], \"reasoning\": \"reason\"}"
        )

        payload = {
            "contents": [{
                "parts": [
                    {"inlineData": {"mimeType": "image/jpeg", "data": img_b64}},
                    {"text": prompt},
                ]
            }],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8192},
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )

        try:
            import certifi
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            ssl_ctx = ssl.create_default_context()

        track_gemini_call("age_date_estimate")
        with urllib.request.urlopen(req, timeout=30, context=ssl_ctx) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        candidates = result.get("candidates", [])
        for candidate in candidates:
            parts = candidate.get("content", {}).get("parts", [])
            for part in parts:
                text = part.get("text", "").strip()
                text = _strip_markdown_fences(text)
                try:
                    data = json.loads(text)
                    year = data.get("year")
                    if year and isinstance(year, (int, float)):
                        year = int(year)
                        if 1950 <= year <= 2030:
                            print(f"Age-based estimate for {os.path.basename(file_path)}: "
                                  f"{year} ({data.get('confidence', '?')}) — {data.get('reasoning', '')[:80]}")
                            return {
                                "year": year,
                                "confidence": data.get("confidence", "low"),
                                "reasoning": data.get("reasoning", ""),
                                "people_seen": data.get("people_seen", []),
                            }
                    elif year is None:
                        print(f"Gemini could not estimate year for {os.path.basename(file_path)}: "
                              f"{data.get('reasoning', 'unknown')[:80]}")
                        return None
                except json.JSONDecodeError:
                    pass
        return None
    except Exception as e:
        print(f"Gemini age-based estimation failed for {file_path}: {e}")
        return None


def remove_person_gemini(source_path: str, person_description: str, output_dir: str, slide_id: int) -> str | None:
    """Use Gemini to remove a specific person from a photo via inpainting.
    Returns path to edited image or None on failure."""
    import base64
    import io

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None

    out_path = os.path.join(output_dir, f"missing_{slide_id}.png")
    if os.path.exists(out_path):
        os.remove(out_path)

    try:
        img = Image.open(source_path).convert("RGB")
        max_dim = 1024
        if max(img.size) > max_dim:
            ratio = max_dim / max(img.size)
            img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        img_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        prompts = load_prompts()
        prompt = prompts.get("missing", _DEFAULT_PROMPTS["missing"]).replace("{person}", person_description)

        for model in _GEMINI_MODELS:
            try:
                print(f"Gemini remove person: trying {model}...")
                img_data = _gemini_generate_image(img_b64, api_key, prompt, model)
                if not img_data:
                    continue
                gen_img = Image.open(io.BytesIO(img_data)).convert("RGB")
                gen_img.save(out_path, "PNG")
                print(f"Person removed with {model}: {out_path}")
                return out_path
            except Exception as e:
                print(f"Gemini remove attempt failed ({model}): {e}")
                continue

        print("All Gemini remove attempts failed")
        return None
    except Exception as e:
        print(f"Gemini person removal failed: {e}")
        return None


def silhouette_person_gemini(source_path: str, person_description: str, output_dir: str, slide_id: int) -> str | None:
    """Use Gemini to silhouette ONLY a specific person (solid black), keeping others visible.
    Returns path to edited image or None on failure."""
    import base64
    import io

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None

    out_path = os.path.join(output_dir, f"silhouette_{slide_id}.png")
    if os.path.exists(out_path):
        os.remove(out_path)

    try:
        img = Image.open(source_path).convert("RGB")
        max_dim = 1024
        if max(img.size) > max_dim:
            ratio = max_dim / max(img.size)
            img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        img_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        prompts = load_prompts()
        prompt = prompts.get("shadow", _DEFAULT_PROMPTS["shadow"]).replace("{person}", person_description)

        for model in _GEMINI_MODELS:
            try:
                print(f"Gemini silhouette person: trying {model}...")
                img_data = _gemini_generate_image(img_b64, api_key, prompt, model)
                if not img_data:
                    continue
                gen_img = Image.open(io.BytesIO(img_data)).convert("RGB")

                # POST-PROCESS: Force any changed pixels to pure black (#000000)
                # Gemini often darkens instead of fully blacking out
                import numpy as np
                orig_arr = np.array(img.resize(gen_img.size))
                gen_arr = np.array(gen_img)
                # Pixels that changed significantly from original
                diff = np.abs(gen_arr.astype(int) - orig_arr.astype(int)).mean(axis=2)
                changed_mask = diff > 15  # threshold: pixel changed meaningfully
                # Also catch any very dark pixels Gemini created (brightness < 80)
                dark_mask = gen_arr.mean(axis=2) < 80
                # Combine: pixels that changed AND got darker
                silhouette_mask = changed_mask & dark_mask
                # Force those pixels to pure black
                gen_arr[silhouette_mask] = [0, 0, 0]
                gen_img = Image.fromarray(gen_arr)
                forced_black = silhouette_mask.sum()
                print(f"Post-process: forced {forced_black} pixels to pure black")

                gen_img.save(out_path, "PNG")
                print(f"Person silhouetted with {model}: {out_path}")
                return out_path
            except Exception as e:
                print(f"Gemini silhouette attempt failed ({model}): {e}")
                continue

        print("All Gemini silhouette person attempts failed")
        return None
    except Exception as e:
        print(f"Gemini person silhouette failed: {e}")
        return None


def list_apple_photos_albums() -> list[dict]:
    """List albums from Apple Photos via AppleScript."""
    script = '''
    tell application "Photos"
        set albumList to {}
        repeat with a in albums
            set end of albumList to {name of a, count of media items of a}
        end repeat
        return albumList
    end tell
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            raise RuntimeError(f"AppleScript error: {result.stderr}")

        # Parse output: {{name, count}, {name, count}, ...}
        raw = result.stdout.strip()
        albums = []
        if raw:
            # Simple parsing of AppleScript list output
            parts = raw.split(", ")
            i = 0
            while i < len(parts) - 1:
                name = parts[i].strip()
                count = parts[i+1].strip()
                try:
                    count = int(count)
                except ValueError:
                    count = 0
                albums.append({"name": name, "count": count})
                i += 2
        return albums
    except FileNotFoundError:
        raise RuntimeError("Apple Photos is not available")
    except subprocess.TimeoutExpired:
        raise RuntimeError("Apple Photos took too long to respond. Is it open?")


def import_apple_photos_album(album_name: str, export_dir: str) -> list[str]:
    """Export photos from an Apple Photos album to a local directory."""
    album_dir = os.path.join(export_dir, album_name.replace("/", "_"))
    os.makedirs(album_dir, exist_ok=True)

    script = f'''
    tell application "Photos"
        set theAlbum to album "{album_name}"
        set mediaItems to media items of theAlbum
        set exportPaths to {{}}
        export mediaItems to POSIX file "{album_dir}" with using originals
        return count of mediaItems
    end tell
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            raise RuntimeError(f"Export failed: {result.stderr}")

        # Return all files in the export directory
        return scan_folder(album_dir)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Export took too long")
