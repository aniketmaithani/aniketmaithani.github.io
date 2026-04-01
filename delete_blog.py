#!/usr/bin/env python3
"""
delete_blog.py — Remove a blog post

Usage:
  python delete_blog.py my-post-slug        # delete by slug
  python delete_blog.py posts/my-post.md   # delete by file path
  python delete_blog.py --list             # list all published posts
"""

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from blog_engine import (
    POSTS_DIR, POSTS_OUT, INDEX_FILE, TAGS_FILE,
    load_meta, save_meta,
)
from templates import render_index, render_tags

GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
DIM    = "\033[2m"
RESET  = "\033[0m"
BOLD   = "\033[1m"


def list_posts(posts):
    if not posts:
        print(f"\n  {YELLOW}No published posts found.{RESET}\n")
        return
    print(f"\n  {'SLUG':<40} {'DATE':<14} TITLE")
    print(f"  {'─'*40} {'─'*14} {'─'*30}")
    for p in posts:
        print(f"  {DIM}{p['slug']:<40}{RESET} {p['date']:<14} {p['title'][:50]}")
    print()


def delete_post(identifier: str):
    # Resolve slug from path or direct slug
    if identifier.endswith(".md"):
        slug = Path(identifier).stem
    else:
        slug = identifier

    all_posts = load_meta()
    match = next((p for p in all_posts if p["slug"] == slug), None)

    if not match:
        print(f"\n{RED}✗ Post not found:{RESET} '{slug}'")
        print(f"  Run {DIM}python delete_blog.py --list{RESET} to see all slugs\n")
        sys.exit(1)

    print(f"\n{CYAN}→ Deleting:{RESET} {match['title']}")
    print(f"  {DIM}slug: {slug}{RESET}")

    # Confirm
    confirm = input(f"\n  {YELLOW}Are you sure? [y/N]{RESET} ").strip().lower()
    if confirm != "y":
        print(f"  {DIM}Aborted.{RESET}\n")
        sys.exit(0)

    # Remove HTML file
    html_out = POSTS_OUT / f"{slug}.html"
    if html_out.exists():
        html_out.unlink()
        print(f"  {GREEN}✓ Removed{RESET} {DIM}{html_out}{RESET}")
    else:
        print(f"  {YELLOW}⊘ HTML file not found{RESET} (already removed?)")

    # Remove from meta
    remaining = [p for p in all_posts if p["slug"] != slug]
    posts_sorted = save_meta(remaining)

    # Rebuild index + tags
    INDEX_FILE.write_text(render_index(posts_sorted), encoding="utf-8")
    TAGS_FILE.write_text(render_tags(posts_sorted), encoding="utf-8")

    print(f"  {GREEN}✓ Index rebuilt{RESET} — {len(posts_sorted)} posts remaining")

    # Ask if they want to delete source .md too
    md_src = POSTS_DIR / f"{slug}.md"
    if md_src.exists():
        del_src = input(f"\n  Delete source markdown {DIM}{md_src.name}{RESET}? [y/N] ").strip().lower()
        if del_src == "y":
            md_src.unlink()
            print(f"  {GREEN}✓ Source file deleted{RESET}")
        else:
            print(f"  {DIM}Source file kept.{RESET}")

    print(f"\n{GREEN}✓ Done!{RESET}\n")


def main():
    parser = argparse.ArgumentParser(description="aniketmaithani.net — Delete blog post")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("slug", nargs="?", help="Post slug or .md filepath")
    group.add_argument("--list", "-l", action="store_true", help="List all posts")
    args = parser.parse_args()

    print(f"\n{BOLD}✦ aniketmaithani.net Blog Engine{RESET}")

    if args.list:
        posts = load_meta()
        list_posts(posts)
    elif args.slug:
        delete_post(args.slug)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
