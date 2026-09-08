"""GitHub threads and releases with the user's read-only token."""

import re

import httpx

from app.sources import Context, Source, SourceError

API = "https://api.github.com"


def parse_thread(data: dict) -> dict:
    pr = data.get("pull_request")
    state = data.get("state") or "open"
    if pr and pr.get("merged_at"):
        state = "merged"
    return {
        "kind": "pr" if pr else "issue",
        "title": data.get("title") or "",
        "state": state,
        "updated_at": data.get("updated_at") or "",
        "comments": int(data.get("comments") or 0),
        "url": data.get("html_url") or "",
        "author": (data.get("user") or {}).get("login"),
    }


def image_tag(image: str | None) -> str | None:
    if not image:
        return None
    _, sep, tag = image.rpartition(":")
    if not sep or "/" in tag:
        return None
    return tag


def tag_version(image: str | None) -> str | None:
    """The image tag when it names a version; "latest" and friends say nothing."""
    tag = image_tag(image)
    return tag if tag and re.search(r"\d", tag) else None


async def npm_version(ctx: Context, url: str) -> str | None:
    """Nginx Proxy Manager states its version on /api/ without a login."""
    try:
        r = await ctx.http.get(url + "/api/")
        r.raise_for_status()
        v = r.json().get("version") or {}
    except (httpx.HTTPError, ValueError):
        return None
    parts = [v.get(k) for k in ("major", "minor", "revision")]
    if any(p is None for p in parts):
        return None
    return ".".join(str(p) for p in parts)


async def resolve_running(ctx: Context, running_from: dict) -> tuple[str | None, str | None]:
    """The version we run, read from what the other sources stored or from the app."""
    db = ctx.db
    if not running_from:
        return None, None
    kind, spec = next(iter(running_from.items()))
    if kind == "dockhand":
        snap = db.get_snapshot("dockhand", f"{spec['env']}:{spec['container']}") or {}
        version = snap.get("version") or tag_version(snap.get("image"))
        return version, f"dockhand env {spec['env']}"
    if kind == "kuma":
        return (db.get_snapshot("kuma", "app") or {}).get("version"), "kuma"
    if kind == "adguard":
        return (db.get_snapshot("adguard", "status") or {}).get("version"), "adguard"
    if kind == "dockhand_version":
        return (db.get_snapshot("dockhand.system", "version") or {}).get("version"), "dockhand"
    if kind == "npm":
        return await npm_version(ctx, spec["url"]), "npm"
    if kind == "pve":
        snap = db.get_snapshot(f"pve.{spec}.version", "version") or {}
        return snap.get("version"), f"pve {spec}"
    return None, None


class GithubSource(Source):
    def __init__(self, ctx: Context):
        super().__init__(ctx)
        token = ctx.secrets.get(ctx.config.github.secret)
        self.headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    async def get(self, path: str, **params) -> httpx.Response:
        return await self.ctx.http.get(API + path, params=params or None, headers=self.headers)


class GithubThreads(GithubSource):
    interval = 900
    name = "github.threads"

    async def collect(self):
        errors = []
        for watch in self.ctx.config.github.threads:
            for number in watch.numbers:
                r = await self.get(f"/repos/{watch.repo}/issues/{number}")
                if r.status_code == 404:
                    errors.append(f"{watch.repo}#{number}: not found")
                    continue
                r.raise_for_status()
                self.ctx.db.upsert_thread(watch.repo, number, **parse_thread(r.json()))
        if errors:
            raise SourceError("; ".join(errors))


class GithubReleases(GithubSource):
    interval = 3600
    name = "github.releases"

    async def latest(self, repo: str):
        r = await self.get(f"/repos/{repo}/releases/latest")
        if r.status_code == 404:
            t = await self.get(f"/repos/{repo}/tags", per_page=1)
            t.raise_for_status()
            tags = t.json() or []
            tag = tags[0].get("name") if tags else None
            return tag, None, f"https://github.com/{repo}/tags"
        r.raise_for_status()
        data = r.json()
        return data.get("tag_name"), data.get("published_at"), data.get("html_url")

    async def collect(self):
        for watch in self.ctx.config.github.releases:
            tag, published, url = await self.latest(watch.repo)
            running, source = await resolve_running(self.ctx, watch.running_from)
            self.ctx.db.upsert_release(watch.repo, tag, published, url, running, source)


def build(ctx: Context):
    gh = ctx.config.github
    if not gh.threads and not gh.releases:
        return [], {}
    if not ctx.secrets.has(gh.secret):
        return [], {"github": f"secret {gh.secret} is missing"}
    sources = []
    if gh.threads:
        sources.append(GithubThreads(ctx))
    if gh.releases:
        sources.append(GithubReleases(ctx))
    return sources, {}
