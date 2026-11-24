#!/usr/bin/env python3
"""Build APT indexes from private AList files using per-file download signatures."""
import bz2
import getpass
import gzip
import hashlib
import json
import lzma
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://alist.diandianderaunjianyuan.top"
ROOT = "/移动云ipa库/debs"


def request(url, data=None, token=""):
    headers = {"User-Agent": "Diandian-APT-index/1.0"}
    if data is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(data).encode()
    if token:
        headers["Authorization"] = token
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=180) as r:
                return r.read()
        except (OSError, urllib.error.URLError):
            if attempt == 2:
                raise
            time.sleep(3 * (attempt + 1))


def api(endpoint, data, token=""):
    result = json.loads(request(BASE + endpoint, data, token))
    if result.get("code") != 200:
        raise RuntimeError(f"AList {result.get('code')}: {result.get('message')}")
    return result["data"]


def credentials(gitdir):
    location = gitdir / "cloud-auth.json"
    token = os.environ.get("ALIST_TOKEN", "")
    if not token and location.exists():
        token = json.loads(location.read_text())["token"]
    if token:
        try:
            api("/api/fs/list", {"path": ROOT, "page": 1, "per_page": 1}, token)
            return token
        except RuntimeError:
            print("Saved login expired; sign in again.")
    print("Sign in to your AList. Credentials are never committed to GitHub.")
    username = input("AList username [admin]: ").strip() or "admin"
    password = getpass.getpass("AList password: ")
    otp = input("Two-factor code (leave blank if disabled): ").strip()
    data = {"username": username, "password": password}
    if otp:
        data["otp_code"] = otp
    token = api("/api/auth/login", data)["token"]
    location.write_text(json.dumps({"token": token}), encoding="utf-8")
    location.chmod(0o600)
    return token


def list_files(path, token):
    files = []
    page = 1
    while True:
        data = api("/api/fs/list", {"path": path, "page": page, "per_page": 100, "refresh": True}, token)
        items = data.get("content") or []
        for item in items:
            child = path + "/" + item["name"]
            if item["is_dir"]:
                files.extend(list_files(child, token))
            elif item["name"].lower().endswith(".deb"):
                files.append((child, item))
        if len(items) < 100:
            break
        page += 1
    return files


def download_url(path, sign):
    if not sign or sign.rsplit(":", 1)[-1] != "0":
        raise ValueError("A non-expiring per-file signature is required: " + path)
    return BASE + "/d" + urllib.parse.quote(path, safe="/") + "?sign=" + urllib.parse.quote(sign, safe="")


def package_fields(payload):
    fields = {}
    current = None
    for line in payload.splitlines():
        if line.startswith((" ", "\t")) and current:
            fields[current] += "\n" + line
        else:
            key, sep, value = line.partition(":")
            if sep:
                current = key
                fields[key] = value.lstrip()
    return fields


def write_indexes(paragraphs):
    payload = ("\n\n".join(paragraphs) + "\n\n").encode("utf-8")
    files = {"Packages": payload, "Packages.bz2": bz2.compress(payload),
             "Packages.gz": gzip.compress(payload, mtime=0), "Packages.xz": lzma.compress(payload)}
    # Keep zstd generation compatible with the existing phone command.
    files["Packages.zst"] = subprocess.check_output(["zstd", "-q", "-c"], input=payload)
    for name, data in files.items():
        tmp = Path(name + ".cloud-new")
        tmp.write_bytes(data)
        tmp.replace(name)


def main():
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    gitdir = Path(subprocess.check_output(["git", "rev-parse", "--absolute-git-dir"], text=True).strip())
    token = credentials(gitdir)
    cache_path = gitdir / "cloud-index-cache.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    files = list_files(ROOT, token)
    if not files:
        raise RuntimeError("No DEB files found; existing indexes were preserved.")
    previous = {}
    if Path("Packages").exists():
        for paragraph in re.split(r"\n\s*\n", Path("Packages").read_text(encoding="utf-8").strip()):
            entry = package_fields(paragraph)
            filename = entry.get("Filename", "")
            if filename:
                path = urllib.parse.unquote(urllib.parse.urlparse(filename).path)
                if path.startswith("/d/"):
                    path = path[2:]
                if path.startswith("debs/"):
                    path = ROOT + "/" + path[5:]
                previous[path] = entry.get("SHA256")
    next_cache, paragraphs = {}, []
    current_hashes = {}
    for number, (path, info) in enumerate(sorted(files), 1):
        url = download_url(path, info.get("sign", ""))
        stamp = [info["size"], info["modified"]]
        old = cache.get(path)
        if old and old["stamp"] == stamp:
            fields = old["fields"].copy()
            print(f"[{number}/{len(files)}] Cached: {info['name']}", flush=True)
        else:
            payload = request(url)
            if len(payload) != info["size"] or not payload.startswith(b"!<arch>\n"):
                raise ValueError("Invalid DEB download: " + info["name"])
            with tempfile.TemporaryDirectory() as tmp:
                deb = Path(tmp) / "package.deb"
                deb.write_bytes(payload)
                control = subprocess.check_output(["dpkg-deb", "--field", str(deb)]).decode("utf-8")
            fields = package_fields(control)
            if any(not fields.get(k) for k in ("Package", "Version", "Architecture")):
                raise ValueError("Missing required DEB fields: " + info["name"])
            fields.update({"Size": str(len(payload)), "MD5sum": hashlib.md5(payload).hexdigest(),
                           "SHA1": hashlib.sha1(payload).hexdigest(), "SHA256": hashlib.sha256(payload).hexdigest()})
            print(f"[{number}/{len(files)}] Verified: {info['name']}", flush=True)
        fields["Filename"] = url
        current_hashes[path] = fields["SHA256"]
        paragraphs.append("\n".join(f"{key}: {value}" for key, value in fields.items()))
        next_cache[path] = {"stamp": stamp, "fields": fields}
    # Write only after every file has been read and validated.
    write_indexes(paragraphs)
    cache_path.write_text(json.dumps(next_cache, ensure_ascii=False), encoding="utf-8")
    cache_path.chmod(0o600)
    added = set(current_hashes) - set(previous)
    removed = set(previous) - set(current_hashes)
    changed = {p for p in current_hashes.keys() & previous.keys() if current_hashes[p] != previous[p]}
    print(f"Added: {len(added)}; removed: {len(removed)}; changed: {len(changed)}")
    print(f"Updated {len(paragraphs)} package entries; DEB directory remains private.")


if __name__ == "__main__":
    main()
