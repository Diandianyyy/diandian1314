#!/usr/bin/env python3
"""Save site edits, integrate remote edits, then refresh cloud package indexes."""
from datetime import datetime
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys

INDEXES = ["Packages", "Packages.bz2", "Packages.gz", "Packages.xz", "Packages.zst", "Release"]

def local_deb_path(name):
    path = Path(name)
    return any(part.lower() in {"deb", "debs"} for part in path.parts) or path.suffix.lower() == ".deb"


def sync_local_path(name):
    path = Path(name)
    if not name:
        return False
    if local_deb_path(name) or any(part.lower() in {".git", "__pycache__"} for part in path.parts):
        return False
    if path.suffix.lower() in {".deb", ".pyc", ".pyo"} or name.endswith(".cloud-new"):
        return False
    if name == "Packages" or name.startswith("Packages."):
        return False
    return True


def git(*args, capture=False):
    return subprocess.run(["git", *args], check=True,
                          stdout=subprocess.PIPE if capture else None).stdout


def preserve_local_debs_and_pull():
    """Protect formerly tracked local DEBs from upstream deletion commits."""
    gitdir = Path(git("rev-parse", "--absolute-git-dir", capture=True).decode().strip())
    backup = gitdir / "local-debs-preserved"
    manifest = backup / "manifest.json"
    if manifest.exists():
        raise RuntimeError("Unfinished local DEB backup exists: " + str(backup) +
                           ". Restore it before trying another synchronization.")
    tracked = [name for name in git("ls-files", "-z", capture=True).decode().split("\0")
               if name and local_deb_path(name)]
    states = {}
    for name in tracked:
        path = Path(name)
        if path.is_dir() and not path.is_symlink():
            raise RuntimeError("Unexpected tracked directory: " + name)
        present = path.exists() or path.is_symlink()
        states[name] = present
        if present:
            saved = backup / "files" / name
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, saved, follow_symlinks=False)
    if tracked:
        backup.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(states, ensure_ascii=False), encoding="utf-8")
    try:
        if tracked:
            # Local bytes are safely copied first. Clean only these excluded
            # tracked paths so a user's local DEB edit cannot block the pull.
            git("checkout-index", "--force", "--", *tracked)
        git("pull", "--rebase", "origin", "main")
    finally:
        for name, present in states.items():
            path = Path(name)
            if present:
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.is_symlink():
                    path.unlink()
                shutil.copy2(backup / "files" / name, path, follow_symlinks=False)
            elif path.exists() or path.is_symlink():
                path.unlink()
        # Git does not retain empty directories; keep the local folder available.
        Path("debs").mkdir(exist_ok=True)
        if manifest.exists():
            manifest.unlink()
    print("本地 debs/ 和 DEB 已保留，不参与站点上传。", flush=True)


def recover_local_debs():
    """Restore only missing DEBs from the last historical debs tree."""
    revisions = git("rev-list", "--all", "--", "debs", capture=True).decode().splitlines()
    for revision in revisions:
        rows = git("ls-tree", "-r", "-z", revision, "--", "debs", capture=True).split(b"\0")
        candidates = []
        for row in rows:
            if not row:
                continue
            metadata, rawname = row.split(b"\t", 1)
            mode, kind, oid = metadata.decode().split()
            name = rawname.decode()
            if kind == "blob" and name.lower().endswith(".deb") and mode in {"100644", "100755"}:
                candidates.append((mode, oid, name))
        if not candidates:
            continue
        restored = 0
        for mode, oid, name in candidates:
            path = Path(name)
            if path.exists() or path.is_symlink():
                continue
            # Git tree paths must stay in the project's local debs directory.
            if path.is_absolute() or ".." in path.parts or path.parts[0] != "debs":
                raise RuntimeError("Unsafe historical package path.")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as destination:
                subprocess.run(["git", "cat-file", "blob", oid], stdout=destination, check=True)
            path.chmod(int(mode[-3:], 8))
            restored += 1
        Path("debs").mkdir(exist_ok=True)
        print(f"已从本地 Git 历史补回 {restored} 个缺失 DEB；现有文件未覆盖。", flush=True)
        return
    Path("debs").mkdir(exist_ok=True)
    print("本地 Git 历史没有可恢复的 DEB，仅创建 debs 文件夹；云盘文件不受影响。", flush=True)


def stage_site():
    # Reject pre-staged changes so an unrelated file cannot enter our commit.
    if git("diff", "--cached", "--name-only", capture=True).strip():
        raise RuntimeError("There are already staged changes. Commit or unstage them before running this command.")
    paths = set(git("ls-files", "-z", "--cached", "--others", "--exclude-standard",
                    capture=True).decode("utf-8").split("\0"))
    selected = []
    for name in sorted(paths):
        if sync_local_path(name):
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
        if "--recover-local-debs" in sys.argv:
            recover_local_debs()
        # Commit local public site edits before pulling, preserving phone edits.
        stage_site()
        print("Synchronizing local site edits with GitHub...", flush=True)
        preserve_local_debs_and_pull()
        # Start the downloaded revision of this program after Git finishes.
        subprocess.run([sys.executable, "scripts/update_cloud.py", "--after-pull"], check=True)
        return
    # Also collect files omitted by an older updater before it downloaded this
    # revision. This makes the existing command sufficient for the first upgrade.
    stage_site()
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
