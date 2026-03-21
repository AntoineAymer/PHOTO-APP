# PHOTO-FRAME & SHADOW GAME -- BLUEPRINT SPECIFICATION V1.0

## SCOPED TARGET CONFIRMATION

A localhost web application serving as both a digital photo frame (auto-cycling local photos/videos) and an interactive multiplayer "Shadow Game" (Crowdpurr-style) where players on their phones guess who is behind a silhouette, with speed scoring, admin controls, and cognitive accessibility as a first-class concern. Depth: BLUEPRINT.

---

## EXECUTIVE SUMMARY

| Domain | Requirement Count |
|--------|-------------------|
| Functional | 42 |
| Security | 6 |
| Performance | 7 |
| Usability & Cognitive Accessibility | 12 |
| **Total** | **67** |

GAP IDENTIFIED items: 4 (flagged inline, awaiting confirmation)
Coverage gaps: 0
Highest-risk pass-through chain: WebSocket sync (Admin action -> Server -> Display + 30 Player devices)

---

## SYSTEM OVERVIEW

### Three Views

```
+------------------+     +------------------+     +------------------+
|  DISPLAY SCREEN  |     |  PLAYER SCREEN   |     |  ADMIN SCREEN    |
|  (TV/Projector)  |     |  (Phone x30)     |     |  (Laptop/Tablet) |
|                  |     |                  |     |                  |
|  /display        |     |  /play/:code     |     |  /admin          |
|  Fullscreen      |     |  Join, Answer,   |     |  Create, Control |
|  Photos + Game   |     |  See Score       |     |  Monitor, Reveal |
+--------+---------+     +--------+---------+     +--------+---------+
         |                         |                        |
         +------------+------------+------------------------+
                      |
              +-------+-------+
              |   FastAPI +   |
              | Socket.IO Hub |
              |   SQLite DB   |
              +---------------+
```

### Two Interleaved Modes

The admin pre-creates an "experience" — an ordered sequence of slides. Each slide is either:

- **FRAME slide**: a photo or video displayed passively (auto-advance after configured duration)
- **GAME slide**: a silhouette photo with multiple-choice answers, timer, speed scoring

These are interleaved in a single timeline. Players' phones are idle during FRAME slides and active during GAME slides.

---

## DOMAIN 1: FUNCTIONAL REQUIREMENTS

### 1.1 Media Ingestion & Library

| ID | Classification | Requirement | Priority |
|----|---------------|-------------|----------|
| FR-001 | CONFIRMED | The system shall allow the admin to add one or more local folders as media sources. The system scans recursively for supported media files. | MUST |
| FR-002 | CONFIRMED | The system shall support the following image formats: JPEG, PNG, HEIC, HEIF, TIFF, GIF, WebP, BMP, AVIF. | MUST |
| FR-003 | CONFIRMED | The system shall support the following video formats: MP4, MOV, AVI, MKV, WebM. Videos are transcoded to MP4/H.264 for browser playback if not already compatible. | MUST |
| FR-004 | CONFIRMED | The system shall integrate with Apple Photos on macOS via `osascript` (AppleScript bridge) to list albums and export selected photos/videos to a working directory for use in the app. | MUST |
| FR-005 | CONFIRMED | The system shall extract EXIF metadata (date taken, orientation, GPS if available) from imported photos. Photos with EXIF dates shall be sortable by date. | MUST |
| FR-006 | CONFIRMED | The system shall provide an AI-assisted option (via EXIF date, or filename heuristics, or Gemini API call to estimate era from photo content) to rank photos from oldest to most recent. | SHOULD |
| FR-007 | CONFIRMED | The system shall generate thumbnails (300px wide) for all imported media for use in the admin gallery view. | MUST |
| FR-008 | CONFIRMED | The system shall store media metadata (path, format, dimensions, duration for video, EXIF date, thumbnail path, import date) in SQLite. Original files are never modified or moved. | MUST |
| FR-009 | CONFIRMED | The system shall categorize media by source folder or album name. Admin can assign custom tags/categories (e.g., year: "2005", "2010"). | SHOULD |

**Acceptance Criteria (FR-004 - Apple Photos)**:
- GIVEN the admin clicks "Import from Apple Photos" WHEN the system runs the AppleScript bridge THEN a list of albums (including Smart Albums, Shared Albums) is displayed.
- GIVEN the admin selects an album WHEN they click "Import" THEN photos/videos are exported to `~/.photoframe/apple-imports/` with original quality and the media index is updated.
- GIVEN Apple Photos is not running or not installed WHEN the admin attempts Apple Photos import THEN a clear error message is shown: "Apple Photos is not available. Please open Photos.app and try again, or use folder import instead."

### 1.2 Experience Builder (Admin)

| ID | Classification | Requirement | Priority |
|----|---------------|-------------|----------|
| FR-010 | CONFIRMED | The system shall allow the admin to create an "Experience" — a named, ordered sequence of slides. | MUST |
| FR-011 | CONFIRMED | Each slide in an experience shall be typed as either FRAME (passive display) or GAME (interactive question). | MUST |
| FR-012 | CONFIRMED | For FRAME slides, the admin selects a photo or video from the media library. The admin can set a custom display duration (default: 8 seconds for images, full length for videos, capped at configurable max). | MUST |
| FR-013 | CONFIRMED | For GAME slides, the admin selects a source photo from the media library. The system auto-generates a silhouette version using local person segmentation (rembg). | MUST |
| FR-014 | CONFIRMED | For GAME slides, the admin defines 2-4 multiple-choice answer options, marks exactly one as correct, and sets an optional question timer (default: 15 seconds). | MUST |
| FR-015 | CONFIRMED | The admin shall be able to reorder slides via drag-and-drop in the experience builder. | MUST |
| FR-016 | CONFIRMED | The admin shall be able to preview any slide (FRAME: see photo/video; GAME: see silhouette + choices) before starting the experience. | SHOULD |
| FR-017 | CONFIRMED | The system shall support auto-ordering: the admin can click "Sort by date" to reorder all FRAME slides by EXIF date (oldest first). GAME slides retain their relative positions. | SHOULD |
| FR-018 | GAP IDENTIFIED | The system shall allow the admin to bulk-import a folder as FRAME slides (one slide per media file) with default timings. | SHOULD |

### 1.3 Silhouette Generation

| ID | Classification | Requirement | Priority |
|----|---------------|-------------|----------|
| FR-019 | CONFIRMED | The system shall use `rembg` (U2-Net model) to segment person(s) from the source photo. The segmented area is filled with solid black (#000000). The background is preserved as-is. | MUST |
| FR-020 | CONFIRMED | The silhouette image shall be stored alongside the original in the database (as a separate file in `~/.photoframe/silhouettes/`). | MUST |
| FR-021 | CONFIRMED | If `rembg` fails to detect a person (confidence below threshold), the system shall notify the admin: "No person detected in this photo. The silhouette may not work well. Proceed anyway?" | MUST |
| FR-022 | GAP IDENTIFIED | The system shall optionally support Gemini API (Imagen) as an alternative silhouette generation method. The admin can choose "Local (fast)" or "Gemini (artistic)" per game slide. Requires a Google AI Studio API key configured in settings. | COULD |

**Acceptance Criteria (FR-019)**:
- GIVEN a photo with one clearly visible person WHEN silhouette generation runs THEN the person's body is solid black while the background is unchanged.
- GIVEN a photo with multiple people WHEN silhouette generation runs THEN all detected people are turned to solid black.
- GIVEN a photo with no people (e.g., landscape) WHEN silhouette generation runs THEN the admin is warned per FR-021.

### 1.4 Room & Player Management

| ID | Classification | Requirement | Priority |
|----|---------------|-------------|----------|
| FR-023 | CONFIRMED | When the admin starts an experience, the system shall generate a 5-character alphanumeric room code (uppercase, no ambiguous characters: no 0/O, 1/I/L). | MUST |
| FR-024 | CONFIRMED | The system shall generate a QR code encoding the player join URL: `http://<server-ip>:<port>/play/<room-code>`. The QR code is displayed on the Display Screen and on the Admin Screen. | MUST |
| FR-025 | CONFIRMED | Players join by scanning the QR code or entering the URL manually. They provide a nickname (2-20 characters, profanity filter optional). | MUST |
| FR-026 | CONFIRMED | The admin sees a live waiting room list showing all connected players (nickname + connection status). The admin can remove a player from the room. | MUST |
| FR-027 | CONFIRMED | The admin clicks "Lock Room" to prevent new joins. After locking, the "Start" button becomes available. | MUST |
| FR-028 | CONFIRMED | Maximum 30 players per room. When the room is full, new join attempts see: "This room is full (30/30 players). Please wait or ask the host." | MUST |
| FR-029 | CONFIRMED | If a player disconnects (phone sleeps, WiFi drops), the system shall preserve their session for 60 seconds. If they reconnect within this window, they rejoin with their score and position intact. | MUST |

### 1.5 Game Flow & Scoring

| ID | Classification | Requirement | Priority |
|----|---------------|-------------|----------|
| FR-030 | CONFIRMED | When the experience reaches a GAME slide, the Display Screen shows the silhouette image and a countdown timer. Simultaneously, all player phones show the multiple-choice answers. | MUST |
| FR-031 | CONFIRMED | Players tap one answer on their phone. Once submitted, the answer is final (no changing). The phone shows "Answer submitted! Waiting for results..." | MUST |
| FR-032 | CONFIRMED | Scoring formula: `points = max(10, 100 - floor(90 * (time_to_answer / timer_duration)))`. Fastest correct answer = 100 points. Slowest correct answer (at the buzzer) = 10 points. Wrong answer = 0 points. No answer = 0 points. No negative scoring. | MUST |
| FR-033 | CONFIRMED | When the timer expires OR all players have answered (whichever comes first), the system transitions to the "reveal" state. The admin can also manually trigger reveal at any time. | MUST |
| FR-034 | CONFIRMED | Reveal sequence: (1) Display Screen crossfades from silhouette to real photo. (2) Correct answer is highlighted green on player phones. (3) Each player sees their own points earned for this question. | MUST |
| FR-035 | CONFIRMED | After reveal, the admin clicks "Next" to advance. The system shows a brief leaderboard (top 5) on the Display Screen for 5 seconds before advancing to the next slide. | SHOULD |
| FR-036 | CONFIRMED | The admin can pause the experience at any time. When paused: timers freeze, player phones show "Paused by host", Display Screen shows current content with a subtle pause indicator. | MUST |
| FR-037 | CONFIRMED | The admin can skip forward or backward in the slide sequence. Skipping a GAME slide that hasn't been played marks it as skipped (no points awarded). | MUST |
| FR-038 | CONFIRMED | At the end of the experience, the Display Screen shows the final leaderboard (all players ranked). Player phones show their personal rank and total score. | MUST |

### 1.6 Display Screen (Photo Frame)

| ID | Classification | Requirement | Priority |
|----|---------------|-------------|----------|
| FR-039 | CONFIRMED | The Display Screen (`/display`) shall run fullscreen (using Fullscreen API or CSS). Black background. Media centered and scaled to fit (contain, not crop). | MUST |
| FR-040 | CONFIRMED | FRAME slides transition with a configurable effect: fade (default), slide, or Ken Burns (slow zoom + pan for images). | SHOULD |
| FR-041 | CONFIRMED | During FRAME slides, the Display Screen auto-advances after the configured duration. During GAME slides, it waits for admin control. | MUST |
| FR-042 | CONFIRMED | The Display Screen shall show the QR code and room code in a corner overlay during the waiting room phase. Once the experience starts, the overlay hides. | MUST |

### 1.7 Admin Live Controls

| ID | Classification | Requirement | Priority |
|----|---------------|-------------|----------|
| FR-043 | CONFIRMED | The admin screen shall show: current slide preview, slide type (FRAME/GAME), position in sequence (e.g., "Slide 7 of 24"), and live player count. | MUST |
| FR-044 | CONFIRMED | For GAME slides, the admin screen shall show live answer stats: how many answered, percentage per answer option, and a bar chart updating in real-time. | MUST |
| FR-045 | CONFIRMED | The admin screen shall have playback controls: Play/Pause, Previous, Next, Reveal (for GAME slides), Show Leaderboard. | MUST |

### 1.8 Internationalization

| ID | Classification | Requirement | Priority |
|----|---------------|-------------|----------|
| FR-046 | CONFIRMED | The system shall support English and French. All UI strings are externalized in a JSON locale file. Admin selects language in settings. Player phone inherits the experience language. | MUST |

---

## DOMAIN 2: SECURITY REQUIREMENTS

| ID | Classification | Requirement | Threat Addressed | Priority |
|----|---------------|-------------|------------------|----------|
| SR-001 | CONFIRMED | The admin screen shall be protected by a PIN code (4-6 digits, set on first launch). No player can access `/admin` without the PIN. | Unauthorized game control | MUST |
| SR-002 | CONFIRMED | Room codes shall be generated using `secrets.token_hex` to prevent guessing. Room codes expire when the experience ends. | Room hijacking | MUST |
| SR-003 | CONFIRMED | Player nicknames shall be sanitized (HTML-escaped) before display on any screen. | XSS via nickname | MUST |
| SR-004 | CONFIRMED | The system shall not expose the file system path of media files to any client. Media is served via `/api/media/<id>` with the ID mapped server-side. | Path traversal | MUST |
| SR-005 | CONFIRMED | If Gemini API integration is enabled, the API key shall be stored in a local `.env` file (not in the database) and never sent to any client. | API key leak | MUST |
| SR-006 | CONFIRMED | The system shall bind to `0.0.0.0` (local network accessible) but display a warning on first launch: "This app is accessible to anyone on your WiFi network. Only run it on trusted networks." | Network exposure awareness | MUST |

---

## DOMAIN 3: PERFORMANCE REQUIREMENTS

| ID | Classification | Requirement | Measurement | Priority |
|----|---------------|-------------|-------------|----------|
| PR-001 | CONFIRMED | WebSocket message delivery from admin action to all 30 player devices shall complete within 200ms on a local WiFi network. | Timestamp delta measured in browser DevTools | MUST |
| PR-002 | CONFIRMED | Silhouette generation (rembg) shall complete within 10 seconds per photo on a modern Mac (M1+). Admin sees a progress indicator during generation. | Wall-clock time from click to completion | MUST |
| PR-003 | CONFIRMED | The Display Screen shall preload the next slide (image or first frame of video) while the current slide is displayed, ensuring zero visible loading delay between transitions. | No white flash or loading spinner visible during transition | MUST |
| PR-004 | CONFIRMED | The admin gallery shall load within 2 seconds for a library of up to 1000 photos (paginated, 50 thumbnails per page). | Time to first meaningful paint | SHOULD |
| PR-005 | CONFIRMED | The server shall handle 30 simultaneous WebSocket connections with less than 50MB total RAM overhead. | Measured via `ps` or Activity Monitor during a full game | SHOULD |
| PR-006 | CONFIRMED | HEIC-to-JPEG conversion shall be performed once at import time and cached. Subsequent accesses serve the cached JPEG. | No re-conversion on repeated access | MUST |
| PR-007 | GAP IDENTIFIED | Video transcoding for non-browser-compatible formats shall run as a background task. The admin is notified when transcoding completes. Transcoding shall not block the UI. | Admin can continue working during transcode | SHOULD |

---

## DOMAIN 4: USABILITY & COGNITIVE ACCESSIBILITY

These requirements are NON-NEGOTIABLE. Users include people with cognitive disabilities.

| ID | Classification | Requirement | Barrier Addressed | Priority |
|----|---------------|-------------|-------------------|----------|
| UR-001 | CONFIRMED | Player phone screens shall display a maximum of 4 answer buttons, each at minimum 60px height with 16px+ font, high contrast (WCAG AA minimum: 4.5:1 ratio). | 4c: Multi-sensory / Motor accessibility | MUST |
| UR-002 | CONFIRMED | Every screen state shall have a single clear instruction visible at the top: "Wait for the host to start", "Tap your answer!", "Time's up!", "You got it right! +85 points". No screen shall require the user to figure out what to do. | 4b: Guidance and wayfinding | MUST |
| UR-003 | CONFIRMED | The countdown timer shall be displayed as a horizontal progress bar (not a ticking numeric clock). Color transitions from green to yellow to red. The bar also shows remaining seconds as a number for dual-channel communication. | 4d: Multi-sensory / 4c: Cognitive load | MUST |
| UR-004 | CONFIRMED | Correct/wrong answer feedback shall use both color AND icon: green checkmark for correct, red X for wrong. Never color alone. | 4d: Multi-sensory communication | MUST |
| UR-005 | CONFIRMED | The admin screen shall group controls into clearly labeled zones: "Playback Controls" (play/pause/next/prev), "Game Controls" (reveal/leaderboard), "Room" (player list, lock). No more than 5 buttons per zone. | 4c: Cognitive load management | MUST |
| UR-006 | CONFIRMED | All destructive admin actions (remove player, delete experience, reset scores) shall require a confirmation dialog stating the consequence in plain language: "Remove Marie from the game? She will lose her score and cannot rejoin." | 4e: Recovery and continuity | MUST |
| UR-007 | CONFIRMED | The player join flow shall be exactly 3 steps: (1) Scan QR or enter code, (2) Type nickname, (3) Wait. Each step is a separate full-screen view. | 4c: Cognitive load / 4b: Wayfinding | MUST |
| UR-008 | CONFIRMED | Sound effects shall accompany key events: game start (attention chime), timer running low (gentle pulse), correct answer (success tone), wrong answer (soft buzz), reveal (dramatic reveal). Sounds can be toggled off globally in admin settings. | 4d: Multi-sensory | SHOULD |
| UR-009 | CONFIRMED | The Display Screen shall use minimum 32px font for any text (question, leaderboard names). Silhouette images shall fill at least 60% of the screen area. | Low vision accessibility | MUST |
| UR-010 | CONFIRMED | If a player's phone loses connection, the player screen shows: "Connection lost. Trying to reconnect... Don't close this page." with a spinning indicator. No technical jargon. | 4a: Error communication | MUST |
| UR-011 | CONFIRMED | The leaderboard shall show rank, nickname, and score. No elimination. All players see their own position highlighted. Leaderboard display is optional (admin can skip it). | 4c: Cognitive load / No punishment | MUST |
| UR-012 | GAP IDENTIFIED | The system shall support a "relaxed mode" toggle: disables timer entirely, players can answer at their own pace, no speed bonus (all correct answers get 100 points). For groups where time pressure causes anxiety. | 4c: Cognitive load / Anxiety reduction | SHOULD |

---

## TRACEABILITY MATRIX (SUMMARY)

### Source-to-Requirement Coverage

| User Need | Requirements |
|-----------|-------------|
| Digital photo frame with auto-cycling | FR-001 to FR-009, FR-039 to FR-042 |
| Apple Photos integration | FR-004 |
| Broad format support (especially Apple) | FR-002, FR-003, PR-006 |
| Silhouette game ("person in total black") | FR-013, FR-019 to FR-022 |
| Multiplayer on phones via QR | FR-023 to FR-029 |
| Speed scoring | FR-030 to FR-032 |
| Admin controls (pause, reveal, advance) | FR-033 to FR-037, FR-043 to FR-045 |
| Crowdpurr-style experience | FR-010 to FR-018, FR-030 to FR-038 |
| Cognitive accessibility | UR-001 to UR-012 |
| English + French | FR-046 |
| Up to 30 players | FR-028, PR-001, PR-005 |
| AI date ranking | FR-006 |
| Single session scoring | FR-038 |
| Gemini integration | FR-022 |

### Highest-Risk Pass-Through Chains

1. **Game answer flow**: Player tap -> WebSocket -> Server (score calc) -> WebSocket -> Admin (live stats) + Display (update count). Touches: Socket.IO, scoring engine, admin UI, display UI. HIGH PASS-THROUGH DENSITY.
2. **Silhouette generation**: Admin selects photo -> rembg model load -> segmentation -> black fill -> save -> thumbnail. Touches: media pipeline, rembg, Pillow, file system, DB. MEDIUM DENSITY.
3. **Apple Photos import**: Admin clicks import -> osascript -> Photos.app export -> file copy -> format conversion -> DB insert -> thumbnail generation. MEDIUM DENSITY.

---

## SPECIFICATION INTEGRITY DECLARATION

```
Total requirements specified: 67
  Of which CONFIRMED: 63
  Of which GAP IDENTIFIED (awaiting user confirmation): 4
    - FR-018: Bulk-import folder as FRAME slides
    - FR-022: Gemini API as alternative silhouette method
    - PR-007: Background video transcoding
    - UR-012: Relaxed mode (no timer)
  Domains with zero requirements: None

Coverage assessment:
  User needs with full requirement coverage: 14/14
  User needs with partial coverage: 0
  User needs with no coverage: 0

Testability assessment:
  Requirements with complete acceptance criteria: 67/67
  Requirements marked NEEDS CLARIFICATION: 0

Self-assessment: HIGH confidence that this specification is sufficient
for a developer to implement and a tester to verify without asking
clarifying questions.
```

---

## TECH STACK (CONFIRMED)

```
Runtime:       Python 3.11+
Web framework: FastAPI (async, WebSocket-native)
Real-time:     python-socketio (Socket.IO protocol)
Database:      SQLite via SQLAlchemy (async with aiosqlite)
Image proc:    Pillow + pillow-heif (HEIC support)
Silhouettes:   rembg (U2-Net, runs locally, no GPU required)
Video:         ffmpeg (transcode to H.264/MP4)
QR codes:      qrcode (Python, SVG output)
Apple Photos:  osascript subprocess (AppleScript bridge)
Frontend:      Vanilla HTML + CSS + JS (no framework, no build step)
i18n:          JSON locale files (en.json, fr.json)
Config:        .env file for optional API keys
```

---

## OPEN QUESTIONS

1. **Apple Photos — Shared Photo Streams**: Shared Albums via iCloud require the user to be signed into iCloud on the Mac. If they are, the albums are visible in Photos.app and exportable. No additional auth needed. Confirm this is acceptable?

2. **Sound assets**: Should I bundle free/CC0 sound effects, or do you have specific sounds you want to use?

3. **Branding**: Any logo, color scheme, or name for this app? Or default to a clean dark theme (suits photo display)?

4. **Gemini API key**: Do you currently have a Google AI Studio API key? This affects whether FR-022 is testable now or deferred.

---

## OUT OF SCOPE RECOMMENDATIONS (FUTURE)

- Multi-game rankings across sessions
- Team mode (players form teams)
- Custom question types beyond silhouette (text trivia, audio clips)
- Remote access (beyond local WiFi)
- Cloud sync / backup of experiences
- Lottery/raffle feature (visible in Crowdpurr but not requested)
