#!/usr/bin/env python3
"""Verify suspicious dates with Gemini — only updates exif_date, never touches quiz data."""
import os, sys, sqlite3, time

sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from media_pipeline import estimate_year_from_ages_gemini

DB_PATH = os.path.expanduser("~/.photoframe/photoframe.db")
FAMILY_CONTEXT = "Flo (Florence) is the central character, born in March 2006. If you see a baby/toddler/child, estimate her age to determine the year. She grows up through these photos from 2006 to 2026."

# IDs to verify (old cameras + 2029 outlier), excluding quiz media 1427
SUSPECT_IDS = [1558, 1559, 1563, 1635, 1632, 1634, 1636, 1633, 1405]

def main():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        f"SELECT id, filename, file_path, web_path, exif_date FROM media WHERE id IN ({','.join(map(str, SUSPECT_IDS))})"
    ).fetchall()

    print(f"Verifying {len(rows)} suspicious photos...\n")
    changes = []

    for mid, filename, file_path, web_path, current_date in rows:
        source = web_path or file_path
        if not source or not os.path.exists(source):
            print(f"  SKIP {filename} — file not found")
            continue
        try:
            result = estimate_year_from_ages_gemini(source, FAMILY_CONTEXT)
            if result and result.get("year"):
                ai_year = result["year"]
                current_year = int(current_date[:4]) if current_date else None
                conf = result.get("confidence", "?")
                reason = (result.get("reasoning") or "")[:80]
                match = "OK" if current_year == ai_year else f"MISMATCH (current={current_year})"
                print(f"  {filename}: AI={ai_year} ({conf}) {match}")
                print(f"    {reason}")
                if current_year != ai_year:
                    changes.append((mid, filename, current_date, ai_year, conf, reason))
            else:
                print(f"  {filename}: AI could not estimate")
        except Exception as e:
            print(f"  {filename}: ERROR {e}")
        time.sleep(0.3)

    if changes:
        print(f"\n{'='*60}")
        print(f"Found {len(changes)} dates to fix:\n")
        for mid, filename, old_date, new_year, conf, reason in changes:
            new_date = f"{new_year}-06-15 00:00:00"
            print(f"  {filename}: {old_date[:10]} -> {new_date[:10]} ({conf})")
            conn.execute("UPDATE media SET exif_date = ? WHERE id = ?", (new_date, mid))
        conn.commit()
        print(f"\nUpdated {len(changes)} dates in DB.")
    else:
        print("\nAll dates look correct!")

    conn.close()

if __name__ == "__main__":
    main()
