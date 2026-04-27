# Organize-Photos
Python script specifically made for Arch Linux to sanitize names and organize in a directory structure
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
