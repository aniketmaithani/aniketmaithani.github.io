# ✦ aniketmaithani.net — Static Blog Engine

> Pure Python static site generator. No frameworks, no build tools, no nonsense.
> Live at **[aniketmaithani.github.io](https://aniketmaithani.github.io)**

---

## Project Structure

```
aniketmaithani.github.io/
├── posts/                        ← Write your .md files here
│   ├── _template.md              ← Copy this to start a new post
│   └── your-post.md
├── output/                       ← Generated HTML — this is what gets deployed
│   ├── index.html
│   ├── tags.html
│   └── posts/
│       └── your-post.html
├── add_blog.py                   ← Add / rebuild posts + auto-prune deleted ones
├── delete_blog.py                ← Delete a post by slug or file path
├── blog_engine.py                ← Core markdown parser and meta management
├── templates.py                  ← All HTML + CSS (Apple-inspired design)
├── deploy_ghpages.py             ← One-command publish to GitHub Pages
├── posts_meta.json               ← Auto-generated post index (do not edit manually)
└── .gitignore
```

---

## Quick Start

### 1. Install dependencies

```bash
pip install markdown python-frontmatter
```

### 2. Write a post

```bash
cp posts/_template.md posts/my-new-post.md
# Edit posts/my-new-post.md with your content, then:
python add_blog.py posts/my-new-post.md
```

This will:
- Parse the markdown and render `output/posts/my-new-post.html`
- Regenerate `output/index.html` and `output/tags.html`
- **Auto-remove** any posts from the index whose `.md` files have been deleted

### 3. Rebuild everything from scratch

```bash
python add_blog.py --all
```

### 4. Delete a post

```bash
python delete_blog.py --list                   # list all published slugs
python delete_blog.py my-post-slug             # delete by slug
python delete_blog.py posts/my-post.md        # delete by file path
```

### 5. Preview locally

```bash
cd output && python -m http.server 8000
# Open http://localhost:8000
```

---

## Frontmatter Reference

Every post starts with a YAML frontmatter block:

```yaml
---
title: "Your Post Title"            # Required
date: 2025-06-01                    # YYYY-MM-DD
author: Aniket Maithani             # Defaults to "Aniket Maithani" if omitted
tags: [django, python, fintech]     # Array of tags (drives the Tags page)
description: "SEO summary"          # Shown on index card and in <meta>
reading_time: 5                     # Minutes — auto-calculated if omitted
status: published                   # Use "draft" to skip during build
---
```

---

## Deploy to GitHub Pages

A single command builds the site and pushes `output/` to the `gh-pages` branch:

```bash
python deploy_ghpages.py
```

**What it does:**
1. Runs `add_blog.py --all` to regenerate `output/`
2. Creates a fresh isolated git repo in a temp directory
3. Copies the contents of `output/` into it
4. Adds a `.nojekyll` file (required for raw HTML on GitHub Pages)
5. Force-pushes to the `gh-pages` branch of your remote

**Options:**

```bash
python deploy_ghpages.py                       # build + deploy (default)
python deploy_ghpages.py --no-build            # skip rebuild, deploy existing output/
python deploy_ghpages.py --remote origin       # specify remote (default: origin)
python deploy_ghpages.py --branch gh-pages     # specify branch (default: gh-pages)
python deploy_ghpages.py -m "new post: foo"    # custom commit message
```

**First-time GitHub Pages setup:**
1. Go to your repo → **Settings → Pages**
2. Set **Source** → branch: `gh-pages`, folder: `/ (root)`
3. Click **Save**

Your site will be live at `https://aniketmaithani.github.io` within 1–2 minutes.

---

## Git Workflow

```bash
# After writing a post and running add_blog.py:
git add .
git commit -m "post: your post title here"
git push origin main

# Deploy to live site:
python deploy_ghpages.py
```

The `main` branch holds the source (Python engine + markdown posts).
The `gh-pages` branch holds only the compiled HTML and is managed entirely by `deploy_ghpages.py` — never edit it directly.

---

## Remote

```
git@github.com:aniketmaithani/aniketmaithani.github.io.git
```
