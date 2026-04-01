#!/usr/bin/env python3
"""
deploy_ghpages.py — Publish output/ to GitHub Pages (gh-pages branch)

This script:
  1. Builds the blog (runs add_blog.py --all) to regenerate output/
  2. Creates/updates the gh-pages branch with only the contents of output/
  3. Force-pushes to GitHub so your site goes live at:
     https://<username>.github.io/<repo>/

Usage:
  python deploy_ghpages.py                         # build + deploy
  python deploy_ghpages.py --no-build              # skip build, just deploy
  python deploy_ghpages.py --remote origin         # specify remote (default: origin)
  python deploy_ghpages.py --branch gh-pages       # specify branch (default: gh-pages)
  python deploy_ghpages.py --message "my commit"   # custom commit message

Requirements:
  - git must be installed and the repo must already be pushed to GitHub
  - pip install markdown python-frontmatter
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# ── Colour helpers ────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
DIM    = "\033[2m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

BASE_DIR   = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"


# ── Utilities ─────────────────────────────────────────────────────────────────

def run(cmd: list[str], cwd: Path = BASE_DIR, check: bool = True) -> subprocess.CompletedProcess:
    """Run a shell command, stream output, and optionally raise on failure."""
    result = subprocess.run(cmd, cwd=cwd, text=True, capture_output=False)
    if check and result.returncode != 0:
        print(f"\n{RED}✗ Command failed:{RESET} {' '.join(cmd)}")
        sys.exit(result.returncode)
    return result


def run_capture(cmd: list[str], cwd: Path = BASE_DIR) -> str:
    """Run a command and return its stdout (stripped)."""
    result = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    return result.stdout.strip()


def banner(msg: str):
    width = len(msg) + 4
    print(f"\n{BOLD}{'─' * width}{RESET}")
    print(f"{BOLD}  {msg}  {RESET}")
    print(f"{BOLD}{'─' * width}{RESET}\n")


# ── Core steps ────────────────────────────────────────────────────────────────

def check_git_repo():
    """Ensure we are inside a git repository."""
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=BASE_DIR, capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"{RED}✗ Not a git repository.{RESET}")
        print(f"  Run: {DIM}git init && git remote add origin <your-repo-url>{RESET}")
        sys.exit(1)
    print(f"{GREEN}✓ Git repository detected{RESET}")


def check_remote(remote: str):
    """Ensure the specified remote exists."""
    remotes = run_capture(["git", "remote"]).splitlines()
    if remote not in remotes:
        print(f"{RED}✗ Remote '{remote}' not found.{RESET}")
        print(f"  Available remotes: {remotes or 'none'}")
        print(f"  Add one with: {DIM}git remote add origin https://github.com/<user>/<repo>.git{RESET}")
        sys.exit(1)
    remote_url = run_capture(["git", "remote", "get-url", remote])
    print(f"{GREEN}✓ Remote '{remote}' → {DIM}{remote_url}{RESET}")
    return remote_url


def build_site():
    """Run add_blog.py --all to regenerate the output/ folder."""
    banner("Step 1 — Building blog")
    add_blog = BASE_DIR / "add_blog.py"
    if not add_blog.exists():
        print(f"{RED}✗ add_blog.py not found at {add_blog}{RESET}")
        sys.exit(1)
    run([sys.executable, str(add_blog), "--all"])
    print(f"\n{GREEN}✓ Build complete → {OUTPUT_DIR}{RESET}")


def ensure_output():
    """Verify that the output/ directory has files to deploy."""
    if not OUTPUT_DIR.exists() or not any(OUTPUT_DIR.iterdir()):
        print(f"{RED}✗ output/ directory is empty or missing.{RESET}")
        print(f"  Run: {DIM}python add_blog.py --all{RESET}  first, or use --no-build flag carefully.")
        sys.exit(1)
    files = list(OUTPUT_DIR.rglob("*"))
    print(f"{GREEN}✓ {len(files)} file(s) ready in output/{RESET}")


def deploy(remote: str, branch: str, commit_msg: str):
    """
    Copy output/ into a temporary worktree on <branch> and push.
    Uses git worktree so we never leave the working directory.
    """
    banner(f"Step 2 — Deploying to '{branch}' branch")

    # ── Stash any local changes so git is clean ───────────────────────────────
    stash_result = subprocess.run(
        ["git", "stash", "--include-untracked", "-m", "deploy-ghpages-autostash"],
        cwd=BASE_DIR, text=True, capture_output=True
    )
    stashed = "No local changes" not in stash_result.stdout

    try:
        _do_deploy(remote, branch, commit_msg)
    finally:
        if stashed:
            print(f"\n{CYAN}Restoring stashed changes...{RESET}")
            subprocess.run(["git", "stash", "pop"], cwd=BASE_DIR)


def _do_deploy(remote: str, branch: str, commit_msg: str):
    # ── Does the branch already exist on origin? ──────────────────────────────
    remote_refs = run_capture(["git", "ls-remote", "--heads", remote, branch])
    branch_exists_remote = bool(remote_refs)

    # ── Does the branch exist locally? ───────────────────────────────────────
    local_branches = run_capture(["git", "branch"]).replace("*", "").split()
    branch_exists_local = branch in local_branches

    # ── Set up a temp worktree ────────────────────────────────────────────────
    tmp_dir = Path(tempfile.mkdtemp(prefix="ghpages_deploy_"))
    print(f"{DIM}Temp worktree: {tmp_dir}{RESET}")

    try:
        if branch_exists_remote:
            # Fetch latest state
            print(f"{CYAN}Fetching remote branch '{branch}'...{RESET}")
            run(["git", "fetch", remote, branch])
            run(["git", "worktree", "add", str(tmp_dir), f"{remote}/{branch}"])
        elif branch_exists_local:
            run(["git", "worktree", "add", str(tmp_dir), branch])
        else:
            # Create the branch as an orphan (no history)
            print(f"{CYAN}Creating orphan branch '{branch}'...{RESET}")
            run(["git", "worktree", "add", "--orphan", "-b", branch, str(tmp_dir)])

        # ── Clear existing content (keep .git) ───────────────────────────────
        for item in tmp_dir.iterdir():
            if item.name == ".git":
                continue
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()

        # ── Copy output/ contents into the worktree ───────────────────────────
        print(f"\n{CYAN}Copying output/ → worktree...{RESET}")
        for src in OUTPUT_DIR.rglob("*"):
            if src.is_file():
                rel = src.relative_to(OUTPUT_DIR)
                dest = tmp_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)

        copied = list(tmp_dir.rglob("*"))
        print(f"{GREEN}✓ {len(copied)} item(s) copied{RESET}")

        # ── Add a .nojekyll file so GH Pages serves raw HTML ─────────────────
        nojekyll = tmp_dir / ".nojekyll"
        nojekyll.touch()
        print(f"{GREEN}✓ .nojekyll added{RESET}")

        # ── Git commit ────────────────────────────────────────────────────────
        run(["git", "add", "--all"], cwd=tmp_dir)

        # Check if there's anything to commit
        status = run_capture(["git", "status", "--porcelain"], cwd=tmp_dir)
        if not status:
            print(f"\n{YELLOW}⊘ Nothing changed — site is already up to date.{RESET}\n")
            return

        run(["git", "commit", "-m", commit_msg], cwd=tmp_dir)
        print(f"{GREEN}✓ Commit created{RESET}")

        # ── Push ──────────────────────────────────────────────────────────────
        print(f"\n{CYAN}Pushing to {remote}/{branch}...{RESET}")
        run(["git", "push", remote, f"HEAD:{branch}", "--force"], cwd=tmp_dir)

    finally:
        # ── Remove temp worktree ──────────────────────────────────────────────
        subprocess.run(["git", "worktree", "remove", "--force", str(tmp_dir)], cwd=BASE_DIR)
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ── Print the live URL ────────────────────────────────────────────────────
    remote_url = run_capture(["git", "remote", "get-url", remote])
    live_url = derive_pages_url(remote_url)
    print(f"\n{BOLD}{GREEN}🚀 Deployed successfully!{RESET}")
    if live_url:
        print(f"   {CYAN}Live at:{RESET} {BOLD}{live_url}{RESET}")
    print(f"   {DIM}(GitHub Pages may take 1–2 minutes to update){RESET}\n")


def derive_pages_url(remote_url: str) -> str:
    """
    Convert a GitHub remote URL to its expected GitHub Pages URL.
    Supports both HTTPS and SSH formats.
      https://github.com/user/repo.git  →  https://user.github.io/repo
      git@github.com:user/repo.git      →  https://user.github.io/repo
    """
    import re
    # HTTPS
    m = re.match(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$", remote_url)
    if m:
        user, repo = m.group(1), m.group(2)
        if repo == f"{user}.github.io":
            return f"https://{user}.github.io/"
        return f"https://{user}.github.io/{repo}/"
    # SSH
    m = re.match(r"git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$", remote_url)
    if m:
        user, repo = m.group(1), m.group(2)
        if repo == f"{user}.github.io":
            return f"https://{user}.github.io/"
        return f"https://{user}.github.io/{repo}/"
    return ""


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="aniketmaithani.net — Deploy blog output/ to GitHub Pages"
    )
    parser.add_argument(
        "--no-build", action="store_true",
        help="Skip running add_blog.py (use existing output/ as-is)"
    )
    parser.add_argument(
        "--remote", default="origin",
        help="Git remote name (default: origin)"
    )
    parser.add_argument(
        "--branch", default="gh-pages",
        help="Target branch for GitHub Pages (default: gh-pages)"
    )
    parser.add_argument(
        "--message", "-m",
        default=f"deploy: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        help="Git commit message for the deploy commit"
    )
    args = parser.parse_args()

    print(f"\n{BOLD}✦ aniketmaithani.net — GitHub Pages Deployer{RESET}")
    print(f"{DIM}Pushing your static blog to the cloud...{RESET}\n")

    check_git_repo()
    check_remote(args.remote)

    if not args.no_build:
        build_site()

    ensure_output()
    deploy(args.remote, args.branch, args.message)


if __name__ == "__main__":
    main()
