#!/usr/bin/env python3
"""Standalone batch date estimator — runs outside the server to avoid restart issues."""
import os, sys, sqlite3, time

sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from media_pipeline import estimate_year_from_ages_gemini

DB_PATH = os.path.expanduser("~/.photoframe/photoframe.db")
FAMILY_CONTEXT = "Flo (Florence) is the central character, born in March 2006. If you see a baby/toddler/child, estimate her age to determine the year. She grows up through these photos from 2006 to 2026."

def main():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, filename, file_path, web_path FROM media WHERE exif_date IS NULL AND media_type = 'IMAGE'"
    ).fetchall()
    print(f"Found {len(rows)} undated images")

    estimated = 0
    errors = 0
    for i, (mid, filename, file_path, web_path) in enumerate(rows, 1):
        source = web_path or file_path
        if not source or not os.path.exists(source):
            print(f"  [{i}/{len(rows)}] SKIP {filename} — file not found")
            continue
        try:
            result = estimate_year_from_ages_gemini(source, FAMILY_CONTEXT)
            if result and result.get("year"):
                year = result["year"]
                date_str = f"{year}-06-15 00:00:00"
                conn.execute("UPDATE media SET exif_date = ?, category = COALESCE(category, ?) WHERE id = ?",
                             (date_str, str(year), mid))
                conn.commit()
                estimated += 1
                conf = result.get("confidence", "?")
                reason = (result.get("reasoning") or "")[:60]
                print(f"  [{i}/{len(rows)}] {filename} -> {year} ({conf}) {reason}")
            else:
                print(f"  [{i}/{len(rows)}] {filename} -> no estimate")
        except Exception as e:
            errors += 1
            print(f"  [{i}/{len(rows)}] {filename} -> ERROR: {e}")
        time.sleep(0.3)

    conn.close()
    print(f"\nDone! Estimated: {estimated}, Errors: {errors}, Total: {len(rows)}")

if __name__ == "__main__":
    main()
