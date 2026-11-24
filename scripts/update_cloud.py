#!/usr/bin/env python3
"""Save site edits, integrate remote edits, then refresh cloud package indexes."""
from datetime import datetime
from pathlib import Path
import os
import subprocess
import sys

INDEXES = ["Packages", "Packages.bz2", "Packages.gz", "Packages.xz", "Packages.zst"]
SITE_NAMES = {"Release", "CNAME", "sileo-featured.json", "README.md"}
SITE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg",
                   ".html", ".htm", ".css"}


def git(*args, capture=False):
    return subprocess.run(["git", *args], check=True,
                          stdout=subprocess.PIPE if capture else None).stdout


def stage_site():
    # Reject pre-staged changes so an unrelated file cannot enter our commit.
    if git("diff", "--cached", "--name-only", capture=True).strip():
        raise RuntimeError("There are already staged changes. Commit or unstage them before running this command.")
    paths = set(git("ls-files", "-z", "--cached", "--others", "--exclude-standard",
                    capture=True).decode("utf-8").split("\0"))
    selected = []
    for name in sorted(paths):
        path = Path(name)
        if not name or any(part.startswith(".") for part in path.parts):
            continue
        if path.parts[0] in {"debs", "scripts"}:
            continue
        if name in SITE_NAMES or path.suffix.lower() in SITE_EXTENSIONS:
            selected.append(name)
    # -A stages changed, new and removed public site files.
    if selected:
        git("add", "-A", "--", *selected)
    result = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if result.returncode == 1:
        git("commit", "-m", "Update source appearance " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    elif result.returncode:
        raise RuntimeError("Unable to inspect staged site changes.")


def main():
    os.chdir(Path(__file__).resolve().parents[1])
    if "--after-pull" not in sys.argv:
        # Commit local public site edits before pulling, preserving phone edits.
        stage_site()
        print("Synchronizing local site edits with GitHub...", flush=True)
        git("pull", "--rebase", "origin", "main")
        # Start the downloaded revision of this program after Git finishes.
        subprocess.run([sys.executable, "scripts/update_cloud.py", "--after-pull"], check=True)
        return
    print("Scanning cloud packages...", flush=True)
    subprocess.run([sys.executable, "scripts/sync_cloud.py"], check=True)
    git("add", "--", *INDEXES)
    result = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if result.returncode == 1:
        git("commit", "-m", "Update cloud package index " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    elif result.returncode:
        raise RuntimeError("Unable to inspect staged index changes.")
    # A failed push is visible and leaves all changes saved in local commits.
    git("push", "origin", "main")
    print("Source appearance and cloud package indexes synchronized.", flush=True)


if __name__ == "__main__":
    main()
