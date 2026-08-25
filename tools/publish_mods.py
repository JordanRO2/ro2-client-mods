#!/usr/bin/env python3
"""Generate and deploy the client-mods manifest from mods_config.json.

The launcher's Client Mods list is driven entirely by a line-oriented manifest served at
`/api/public/mods/mods_manifest.txt`. This script is the one place that manifest is
authored: edit `mods_config.json` (add / update / deprecate a mod), then run this to
regenerate the manifest and, optionally, upload it (and any changed mod files) to the
server.

Manifest line types (older launchers understand only V/C/M and ignore D/X/A):
  V <version>
  M <id> <md5> <size> <installPath> <url_ours> <url_github> <title...>
  C <id> <cleanMd5> <cleanSize> <clean_url_ours> <clean_url_github>   stock, for restore
  D <id>                                                              deprecated: revert-only
  X <id> <version>                                                    human version string
  A <id> <notice...>                                                  require accept, show notice

Config (mods_config.json): serverBase/githubBase + a list of mods. Each mod:
  id, installPath, file, md5, size, title           (required for an active mod)
  version, deprecated (bool), accept (notice str)    (optional)
  clean: { file, md5, size }                         (optional stock, enables restore)
URLs are derived as serverBase/githubBase + the file name.

Usage:
  # regenerate the manifest text (to stdout) from the config as-is:
  python publish_mods.py

  # recompute md5/size from the built mod files in <dir> before generating:
  python publish_mods.py --files "C:/path/to/built/mods"

  # write the manifest to a file:
  python publish_mods.py --out mods_manifest.txt

  # regenerate AND deploy to the server (scp manifest; also scp any --files that changed):
  python publish_mods.py --deploy
  python publish_mods.py --files "C:/path/to/built/mods" --deploy

Deploying to GitHub release assets (the second source) is not automated here; upload the
changed files to the release named by githubBase with `gh release upload <tag> <files...>`.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "mods_config.json")
SERVER_MANIFEST = "~/rag2-site/static/downloads/mods/mods_manifest.txt"
SERVER_FILES = "~/rag2-site/static/downloads/mods/files"


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def refresh_from_files(cfg, files_dir):
    """Recompute md5/size for every mod (and its clean) from files in files_dir.
    Returns the list of local file paths that exist (to upload)."""
    to_upload = []
    for m in cfg["mods"]:
        for key in (None, "clean"):
            spec = m if key is None else m.get("clean")
            if not spec or "file" not in spec:
                continue
            p = os.path.join(files_dir, spec["file"])
            if os.path.isfile(p):
                spec["md5"] = md5_of(p)
                spec["size"] = os.path.getsize(p)
                to_upload.append(p)
                print(f"  {spec['file']}: md5={spec['md5']} size={spec['size']}", file=sys.stderr)
            else:
                print(f"  (skip, not found) {p}", file=sys.stderr)
    return to_upload


def build_manifest(cfg):
    sb, gb = cfg["serverBase"], cfg["githubBase"]
    L = [f"V {cfg['version']}",
         "# M <id> <md5> <size> <installPath> <url_ours> <url_github> <title...>"]
    for m in cfg["mods"]:
        L.append("M {id} {md5} {size} {path} {ours} {gh} {title}".format(
            id=m["id"], md5=m["md5"], size=m["size"], path=m["installPath"],
            ours=sb + m["file"], gh=gb + m["file"], title=m["title"]))
    L.append("# C <id> <cleanMd5> <cleanSize> <clean_url_ours> <clean_url_github>  (stock, for restore)")
    for m in cfg["mods"]:
        c = m.get("clean")
        if c:
            L.append("C {id} {md5} {size} {ours} {gh}".format(
                id=m["id"], md5=c["md5"], size=c["size"],
                ours=sb + c["file"], gh=gb + c["file"]))
    # D / X / A attach to the M of the same id and must follow it (all M lines are above).
    dep = [m for m in cfg["mods"] if m.get("deprecated")]
    if dep:
        L.append("# D <id>  (deprecated: revert-only)")
        for m in dep:
            L.append(f"D {m['id']}")
    ver = [m for m in cfg["mods"] if m.get("version")]
    if ver:
        L.append("# X <id> <version>")
        for m in ver:
            L.append(f"X {m['id']} {m['version']}")
    acc = [m for m in cfg["mods"] if m.get("accept")]
    if acc:
        L.append("# A <id> <notice...>  (require accept)")
        for m in acc:
            L.append(f"A {m['id']} {m['accept']}")
    return "\n".join(L) + "\n"


def scp(local, remote, host):
    subprocess.run(["scp", local, f"{host}:{remote}"], check=True)


def main():
    ap = argparse.ArgumentParser(description="Generate/deploy the client-mods manifest.")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--files", help="dir with built mod files; recompute md5/size + mark for upload")
    ap.add_argument("--out", help="write the manifest to this path (default: stdout)")
    ap.add_argument("--deploy", action="store_true", help="scp the manifest (and --files) to the server")
    ap.add_argument("--host", default="booth-stash")
    ap.add_argument("--server-manifest", default=SERVER_MANIFEST)
    ap.add_argument("--server-files", default=SERVER_FILES)
    args = ap.parse_args()

    cfg = json.load(open(args.config, encoding="utf-8"))
    uploads = refresh_from_files(cfg, args.files) if args.files else []
    manifest = build_manifest(cfg)

    out_path = args.out or (os.path.join(HERE, "mods_manifest.txt") if args.deploy else None)
    if out_path:
        open(out_path, "w", encoding="utf-8", newline="\n").write(manifest)
        print(f"wrote {out_path}", file=sys.stderr)
    else:
        sys.stdout.write(manifest)

    if args.deploy:
        for p in uploads:
            print(f"scp {os.path.basename(p)} -> {args.server_files}/", file=sys.stderr)
            scp(p, args.server_files.rstrip("/") + "/" + os.path.basename(p), args.host)
        print(f"scp manifest -> {args.server_manifest}", file=sys.stderr)
        scp(out_path, args.server_manifest, args.host)
        print("deployed. (GitHub release assets are not updated by this script.)", file=sys.stderr)


if __name__ == "__main__":
    main()
