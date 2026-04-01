# ✦ aniketmaithani.net — Static Blog Engine
> Zero frameworks, pure Python, Apple-level aesthetics.

## Project Structure

```
aniketmaithani-blog/
├── posts/                  ← Put your .md files here
│   ├── _template.md        ← Copy this to start a new post
│   └── your-post.md
├── output/                 ← Generated HTML (deploy this folder)
│   ├── index.html
│   ├── tags.html
│   └── posts/
│       └── your-post.html
├── add_blog.py             ← Add / rebuild posts
├── delete_blog.py          ← Delete posts
├── blog_engine.py          ← Core parsing engine
├── templates.py            ← HTML + CSS templates
├── posts_meta.json         ← Auto-generated post index
└── README.md
```

## Quick Start

### Install dependencies
```bash
pip install markdown python-frontmatter
```

### Add a post
```bash
# Write your post
cp posts/_template.md posts/my-new-post.md
# Edit it, then:
python add_blog.py posts/my-new-post.md

# Or rebuild everything at once:
python add_blog.py --all
```

### Delete a post
```bash
python delete_blog.py --list                    # see all slugs
python delete_blog.py my-post-slug              # delete by slug
python delete_blog.py posts/my-post.md         # delete by file
```

### View locally
```bash
cd output && python -m http.server 8000
# Open http://localhost:8000
```

---

## Frontmatter Reference

```yaml
---
title: "Your Post Title"           # Required
date: 2025-06-01                   # YYYY-MM-DD
author: Aniket Maithani              # Your name
tags: [django, python, fintech]    # Array of tags
description: "SEO summary"         # Shown on index card
reading_time: 5                    # Minutes (auto-calculated if omitted)
status: published                  # or: draft (skipped during build)
---
```

## Deploy

The `output/` folder is a static site. Drop it on:
- **Netlify**: Drag & drop or `netlify deploy --dir=output`
- **GitHub Pages**: Push `output/` as the site root
- **S3**: `aws s3 sync output/ s3://your-bucket --acl public-read`
- **Any VPS**: `rsync -avz output/ user@server:/var/www/html/`
