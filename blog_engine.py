"""
aniketmaithani.net — Static Blog Engine
Core utilities shared across add_blog.py and delete_blog.py
"""

import os
import json
import shutil
import re
from datetime import datetime
from pathlib import Path

import frontmatter
import markdown
from markdown.extensions.codehilite import CodeHiliteExtension
from markdown.extensions.fenced_code import FencedCodeExtension
from markdown.extensions.tables import TableExtension
from markdown.extensions.toc import TocExtension

# ── Directory config ─────────────────────────────────────────
BASE_DIR     = Path(__file__).parent
POSTS_DIR    = BASE_DIR / "posts"
OUTPUT_DIR   = BASE_DIR / "output"
POSTS_OUT    = OUTPUT_DIR / "posts"
ASSETS_OUT   = OUTPUT_DIR / "assets"
INDEX_FILE   = OUTPUT_DIR / "index.html"
TAGS_FILE    = OUTPUT_DIR / "tags.html"
META_FILE    = BASE_DIR / "posts_meta.json"

# ── Markdown processor ────────────────────────────────────────
MD_EXTENSIONS = [
    FencedCodeExtension(),
    CodeHiliteExtension(linenums=False, css_class="code-block"),
    TableExtension(),
    TocExtension(permalink=True),
    "markdown.extensions.nl2br",
    "markdown.extensions.smarty",
    "markdown.extensions.attr_list",
]

def parse_post(md_path: Path) -> dict | None:
    """Parse a markdown file with frontmatter. Returns None if not published."""
    post = frontmatter.load(str(md_path))
    meta = post.metadata

    status = meta.get("status", "published")
    if status == "draft":
        return None

    slug = md_path.stem
    date_raw = meta.get("date", datetime.today())
    if isinstance(date_raw, str):
        date_obj = datetime.strptime(date_raw, "%Y-%m-%d")
    else:
        date_obj = datetime.combine(date_raw, datetime.min.time()) if hasattr(date_raw, 'year') else datetime.today()

    html_content = markdown.markdown(post.content, extensions=MD_EXTENSIONS)

    tags = meta.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]

    return {
        "slug":         slug,
        "title":        meta.get("title", slug.replace("-", " ").title()),
        "date":         date_obj.strftime("%Y-%m-%d"),
        "date_display": date_obj.strftime("%B %d, %Y"),
        "date_iso":     date_obj.isoformat(),
        "author":       meta.get("author", "Aniket Maithani"),
        "tags":         tags,
        "description":  meta.get("description", ""),
        "reading_time": meta.get("reading_time", estimate_reading_time(post.content)),
        "cover_image":  meta.get("cover_image", ""),
        "content_html": html_content,
        "source_file":  str(md_path),
    }

def estimate_reading_time(text: str) -> int:
    words = len(text.split())
    return max(1, round(words / 200))

def load_meta() -> list:
    if META_FILE.exists():
        return json.loads(META_FILE.read_text())
    return []

def prune_orphaned_posts(posts: list, posts_out_dir: Path) -> tuple[list, list]:
    """
    Remove meta entries whose source .md file no longer exists.
    Also deletes the orphaned HTML file from output/posts/.
    Returns (kept_posts, removed_slugs).
    """
    kept, removed = [], []
    for p in posts:
        src = Path(p.get("source_file", ""))
        # Accept both absolute path stored in meta OR derived from slug
        if not src.exists():
            src = POSTS_DIR / f"{p['slug']}.md"
        if src.exists():
            kept.append(p)
        else:
            removed.append(p["slug"])
            html = posts_out_dir / f"{p['slug']}.html"
            if html.exists():
                html.unlink()
    return kept, removed

def save_meta(posts: list):
    posts_sorted = sorted(posts, key=lambda p: p["date"], reverse=True)
    META_FILE.write_text(json.dumps(posts_sorted, indent=2))
    return posts_sorted

def get_all_tags(posts: list) -> dict:
    tags = {}
    for p in posts:
        for t in p.get("tags", []):
            tags.setdefault(t, []).append(p)
    return tags
