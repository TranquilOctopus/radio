from __future__ import annotations

import logging
from typing import Iterable

import httpx

# discord.py is lazy-imported inside run_bot() so the package is importable
# without it. Install via:  pip install podcast-brain[discord]

log = logging.getLogger(__name__)

_STYLES = ("informational", "banter", "narrative", "skip")
# Discord message body cap is 2000 chars; the slash-command response embed
# shares that limit. We aim a bit lower to leave room for code-fence padding.
_MSG_LIMIT = 1900


def _truncate(text: str, limit: int = _MSG_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 30] + "\n…(truncated)"


def _http(base_url: str, timeout: float = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base_url, timeout=timeout)


def run_bot(
    token: str,
    api_base_url: str = "http://127.0.0.1:8765",
    allowed_user_ids: Iterable[int] = (),
) -> None:
    """Start the Discord bot. Blocks until the bot is killed.

    The bot connects to the local FastAPI dashboard at api_base_url. Slash
    commands are gated to allowed_user_ids when that list is non-empty;
    empty list = anyone in the bot's reachable scope can issue commands
    (only safe for private guilds you fully control).
    """
    import discord
    from discord import app_commands

    allowed = set(allowed_user_ids)
    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)

    def _is_allowed(user_id: int) -> bool:
        if not allowed:
            return True
        return user_id in allowed

    async def _gate(interaction: discord.Interaction) -> bool:
        if _is_allowed(interaction.user.id):
            return True
        await interaction.response.send_message(
            "You're not authorised to use this bot.", ephemeral=True,
        )
        return False

    @client.event
    async def on_ready() -> None:
        await tree.sync()
        log.info("Discord bot ready as %s; %d commands synced",
                 client.user, len(tree.get_commands()))

    # ---------------------------------------------------------------- feed --

    feed_group = app_commands.Group(name="feed", description="Manage podcast feeds")
    tree.add_command(feed_group)

    @feed_group.command(name="add", description="Subscribe to a feed by RSS or Apple Podcasts URL")
    @app_commands.describe(url="RSS URL or Apple Podcasts share URL", style="Show style")
    @app_commands.choices(style=[app_commands.Choice(name=s, value=s) for s in _STYLES])
    async def feed_add(
        interaction: discord.Interaction, url: str,
        style: app_commands.Choice[str] | None = None,
    ) -> None:
        if not await _gate(interaction):
            return
        await interaction.response.defer(thinking=True)
        s = style.value if style else "informational"
        async with _http(api_base_url) as h:
            r = await h.post("/api/feeds", json={"url": url, "style": s})
        if r.status_code == 200:
            body = r.json()
            await interaction.followup.send(
                f"Added: `{body['slug']}` (style: `{body['style']}`)\n<{body['feed_url']}>"
            )
        else:
            await interaction.followup.send(f"Failed: {r.status_code} {r.text[:300]}")

    @feed_group.command(name="search", description="Search iTunes for a show by name")
    @app_commands.describe(name="Show name to search")
    async def feed_search(interaction: discord.Interaction, name: str) -> None:
        if not await _gate(interaction):
            return
        await interaction.response.defer(thinking=True)
        async with _http(api_base_url) as h:
            r = await h.get("/api/feeds/search", params={"term": name, "limit": 5})
        if r.status_code != 200:
            await interaction.followup.send(f"Search failed: {r.status_code}")
            return
        results = r.json()
        if not results:
            await interaction.followup.send(f"No results for `{name}`.")
            return
        lines = [
            f"{i+1}. **{r['name']}** — {r['artist'] or '?'}\n"
            f"   `/feed add url:{r['feed_url']}`"
            for i, r in enumerate(results)
        ]
        await interaction.followup.send(_truncate("\n".join(lines)))

    @feed_group.command(name="list", description="List subscribed feeds")
    async def feed_list(interaction: discord.Interaction) -> None:
        if not await _gate(interaction):
            return
        await interaction.response.defer(thinking=True)
        async with _http(api_base_url) as h:
            r = await h.get("/api/feeds")
        if r.status_code != 200:
            await interaction.followup.send(f"Fetch failed: {r.status_code}")
            return
        feeds = r.json()
        if not feeds:
            await interaction.followup.send("No feeds subscribed yet.")
            return
        lines = [
            f"`{f['podcast_slug']}` — {f['title']} (style: {f['style']}, pending: {f['pending_jobs']})"
            for f in feeds
        ]
        await interaction.followup.send(_truncate("\n".join(lines)))

    @feed_group.command(name="style", description="Change the processing style of a subscribed feed")
    @app_commands.describe(slug="Show slug (from /feed list)", style="New style")
    @app_commands.choices(style=[app_commands.Choice(name=s, value=s) for s in _STYLES])
    async def feed_style(
        interaction: discord.Interaction, slug: str, style: app_commands.Choice[str],
    ) -> None:
        if not await _gate(interaction):
            return
        await interaction.response.defer(thinking=True)
        async with _http(api_base_url) as h:
            feeds = (await h.get("/api/feeds")).json()
            match = next((f for f in feeds if f["podcast_slug"] == slug), None)
            if match is None:
                await interaction.followup.send(f"No feed with slug `{slug}`.")
                return
            r = await h.post(
                f"/api/feeds/{match['id']}/style", json={"style": style.value},
            )
        if r.status_code == 200:
            await interaction.followup.send(f"Updated `{slug}` → style `{style.value}`")
        else:
            await interaction.followup.send(f"Failed: {r.status_code}")

    # --------------------------------------------------------------- queue --

    queue_group = app_commands.Group(name="queue", description="Inspect the ingest queue")
    tree.add_command(queue_group)

    @queue_group.command(name="status", description="Counts of jobs by status")
    async def queue_status(interaction: discord.Interaction) -> None:
        if not await _gate(interaction):
            return
        await interaction.response.defer(thinking=True)
        async with _http(api_base_url) as h:
            r = await h.get("/api/queue/summary")
        if r.status_code != 200:
            await interaction.followup.send(f"Fetch failed: {r.status_code}")
            return
        body = r.json()
        if body["total"] == 0:
            await interaction.followup.send("Queue is empty.")
            return
        lines = ["```"]
        for status, count in sorted(body["counts"].items()):
            lines.append(f"  {status:<24} {count:>5}")
        lines.append(f"  {'TOTAL':<24} {body['total']:>5}")
        lines.append("```")
        await interaction.followup.send("\n".join(lines))

    @queue_group.command(name="jobs", description="List recent jobs (optionally filtered by status)")
    @app_commands.describe(status="Filter by status (e.g. PENDING, FAILED)", limit="Max rows (default 10)")
    async def queue_jobs(
        interaction: discord.Interaction, status: str | None = None, limit: int = 10,
    ) -> None:
        if not await _gate(interaction):
            return
        await interaction.response.defer(thinking=True)
        params: dict = {"limit": max(1, min(limit, 25))}
        if status:
            params["status"] = status
        async with _http(api_base_url) as h:
            r = await h.get("/api/queue/jobs", params=params)
        if r.status_code != 200:
            await interaction.followup.send(f"Fetch failed: {r.status_code}")
            return
        jobs = r.json()
        if not jobs:
            await interaction.followup.send("No jobs match.")
            return
        lines = ["```"]
        for j in jobs:
            title = j["episode_title"][:50]
            err = f" ({j['last_error'][:40]})" if j.get("last_error") else ""
            lines.append(f"#{j['id']:<5} {j['status']:<18} {title}{err}")
        lines.append("```")
        await interaction.followup.send(_truncate("\n".join(lines)))

    # -------------------------------------------------------------- budget --

    @tree.command(name="budget", description="Month-to-date Claude API spend")
    async def budget_cmd(interaction: discord.Interaction) -> None:
        if not await _gate(interaction):
            return
        await interaction.response.defer(thinking=True)
        async with _http(api_base_url) as h:
            r = await h.get("/api/budget")
        if r.status_code != 200:
            await interaction.followup.send(f"Fetch failed: {r.status_code}")
            return
        b = r.json()
        cap = f"${b['monthly_cap_usd']:.2f}" if b["monthly_cap_usd"] > 0 else "unlimited"
        pct = f"{b['pct_used']:.1f}%" if b["pct_used"] is not None else "—"
        msg = (
            f"**MTD spend:** ${b['mtd_spend_usd']:.4f}\n"
            f"**Cap:** {cap}  ({pct} used)"
        )
        if b["by_model"]:
            msg += "\n```"
            for row in b["by_model"]:
                msg += f"\n  {row['model']:<24} {row['calls']:>4} calls  ${row['cost_usd']:.4f}"
            msg += "\n```"
        await interaction.followup.send(msg)

    # -------------------------------------------------------------- digest --

    @tree.command(name="digest", description="Latest weekly digest")
    async def digest_cmd(interaction: discord.Interaction) -> None:
        if not await _gate(interaction):
            return
        await interaction.response.defer(thinking=True)
        async with _http(api_base_url) as h:
            r = await h.get("/api/digest/latest")
        if r.status_code == 404:
            await interaction.followup.send("No weekly digests yet.")
            return
        if r.status_code != 200:
            await interaction.followup.send(f"Fetch failed: {r.status_code}")
            return
        body = r.json()
        header = f"**Digest {body['week']}**\n"
        await interaction.followup.send(_truncate(header + body["markdown"]))

    # ----------------------------------------------------------------- url --

    @tree.command(name="url", description="One-off URL ingest (yt-dlp)")
    @app_commands.describe(url="YouTube / SoundCloud / episode page URL", style="Show style")
    @app_commands.choices(style=[app_commands.Choice(name=s, value=s) for s in _STYLES])
    async def url_cmd(
        interaction: discord.Interaction, url: str,
        style: app_commands.Choice[str] | None = None,
    ) -> None:
        if not await _gate(interaction):
            return
        await interaction.response.defer(thinking=True)
        s = style.value if style else "informational"
        async with _http(api_base_url) as h:
            r = await h.post("/api/submit/url", json={"url": url, "style": s})
        if r.status_code != 200:
            await interaction.followup.send(f"Failed: {r.status_code} {r.text[:300]}")
            return
        body = r.json()
        if not body.get("queued"):
            await interaction.followup.send(f"Already in queue ({body.get('reason', '?')}).")
        else:
            await interaction.followup.send(f"Queued (job #{body['job_id']}).")

    client.run(token, log_handler=None)
