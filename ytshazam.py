"""
shazam_downloader.py
────────────────────
Reads your Shazam CSV export, deduplicates songs, then downloads each one
as a high-quality MP3 using yt-dlp + YouTube Music (no API key required,
no quota limits).

Requirements:
    pip install yt-dlp mutagen

Also requires ffmpeg on PATH:
    Windows : https://www.gyan.dev/ffmpeg/builds/  (add to PATH)
    macOS   : brew install ffmpeg
    Linux   : sudo apt install ffmpeg   (or equivalent)

The script will ask you for:
    1. Path to your Shazam CSV file
    2. Output directory for the MP3 files

Shazam CSV export:
    shazam.com → Profile → Library → Export (top-right button)
    The CSV columns are: Index, TagTime, Title, Artist, URL
"""

import csv
import os
import re
import sys
import time
import unicodedata
from pathlib import Path


# ── optional colour output ─────────────────────────────────────────────────
try:
    import ctypes
    if sys.platform == "win32":
        ctypes.windll.kernel32.SetConsoleMode(
            ctypes.windll.kernel32.GetStdHandle(-11), 7
        )
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    CYAN   = "\033[96m"
    DIM    = "\033[2m"
except Exception:
    RESET = BOLD = GREEN = YELLOW = RED = CYAN = DIM = ""


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def slugify(text: str) -> str:
    """Remove characters that are illegal in file/folder names."""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text)
    text = text.strip(". ")
    return text or "unknown"


def print_banner():
    banner = f"""
{CYAN}{BOLD}
 ╔══════════════════════════════════════════════╗
 ║        SHAZAM → MP3  BULK DOWNLOADER         ║
 ║   yt-dlp + YouTube Music  |  No API limits   ║
 ╚══════════════════════════════════════════════╝
{RESET}"""
    print(banner)


def ask_path(prompt: str, must_exist: bool = False) -> Path:
    """Ask the user for a filesystem path with basic validation."""
    while True:
        raw = input(f"{CYAN}{prompt}{RESET} ").strip().strip('"').strip("'")
        if not raw:
            print(f"{RED}  ✗ Please enter a path.{RESET}")
            continue
        p = Path(raw).expanduser().resolve()
        if must_exist and not p.exists():
            print(f"{RED}  ✗ Path does not exist: {p}{RESET}")
            continue
        return p


def ask_int(prompt: str, default: int, min_val: int, max_val: int) -> int:
    """Ask for an integer in [min_val, max_val], return default on blank."""
    while True:
        raw = input(f"{CYAN}{prompt} [{default}]: {RESET}").strip()
        if raw == "":
            return default
        if raw.isdigit() and min_val <= int(raw) <= max_val:
            return int(raw)
        print(f"{RED}  ✗ Enter a number between {min_val} and {max_val}.{RESET}")


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 1 — READ & DEDUPLICATE CSV
# ══════════════════════════════════════════════════════════════════════════════

def load_shazam_csv(csv_path: Path) -> list[dict]:
    """
    Parse a Shazam CSV export and return a deduplicated list of songs.

    Shazam CSV format (two variants seen in the wild):
      • New format  : Index, TagTime, Title, Artist, URL
      • Legacy      : TrackKey, TagTime, Title, Artist, …

    Deduplication key: (title.lower(), artist.lower())
    """
    songs: list[dict] = []
    seen: set[tuple[str, str]] = set()
    duplicates = 0
    skipped = 0

    try:
        with open(csv_path, newline="", encoding="utf-8-sig") as fh:
            # Shazam sometimes adds a BOM and/or extra blank header rows
            raw = fh.read()
    except UnicodeDecodeError:
        with open(csv_path, newline="", encoding="latin-1") as fh:
            raw = fh.read()

    # Remove any leading blank lines before the header
    lines = [l for l in raw.splitlines() if l.strip()]
    if not lines:
        print(f"{RED}  ✗ CSV is empty.{RESET}")
        sys.exit(1)

    reader = csv.DictReader(lines)
    headers = [h.strip().lower() for h in (reader.fieldnames or [])]

    # Map flexible column names → canonical names
    col_title  = next((h for h in headers if "title" in h),  None)
    col_artist = next((h for h in headers if "artist" in h), None)

    if not col_title or not col_artist:
        print(
            f"{RED}  ✗ Could not find 'Title' and 'Artist' columns.\n"
            f"     Found columns: {reader.fieldnames}{RESET}"
        )
        sys.exit(1)

    # Re-open with correct field mapping
    reader = csv.DictReader(lines)
    for row in reader:
        # Normalise column names to lower-stripped versions
        row_norm = {k.strip().lower(): v.strip() for k, v in row.items()}

        title  = row_norm.get(col_title,  "").strip()
        artist = row_norm.get(col_artist, "").strip()

        if not title or not artist:
            skipped += 1
            continue

        key = (title.lower(), artist.lower())
        if key in seen:
            duplicates += 1
            continue

        seen.add(key)
        songs.append({"title": title, "artist": artist})

    print(
        f"  {GREEN}✓{RESET} Parsed CSV — "
        f"{BOLD}{len(songs)}{RESET} unique songs, "
        f"{YELLOW}{duplicates}{RESET} duplicates removed, "
        f"{DIM}{skipped} rows skipped (missing data){RESET}"
    )
    return songs


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 2 — DOWNLOAD WITH yt-dlp  (YouTube Music, no API key)
# ══════════════════════════════════════════════════════════════════════════════

def check_yt_dlp():
    """Verify yt-dlp is importable; give a friendly error if not."""
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        print(
            f"\n{RED}{BOLD}  ✗ yt-dlp is not installed.{RESET}\n"
            f"  Run:  {CYAN}pip install yt-dlp{RESET}\n"
        )
        sys.exit(1)


def check_ffmpeg():
    """Warn if ffmpeg is not on PATH (needed for MP3 conversion)."""
    import shutil
    if shutil.which("ffmpeg") is None:
        print(
            f"\n{YELLOW}  ⚠  ffmpeg not found on PATH.{RESET}\n"
            f"     Audio will still download but may be in .webm / .m4a\n"
            f"     Install ffmpeg for automatic MP3 conversion:\n"
            f"       Windows : https://www.gyan.dev/ffmpeg/builds/\n"
            f"       macOS   : brew install ffmpeg\n"
            f"       Linux   : sudo apt install ffmpeg\n"
        )
        return False
    return True


def build_ydl_opts(output_dir: Path, sleep_interval: int) -> dict:
    """
    Build yt-dlp options for best-quality audio → MP3 conversion.

    Strategy:
      • Search YouTube Music  (ytsearch: prefix → music.youtube.com)
      • Extract audio only
      • Convert to MP3 320 kbps with ffmpeg
      • Embed thumbnail + metadata (title, artist, album, year)
      • Random sleep between downloads to avoid soft rate-limiting
    """
    outtmpl = str(output_dir / "%(artist)s - %(title)s.%(ext)s")

    return {
        # ── Search source ──────────────────────────────────────────────────
        # "ytmsearch1:" searches YouTube Music and picks the top result.
        # This gives far better music-specific results than plain YouTube.
        # No API key, no quota — it's just a web scrape.
        "default_search": "ytmsearch",

        # ── Audio extraction ──────────────────────────────────────────────
        "format": "bestaudio/best",
        "postprocessors": [
            {
                # Convert to MP3, highest quality
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            },
            {
                # Embed thumbnail as album art
                "key": "EmbedThumbnail",
            },
            {
                # Write title/artist/year into ID3 tags
                "key": "FFmpegMetadata",
                "add_metadata": True,
            },
        ],
        "writethumbnail": True,

        # ── Output ────────────────────────────────────────────────────────
        "outtmpl": outtmpl,

        # ── Rate-limiting / politeness ────────────────────────────────────
        # Random wait between downloads — keeps YouTube Music happy
        "sleep_interval": max(1, sleep_interval - 1),
        "max_sleep_interval": sleep_interval + 2,
        "sleep_interval_requests": 1,

        # ── Error handling ────────────────────────────────────────────────
        "ignoreerrors": True,      # skip failed downloads, don't abort
        "retries": 5,
        "fragment_retries": 5,
        "retry_sleep_functions": {"http": lambda n: 2 ** n},  # exponential back-off

        # ── Misc ──────────────────────────────────────────────────────────
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,        # never download a whole playlist by accident
        "geo_bypass": True,
        "nocheckcertificate": False,
        "prefer_insecure": False,
    }


def download_song(ydl, title: str, artist: str, output_dir: Path) -> str:
    """
    Download a single song.  Returns 'ok', 'skipped', or 'failed'.
    """
    # Skip if a file with this name already exists
    safe_name = slugify(f"{artist} - {title}")
    for ext in ("mp3", "m4a", "webm", "opus"):
        if (output_dir / f"{safe_name}.{ext}").exists():
            return "skipped"

    # Search query — "Artist Title" gives best YouTube Music match
    query = f"{artist} {title}"

    try:
        # ytmsearch1: → top 1 result from YouTube Music
        info = ydl.extract_info(f"ytmsearch1:{query}", download=True)
        if info and info.get("entries"):
            entry = info["entries"][0]
            if entry:
                return "ok"
        return "failed"
    except Exception:
        return "failed"


def download_all(
    songs: list[dict],
    output_dir: Path,
    sleep_interval: int,
    resume_from: int = 0,
):
    """
    Iterate over the deduplicated song list and download each one.
    Writes a progress log file alongside the downloads.
    """
    import yt_dlp

    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "_download_log.csv"

    # Load existing log to allow resuming interrupted sessions
    completed: set[tuple[str, str]] = set()
    if log_path.exists():
        with open(log_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("status") in ("ok", "skipped"):
                    completed.add((row["title"].lower(), row["artist"].lower()))

    log_file = open(log_path, "a", newline="", encoding="utf-8")
    log_writer = csv.DictWriter(log_file, fieldnames=["title", "artist", "status", "timestamp"])
    if log_path.stat().st_size == 0:
        log_writer.writeheader()

    opts = build_ydl_opts(output_dir, sleep_interval)

    total   = len(songs)
    ok      = 0
    skipped = 0
    failed  = 0
    failed_list: list[str] = []

    print(f"\n{BOLD}  Starting downloads → {output_dir}{RESET}\n")

    with yt_dlp.YoutubeDL(opts) as ydl:
        for idx, song in enumerate(songs, start=1):
            title  = song["title"]
            artist = song["artist"]
            key    = (title.lower(), artist.lower())

            # Resume: skip already-completed entries
            if key in completed:
                skipped += 1
                print(
                    f"  {DIM}[{idx:>4}/{total}] ⏭  {artist} — {title} (already done){RESET}"
                )
                continue

            print(
                f"  {CYAN}[{idx:>4}/{total}]{RESET} ⬇  "
                f"{BOLD}{artist}{RESET} — {title}  ",
                end="",
                flush=True,
            )

            status = download_song(ydl, title, artist, output_dir)

            if status == "ok":
                ok += 1
                print(f"{GREEN}✓ done{RESET}")
            elif status == "skipped":
                skipped += 1
                print(f"{YELLOW}⏭ skipped (file exists){RESET}")
            else:
                failed += 1
                failed_list.append(f"{artist} — {title}")
                print(f"{RED}✗ failed{RESET}")

            log_writer.writerow({
                "title":     title,
                "artist":    artist,
                "status":    status,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            log_file.flush()

    log_file.close()

    # ── Summary ────────────────────────────────────────────────────────────
    print(f"\n{BOLD}{'─'*50}{RESET}")
    print(f"  {GREEN}✓  Downloaded : {ok}{RESET}")
    print(f"  {YELLOW}⏭  Skipped    : {skipped}{RESET}")
    print(f"  {RED}✗  Failed     : {failed}{RESET}")
    print(f"  📁 Output dir : {output_dir}")
    print(f"  📋 Log file   : {log_path}")

    if failed_list:
        failed_log = output_dir / "_failed_songs.txt"
        failed_log.write_text("\n".join(failed_list), encoding="utf-8")
        print(
            f"\n  {YELLOW}Failed songs saved to:{RESET} {failed_log}\n"
            f"  Tip: re-run the script — it will skip completed songs\n"
            f"       and retry only the failed ones automatically.\n"
        )

    print(f"{BOLD}{'─'*50}{RESET}\n")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print_banner()

    # ── Dependency check ───────────────────────────────────────────────────
    check_yt_dlp()
    has_ffmpeg = check_ffmpeg()

    # ── User inputs ────────────────────────────────────────────────────────
    print(f"{BOLD}  Step 1 of 3 — Locate your Shazam CSV{RESET}")
    print(
        f"  {DIM}Export it at shazam.com → Library → top-right ⋮ → Export{RESET}\n"
    )
    csv_path = ask_path("  CSV file path:", must_exist=True)

    print(f"\n{BOLD}  Step 2 of 3 — Output directory{RESET}")
    print(f"  {DIM}Where should the MP3 files be saved?{RESET}\n")
    output_dir = ask_path("  Output directory:", must_exist=False)

    print(f"\n{BOLD}  Step 3 of 3 — Download speed{RESET}")
    print(
        f"  {DIM}Seconds to wait between downloads.\n"
        f"  Lower = faster, higher = safer (avoids soft rate-limits).\n"
        f"  Recommended: 3–5 for a large library, 1–2 for small.{RESET}\n"
    )
    sleep_interval = ask_int("  Delay (seconds)", default=3, min_val=1, max_val=30)

    # ── Parse CSV ──────────────────────────────────────────────────────────
    print(f"\n{BOLD}  Parsing CSV …{RESET}")
    songs = load_shazam_csv(csv_path)

    if not songs:
        print(f"{RED}  No songs found. Check your CSV file.{RESET}")
        sys.exit(1)

    # Preview first 10
    print(f"\n  {DIM}First 10 songs in queue:{RESET}")
    for i, s in enumerate(songs[:10], 1):
        print(f"  {DIM}  {i:>3}. {s['artist']} — {s['title']}{RESET}")
    if len(songs) > 10:
        print(f"  {DIM}  … and {len(songs) - 10} more{RESET}")

    # Estimated time
    est_min = len(songs) * (sleep_interval + 20) // 60
    print(
        f"\n  {YELLOW}Estimated time: ~{est_min} min "
        f"({len(songs)} songs × ~{sleep_interval + 20}s each){RESET}"
    )

    # Confirm
    print()
    confirm = input(
        f"  {CYAN}Start downloading {BOLD}{len(songs)}{RESET}{CYAN} songs? [Y/n]: {RESET}"
    ).strip().lower()
    if confirm not in ("", "y", "yes"):
        print(f"\n  {YELLOW}Aborted.{RESET}\n")
        sys.exit(0)

    # ── Download ───────────────────────────────────────────────────────────
    if not has_ffmpeg:
        print(
            f"\n  {YELLOW}⚠  Continuing without ffmpeg — "
            f"files will be saved in original audio format (usually .webm or .m4a){RESET}"
        )

    download_all(songs, output_dir, sleep_interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n\n  {YELLOW}Interrupted by user. Progress saved — re-run to resume.{RESET}\n")
        sys.exit(0)