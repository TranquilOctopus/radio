from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class FetchError(RuntimeError):
    pass


@dataclass(slots=True)
class FetchedAudio:
    title: str
    audio_path: Path
    duration_s: int | None
    source_url: str
    uploader: str | None
    upload_date: str | None  # ISO YYYY-MM-DD


def fetch_audio_url(
    url: str,
    output_dir: Path,
    *,
    audio_format: str = "mp3",
    quality: str = "192",
) -> FetchedAudio:
    """
    Downloads audio from a URL via yt-dlp. Extracts audio if the source is video.
    Returns metadata + the local path. Raises FetchError on failure.

    Requires yt-dlp (pip install podcast-brain[url]) and ffmpeg on PATH.
    """
    try:
        import yt_dlp
        from yt_dlp.utils import DownloadError
    except ImportError as exc:
        raise ImportError(
            "yt-dlp is not installed. Run: pip install podcast-brain[url]"
        ) from exc

    output_dir.mkdir(parents=True, exist_ok=True)

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(output_dir / "%(id)s.%(ext)s"),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_format,
                "preferredquality": quality,
            }
        ],
        "quiet": True,
        "no_warnings": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except DownloadError as exc:
        raise FetchError(str(exc)) from exc

    if info is None:
        raise FetchError(f"yt-dlp returned no info for {url!r}")

    # yt-dlp returns playlist dicts when the URL resolves to a playlist; take
    # the first entry so we always deal with a single track.
    if "entries" in info:
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise FetchError(f"yt-dlp playlist for {url!r} contained no entries")
        info = entries[0]

    raw_date = info.get("upload_date")  # YYYYMMDD or None
    iso_date: str | None = None
    if raw_date and len(raw_date) == 8:
        iso_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"

    duration = info.get("duration")
    duration_s = int(duration) if duration is not None else None

    audio_path = output_dir / f"{info['id']}.{audio_format}"

    return FetchedAudio(
        title=info.get("title") or url,
        audio_path=audio_path,
        duration_s=duration_s,
        source_url=url,
        uploader=info.get("uploader"),
        upload_date=iso_date,
    )
