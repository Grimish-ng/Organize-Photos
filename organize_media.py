#!/usr/bin/env python3
"""
organize_media.py — Organize photos & videos by creation date; remove duplicates.
Optimized for Arch Linux with exiftool (perl-image-exiftool).

SETUP (Arch Linux):
  sudo pacman -S perl-image-exiftool python-xxhash
  pip install Pillow --break-system-packages   # optional, for JPEG fallback

OUTPUT STRUCTURE (optimized for OneDrive / ProtonDrive):
  OUTPUT/
    2024/
      2024-06-15/
        photo.jpg
        video.mp4
    duplicates/        <- moved here for review (use --delete-dupes to remove)
    unorganized/       <- files where no date metadata could be found

USAGE:
  python organize_media.py --input ~/Pictures --output ~/Organized --dry-run
  python organize_media.py --input ~/Pictures --output ~/Organized --move
  python organize_media.py --input ~/Pictures --output ~/Organized --move --delete-dupes

OPTIONS:
  --input DIR         Source directory (searched recursively)
  --output DIR        Destination directory (created if needed)
  --move              Move files instead of copying (default: copy)
  --dry-run           Preview only - no files changed
  --no-dedupe         Skip duplicate detection
  --delete-dupes      Permanently delete duplicates instead of moving them
  --workers N         Parallel hashing workers (default: 4)
"""

import argparse
import json
import logging
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# -- Optional fast hasher (python-xxhash via pacman) -------------------------
try:
    import xxhash
    def file_hash(path: Path, chunk: int = 65536) -> str:
        h = xxhash.xxh3_128()
        with open(path, "rb") as f:
            while buf := f.read(chunk):
                h.update(buf)
        return h.hexdigest()
except ImportError:
    import hashlib
    def file_hash(path: Path, chunk: int = 65536) -> str:  # type: ignore[misc]
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while buf := f.read(chunk):
                h.update(buf)
        return h.hexdigest()

# -- Optional Pillow (JPEG EXIF fallback only) --------------------------------
try:
    from PIL import Image
    from PIL.ExifTags import TAGS as PIL_TAGS
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# -- Logging ------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("organize_media")

# -- Supported extensions -----------------------------------------------------
IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".tiff", ".tif",
    ".webp", ".bmp", ".gif", ".raw", ".cr2", ".cr3", ".nef", ".arw",
    ".dng", ".orf", ".rw2", ".pef", ".srw", ".erf", ".raf", ".3fr",
}
VIDEO_EXTS = {
    ".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp", ".wmv",
    ".flv", ".webm", ".mts", ".m2ts", ".ts", ".mpg", ".mpeg",
    ".f4v", ".vob", ".ogv", ".dv",
}
ALL_EXTS = IMAGE_EXTS | VIDEO_EXTS

# -- OneDrive-illegal filename characters -------------------------------------
# Characters banned in OneDrive/SharePoint filenames (and Windows paths)
_ONEDRIVE_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Names reserved on Windows (case-insensitive, with or without extension)
_WINDOWS_RESERVED = re.compile(
    r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)', re.IGNORECASE
)

def sanitize_filename(name: str) -> str:
    """
    Make a filename safe for OneDrive / Windows:
      - Replace illegal characters with '_'
      - Strip leading/trailing spaces and dots (Windows dislikes them)
      - Prefix reserved names with '_'
      - Collapse multiple consecutive underscores introduced by substitution
    The file extension is preserved unchanged.
    """
    # Split on the LAST dot so 'photo.jpg' -> stem='photo', suffix='.jpg'
    # Special-case bare extensions like '.jpg' -> stem='', suffix='.jpg'
    dot = name.rfind('.')
    if dot > 0:
        stem, suffix = name[:dot], name[dot:]
    elif dot == 0:          # e.g. '.jpg' — treat whole thing as an extension
        stem, suffix = '', name
    else:
        stem, suffix = name, ''

    # Replace illegal chars (includes control chars \x00-\x1f)
    stem = _ONEDRIVE_ILLEGAL.sub('_', stem)
    # Collapse runs of underscores created by substitution
    stem = re.sub(r'_{2,}', '_', stem)
    # Strip leading/trailing spaces, dots, and lone underscores left by stripping
    stem = stem.strip(' ._')
    # Fall back to 'file' if stem is now empty
    if not stem:
        stem = 'file'
    # Prefix Windows reserved names
    if _WINDOWS_RESERVED.match(stem):
        stem = '_' + stem

    return stem + suffix


# -- Graceful Ctrl+C ----------------------------------------------------------
_interrupted = False

def _handle_sigint(sig, frame):
    global _interrupted
    _interrupted = True
    print("\n\n  Interrupted - finishing in-flight jobs then stopping...\n")

signal.signal(signal.SIGINT, _handle_sigint)


# -- exiftool wrapper (primary date source) -----------------------------------

_EXIFTOOL_AVAILABLE: Optional[bool] = None

def _check_exiftool() -> bool:
    global _EXIFTOOL_AVAILABLE
    if _EXIFTOOL_AVAILABLE is None:
        try:
            subprocess.run(["exiftool", "-ver"], capture_output=True, timeout=5)
            _EXIFTOOL_AVAILABLE = True
        except FileNotFoundError:
            _EXIFTOOL_AVAILABLE = False
            log.warning(
                "exiftool not found - metadata coverage will be very limited.\n"
                "         Install with: sudo pacman -S perl-image-exiftool"
            )
    return _EXIFTOOL_AVAILABLE


def _exiftool_date(path: Path) -> Optional[datetime]:
    """
    Call exiftool to get the best creation date.
    Tries DateTimeOriginal, CreateDate, TrackCreateDate, MediaCreateDate in order.
    """
    try:
        result = subprocess.run(
            [
                "exiftool", "-s3", "-fast2",
                "-DateTimeOriginal",
                "-CreateDate",
                "-TrackCreateDate",
                "-MediaCreateDate",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if line and not line.startswith("0000"):
                dt = _parse_dt(line)
                if dt and dt.year > 1970:
                    return dt
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


# -- Date parsing helpers -----------------------------------------------------

def _parse_dt(value: str) -> Optional[datetime]:
    for fmt in (
        "%Y:%m:%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(value.strip()[:19], fmt)
        except ValueError:
            continue
    return None


def _date_from_pil(path: Path) -> Optional[datetime]:
    """JPEG/TIFF EXIF via Pillow - fallback when exiftool unavailable."""
    if not HAS_PIL:
        return None
    try:
        img = Image.open(path)
        exif = img._getexif()
        if not exif:
            return None
        tag_map = {v: k for k, v in PIL_TAGS.items()}
        for name in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
            tid = tag_map.get(name)
            if tid and tid in exif:
                dt = _parse_dt(str(exif[tid]))
                if dt:
                    return dt
    except Exception:
        pass
    return None


def _date_from_mp4_box(path: Path) -> Optional[datetime]:
    """
    Read QuickTime 'mvhd' atom from MP4/MOV - seconds since 1904-01-01.
    Fallback for when exiftool is absent.
    """
    QT_EPOCH = datetime(1904, 1, 1)
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            data = f.read(min(2 * 1024 * 1024, size))
        idx = 0
        while idx < len(data) - 8:
            box_size = struct.unpack(">I", data[idx: idx + 4])[0]
            box_type = data[idx + 4: idx + 8]
            if box_type == b"mvhd" and box_size >= 32:
                version = data[idx + 8]
                ts = (
                    struct.unpack(">I", data[idx + 12: idx + 16])[0]
                    if version == 0
                    else struct.unpack(">Q", data[idx + 12: idx + 20])[0]
                )
                if ts > 0:
                    dt = QT_EPOCH + timedelta(seconds=int(ts))
                    if dt.year > 1970:
                        return dt
            if box_size < 8:
                break
            idx += box_size
    except Exception:
        pass
    return None


def _date_from_filename(path: Path) -> Optional[datetime]:
    """Extract date from filename patterns like IMG_20230615, 2023-06-15, etc."""
    name = path.stem
    patterns = [
        r"(\d{4})[-_](\d{2})[-_](\d{2})",
        r"(\d{4})(\d{2})(\d{2})",
    ]
    for pat in patterns:
        m = re.search(pat, name)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 1970 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
                try:
                    return datetime(y, mo, d)
                except ValueError:
                    continue
    return None


def get_creation_date(path: Path) -> tuple:
    """
    Return (datetime, is_from_metadata).
    Priority: exiftool -> Pillow EXIF -> MP4 box -> filename -> mtime.
    'is_from_metadata' is False only when falling back to mtime.
    """
    ext = path.suffix.lower()

    # 1. exiftool - handles images, RAWs, all video formats, HEIC
    if _check_exiftool():
        dt = _exiftool_date(path)
        if dt:
            return dt, True

    # 2. Pillow EXIF (JPEG/TIFF only) - fallback if exiftool missing
    if ext in {".jpg", ".jpeg", ".tiff", ".tif"}:
        dt = _date_from_pil(path)
        if dt:
            return dt, True

    # 3. MP4/MOV QuickTime atom
    if ext in {".mp4", ".mov", ".m4v", ".3gp"}:
        dt = _date_from_mp4_box(path)
        if dt:
            return dt, True

    # 4. Filename heuristic
    dt = _date_from_filename(path)
    if dt:
        return dt, True

    # 5. Filesystem mtime - least reliable
    return datetime.fromtimestamp(path.stat().st_mtime), False


# -- Destination helpers -------------------------------------------------------

def build_dest_path(output_root: Path, dt: datetime, source: Path) -> Path:
    """
    Year / Year-Month-Day / filename  (3 levels — optimal for OneDrive)
    e.g.  2024/2024-06-15/IMG_1234.jpg
    Filename is sanitized for OneDrive/Windows compatibility.
    """
    safe_name = sanitize_filename(source.name)
    return (
        output_root
        / dt.strftime("%Y")
        / dt.strftime("%Y-%m-%d")
        / safe_name
    )


def unique_path(dest: Path) -> Path:
    """Append _2, _3, ... if destination already exists."""
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    counter = 2
    while True:
        candidate = dest.parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def safe_transfer(src: Path, dest: Path, move: bool) -> None:
    """Copy or move, preserving all timestamps and permissions."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if move:
        # shutil.move loses metadata on cross-device moves; do it manually
        shutil.copy2(src, dest)
        shutil.copystat(src, dest)
        src.unlink()
    else:
        shutil.copy2(src, dest)
        shutil.copystat(src, dest)


# -- Organizer ----------------------------------------------------------------

class MediaOrganizer:
    def __init__(
        self,
        input_dir: Path,
        output_dir: Path,
        move: bool = False,
        dry_run: bool = False,
        dedupe: bool = True,
        delete_dupes: bool = False,
        workers: int = 4,
    ):
        self.input_dir    = input_dir
        self.output_dir   = output_dir
        self.move         = move
        self.dry_run      = dry_run
        self.dedupe       = dedupe
        self.delete_dupes = delete_dupes
        self.workers      = workers

        self.seen_hashes: dict = {}
        self._hash_lock = threading.Lock()

        self.stats = {
            "total":       0,
            "organized":   0,
            "duplicates":  0,
            "unorganized": 0,
            "errors":      0,
        }

    # -- Discovery ------------------------------------------------------------

    def collect_files(self) -> list:
        files = []
        for p in self.input_dir.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() not in ALL_EXTS:
                continue
            try:
                p.relative_to(self.output_dir)
                continue  # skip files already inside output tree
            except ValueError:
                pass
            files.append(p)
        log.info(f"Found {len(files):,} media files under {self.input_dir}")
        return files

    # -- Single file ----------------------------------------------------------

    def process_file(self, src: Path) -> dict:
        result: dict = {"src": src, "action": None, "dest": None, "error": None}
        try:
            h = file_hash(src) if self.dedupe else None

            # Duplicate check
            if h is not None:
                with self._hash_lock:
                    if h in self.seen_hashes:
                        result["action"] = "duplicates"
                        result["original"] = self.seen_hashes[h]
                        if not self.dry_run:
                            if self.delete_dupes:
                                src.unlink()
                            else:
                                dest = unique_path(
                                    self.output_dir / "duplicates" / sanitize_filename(src.name)
                                )
                                safe_transfer(src, dest, move=True)
                                result["dest"] = dest
                        return result
                    self.seen_hashes[h] = src

            # Date extraction
            dt, has_real_date = get_creation_date(src)

            if has_real_date:
                dest = build_dest_path(self.output_dir, dt, src)
                result["action"] = "organized"
            else:
                dest = self.output_dir / "unorganized" / sanitize_filename(src.name)
                result["action"] = "unorganized"

            dest = unique_path(dest)
            result["dest"] = dest

            if not self.dry_run:
                safe_transfer(src, dest, self.move)

        except Exception as e:
            result["action"] = "errors"
            result["error"] = str(e)
            log.error(f"  ERROR processing {src.name}: {e}")

        return result

    # -- Run ------------------------------------------------------------------

    def run(self):
        if not self.input_dir.exists():
            log.error(f"Input directory not found: {self.input_dir}")
            sys.exit(1)

        if self.dry_run:
            log.info("DRY RUN - no files will be changed")

        if self.delete_dupes and not self.dry_run:
            confirm = input(
                "\n  WARNING: --delete-dupes will PERMANENTLY DELETE duplicate files.\n"
                "  Type YES to confirm: "
            )
            if confirm.strip() != "YES":
                print("Aborted.")
                sys.exit(0)

        files = self.collect_files()
        self.stats["total"] = len(files)
        log.info(f"Processing {len(files):,} files with {self.workers} worker(s)...")

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self.process_file, f): f for f in files}
            done = 0
            for future in as_completed(futures):
                if _interrupted:
                    pool.shutdown(wait=False, cancel_futures=True)
                    break
                done += 1
                r = future.result()
                action = r.get("action")
                if action in self.stats:
                    self.stats[action] += 1
                else:
                    self.stats["errors"] += 1

                if done % 200 == 0 or done == len(files):
                    pct = done / len(files) * 100
                    log.info(
                        f"  {done:,}/{len(files):,} ({pct:.0f}%)  "
                        f"organized={self.stats['organized']}  "
                        f"dupes={self.stats['duplicates']}  "
                        f"unorganized={self.stats['unorganized']}"
                    )

        self._print_summary()
        if not self.dry_run:
            self._write_report()

    def _print_summary(self):
        s = self.stats
        verb = "Would move/copy" if self.dry_run else ("Moved" if self.move else "Copied")
        print("\n" + "=" * 55)
        print("  ORGANIZE MEDIA - SUMMARY")
        print("=" * 55)
        print(f"  Total files scanned  : {s['total']:>7,}")
        print(f"  {verb:<21s}: {s['organized']:>7,}")
        dupes_note = "(deleted)" if self.delete_dupes else f"-> {self.output_dir}/duplicates/"
        print(f"  Duplicates           : {s['duplicates']:>7,}  {dupes_note}")
        print(f"  No metadata (mtime)  : {s['unorganized']:>7,}  -> {self.output_dir}/unorganized/")
        print(f"  Errors               : {s['errors']:>7,}")
        print("=" * 55)
        if s["duplicates"] and not self.delete_dupes:
            print(f"\n  Review duplicates/ before deleting.")
            print(f"  Re-run with --delete-dupes to remove them automatically.")
        if s["unorganized"]:
            print(f"\n  {s['unorganized']} file(s) had no EXIF/metadata date.")
            print(f"  Check unorganized/ and tag or rename them manually.")
        print()

    def _write_report(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        report = {
            "run_at": datetime.now().isoformat(),
            "input":  str(self.input_dir),
            "output": str(self.output_dir),
            "move":   self.move,
            "stats":  self.stats,
        }
        path = self.output_dir / "organize_report.json"
        with open(path, "w") as f:
            json.dump(report, f, indent=2)
        log.info(f"Report saved -> {path}")


# -- CLI ----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Organize photos/videos by metadata date; remove duplicates.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--input",        required=True, help="Source directory")
    ap.add_argument("--output",       required=True, help="Destination directory")
    ap.add_argument("--move",         action="store_true",
                    help="Move files instead of copying (default: copy)")
    ap.add_argument("--dry-run",      action="store_true",
                    help="Preview only - no files changed")
    ap.add_argument("--no-dedupe",    action="store_true",
                    help="Skip duplicate detection")
    ap.add_argument("--delete-dupes", action="store_true",
                    help="Permanently delete duplicates (requires confirmation)")
    ap.add_argument("--workers",      type=int, default=4,
                    help="Parallel hashing workers (default: 4)")
    args = ap.parse_args()

    organizer = MediaOrganizer(
        input_dir    = Path(args.input).expanduser().resolve(),
        output_dir   = Path(args.output).expanduser().resolve(),
        move         = args.move,
        dry_run      = args.dry_run,
        dedupe       = not args.no_dedupe,
        delete_dupes = args.delete_dupes,
        workers      = args.workers,
    )
    organizer.run()


if __name__ == "__main__":
    main()
