from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Iterator, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from pkm.budget import BudgetTracker
from pkm.config import Config, load_config
from pkm.queue import FeedRow, JobRow, Queue

log = logging.getLogger(__name__)

_VALID_STYLES = ("informational", "banter", "narrative", "skip")


# ---------------------------------------------------------------------------
# Dependency providers (overridable via app.dependency_overrides in tests)
# ---------------------------------------------------------------------------


def _make_config_provider(config_path: Path | None):
    def provide_config() -> Config:
        return load_config(config_path)
    return provide_config


def _make_queue_provider(config_path: Path | None):
    def provide_queue() -> Iterator[Queue]:
        cfg = load_config(config_path)
        q = Queue(Path(cfg.paths.db_path))
        q.init_schema()
        try:
            yield q
        finally:
            q.close()
    return provide_queue


def _make_budget_provider(config_path: Path | None):
    def provide_budget() -> Iterator[BudgetTracker]:
        cfg = load_config(config_path)
        b = BudgetTracker(Path(cfg.paths.db_path), cfg.budget)
        try:
            yield b
        finally:
            b.close()
    return provide_budget


# ---------------------------------------------------------------------------
# Response / request models
# ---------------------------------------------------------------------------


class QueueSummary(BaseModel):
    counts: dict[str, int]
    total: int


class JobView(BaseModel):
    id: int
    feed_id: int
    feed_title: str | None = None
    feed_slug: str | None = None
    episode_title: str
    status: str
    priority: int
    attempts: int
    last_error: str | None
    enqueued_at: str | None
    updated_at: str | None


class FeedView(BaseModel):
    id: int
    feed_url: str
    title: str
    podcast_slug: str
    style: str
    language: str | None
    last_polled_at: str | None
    pending_jobs: int


class BudgetView(BaseModel):
    mtd_spend_usd: float
    monthly_cap_usd: float
    remaining_usd: float | None  # None when cap is unlimited
    pct_used: float | None
    by_model: list[dict]


class StyleUpdate(BaseModel):
    style: Literal["informational", "banter", "narrative", "skip"]


class UrlSubmit(BaseModel):
    url: str
    style: Literal["informational", "banter", "narrative", "skip"] = "informational"


class FileSubmit(BaseModel):
    path: str
    style: Literal["informational", "banter", "narrative", "skip"] = "informational"


class FeedAdd(BaseModel):
    url: str
    style: Literal["informational", "banter", "narrative", "skip"] = "informational"


class FeedSearchResult(BaseModel):
    itunes_id: int
    name: str
    artist: str | None
    feed_url: str


class DigestView(BaseModel):
    week: str  # YYYY-Www
    path: str
    markdown: str


# ---------------------------------------------------------------------------
# Synthetic feed helpers (matches conventions in inbox.py / url.py)
# ---------------------------------------------------------------------------


def _ensure_feed(queue: Queue, slug: str, title: str, feed_url: str) -> FeedRow:
    existing = queue.get_feed_by_slug(slug)
    if existing is not None:
        return existing
    feed_id = queue.upsert_feed(
        FeedRow(
            feed_url=feed_url, title=title, podcast_slug=slug, style="informational",
        )
    )
    out = queue.get_feed_by_slug(slug)
    assert out is not None and out.id == feed_id
    return out


def _job_view(queue: Queue, job: JobRow) -> JobView:
    feed = queue.get_feed_by_id(job.feed_id) if hasattr(queue, "get_feed_by_id") else None
    return JobView(
        id=job.id or 0,
        feed_id=job.feed_id,
        feed_title=feed.title if feed else None,
        feed_slug=feed.podcast_slug if feed else None,
        episode_title=job.episode_title,
        status=job.status,
        priority=job.priority,
        attempts=job.attempts,
        last_error=(job.last_error[:200] if job.last_error else None),
        enqueued_at=job.enqueued_at,
        updated_at=job.updated_at,
    )


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(config_path: Path | None = None) -> FastAPI:
    app = FastAPI(title="podcast-brain", version="0.0.1")
    get_config = _make_config_provider(config_path)
    get_queue = _make_queue_provider(config_path)
    get_budget = _make_budget_provider(config_path)

    # Stash provider refs so tests can override.
    app.state.get_config = get_config
    app.state.get_queue = get_queue
    app.state.get_budget = get_budget

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> HTMLResponse:
        return HTMLResponse(_DASHBOARD_HTML)

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/queue/summary", response_model=QueueSummary)
    def queue_summary(queue: Queue = Depends(get_queue)) -> QueueSummary:
        cur = queue._conn.execute(
            "SELECT status, COUNT(*) FROM jobs GROUP BY status ORDER BY status"
        )
        counts = {row[0]: int(row[1]) for row in cur.fetchall()}
        return QueueSummary(counts=counts, total=sum(counts.values()))

    @app.get("/api/queue/jobs", response_model=list[JobView])
    def queue_jobs(
        status: str | None = None,
        limit: int = 50,
        queue: Queue = Depends(get_queue),
    ) -> list[JobView]:
        limit = max(1, min(limit, 500))
        if status:
            jobs = queue.jobs_by_status(status, limit=limit)
        else:
            cur = queue._conn.execute(
                "SELECT * FROM jobs ORDER BY updated_at DESC LIMIT ?", (limit,)
            )
            from pkm.queue import _row_to_job
            jobs = [_row_to_job(r) for r in cur.fetchall()]
        return [_job_view(queue, j) for j in jobs]

    @app.get("/api/feeds", response_model=list[FeedView])
    def feeds_list(queue: Queue = Depends(get_queue)) -> list[FeedView]:
        feeds = queue.list_feeds()
        out: list[FeedView] = []
        for f in feeds:
            cur = queue._conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE feed_id = ? AND status = 'PENDING'",
                (f.id,),
            )
            pending = int(cur.fetchone()[0])
            out.append(
                FeedView(
                    id=f.id or 0,
                    feed_url=f.feed_url,
                    title=f.title,
                    podcast_slug=f.podcast_slug,
                    style=f.style,
                    language=f.language,
                    last_polled_at=f.last_polled_at,
                    pending_jobs=pending,
                )
            )
        return out

    @app.post("/api/feeds")
    def feed_add(
        body: FeedAdd,
        config: Config = Depends(get_config),
    ) -> dict:
        from pkm.ingest.subscribe import add_feed_from_url, resolve_input_to_feed_url

        feed_url = resolve_input_to_feed_url(body.url, config=config)
        if feed_url is None:
            raise HTTPException(
                status_code=400,
                detail="Input is not a recognised RSS or Apple Podcasts URL. "
                "Use /api/feeds/search for show-name lookups.",
            )
        try:
            feed_id, slug, _ = add_feed_from_url(
                feed_url, requested_style=body.style, auto_style=False, config=config,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Add failed: {exc}")
        return {"feed_id": feed_id, "slug": slug, "style": body.style, "feed_url": feed_url}

    @app.get("/api/feeds/search", response_model=list[FeedSearchResult])
    def feed_search(term: str, limit: int = 5) -> list[FeedSearchResult]:
        from pkm.ingest.itunes import search_podcast

        limit = max(1, min(limit, 20))
        results = search_podcast(term, limit=limit)
        return [
            FeedSearchResult(
                itunes_id=r.collection_id, name=r.collection_name,
                artist=r.artist_name, feed_url=r.feed_url,
            )
            for r in results
        ]

    @app.get("/api/digest/latest", response_model=DigestView)
    def digest_latest(config: Config = Depends(get_config)) -> DigestView:
        digests_dir = Path(config.paths.vault_dir) / "digests" / "weekly"
        if not digests_dir.exists():
            raise HTTPException(status_code=404, detail="No digests yet")
        files = sorted(digests_dir.glob("*.md"))
        if not files:
            raise HTTPException(status_code=404, detail="No digests yet")
        latest = files[-1]
        return DigestView(
            week=latest.stem, path=str(latest), markdown=latest.read_text(encoding="utf-8"),
        )

    @app.post("/api/feeds/{feed_id}/style")
    def feed_set_style(
        feed_id: int,
        body: StyleUpdate,
        queue: Queue = Depends(get_queue),
    ) -> dict:
        try:
            queue.set_feed_style(feed_id, body.style)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"updated": True, "feed_id": feed_id, "style": body.style}

    @app.get("/api/budget", response_model=BudgetView)
    def budget_view(
        config: Config = Depends(get_config),
        budget: BudgetTracker = Depends(get_budget),
    ) -> BudgetView:
        spend = budget.mtd_spend_usd()
        cap = config.budget.monthly_cap_usd
        remaining = budget.mtd_remaining_usd() if cap > 0 else None
        pct = (spend / cap * 100.0) if cap > 0 else None
        return BudgetView(
            mtd_spend_usd=spend,
            monthly_cap_usd=cap,
            remaining_usd=remaining if cap > 0 else None,
            pct_used=pct,
            by_model=budget.usage_breakdown(),
        )

    @app.post("/api/submit/url")
    def submit_url(
        body: UrlSubmit,
        background: BackgroundTasks,
        queue: Queue = Depends(get_queue),
        config: Config = Depends(get_config),
    ) -> dict:
        feed = _ensure_feed(
            queue, slug="url-imports", title="URL imports",
            feed_url="url-imports://local",
        )
        if body.style != feed.style:
            queue.set_feed_style(feed.id, body.style)

        # The job is enqueued as PENDING with the URL; the background task
        # downloads via yt-dlp and rewrites episode_url to file:// before the
        # pipeline picks it up. If the download fails, the job is marked FAILED.
        guid = "url-" + hashlib.sha1(body.url.encode()).hexdigest()[:16]
        job_id = queue.enqueue_job(
            JobRow(
                feed_id=feed.id, episode_guid=guid,
                episode_title=body.url, episode_url=body.url, status="URL_PENDING",
            )
        )
        if job_id is None:
            return {"queued": False, "reason": "duplicate"}

        background.add_task(
            _background_url_fetch, body.url, job_id,
            Path(config.paths.audio_dir), Path(config.paths.db_path),
        )
        return {"queued": True, "job_id": job_id}

    @app.post("/api/submit/file")
    def submit_file(
        body: FileSubmit,
        queue: Queue = Depends(get_queue),
    ) -> dict:
        path = Path(body.path).expanduser().resolve()
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=400, detail=f"File not found: {body.path}")

        feed = _ensure_feed(
            queue, slug="inbox", title="Inbox", feed_url="inbox://local",
        )
        guid = "file-" + hashlib.sha1(str(path).encode()).hexdigest()[:16]
        job_id = queue.enqueue_job(
            JobRow(
                feed_id=feed.id, episode_guid=guid,
                episode_title=path.stem, episode_url=f"file://{path}",
            )
        )
        if job_id is None:
            return {"queued": False, "reason": "duplicate"}
        return {"queued": True, "job_id": job_id}

    return app


def _background_url_fetch(
    url: str, job_id: int, audio_dir: Path, db_path: Path,
) -> None:
    """yt-dlp runs in a background task to keep the request fast."""
    try:
        from pkm.ingest.url import fetch_audio_url
    except ImportError as exc:
        log.error("yt-dlp not installed: %s", exc)
        with Queue(db_path) as q:
            q.update_job_status(job_id, "FAILED", error=f"yt-dlp not installed: {exc}")
        return

    try:
        audio = fetch_audio_url(url, audio_dir)
        with Queue(db_path) as q:
            q._conn.execute(
                "UPDATE jobs SET episode_url = ?, episode_title = ?, "
                "episode_duration_s = ?, status = 'PENDING', "
                "updated_at = datetime('now') WHERE id = ?",
                (
                    f"file://{audio.audio_path}",
                    audio.title or url,
                    audio.duration_s,
                    job_id,
                ),
            )
            q._conn.commit()
    except Exception as exc:
        log.exception("URL fetch failed for %s", url)
        with Queue(db_path) as q:
            q.update_job_status(job_id, "FAILED", error=str(exc))


_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>podcast-brain</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; padding: 1rem 2rem; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
         background: #111; color: #ddd; }
  h1 { font-size: 1.2rem; margin: 0 0 0.5rem 0; color: #fff; }
  h2 { font-size: 1rem; margin: 1.5rem 0 0.5rem 0; color: #aaa; border-bottom: 1px solid #333; padding-bottom: 0.25rem; }
  table { border-collapse: collapse; width: 100%; font-size: 0.85rem; }
  th, td { text-align: left; padding: 0.25rem 0.5rem; border-bottom: 1px solid #222; }
  th { color: #999; font-weight: normal; }
  td.err { color: #f88; max-width: 30em; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  td.s-DONE { color: #8f8; } td.s-FAILED { color: #f88; }
  td.s-PENDING, td.s-PENDING-BACKLOG, td.s-URL_PENDING { color: #fc6; }
  .summary { display: flex; gap: 1.5rem; flex-wrap: wrap; align-items: baseline; }
  .summary span { display: inline-block; }
  .summary .k { color: #999; }
  .summary .v { color: #fff; font-weight: bold; }
  form { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; }
  input, select, button { background: #222; color: #ddd; border: 1px solid #444;
                          padding: 0.3rem 0.5rem; font-family: inherit; font-size: 0.9rem; }
  input[type=text] { width: 28em; }
  button { cursor: pointer; }
  button:hover { background: #333; }
  .stamp { color: #666; font-size: 0.8rem; }
</style>
</head>
<body>
<h1>podcast-brain <span class="stamp" id="stamp"></span></h1>
<div class="summary" id="topbar"></div>

<h2>Queue</h2>
<div class="summary" id="queue-summary"></div>

<h2>Recent jobs</h2>
<table id="jobs-table">
  <thead><tr><th>id</th><th>feed</th><th>title</th><th>status</th><th>updated</th><th>error</th></tr></thead>
  <tbody></tbody>
</table>

<h2>Feeds</h2>
<table id="feeds-table">
  <thead><tr><th>id</th><th>title</th><th>slug</th><th>style</th><th>last polled</th><th>pending</th></tr></thead>
  <tbody></tbody>
</table>

<h2>Submit URL</h2>
<form id="submit-form">
  <input type="text" name="url" placeholder="https://www.youtube.com/watch?v=..." required>
  <select name="style">
    <option>informational</option><option>banter</option>
    <option>narrative</option><option>skip</option>
  </select>
  <button type="submit">Queue</button>
  <span id="submit-result"></span>
</form>

<script>
async function fetchJSON(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}

function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const k in (attrs || {})) {
    if (k === 'class') e.className = attrs[k];
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), attrs[k]);
    else e.setAttribute(k, attrs[k]);
  }
  for (const k of kids) e.append(k);
  return e;
}

async function refreshTopBar() {
  const b = await fetchJSON('/api/budget');
  const bar = document.getElementById('topbar');
  bar.innerHTML = '';
  bar.append(
    el('span', {}, el('span', {class:'k'}, 'MTD: '), el('span', {class:'v'}, '$' + b.mtd_spend_usd.toFixed(4))),
    el('span', {}, el('span', {class:'k'}, 'cap: '), el('span', {class:'v'},
      b.monthly_cap_usd > 0 ? '$' + b.monthly_cap_usd.toFixed(2) : 'unlimited')),
    el('span', {}, el('span', {class:'k'}, 'used: '), el('span', {class:'v'},
      b.pct_used != null ? b.pct_used.toFixed(1) + '%' : '—')),
  );
}

async function refreshQueue() {
  const s = await fetchJSON('/api/queue/summary');
  const sum = document.getElementById('queue-summary');
  sum.innerHTML = '';
  const statuses = Object.keys(s.counts).sort();
  for (const st of statuses) {
    sum.append(el('span', {}, el('span', {class:'k'}, st + ': '),
                              el('span', {class:'v'}, s.counts[st])));
  }
  const jobs = await fetchJSON('/api/queue/jobs?limit=50');
  const tbody = document.querySelector('#jobs-table tbody');
  tbody.innerHTML = '';
  for (const j of jobs) {
    const tr = el('tr', {},
      el('td', {}, j.id),
      el('td', {}, j.feed_slug || ''),
      el('td', {}, j.episode_title.length > 60 ? j.episode_title.slice(0, 60) + '…' : j.episode_title),
      el('td', {class: 's-' + j.status}, j.status),
      el('td', {}, (j.updated_at || '').slice(5, 16)),
      el('td', {class: 'err'}, j.last_error || ''));
    tbody.append(tr);
  }
}

async function refreshFeeds() {
  const feeds = await fetchJSON('/api/feeds');
  const tbody = document.querySelector('#feeds-table tbody');
  tbody.innerHTML = '';
  for (const f of feeds) {
    const sel = el('select', {onchange: async (e) => {
      await fetch(`/api/feeds/${f.id}/style`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({style: e.target.value}),
      });
    }});
    for (const s of ['informational', 'banter', 'narrative', 'skip']) {
      const o = el('option', {}, s);
      if (s === f.style) o.selected = true;
      sel.append(o);
    }
    tbody.append(el('tr', {},
      el('td', {}, f.id), el('td', {}, f.title), el('td', {}, f.podcast_slug),
      el('td', {}, sel),
      el('td', {}, (f.last_polled_at || '').slice(5, 16) || '—'),
      el('td', {}, f.pending_jobs)));
  }
}

document.getElementById('submit-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const result = document.getElementById('submit-result');
  result.textContent = 'submitting…';
  try {
    const r = await fetchJSON('/api/submit/url', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url: fd.get('url'), style: fd.get('style')}),
    });
    result.textContent = r.queued ? `queued (job ${r.job_id})` : 'duplicate (skipped)';
    refreshQueue();
  } catch (err) { result.textContent = `error: ${err.message}`; }
});

async function refreshAll() {
  await Promise.all([refreshTopBar(), refreshQueue(), refreshFeeds()]);
  document.getElementById('stamp').textContent = '— ' + new Date().toLocaleTimeString();
}
refreshAll();
setInterval(refreshAll, 30000);
</script>
</body>
</html>
"""
