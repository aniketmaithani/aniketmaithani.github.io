#!/usr/bin/env python3
"""
add_blog.py — Add/rebuild blog posts

Usage:
  python add_blog.py posts/my-post.md     # add/update one post
  python add_blog.py --all                # rebuild everything
  python add_blog.py --rebuild            # same as --all
"""

import sys
import shutil
import argparse
from pathlib import Path

# Make local imports work regardless of cwd
sys.path.insert(0, str(Path(__file__).parent))

from blog_engine import (
    POSTS_DIR, OUTPUT_DIR, POSTS_OUT, ASSETS_OUT, INDEX_FILE, TAGS_FILE,
    parse_post, load_meta, save_meta, prune_orphaned_posts,
)
from templates import render_index, render_tags, render_post

GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
DIM    = "\033[2m"
RESET  = "\033[0m"
BOLD   = "\033[1m"


def ensure_dirs():
    for d in [OUTPUT_DIR, POSTS_OUT, ASSETS_OUT]:
        d.mkdir(parents=True, exist_ok=True)


def write_post_html(post: dict):
    out = POSTS_OUT / f"{post['slug']}.html"
    out.write_text(render_post(post), encoding="utf-8")
    return out


def rebuild_index(posts: list):
    INDEX_FILE.write_text(render_index(posts), encoding="utf-8")
    TAGS_FILE.write_text(render_tags(posts), encoding="utf-8")


def add_or_update(md_path: Path, all_posts: list) -> list:
    print(f"\n{CYAN}→ Parsing{RESET} {md_path.name}")
    post = parse_post(md_path)

    if post is None:
        print(f"  {YELLOW}⊘ Skipping{RESET} (status: draft)")
        return all_posts

    # Remove old entry if exists (update flow)
    all_posts = [p for p in all_posts if p["slug"] != post["slug"]]
    all_posts.append({k: v for k, v in post.items() if k != "content_html"})

    out = write_post_html(post)
    print(f"  {GREEN}✓ Written{RESET} {DIM}{out}{RESET}")
    return all_posts


def rebuild_all():
    print(f"\n{BOLD}Rebuilding all posts...{RESET}\n")
    ensure_dirs()
    all_posts = []

    md_files = sorted(POSTS_DIR.glob("*.md"))
    if not md_files:
        print(f"{YELLOW}No markdown files found in {POSTS_DIR}{RESET}")
    else:
        for md in md_files:
            all_posts = add_or_update(md, all_posts)

    posts_sorted = save_meta(all_posts)
    rebuild_index(posts_sorted)

    print(f"\n{GREEN}✓ Built {len(posts_sorted)} posts{RESET}")
    print(f"  {DIM}index  → {INDEX_FILE}{RESET}")
    print(f"  {DIM}tags   → {TAGS_FILE}{RESET}\n")


def add_single(md_path: Path):
    ensure_dirs()

    if not md_path.exists():
        # Try relative to posts dir
        alt = POSTS_DIR / md_path.name
        if alt.exists():
            md_path = alt
        else:
            print(f"{RED}✗ File not found:{RESET} {md_path}")
            sys.exit(1)

    all_posts = load_meta()

    # ── Remove posts whose .md files were deleted ──────────────
    all_posts, pruned = prune_orphaned_posts(all_posts, POSTS_OUT)
    for slug in pruned:
        print(f"  {YELLOW}⊘ Removed orphan:{RESET} {slug} (no .md file found)")

    all_posts = add_or_update(md_path, all_posts)
    posts_sorted = save_meta(all_posts)
    rebuild_index(posts_sorted)

    print(f"\n{GREEN}✓ Done!{RESET} Total: {len(posts_sorted)} posts")
    print(f"  Open {DIM}{OUTPUT_DIR}/index.html{RESET} in browser\n")


def main():
    parser = argparse.ArgumentParser(description="aniketmaithani.net — Add blog post")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("file", nargs="?", help="Markdown file to add/update")
    group.add_argument("--all", action="store_true", help="Rebuild all posts")
    group.add_argument("--rebuild", action="store_true", help="Rebuild all posts")
    args = parser.parse_args()

    print(f"\n{BOLD}✦ aniketmaithani.net Blog Engine{RESET}")

    if args.all or args.rebuild:
        rebuild_all()
    elif args.file:
        add_single(Path(args.file))
    else:
        # Default: rebuild all
        rebuild_all()


if __name__ == "__main__":
    main()
