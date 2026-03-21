# PHOTO-APP — QUIZ CREATION SYSTEM
## Specification — Blueprint Level — V1.0

**Specification Target**: The quiz creation flow within the experience editor — from selecting a slide thumbnail to generating a playable quiz question via Gemini API.

**Input Artifacts**: Existing codebase (server.py, admin.html, media_pipeline.py), user description, screenshots.

**Known Constraints**:
- Gemini API is the only external dependency allowed
- Users include people with cognitive disabilities — accessibility is non-negotiable
- Always 3 answer choices maximum
- Speed scoring: positive for correct, negative for wrong
- All processing must work on localhost (no cloud besides Gemini)
- Silhouette generation: rembg (local) or Gemini (artistic)

**Domain Focus**: Functional + Usability (primary), Security + Performance (secondary)

---

## OVERVIEW

When an admin opens an experience (e.g., "Flo 20") and views their slide thumbnails, they can tap any slide to reveal a **Quiz Panel**. This panel lets them choose a quiz type, configure it with minimal effort, and have Gemini auto-generate the question + answers. The goal: creating a quiz from a photo should take under 30 seconds.

### Quiz Types

| ID | Name | Description | Gemini Prompt Strategy |
|----|------|-------------|----------------------|
| QT-SILHOUETTE | Shadow Quiz | One or more people replaced by solid black silhouettes. Players guess who. | Gemini identifies people → rembg/Gemini blacks out all people → quiz asks "Who is behind the silhouette?" |
| QT-WHOS-MISSING | Who's Missing? | One person is removed from the photo entirely. Players spot who's gone. | Gemini identifies people → Gemini edits photo to remove one person cleanly → quiz asks "Who is missing?" |
| QT-ZOOM | Zoom In | A tight crop of a detail (face, object, clothing). Players guess what/who. | Gemini identifies an interesting detail → system crops to that region → quiz asks "What is this?" or "Whose detail is this?" |

---

## DOMAIN 1: FUNCTIONAL REQUIREMENTS

### FR-001: Slide Tap Opens Quiz Panel
| Field | Content |
|-------|---------|
| CLASSIFICATION | CONFIRMED |
| REQUIREMENT | The system shall display a Quiz Panel when the admin taps/clicks a slide thumbnail in the experience editor. |
| RATIONALE | User wants quiz creation integrated into the slide grid, not buried in a separate flow. |
| SOURCE | User: "when I select a picture, I should be able to have the option to create a quiz" |
| ACCEPTANCE CRITERIA | GIVEN an experience with slides WHEN admin clicks a slide thumbnail THEN a panel appears below or beside the thumbnail showing: the photo preview, a "Make Quiz" toggle, and quiz type options. |
| DEPENDENCIES | None |
| PRIORITY | MUST |

### FR-002: Quiz Type Selection
| Field | Content |
|-------|---------|
| CLASSIFICATION | CONFIRMED |
| REQUIREMENT | The system shall present exactly 3 quiz types as large, icon-labeled buttons: Shadow Quiz, Who's Missing?, and Zoom In. Only one can be active at a time. |
| RATIONALE | User explicitly requested multiple quiz types with clear differentiation. |
| SOURCE | User: "a panel asking for what type of quiz — turn a person as black silhouette, removing someone from picture, and maybe another simple game" |
| ACCEPTANCE CRITERIA | GIVEN the quiz panel is open WHEN admin views quiz type buttons THEN exactly 3 options are visible, each with an icon + short label + one-line description. Selecting one deselects the others. |
| DEPENDENCIES | FR-001 |
| PRIORITY | MUST |

### FR-003: Shadow Quiz — Gemini Analyze → Auto-Generate
| Field | Content |
|-------|---------|
| CLASSIFICATION | CONFIRMED |
| REQUIREMENT | When Shadow Quiz is selected, the system shall: (1) call Gemini to identify people in the photo, (2) present the results with a "Generate Quiz" button, (3) auto-fill correct answer and 2 wrong answers, (4) allow admin to edit any answer text before confirming. |
| RATIONALE | Must be extremely simple — admin should only need to confirm, not compose questions. |
| SOURCE | User: "VERY simply" + existing analyze_people_gemini function |
| ACCEPTANCE CRITERIA | GIVEN admin selects Shadow Quiz on a photo with 2+ people WHEN Gemini responds THEN the panel shows: identified people list, pre-filled correct answer, pre-filled wrong answers. Admin can edit text fields and click "Create Quiz". |
| DEPENDENCIES | FR-001, FR-002 |
| PRIORITY | MUST |

### FR-004: Shadow Quiz — Silhouette Generation
| Field | Content |
|-------|---------|
| CLASSIFICATION | CONFIRMED |
| REQUIREMENT | When the admin confirms a Shadow Quiz, the system shall generate a silhouette image (all people blacked out, background preserved) using rembg (local) or Gemini (artistic), and store it as the slide's silhouette_path. |
| RATIONALE | Silhouette is the core visual for the Shadow Quiz gameplay. |
| SOURCE | Existing generate_silhouette function in media_pipeline.py |
| ACCEPTANCE CRITERIA | GIVEN admin confirms Shadow Quiz WHEN silhouette generation completes THEN the slide thumbnail updates to show the silhouette. If generation fails, the system shows a toast error and does not create a broken slide. |
| DEPENDENCIES | FR-003 |
| PRIORITY | MUST |

### FR-005: Who's Missing Quiz — Gemini Remove Person
| Field | Content |
|-------|---------|
| CLASSIFICATION | CONFIRMED |
| REQUIREMENT | When Who's Missing is selected, the system shall: (1) call Gemini to identify people, (2) let admin pick which person to remove, (3) call Gemini to generate an edited photo with that person cleanly removed (inpainting), (4) auto-fill quiz answers. |
| RATIONALE | New quiz type using Gemini's image editing to remove a person. |
| SOURCE | User: "removing someone from picture" |
| ACCEPTANCE CRITERIA | GIVEN admin selects Who's Missing on a group photo WHEN admin picks "person in red shirt" to remove THEN Gemini generates an edited image where that person is gone and the background is plausibly filled in. Quiz answers are: correct = removed person's description, wrong = 2 other people still visible. |
| DEPENDENCIES | FR-001, FR-002 |
| PRIORITY | MUST |

### FR-006: Zoom In Quiz — Auto-Crop Detail
| Field | Content |
|-------|---------|
| CLASSIFICATION | CONFIRMED |
| REQUIREMENT | When Zoom In is selected, the system shall: (1) call Gemini to identify an interesting detail in the photo (face, object, clothing pattern), (2) generate a tight square crop around that detail, (3) auto-fill quiz with "What/who is this?" + 3 answer options. |
| RATIONALE | Simple quiz type that doesn't need image editing — just cropping. Works for any photo. |
| SOURCE | User: "maybe another simple game" |
| ACCEPTANCE CRITERIA | GIVEN admin selects Zoom In WHEN Gemini identifies a detail (e.g., "the necklace worn by the woman in blue") THEN the system crops a 300x300px region around it. Admin sees the crop preview and can regenerate if unsatisfied. |
| DEPENDENCIES | FR-001, FR-002 |
| PRIORITY | SHOULD |

### FR-007: Quiz Panel Shows on Existing Quiz Slides
| Field | Content |
|-------|---------|
| CLASSIFICATION | CONFIRMED |
| REQUIREMENT | If a slide is already a quiz (slide_type = game), tapping it shall show the quiz panel in "edit" mode: displaying the current quiz type, answers, and silhouette/edited image, with options to regenerate or delete the quiz. |
| RATIONALE | Admin needs to review and modify existing quizzes. |
| SOURCE | User: "see all assets selected and remove easily" |
| ACCEPTANCE CRITERIA | GIVEN a slide with slide_type=game WHEN admin taps it THEN the quiz panel shows current answers, the generated image, and buttons: "Regenerate", "Edit Answers", "Remove Quiz" (reverts to frame slide). |
| DEPENDENCIES | FR-001 |
| PRIORITY | SHOULD |

### FR-008: Always 3 Answer Choices
| Field | Content |
|-------|---------|
| CLASSIFICATION | CONFIRMED |
| REQUIREMENT | The system shall always generate exactly 3 answer choices for any quiz type. The admin cannot add or remove choices — only edit text. Answer D is always null. |
| RATIONALE | Simplicity + cognitive accessibility. 3 choices is the sweet spot. |
| SOURCE | User: "always 3 choices max (no ability to change)" |
| ACCEPTANCE CRITERIA | GIVEN any quiz creation flow WHEN quiz is generated THEN exactly 3 answer fields (A, B, C) are shown. No "add answer" or "remove answer" buttons exist. |
| DEPENDENCIES | None |
| PRIORITY | MUST |

### FR-009: Scoring — Positive and Negative
| Field | Content |
|-------|---------|
| CLASSIFICATION | CONFIRMED |
| REQUIREMENT | The system shall award +10 to +100 points for correct answers (speed-based: faster = more) and -10 to -50 points for wrong answers (speed-based: faster wrong = more penalty). In relaxed mode: +100 correct, -20 wrong. |
| RATIONALE | User explicitly requested both positive and negative scoring. |
| SOURCE | User: "speed scoring negatively and positively" |
| ACCEPTANCE CRITERIA | GIVEN timed mode with 15s timer WHEN player answers correctly in 2s THEN ~88 points awarded. WHEN player answers wrong in 2s THEN ~-45 points. WHEN player answers wrong in 14s THEN ~-10 points. |
| DEPENDENCIES | None |
| PRIORITY | MUST |

### FR-010: Gemini Prompt — Shadow Quiz
| Field | Content |
|-------|---------|
| CLASSIFICATION | CONFIRMED |
| REQUIREMENT | The Shadow Quiz Gemini analysis prompt shall: (1) ask Gemini to list each visible person with a short physical description, (2) suggest which person to silhouette, (3) generate correct answer + 2 plausible wrong answers. Response must be valid JSON. |
| RATIONALE | Structured response enables auto-fill UI. |
| SOURCE | Existing analyze_people_gemini in media_pipeline.py |
| ACCEPTANCE CRITERIA | GIVEN a photo with 3 people WHEN Gemini is called THEN response parses as JSON with fields: people (array of descriptions), quiz.correct (string), quiz.wrong (array of 2 strings). |
| DEPENDENCIES | FR-003 |
| PRIORITY | MUST |

### FR-011: Gemini Prompt — Who's Missing
| Field | Content |
|-------|---------|
| CLASSIFICATION | GAP IDENTIFIED |
| REQUIREMENT | The Who's Missing Gemini image edit prompt shall: (1) identify the selected person to remove, (2) instruct Gemini to cleanly erase that person and inpaint the background, (3) preserve all other people and the rest of the scene unchanged. |
| RATIONALE | Quality of the edited image is critical — a poorly inpainted removal ruins the game. |
| SOURCE | User: "removing someone from picture" |
| ACCEPTANCE CRITERIA | GIVEN a group photo with person X selected for removal WHEN Gemini edits the image THEN: person X is not visible, background behind person X is plausibly filled, all other people remain unchanged, image dimensions are preserved. |
| DEPENDENCIES | FR-005 |
| PRIORITY | MUST |

### FR-012: Gemini Prompt — Zoom In
| Field | Content |
|-------|---------|
| CLASSIFICATION | GAP IDENTIFIED |
| REQUIREMENT | The Zoom In Gemini prompt shall ask Gemini to identify an interesting/recognizable detail in the photo and return bounding box coordinates (x, y, width, height as percentages) plus a description. The system crops locally — no image generation needed. |
| RATIONALE | Zoom In is the simplest quiz type — no Gemini image editing, just analysis + local crop. |
| SOURCE | Inferred from "another simple game" |
| ACCEPTANCE CRITERIA | GIVEN a photo WHEN Gemini is called THEN response includes: detail_description (string), bounding_box {x, y, w, h} as 0-1 percentages, quiz.correct (string), quiz.wrong (array of 2 strings). |
| DEPENDENCIES | FR-006 |
| PRIORITY | SHOULD |

### FR-013: Slide Converts Between Frame and Game
| Field | Content |
|-------|---------|
| CLASSIFICATION | CONFIRMED |
| REQUIREMENT | Creating a quiz on a frame slide shall convert it to slide_type=game. Removing a quiz from a game slide shall revert it to slide_type=frame. The media_id remains unchanged. |
| RATIONALE | The same photo can be a passive frame or an interactive quiz. |
| SOURCE | Existing slide model: slide_type enum (frame/game) |
| ACCEPTANCE CRITERIA | GIVEN a frame slide WHEN admin creates a quiz THEN slide_type changes to "game" in DB. GIVEN a game slide WHEN admin clicks "Remove Quiz" THEN slide_type reverts to "frame", silhouette_path is cleared, and answers are nulled. |
| DEPENDENCIES | FR-001, FR-007 |
| PRIORITY | MUST |

---

## DOMAIN 2: SECURITY REQUIREMENTS

### SR-001: Gemini API Key Protection
| Field | Content |
|-------|---------|
| CLASSIFICATION | CONFIRMED |
| REQUIREMENT | The Gemini API key shall never be sent to the browser, logged in full, or stored in the database. It is read from the GEMINI_API_KEY environment variable server-side only. |
| THREAT ADDRESSED | API key leakage via client-side code or database dump. |
| SOURCE | Existing pattern in media_pipeline.py (os.environ.get("GEMINI_API_KEY")) |
| ACCEPTANCE CRITERIA | GIVEN any API call WHEN browser network tab is inspected THEN no request or response contains the Gemini API key. |
| TRUST BOUNDARY | Browser is untrusted. Server is trusted. Gemini API is external-trusted. |
| PRIORITY | MUST |

### SR-002: Answer Text Sanitization
| Field | Content |
|-------|---------|
| CLASSIFICATION | CONFIRMED |
| REQUIREMENT | All answer text (whether from Gemini or admin-edited) shall be HTML-escaped before storage and rendering to prevent XSS. |
| THREAT ADDRESSED | Gemini or admin could inject HTML/JS via answer text fields. |
| SOURCE | Existing escapeHtml function in admin.html |
| ACCEPTANCE CRITERIA | GIVEN answer text containing `<script>alert(1)</script>` WHEN rendered on display or player views THEN the literal text is shown, not executed. |
| TRUST BOUNDARY | Gemini responses are untrusted. Admin input is semi-trusted. |
| PRIORITY | MUST |

---

## DOMAIN 3: PERFORMANCE REQUIREMENTS

### PR-001: Gemini Analysis Response Time
| Field | Content |
|-------|---------|
| CLASSIFICATION | CONFIRMED |
| REQUIREMENT | The Gemini people analysis call shall complete within 30 seconds. A loading spinner with text ("Analyzing photo...") shall be shown during the call. |
| MEASUREMENT METHOD | Time from button click to UI update, measured via browser network tab. |
| SOURCE | Existing 30s timeout in analyze_people_gemini |
| ACCEPTANCE CRITERIA | GIVEN a standard photo (< 5MB) WHEN Gemini is called THEN response arrives within 30s. WHEN 30s is exceeded THEN a timeout error toast is shown. |
| DEGRADATION BEHAVIOR | Toast: "Analysis took too long. Try a different photo or check your connection." |
| PRIORITY | MUST |

### PR-002: Silhouette/Image Edit Generation Time
| Field | Content |
|-------|---------|
| CLASSIFICATION | CONFIRMED |
| REQUIREMENT | Silhouette generation (rembg local) shall complete within 60 seconds. Gemini image editing (artistic silhouette, person removal) shall complete within 120 seconds. A progress indicator shall be shown. |
| MEASUREMENT METHOD | Server-side timing from generate_silhouette call to completion. |
| SOURCE | Existing silhouette generation pipeline |
| ACCEPTANCE CRITERIA | GIVEN a 4000x3000 photo WHEN rembg silhouette is generated THEN completes within 60s. WHEN Gemini artistic mode is used THEN completes within 120s. |
| DEGRADATION BEHAVIOR | If local rembg fails, fall back to Gemini. If both fail, show error and allow retry. |
| PRIORITY | MUST |

---

## DOMAIN 4: USABILITY AND COGNITIVE ACCESSIBILITY REQUIREMENTS

### UR-001: Quiz Panel — One Screen, No Scrolling Required
| Field | Content |
|-------|---------|
| CLASSIFICATION | CONFIRMED |
| REQUIREMENT | The quiz panel shall fit within the visible viewport without scrolling on a 1280x800 screen. Quiz type buttons, answer fields, and confirm button shall all be visible at once. |
| BARRIER ADDRESSED | Working memory — users with cognitive disabilities lose track of off-screen content. |
| SOURCE | CLAUDE.md: "One clear instruction per screen in plain language" |
| ACCEPTANCE CRITERIA | GIVEN a 1280x800 viewport WHEN quiz panel is open THEN all interactive elements are visible without scrolling. |
| DEPENDENCIES | FR-001 |
| PRIORITY | MUST |

### UR-002: Quiz Type Buttons — Large, Icon + Text
| Field | Content |
|-------|---------|
| CLASSIFICATION | CONFIRMED |
| REQUIREMENT | Each quiz type button shall be minimum 80px tall, have a distinct icon (not color alone), a short label (max 2 words), and a one-line plain-language description below. |
| BARRIER ADDRESSED | Visual discrimination + reading comprehension. |
| SOURCE | CLAUDE.md: "Color is ALWAYS paired with icon/text (never color alone)" |
| ACCEPTANCE CRITERIA | GIVEN the quiz type selection WHEN admin views the 3 buttons THEN each has: an icon (e.g., silhouette icon, eraser icon, magnifying glass), bold label, subtitle description. Icons are visually distinct from each other. |
| DEPENDENCIES | FR-002 |
| PRIORITY | MUST |

### UR-003: Answer Fields — Editable but Pre-Filled
| Field | Content |
|-------|---------|
| CLASSIFICATION | CONFIRMED |
| REQUIREMENT | Answer text fields shall be pre-filled by Gemini's suggestions. The correct answer field shall have a green left border. Admin can edit any field but cannot leave any blank. |
| BARRIER ADDRESSED | Cognitive load — pre-filling minimizes typing and decision-making. |
| SOURCE | User: "VERY simply" — minimize admin effort |
| ACCEPTANCE CRITERIA | GIVEN Gemini has suggested answers WHEN admin views the quiz panel THEN 3 fields are pre-filled with editable text. The correct answer is visually marked (green border/checkmark). Attempting to confirm with a blank field shows inline validation. |
| DEPENDENCIES | FR-003, FR-008 |
| PRIORITY | MUST |

### UR-004: Loading State — Clear, Non-Anxious
| Field | Content |
|-------|---------|
| CLASSIFICATION | CONFIRMED |
| REQUIREMENT | During Gemini calls, the system shall show a calm loading state: a spinner icon, descriptive text ("Analyzing your photo..."), and no progress percentage (since it's indeterminate). |
| BARRIER ADDRESSED | Anxiety — users with cognitive disabilities may panic at unclear waits. |
| SOURCE | CLAUDE.md: "Disconnection message: reassuring, no jargon" |
| ACCEPTANCE CRITERIA | GIVEN Gemini analysis is in progress WHEN admin views the quiz panel THEN a spinner with plain text is shown. No technical error codes are visible. If it fails, message is: "Couldn't analyze this photo. Try another one." |
| DEPENDENCIES | PR-001 |
| PRIORITY | MUST |

### UR-005: Destructive Actions — Plain Confirmation
| Field | Content |
|-------|---------|
| CLASSIFICATION | CONFIRMED |
| REQUIREMENT | "Remove Quiz" (reverting game slide to frame) shall require a plain-language confirmation: "Remove the quiz from this photo? The photo stays, only the quiz is removed." with "Remove Quiz" and "Keep it" buttons. |
| BARRIER ADDRESSED | Preventing accidental destructive actions. |
| SOURCE | CLAUDE.md: "Destructive actions require plain-language confirmation" |
| ACCEPTANCE CRITERIA | GIVEN a game slide WHEN admin clicks "Remove Quiz" THEN a confirmation dialog appears with plain language. Clicking "Keep it" cancels. Clicking "Remove Quiz" reverts to frame. |
| DEPENDENCIES | FR-007, FR-013 |
| PRIORITY | MUST |

---

## OPEN QUESTIONS

1. **Family name context**: Should there be a "family names" setting where admin pre-enters names (e.g., "Grand-père, Maman, Florence, Hugo") so Gemini can use actual names instead of physical descriptions?

2. **Who's Missing validation**: How do we validate that Gemini actually removed the correct person? The inpainting quality varies — should there be a "looks good? / regenerate" step?

3. **Zoom In crop size**: Should the crop be a fixed 300x300 region, or should it adapt to the detail size Gemini suggests?

4. **Quiz type on existing slides**: If admin already added 50 frame slides, should there be a batch "Make Quiz" option to convert multiple slides at once?

---

## OUT OF SCOPE RECOMMENDATIONS

- **AI auto-quiz for entire experience**: Auto-scan all slides and suggest which ones would make good quizzes. Useful but requires separate spec.
- **Custom Gemini prompts**: Let advanced admins write their own prompts. Adds complexity, defer to V2.
- **Video quiz**: Quiz from a video frame. Requires frame extraction pipeline.
