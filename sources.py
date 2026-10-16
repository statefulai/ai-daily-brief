import re
import httpx
import feedparser
import logging
from calendar import timegm
from datetime import date, datetime, time, timezone, timedelta
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from edition import BEIJING_TZ, edition_window

logger = logging.getLogger(__name__)

USER_AGENT = "ai-daily-brief/1.0"


def _published_on(day: date) -> datetime:
    probe = datetime.combine(day, time(23, 59, 59), tzinfo=BEIJING_TZ)
    start, cutoff = edition_window(probe)
    if not start <= probe < cutoff:
        start, cutoff = cutoff, cutoff + timedelta(days=1)
    return start + (cutoff - start) / 2


class SourceFetchError(RuntimeError):
    """Raised when a source request fails and must not look like an empty day."""


def _client(timeout: float = 30) -> httpx.AsyncClient:
    # news.microsoft.com answers httpx's default User-Agent with HTTP 403.
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )


def _host(url: str) -> str:
    return urlparse(url).netloc


def _tag(name: str) -> str:
    return name.lower().replace(" ", "-")


def _status_error(url: str, status_code: int) -> SourceFetchError:
    return SourceFetchError(f"{_host(url)}: HTTP {status_code}")


async def _get_ok(client: httpx.AsyncClient, url: str) -> httpx.Response:
    resp = await client.get(url)
    if resp.status_code != 200:
        raise _status_error(url, resp.status_code)
    return resp


@dataclass
class NewsItem:
    title: str
    url: str
    source: str
    summary: str = ""
    score: int = 0
    published: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    tags: list[str] = field(default_factory=list)
    category: str = ""

    @property
    def age_hours(self) -> float:
        delta = datetime.now(timezone.utc) - self.published
        return delta.total_seconds() / 3600


async def fetch_hackernews(config: dict) -> list[NewsItem]:
    """Fetch top stories from HackerNews API."""
    top_n = config.get("top_n", 30)
    min_score = config.get("min_score", 50)
    items = []

    async with _client() as client:
        resp = await client.get("https://hacker-news.firebaseio.com/v0/topstories.json")
        if resp.status_code != 200:
            raise SourceFetchError(f"HackerNews topstories returned HTTP {resp.status_code}")
        story_ids = resp.json()[:top_n * 2]  # fetch extra to filter

        for sid in story_ids:
            try:
                resp = await client.get(f"https://hacker-news.firebaseio.com/v0/item/{sid}.json")
                story = resp.json()
                if not story or story.get("type") != "story":
                    continue
                if story.get("score", 0) < min_score:
                    continue

                published = datetime.fromtimestamp(story.get("time", 0), tz=timezone.utc)
                items.append(NewsItem(
                    title=story.get("title", ""),
                    url=story.get("url", f"https://news.ycombinator.com/item?id={sid}"),
                    source="HackerNews",
                    score=story.get("score", 0),
                    published=published,
                    tags=["hackernews"],
                ))

                if len(items) >= top_n:
                    break
            except Exception as e:
                logger.warning(f"Failed to fetch HN story {sid}: {e}")
                continue

    logger.info(f"HackerNews: fetched {len(items)} stories")
    return items


async def fetch_github_trending(config: dict) -> list[NewsItem]:
    """Fetch trending repos from GitHub (unofficial scrape-free approach via search API)."""
    languages = config.get("languages", ["python", "typescript"])
    since = config.get("since", "daily")
    items = []
    errors: list[str] = []

    # Repos with very high star counts are "permanent fixtures" — filter them out
    # to prioritize genuinely new/trending projects
    max_existing_stars = config.get("max_existing_stars", 50000)

    # Use GitHub search API: repos created/pushed recently with many stars
    date_map = {"daily": 1, "weekly": 7, "monthly": 30}
    days = date_map.get(since, 1)
    since_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

    async with _client() as client:
        for lang in languages:
            try:
                query = f"language:{lang} pushed:>{since_date} stars:>10"
                resp = await client.get(
                    "https://api.github.com/search/repositories",
                    params={"q": query, "sort": "stars", "order": "desc", "per_page": 10},
                    headers={"Accept": "application/vnd.github.v3+json"},
                )
                if resp.status_code != 200:
                    logger.warning(f"GitHub API returned {resp.status_code} for {lang}")
                    errors.append(f"{lang}: HTTP {resp.status_code}")
                    continue

                for repo in resp.json().get("items", [])[:5]:
                    stars = repo.get("stargazers_count", 0)
                    if stars > max_existing_stars:
                        logger.debug(f"Skipping {repo['full_name']} ({stars} stars) — permanent fixture")
                        continue
                    items.append(NewsItem(
                        title=f"{repo['full_name']}: {repo.get('description', '')[:100]}",
                        url=repo["html_url"],
                        source="GitHub Trending",
                        score=stars,
                        published=datetime.fromisoformat(
                            repo["pushed_at"].replace("Z", "+00:00")
                        ),
                        tags=["github", lang] + (repo.get("topics", []) or [])[:5],
                    ))
            except Exception as e:
                logger.warning(f"Failed to fetch GitHub trending for {lang}: {e}")
                errors.append(f"{lang}: {e}")

    if not items and errors:
        raise SourceFetchError("GitHub Trending failed: " + "; ".join(errors))
    logger.info(f"GitHub Trending: fetched {len(items)} repos")
    return items


async def fetch_huggingface(config: dict) -> list[NewsItem]:
    """Fetch daily papers and trending models from HuggingFace."""
    items = []
    errors: list[str] = []
    async with _client() as client:
        # Daily papers
        if config.get("daily_papers", True):
            try:
                resp = await client.get("https://huggingface.co/api/daily_papers")
                if resp.status_code != 200:
                    errors.append(f"daily_papers: HTTP {resp.status_code}")
                if resp.status_code == 200:
                    for paper in resp.json()[:config.get("top_n", 10)]:
                        paper_info = paper.get("paper", {})
                        # Use paper's publishedAt if available, otherwise today's date
                        pub_str = paper_info.get("publishedAt", "")
                        if pub_str:
                            try:
                                published = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
                            except ValueError:
                                continue
                        else:
                            continue
                        items.append(NewsItem(
                            title=paper_info.get("title", "Untitled"),
                            url=f"https://huggingface.co/papers/{paper_info.get('id', '')}",
                            source="HuggingFace Papers",
                            score=paper.get("numLikes", 0),
                            published=published,
                            tags=["huggingface", "paper", "research"],
                        ))
            except Exception as e:
                logger.warning(f"Failed to fetch HF daily papers: {e}")
                errors.append(f"daily_papers: {e}")

        # Trending models
        if config.get("trending_models", True):
            try:
                resp = await client.get(
                    "https://huggingface.co/api/models",
                    params={"sort": "trending", "limit": config.get("top_n", 10)},
                )
                if resp.status_code != 200:
                    errors.append(f"trending_models: HTTP {resp.status_code}")
                if resp.status_code == 200:
                    for model in resp.json():
                        # Use model's lastModified if available
                        mod_str = model.get("lastModified", "")
                        if mod_str:
                            try:
                                published = datetime.fromisoformat(mod_str.replace("Z", "+00:00"))
                            except ValueError:
                                continue
                        else:
                            continue
                        items.append(NewsItem(
                            title=f"🤗 {model.get('modelId', 'unknown')}",
                            url=f"https://huggingface.co/{model.get('modelId', '')}",
                            source="HuggingFace Models",
                            score=model.get("likes", 0),
                            published=published,
                            tags=["huggingface", "model"] + (model.get("tags", []) or [])[:3],
                        ))
            except Exception as e:
                logger.warning(f"Failed to fetch HF trending models: {e}")
                errors.append(f"trending_models: {e}")

    if not items and errors:
        raise SourceFetchError("HuggingFace failed: " + "; ".join(errors))
    logger.info(f"HuggingFace: fetched {len(items)} items")
    return items


async def fetch_feed(config: dict) -> list[NewsItem]:
    url, name = config["url"], config["name"]
    async with _client(timeout=20) as client:
        resp = await _get_ok(client, url)

    parsed = feedparser.parse(resp.content)
    if not parsed.version and not parsed.entries:
        raise SourceFetchError(f"{_host(url)}: HTTP 200 but not a feed")

    items = []
    for entry in parsed.entries[: config.get("max_items", 10)]:
        timestamp = entry.get("published_parsed") or entry.get("updated_parsed")
        if not timestamp:
            continue
        items.append(NewsItem(
            title=entry.get("title", "Untitled"),
            url=entry.get("link", ""),
            source=name,
            summary=re.sub(r"<[^>]+>", "", entry.get("summary", ""))[:200],
            published=datetime.fromtimestamp(timegm(timestamp), tz=timezone.utc),
            tags=["rss", _tag(name)],
        ))
    logger.info(f"{name}: fetched {len(items)} feed items")
    return items


@dataclass
class _Anchor:
    href: str
    time_attr: str = ""
    time_text: str = ""
    heading: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)


class _ListingParser(HTMLParser):
    HEADINGS = ("h1", "h2", "h3", "h4")

    def __init__(self):
        super().__init__()
        self.anchors: list[_Anchor] = []
        self._anchor: _Anchor | None = None
        self._capture: str | None = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "a":
            href = attributes.get("href")
            self._anchor = _Anchor(href) if href else None
            if self._anchor:
                self.anchors.append(self._anchor)
        elif self._anchor is None:
            return
        elif tag == "time":
            self._anchor.time_attr = attributes.get("datetime") or ""
            self._capture = "time"
        elif tag in self.HEADINGS and not self._anchor.heading:
            self._capture = "heading"

    def handle_endtag(self, tag):
        if tag == "a":
            self._anchor = None
            self._capture = None
        elif tag == "time" or tag in self.HEADINGS:
            self._capture = None

    def handle_data(self, data):
        if self._anchor is None:
            return
        if self._capture == "time":
            self._anchor.time_text += data.strip()
        elif self._capture == "heading":
            self._anchor.heading.append(data)
        elif data.strip():
            self._anchor.texts.append(data.strip())


def _listing_date(time_attr: str, time_text: str) -> datetime | None:
    try:
        if time_attr:
            day = date.fromisoformat(time_attr[:10])
        else:
            day = datetime.strptime(time_text, "%b %d, %Y").date()
    except ValueError:
        return None
    return _published_on(day)


def parse_html_listing(html: str, listing_url: str, source: str) -> list[NewsItem]:
    parser = _ListingParser()
    parser.feed(html)
    host = _host(listing_url)
    prefix = urlparse(listing_url).path.rstrip("/") + "/"
    items: list[NewsItem] = []
    seen: set[str] = set()
    for anchor in parser.anchors:
        url = urljoin(listing_url, anchor.href)
        path = urlparse(url).path
        slug = path[len(prefix):].rstrip("/") if path.startswith(prefix) else ""
        if _host(url) != host or not slug or "/" in slug or url in seen:
            continue
        published = _listing_date(anchor.time_attr, anchor.time_text)
        heading = " ".join("".join(anchor.heading).split())
        title = heading or (anchor.texts[-1] if anchor.texts else "")
        if published is None or not title:
            continue
        seen.add(url)
        items.append(NewsItem(
            title=title,
            url=url,
            source=source,
            published=published,
            tags=["web", _tag(source)],
        ))
    return items


async def fetch_html_listing(config: dict) -> list[NewsItem]:
    url, name = config["url"], config["name"]
    async with _client(timeout=20) as client:
        resp = await _get_ok(client, url)
    items = parse_html_listing(resp.text, url, name)
    if not items:
        raise SourceFetchError(f"{_host(url)}: HTTP 200 but no dated article links")
    logger.info(f"{name}: fetched {len(items)} listing items")
    return items


_DATE_PUBLISHED = re.compile(r'"datePublished"\s*:\s*"([^"]+)"')
_BLOCKED_INDEXES = {"/news", "/blog"}


def parse_html_page(html: str, page_url: str, source: str) -> NewsItem | None:
    title = _first_h1(html)
    match = _DATE_PUBLISHED.search(html)
    if not title or match is None:
        return None
    try:
        published = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
    except ValueError:
        return None
    return NewsItem(
        title=title,
        url=page_url,
        source=source,
        published=published,
        tags=["web", _tag(source)],
    )


async def fetch_html_pages(config: dict) -> list[NewsItem]:
    name = config["name"]
    items = []
    async with _client(timeout=20) as client:
        for url in config.get("pages") or []:
            if urlparse(url).path.rstrip("/") in _BLOCKED_INDEXES:
                logger.warning(f"{name}: skipped blocked index {url}")
                continue
            try:
                page = await _get_ok(client, url)
            except (SourceFetchError, httpx.HTTPError) as exc:
                logger.warning(f"{name}: page skipped: {url}: {exc}")
                continue
            item = parse_html_page(page.text, url, name)
            if item is None:
                logger.warning(f"{name}: page skipped: {url}: no h1 or datePublished")
                continue
            items.append(item)
    if not items:
        raise SourceFetchError(f"{name}: no readable article page")
    logger.info(f"{name}: fetched {len(items)} article pages")
    return items


_DEEPSEEK_PAGE = re.compile(r"/news/news(\d{2})(\d{2})(\d{2})/?$")


def parse_deepseek_links(html: str, base_url: str) -> list[tuple[str, datetime]]:
    links: dict[str, datetime] = {}
    for href in re.findall(r"""href=["']([^"']+)["']""", html):
        url = urljoin(base_url, href)
        match = _DEEPSEEK_PAGE.search(urlparse(url).path)
        if not match or _host(url) != _host(base_url) or url in links:
            continue
        yy, mm, dd = (int(part) for part in match.groups())
        try:
            links[url] = _published_on(date(2000 + yy, mm, dd))
        except ValueError:
            continue
    return sorted(links.items(), key=lambda link: link[1], reverse=True)


def _first_h1(html: str) -> str:
    match = re.search(r"<h1\b[^>]*>(.*?)</h1>", html, re.S | re.I)
    if not match:
        return ""
    return " ".join(unescape(re.sub(r"<[^>]+>", "", match.group(1))).split())


async def fetch_deepseek_updates(config: dict) -> list[NewsItem]:
    """The updates page links each release as "this documentation", so the title is the page's h1."""
    url, name = config["url"], config["name"]
    items = []
    async with _client(timeout=20) as client:
        links = parse_deepseek_links((await _get_ok(client, url)).text, url)
        if not links:
            raise SourceFetchError(f"{_host(url)}: HTTP 200 but no /news/newsYYMMDD links")
        for page_url, published in links[: config.get("max_items", 3)]:
            try:
                page = await _get_ok(client, page_url)
            except (SourceFetchError, httpx.HTTPError) as exc:
                logger.warning(f"DeepSeek page skipped: {page_url}: {exc}")
                continue
            title = _first_h1(page.text)
            if not title:
                logger.warning(f"DeepSeek page skipped: {page_url}: no h1")
                continue
            items.append(NewsItem(
                title=title,
                url=page_url,
                source=name,
                published=published,
                tags=["web", _tag(name)],
            ))
    if not items:
        raise SourceFetchError(f"{_host(url)}: no readable /news/newsYYMMDD page")
    logger.info(f"{name}: fetched {len(items)} release pages")
    return items


async def fetch_ruanyf_weekly(config: dict) -> list[NewsItem]:
    """Fetch latest issue from ruanyf/weekly (阮一峰科技爱好者周刊)."""
    items = []
    repo = config.get("repo", "ruanyf/weekly")

    async with _client() as client:
        try:
            # Find latest issue number and its commit date
            resp = await client.get(
                f"https://api.github.com/repos/{repo}/commits",
                params={"per_page": 10},
                headers={"Accept": "application/vnd.github.v3+json"},
            )
            if resp.status_code != 200:
                raise SourceFetchError(f"ruanyf/weekly commits API returned {resp.status_code}")

            issue_num = None
            commit_date = None
            for commit in resp.json():
                msg = commit.get("commit", {}).get("message", "")
                match = re.search(r"issue[_\s-]*(\d+)", msg, re.IGNORECASE)
                if match:
                    issue_num = match.group(1)
                    # Use the commit date as publication time
                    date_str = commit.get("commit", {}).get("committer", {}).get("date", "")
                    if date_str:
                        commit_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    break

            if not issue_num:
                raise SourceFetchError("Could not find latest ruanyf/weekly issue number")
            if commit_date is None:
                raise SourceFetchError("Latest ruanyf/weekly issue has no commit timestamp")

            # P2: Skip if the issue commit is older than 7 days
            age_days = (datetime.now(timezone.utc) - commit_date).days
            if age_days > 7:
                logger.info(f"阮一峰周刊 issue #{issue_num} is {age_days} days old, skipping")
                return []

            # Fetch the markdown file
            file_resp = await client.get(
                f"https://raw.githubusercontent.com/{repo}/master/docs/issue-{issue_num}.md"
            )
            if file_resp.status_code != 200:
                raise SourceFetchError(f"Failed to fetch issue-{issue_num}.md: {file_resp.status_code}")

            # Use commit date instead of now() for accurate age filtering
            items = _parse_ruanyf_markdown(file_resp.text, config, published=commit_date)
            logger.info(f"阮一峰周刊: fetched {len(items)} items from issue #{issue_num} (age: {age_days}d)")

        except SourceFetchError:
            raise
        except Exception as e:
            raise SourceFetchError(f"Failed to fetch ruanyf/weekly: {e}") from e

    if not items:
        logger.info("阮一峰周刊: fetched 0 items")
    return items


def _parse_ruanyf_markdown(
    markdown: str, config: dict, published: datetime | None = None
) -> list[NewsItem]:
    """Extract linked items from ruanyf weekly markdown."""
    if published is None:
        published = datetime.now(timezone.utc)

    items = []
    max_items = config.get("max_items", 20)

    # Match markdown links: [title](url) with optional surrounding text as summary
    link_pattern = re.compile(
        r"(?:^|\n).*?\[([^\]]{5,})\]\((https?://[^\)]+)\)(.{0,200})",
        re.MULTILINE,
    )

    seen_urls: set[str] = set()
    for match in link_pattern.finditer(markdown):
        title = match.group(1).strip()
        url = match.group(2).strip()
        context = match.group(3).strip()

        # Skip navigation/self links
        if "github.com/ruanyf/weekly" in url:
            continue
        if url.rstrip("/").lower() in seen_urls:
            continue
        seen_urls.add(url.rstrip("/").lower())

        # Clean up context as summary
        summary = re.sub(r"\[.*?\]\(.*?\)", "", context)
        summary = re.sub(r"[#*`>]", "", summary).strip()
        summary = summary[:200]

        items.append(NewsItem(
            title=title,
            url=url,
            source="阮一峰周刊",
            summary=summary,
            published=published,
            tags=["ruanyf", "weekly", "chinese-tech"],
        ))

        if len(items) >= max_items:
            break

    return items


async def fetch_reddit(config: dict) -> list[NewsItem]:
    """Fetch hot posts from configured subreddits (default: r/LocalLLaMA)."""
    subreddits = config.get("subreddits", ["LocalLLaMA"])
    top_n = config.get("top_n", 15)
    min_score = config.get("min_score", 50)
    items = []
    errors: list[str] = []

    async with _client() as client:
        for sub in subreddits:
            try:
                resp = await client.get(
                    f"https://www.reddit.com/r/{sub}/hot.json",
                    params={"limit": top_n * 2},
                )
                if resp.status_code != 200:
                    logger.warning(f"Reddit r/{sub} returned {resp.status_code}")
                    errors.append(f"r/{sub}: HTTP {resp.status_code}")
                    continue

                posts = resp.json().get("data", {}).get("children", [])
                count = 0
                for post in posts:
                    data = post.get("data", {})
                    if data.get("stickied"):
                        continue
                    score = data.get("score", 0)
                    if score < min_score:
                        continue

                    created_utc = data.get("created_utc", 0)
                    published = datetime.fromtimestamp(created_utc, tz=timezone.utc)

                    # Use external URL if it's a link post, otherwise reddit permalink
                    url = data.get("url", "")
                    if not url or "reddit.com" in url:
                        url = f"https://www.reddit.com{data.get('permalink', '')}"

                    items.append(NewsItem(
                        title=data.get("title", ""),
                        url=url,
                        source=f"Reddit r/{sub}",
                        summary=data.get("selftext", "")[:200],
                        score=score,
                        published=published,
                        tags=["reddit", sub.lower()],
                    ))
                    count += 1
                    if count >= top_n:
                        break

            except Exception as e:
                logger.warning(f"Failed to fetch Reddit r/{sub}: {e}")
                errors.append(f"r/{sub}: {e}")

    if not items and errors:
        raise SourceFetchError("Reddit failed: " + "; ".join(errors))
    logger.info(f"Reddit: fetched {len(items)} posts")
    return items


FETCHERS = {
    "hackernews": fetch_hackernews,
    "github_trending": fetch_github_trending,
    "huggingface": fetch_huggingface,
    "ruanyf_weekly": fetch_ruanyf_weekly,
    "reddit": fetch_reddit,
    "feed": fetch_feed,
    "html_listing": fetch_html_listing,
    "html_pages": fetch_html_pages,
    "deepseek_updates": fetch_deepseek_updates,
}
