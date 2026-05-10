from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from queue import Queue as ThreadQueue
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pkm.queue import Queue

log = logging.getLogger(__name__)

_AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".flac", ".opus"}

# Synthetic feed URL for the inbox; never fetched over the network.
_INBOX_FEED_URL = "inbox://local"
_INBOX_TITLE = "Inbox"
_INBOX_SLUG = "inbox"


def _file_guid(path: Path) -> str:
    return hashlib.sha1(str(path.resolve()).encode()).hexdigest()


def _is_settled(path: Path, settle_seconds: float) -> bool:
    """
    Return True only after the file has stopped growing for settle_seconds.
    Polling prevents us from enqueuing a file that a slow copy is still writing.
    """
    try:
        size_before = path.stat().st_size
    except OSError:
        return False
    time.sleep(settle_seconds)
    try:
        size_after = path.stat().st_size
    except OSError:
        return False
    return size_before == size_after and size_after > 0


def _resolve_inbox_feed(queue: Queue, feed_id_override: int | None) -> int:
    from pkm.queue import FeedRow

    if feed_id_override is not None:
        return feed_id_override

    existing = queue.get_feed_by_slug(_INBOX_SLUG)
    if existing is not None:
        return existing.id  # type: ignore[return-value]

    return queue.upsert_feed(
        FeedRow(
            feed_url=_INBOX_FEED_URL,
            title=_INBOX_TITLE,
            podcast_slug=_INBOX_SLUG,
            style="informational",
        )
    )


def _enqueue_file(path: Path, queue: Queue, feed_id: int, settle_seconds: float) -> None:
    if path.suffix.lower() not in _AUDIO_EXTENSIONS:
        return
    if not _is_settled(path, settle_seconds):
        log.warning("inbox: %s not settled after %.1fs — skipping", path, settle_seconds)
        return

    from pkm.queue import JobRow

    job_id = queue.enqueue_job(
        JobRow(
            feed_id=feed_id,
            episode_guid=_file_guid(path),
            episode_title=path.stem,
            episode_url=f"file://{path.resolve()}",
            status="PENDING",
        )
    )
    if job_id is not None:
        log.info("inbox: enqueued %s as job %d", path.name, job_id)
    else:
        log.debug("inbox: %s already enqueued (deduped)", path.name)


class InboxWatcher:
    def __init__(
        self,
        watch_dir: Path,
        queue: Queue,
        feed_id_for_inbox: int | None = None,
        settle_seconds: float = 2.0,
    ) -> None:
        self._watch_dir = watch_dir
        self._queue = queue
        self._feed_id_override = feed_id_for_inbox
        self._settle_seconds = settle_seconds
        self._observer = None

    def _get_feed_id(self) -> int:
        return _resolve_inbox_feed(self._queue, self._feed_id_override)

    def start(self) -> None:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        feed_id = self._get_feed_id()

        # Process files already present so nothing dropped before the daemon
        # started is silently ignored.
        for existing in self._watch_dir.iterdir():
            if existing.is_file():
                _enqueue_file(existing, self._queue, feed_id, self._settle_seconds)

        watcher = self

        class _Handler(FileSystemEventHandler):
            def on_created(self, event):
                if event.is_directory:
                    return
                path = Path(event.src_path)
                _enqueue_file(path, watcher._queue, feed_id, watcher._settle_seconds)

            # Also handle moves into the watch dir (e.g. mv file.mp3 inbox/).
            def on_moved(self, event):
                if event.is_directory:
                    return
                path = Path(event.dest_path)
                if path.parent.resolve() == watcher._watch_dir.resolve():
                    _enqueue_file(path, watcher._queue, feed_id, watcher._settle_seconds)

        observer = Observer()
        observer.schedule(_Handler(), str(self._watch_dir), recursive=False)
        observer.start()
        self._observer = observer
        log.info("inbox: watching %s", self._watch_dir)

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join()
            self._observer = None
