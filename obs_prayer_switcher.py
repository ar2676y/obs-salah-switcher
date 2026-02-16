#!/usr/bin/env python3
"""
OBS Prayer Time Auto-Switcher
Scrapes iqama times from The Masjid App and automatically switches OBS scenes.

Default scene: "The Masjid App View"
At iqama time: switches to "PTZ Camera & Masjid App" for 10 minutes, then back.
On Fridays: switches to "PTZ Camera & Masjid App" at 1:25 PM, back at 2:15 PM.

Requirements:
    pip install obsws-python playwright apscheduler python-dotenv
    playwright install chromium
"""

import logging
import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import obsws_python as obs
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

# ── Configuration ────────────────────────────────────────────────────────────

# OBS WebSocket settings
OBS_HOST = os.getenv("OBS_HOST", "localhost")
OBS_PORT = int(os.getenv("OBS_PORT", "4455"))
OBS_PASSWORD = os.getenv("OBS_PASSWORD", "")

# Scene names
SCENE_DEFAULT = "The Masjid App View"
SCENE_PRAYER = "PTZ Camera & Masjid App"

# How long to stay on the prayer scene (in minutes)
PRAYER_DURATION_MINUTES = 10

# Friday Jumu'ah override
JUMUAH_START = "13:25"  # 1:25 PM
JUMUAH_END = "14:15"    # 2:15 PM

# Masjid App URL to scrape iqama times
MASJID_URL = "https://themasjidapp.org/601/slides"

# Timezone
TIMEZONE = ZoneInfo("America/New_York")  # Change if your masjid is in a different timezone

# Prayer names to look for when scraping (in order)
PRAYER_NAMES = ["Fajr", "Dhuhr", "Asr", "Maghrib", "Isha"]

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("obs_prayer_switcher.log"),
    ],
)
log = logging.getLogger(__name__)

# ── OBS Control ──────────────────────────────────────────────────────────────

def switch_scene(scene_name: str):
    """Connect to OBS and switch to the specified scene."""
    try:
        kwargs = {"host": OBS_HOST, "port": OBS_PORT}
        if OBS_PASSWORD:
            kwargs["password"] = OBS_PASSWORD
        cl = obs.ReqClient(**kwargs)
        cl.set_current_program_scene(scene_name)
        log.info(f"✅ Switched to scene: {scene_name}")
        cl.disconnect()
    except Exception as e:
        log.error(f"❌ Failed to switch scene: {e}")


def switch_to_prayer():
    """Switch to the prayer camera scene."""
    log.info("🕌 Iqama time — switching to prayer camera")
    switch_scene(SCENE_PRAYER)


def switch_to_default():
    """Switch back to the default Masjid App view."""
    log.info("📺 Switching back to Masjid App view")
    switch_scene(SCENE_DEFAULT)


# ── Scraping ─────────────────────────────────────────────────────────────────

def scrape_iqama_times():
    log.info(f"🔍 Scraping iqama times from {MASJID_URL}")
    iqama_times = {}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(MASJID_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(10000)

            prayer_labels = page.query_selector_all("[class*='font-bold'][class*='text-3vvh']")

            for label in prayer_labels:
                prayer_name = label.inner_text().strip()

                if prayer_name not in PRAYER_NAMES:
                    log.info(f"  Skipping non-fard: {prayer_name}")
                    continue

                card = label.evaluate_handle(
                    "el => el.closest('[class*=\"w-12vvw\"]') || el.parentElement.parentElement"
                )

                iqama_label = card.as_element().query_selector(
                    "[class*='font-bold'][class*='text-2vvh']"
                )
                if not iqama_label:
                    log.warning(f"  ⚠️  No iqama label found for {prayer_name}")
                    continue

                iqama_time_el = iqama_label.evaluate_handle("el => el.nextElementSibling")
                if not iqama_time_el:
                    log.warning(f"  ⚠️  No iqama time element found for {prayer_name}")
                    continue

                raw_time = iqama_time_el.as_element().inner_text().strip()
                converted = convert_to_24h(raw_time)
                if converted:
                    iqama_times[prayer_name] = converted
                    log.info(f"  Found {prayer_name} iqama: {raw_time} → {converted}")
                else:
                    log.warning(f"  ⚠️  Could not parse time for {prayer_name}: {raw_time}")

            browser.close()

    except Exception as e:
        log.error(f"❌ Scraping failed: {e}")

    if not iqama_times:
        log.warning("⚠️  No iqama times found! Falling back to manual times if available.")

    return iqama_times


def convert_to_24h(time_str):
    import re
    time_str = time_str.replace("\n", "").replace(" ", "").strip()
    match = re.match(r"(\d{1,2}):(\d{2})\s*(AM|PM|am|pm)", time_str)
    if not match:
        return None
    h, m, ampm = int(match.group(1)), int(match.group(2)), match.group(3).upper()
    if ampm == "PM" and h != 12:
        h += 12
    elif ampm == "AM" and h == 12:
        h = 0
    if 0 <= h <= 23 and 0 <= m <= 59:
        return f"{h:02d}:{m:02d}"
    return None


# ── Manual Fallback ──────────────────────────────────────────────────────────

def get_manual_iqama_times():
    """
    Fallback: manually set iqama times here if scraping doesn't work.
    Update these periodically as the masjid changes them.
    Times are in 24-hour format.
    """
    return {
        # "Fajr":    "06:15",
        # "Dhuhr":   "13:30",
        # "Asr":     "16:45",
        # "Maghrib": "18:30",
        # "Isha":    "20:00",
    }


# ── Scheduler ────────────────────────────────────────────────────────────────

scheduler = BlockingScheduler(timezone=TIMEZONE)


def schedule_today():
    """Fetch iqama times and schedule scene switches for today."""
    today = datetime.now(TIMEZONE).date()
    is_friday = today.weekday() == 4  # Monday=0, Friday=4
    day_label = "Friday/Jumuah" if is_friday else today.strftime("%A")
    log.info(f"📅 Scheduling for {today} ({day_label})")

    # Remove any previously scheduled prayer jobs
    for job in scheduler.get_jobs():
        if job.id.startswith("prayer_") or job.id.startswith("jumuah_"):
            job.remove()

    # Get iqama times (try scraping first, fall back to manual)
    iqama_times = scrape_iqama_times()
    manual_times = get_manual_iqama_times()

    # Merge: scraped times take priority, manual fills gaps
    for prayer in PRAYER_NAMES:
        if prayer not in iqama_times and prayer in manual_times:
            iqama_times[prayer] = manual_times[prayer]
            log.info(f"  Using manual time for {prayer}: {manual_times[prayer]}")

    if not iqama_times:
        log.error("❌ No iqama times available! Cannot schedule switches.")
        return

    now = datetime.now(TIMEZONE)

    for prayer, time_str in iqama_times.items():
        h, m = map(int, time_str.split(":"))
        prayer_dt = datetime.combine(today, datetime.min.time().replace(hour=h, minute=m), tzinfo=TIMEZONE)
        back_dt = prayer_dt + timedelta(minutes=PRAYER_DURATION_MINUTES)

        # On Friday, skip Dhuhr — Jumu'ah override handles it
        if is_friday and prayer == "Dhuhr":
            log.info(f"  ⏭️  Skipping Dhuhr on Friday (Jumu'ah override active)")
            continue

        # Only schedule if the time hasn't passed yet
        if prayer_dt > now:
            scheduler.add_job(
                switch_to_prayer,
                "date",
                run_date=prayer_dt,
                id=f"prayer_start_{prayer}",
                replace_existing=True,
            )
            scheduler.add_job(
                switch_to_default,
                "date",
                run_date=back_dt,
                id=f"prayer_end_{prayer}",
                replace_existing=True,
            )
            log.info(f"  🕐 {prayer}: camera ON at {time_str}, OFF at {back_dt.strftime('%H:%M')}")
        else:
            log.info(f"  ⏭️  {prayer} at {time_str} already passed, skipping")

    # Friday Jumu'ah override
    if is_friday:
        jh, jm = map(int, JUMUAH_START.split(":"))
        jumuah_start_dt = datetime.combine(today, datetime.min.time().replace(hour=jh, minute=jm), tzinfo=TIMEZONE)
        jh2, jm2 = map(int, JUMUAH_END.split(":"))
        jumuah_end_dt = datetime.combine(today, datetime.min.time().replace(hour=jh2, minute=jm2), tzinfo=TIMEZONE)

        if jumuah_start_dt > now:
            scheduler.add_job(
                switch_to_prayer,
                "date",
                run_date=jumuah_start_dt,
                id="jumuah_start",
                replace_existing=True,
            )
            scheduler.add_job(
                switch_to_default,
                "date",
                run_date=jumuah_end_dt,
                id="jumuah_end",
                replace_existing=True,
            )
            log.info(f"  🕌 Jumu'ah: camera ON at {JUMUAH_START}, OFF at {JUMUAH_END}")

    # Make sure we're on the default scene now
    switch_to_default()

    log.info("✅ Today's schedule is set!\n")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("  OBS Prayer Time Auto-Switcher")
    log.info("=" * 60)
    log.info(f"  Default scene : {SCENE_DEFAULT}")
    log.info(f"  Prayer scene  : {SCENE_PRAYER}")
    log.info(f"  Duration      : {PRAYER_DURATION_MINUTES} min")
    log.info(f"  Jumu'ah       : {JUMUAH_START} - {JUMUAH_END}")
    log.info(f"  OBS           : {OBS_HOST}:{OBS_PORT}")
    log.info(f"  Timezone      : {TIMEZONE}")
    log.info("=" * 60 + "\n")

    # Schedule the daily refresh at 12:05 AM
    scheduler.add_job(
        schedule_today,
        "cron",
        hour=0,
        minute=5,
        id="daily_refresh",
        replace_existing=True,
    )

    # Also re-scrape at noon in case times update mid-day
    scheduler.add_job(
        schedule_today,
        "cron",
        hour=12,
        minute=0,
        id="midday_refresh",
        replace_existing=True,
    )

    # Run immediately on startup
    schedule_today()

    log.info("🚀 Scheduler running. Press Ctrl+C to stop.\n")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("🛑 Shutting down...")
        scheduler.shutdown()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        log.info("🧪 TEST MODE — switching in 10 seconds...")
        for i in range(10, 0, -1):
            log.info(f"  {i}...")
            time.sleep(1)
        switch_to_prayer()
        log.info("⏳ Switching back in 10 seconds...")
        for i in range(10, 0, -1):
            log.info(f"  {i}...")
            time.sleep(1)
        switch_to_default()
        log.info("✅ Test complete!")
    else:
        main()