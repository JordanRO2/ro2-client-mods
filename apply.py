#!/usr/bin/env python3
"""
RO2 client mods — unified, selectable applier.

Applies any subset of the mods declared in `mods.toml` to a client install:
  * exe             -> byte-patches the client exe via exe/patch_client.py
  * shader-bytepatch -> pattern byte-edits inside a .fxo in DATA.VDK (constants only;
                        the vertex shader / GPU-skinning bytes stay identical)
  * shader-replace  -> swaps a whole .fxo in DATA.VDK for a modified local build
  * texture         -> (reserved)

Model: the pristine exe + DATA.VDK are captured to backups/ on first apply, and every
apply REBUILDS the client from that baseline (exe: re-patch from pristine; VDK: extract
pristine -> overlay enabled shader mods -> repack). So disabling a mod and re-applying
truly removes it, and `--revert` just restores the two pristine files.

Which mods are "enabled" = the `enabled` default in mods.toml, overridden per-id by
enabled.json (written by --menu / --enable / --disable). Selection resolves live, so the
config stays declarative and nothing rewrites mods.toml.

Usage:
  python apply.py --list                 # catalog + current enabled/applied state
  python apply.py --apply enabled        # apply every currently-enabled mod
  python apply.py --apply all            # apply every mod
  python apply.py --apply id1,id2        # apply exactly these (does not change enabled set)
  python apply.py --menu                 # interactive select -> saves enabled.json -> applies
  python apply.py --enable id1,id2       # mark enabled (persist), no apply
  python apply.py --disable id1          # mark disabled (persist), no apply
  python apply.py --revert               # restore pristine exe + DATA.VDK
  python apply.py --dry-run --apply all  # show what would happen, touch nothing
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    sys.exit("This tool needs Python 3.11+ (tomllib). Please run with a newer python.")

try:  # keep box-drawing / arrows readable on a legacy Windows console codepage
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT     = Path(__file__).resolve().parent
CONFIG   = ROOT / "mods.toml"
BACKUPS  = ROOT / "backups"
SHADERS  = ROOT / "shaders"
PATCHER  = ROOT / "exe" / "patch_client.py"
STATE    = ROOT / "enabled.json"
MANIFEST = BACKUPS / "baseline.json"

C_OK, C_OFF, C_ON, C_WARN, C_DIM, C_END = "\033[92m", "\033[90m", "\033[96m", "\033[93m", "\033[2m", "\033[0m"


# --------------------------------------------------------------------------- config / state
def load_config():
    if not CONFIG.exists():
        sys.exit(f"missing {CONFIG}")
    with open(CONFIG, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_mods"] = {m["id"]: m for m in cfg.get("mod", [])}
    if len(cfg["_mods"]) != len(cfg.get("mod", [])):
        sys.exit("duplicate mod id in mods.toml")
    return cfg


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {}


def save_state(state):
    STATE.write_text(json.dumps(state, indent=2))


def is_enabled(mod, state):
    return bool(state.get(mod["id"], mod.get("enabled", False)))


def client_paths(cfg):
    d = Path(cfg["client"]["dir"])
    return d, d / cfg["client"]["exe"], d / cfg["client"]["vdk"]


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- baseline backup
def ensure_baseline(cfg, dry=False):
    """Capture the pristine exe + DATA.VDK once. Returns (exe_backup, vdk_backup)."""
    _, exe, vdk = client_paths(cfg)
    for p, what in ((exe, "exe"), (vdk, "DATA.VDK")):
        if not p.exists():
            sys.exit(f"client {what} not found: {p}")
    exe_bak = BACKUPS / "Rag2.exe.orig"
    vdk_bak = BACKUPS / "DATA.VDK.orig"
    if MANIFEST.exists():
        return exe_bak, vdk_bak
    if dry:
        print(f"{C_DIM}  [dry] would back up pristine exe + DATA.VDK to backups/{C_END}")
        return exe_bak, vdk_bak
    BACKUPS.mkdir(exist_ok=True)
    print("  capturing pristine baseline (first run)...")
    shutil.copy2(exe, exe_bak)
    shutil.copy2(vdk, vdk_bak)
    man = {"exe": {"src": str(exe), "sha256": sha256(exe_bak)},
           "vdk": {"src": str(vdk), "sha256": sha256(vdk_bak)}}
    MANIFEST.write_text(json.dumps(man, indent=2))
    print(f"    exe  {man['exe']['sha256'][:16]}  -> backups/Rag2.exe.orig")
    print(f"    vdk  {man['vdk']['sha256'][:16]}  -> backups/DATA.VDK.orig")
    return exe_bak, vdk_bak


# --------------------------------------------------------------------------- exe apply
def apply_exe(cfg, exe_mods, exe_bak, dry=False):
    _, exe, _ = client_paths(cfg)
    fix_ids = []
    for m in exe_mods:
        fix_ids += m.get("fix_ids", [])
    if not fix_ids:
        if not dry:
            shutil.copy2(exe_bak, exe)          # no exe mods -> pristine exe
        print(f"  exe: no exe mods enabled -> restored pristine {exe.name}")
        return
    only = ",".join(fix_ids)
    print(f"  exe: {len(exe_mods)} bundle(s), {len(fix_ids)} fix(es) -> {exe.name}")
    if dry:
        print(f"{C_DIM}    [dry] patch_client.py --in <pristine> --out {exe.name} --only {only}{C_END}")
        return
    cmd = [cfg["tools"]["python"], str(PATCHER), "--in", str(exe_bak), "--out", str(exe), "--only", only]
    r = subprocess.run(cmd, capture_output=True, text=True)
    sys.stdout.write("".join("    " + ln + "\n" for ln in r.stdout.splitlines() if ln.strip()))
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        sys.exit(f"  exe patch FAILED (rc={r.returncode})")


# --------------------------------------------------------------------------- shader apply
def _bytepatch(data, find_hex, repl_hex, expected):
    find, repl = bytes.fromhex(find_hex), bytes.fromhex(repl_hex)
    if len(find) != len(repl):
        raise ValueError("find/repl length mismatch")
    n = data.count(find)
    already = data.count(repl)
    if n == 0 and already:
        return data, already, "already"
    if expected is not None and n != expected:
        raise ValueError(f"pattern count {n} != expected {expected} (refusing to patch)")
    return data.replace(find, repl), n, "ok"


def apply_shaders(cfg, shader_mods, vdk_bak, dry=False):
    _, _, vdk = client_paths(cfg)
    tool = cfg["tools"]["vdk_tool"]
    if not shader_mods:
        if not dry:
            shutil.copy2(vdk_bak, vdk)          # no shader mods -> pristine VDK
        print(f"  shaders: none enabled -> restored pristine {vdk.name}")
        return
    print(f"  shaders: {len(shader_mods)} mod(s) -> rebuild {vdk.name}")
    if dry:
        for m in shader_mods:
            print(f"{C_DIM}    [dry] {m['id']}: {m['type']}{C_END}")
        return
    work = Path(tempfile.mkdtemp(prefix="ro2vdk_"))
    try:
        r = subprocess.run([tool, "extract", str(vdk_bak), str(work)], capture_output=True, text=True)
        if r.returncode != 0:
            sys.stderr.write(r.stdout + r.stderr)
            sys.exit("  VDK extract failed")
        for m in shader_mods:
            if m["type"] == "shader-replace":
                for fn in m["files"]:
                    src = SHADERS / fn
                    if not src.exists():
                        sys.exit(f"  {m['id']}: local shader missing: {src}  (copyrighted .fxo are kept out of git; place the built file in shaders/)")
                    dst = work / "SHADERS" / fn
                    shutil.copy2(src, dst)
                    print(f"    replace SHADERS/{fn}")
            elif m["type"] == "shader-bytepatch":
                for p in m["patch"]:
                    tgt = work / p["file"]
                    data = tgt.read_bytes()
                    data, n, status = _bytepatch(data, p["find"], p["repl"], p.get("count"))
                    tgt.write_bytes(data)
                    print(f"    bytepatch {p['file']}: {n} site(s) [{status}]")
            elif m["type"] == "texture":
                for fe in m["file"]:
                    src = ROOT / fe["src"]
                    if not src.exists():
                        sys.exit(f"  {m['id']}: local file missing: {src}")
                    dst = work / fe["dst"]
                    if not dst.exists():
                        print(f"    WARNING: {fe['dst']} not in VDK - adding new (verify path)")
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    print(f"    texture {fe['dst']}")
        out = vdk.with_suffix(".vdk.new")
        r = subprocess.run([tool, "pack", str(work), str(out)], capture_output=True, text=True)
        if r.returncode != 0:
            sys.stderr.write(r.stdout + r.stderr)
            sys.exit("  VDK pack failed")
        shutil.move(str(out), str(vdk))
        print(f"    packed -> {vdk.name} ({vdk.stat().st_size:,} B)")
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- commands
def resolve_selection(cfg, state, selection):
    mods = cfg["_mods"]
    if selection == "all":
        return list(cfg["mod"])
    if selection == "enabled":
        return [m for m in cfg["mod"] if is_enabled(m, state)]
    ids = [s.strip() for s in selection.split(",") if s.strip()]
    bad = [i for i in ids if i not in mods]
    if bad:
        sys.exit(f"unknown mod id(s): {', '.join(bad)}")
    return [mods[i] for i in ids]


def cmd_apply(cfg, state, selection, dry):
    chosen = resolve_selection(cfg, state, selection)
    _, exe, vdk = client_paths(cfg)
    print(f"\nTarget: {Path(cfg['client']['dir']).name}")
    print(f"Applying: {', '.join(m['id'] for m in chosen) or '(nothing)'}\n")
    exe_bak, vdk_bak = ensure_baseline(cfg, dry)
    apply_exe(cfg, [m for m in chosen if m["type"] == "exe"], exe_bak, dry)
    apply_shaders(cfg, [m for m in chosen if m["type"] != "exe"], vdk_bak, dry)
    if not dry:
        (BACKUPS / "applied.json").write_text(json.dumps([m["id"] for m in chosen], indent=2))
    print(f"\n{C_OK}Done.{C_END} Launch the client to test. Undo everything with: python apply.py --revert")


def cmd_revert(cfg, dry):
    if not MANIFEST.exists():
        sys.exit("no baseline captured yet — nothing to revert")
    _, exe, vdk = client_paths(cfg)
    exe_bak, vdk_bak = BACKUPS / "Rag2.exe.orig", BACKUPS / "DATA.VDK.orig"
    print(f"Reverting {Path(cfg['client']['dir']).name} to pristine baseline...")
    if dry:
        print(f"{C_DIM}  [dry] restore {exe.name} and {vdk.name} from backups/{C_END}")
        return
    shutil.copy2(exe_bak, exe)
    shutil.copy2(vdk_bak, vdk)
    (BACKUPS / "applied.json").write_text("[]")
    print(f"{C_OK}Reverted.{C_END} exe + DATA.VDK restored from backups/.")


def cmd_list(cfg, state):
    applied = []
    ap = BACKUPS / "applied.json"
    if ap.exists():
        applied = json.loads(ap.read_text())
    print(f"\n{C_DIM}Client:{C_END} {cfg['client']['dir']}")
    print(f"{C_DIM}Baseline captured:{C_END} {'yes' if MANIFEST.exists() else 'no (first --apply will capture it)'}\n")
    groups = {}
    for m in cfg["mod"]:
        groups.setdefault(m["type"], []).append(m)
    order = ["exe", "shader-bytepatch", "shader-replace", "texture"]
    for typ in sorted(groups, key=lambda t: order.index(t) if t in order else 99):
        print(f"{C_ON}== {typ} =={C_END}")
        for m in groups[typ]:
            en = is_enabled(m, state)
            mark = f"{C_OK}[x]{C_END}" if en else f"{C_OFF}[ ]{C_END}"
            live = f" {C_OK}* applied{C_END}" if m["id"] in applied else ""
            print(f"  {mark} {m['id']:<24} {m['title']}{live}")
            print(f"      {C_DIM}{m.get('desc','')}{C_END}")
        print()
    print(f"{C_DIM}[x]=enabled. Edit with --menu / --enable / --disable, then: python apply.py --apply enabled{C_END}")


def cmd_set_enabled(cfg, state, ids, value):
    mods = cfg["_mods"]
    for i in (s.strip() for s in ids.split(",") if s.strip()):
        if i not in mods:
            sys.exit(f"unknown mod id: {i}")
        state[i] = value
    save_state(state)
    print(f"{'enabled' if value else 'disabled'}: {ids}  (saved to enabled.json)")


def cmd_menu(cfg, state, dry):
    ordered = list(cfg["mod"])
    print("\nSelect mods to apply (space-separated numbers to TOGGLE, 'a'=all, 'n'=none, Enter=apply):\n")
    while True:
        for idx, m in enumerate(ordered, 1):
            en = is_enabled(m, state)
            mark = f"{C_OK}[x]{C_END}" if en else f"{C_OFF}[ ]{C_END}"
            print(f"  {idx:>2}. {mark} {C_DIM}{m['type']:<16}{C_END} {m['title']}")
        raw = input("\n> ").strip().lower()
        if raw == "":
            break
        if raw == "a":
            for m in ordered:
                state[m["id"]] = True
        elif raw == "n":
            for m in ordered:
                state[m["id"]] = False
        else:
            for tok in raw.split():
                if tok.isdigit() and 1 <= int(tok) <= len(ordered):
                    mid = ordered[int(tok) - 1]["id"]
                    state[mid] = not is_enabled(ordered[int(tok) - 1], state)
        save_state(state)
        print()
    save_state(state)
    cmd_apply(cfg, state, "enabled", dry)


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="RO2 client mods — selectable applier")
    ap.add_argument("--list", action="store_true", help="show the mod catalog + state")
    ap.add_argument("--apply", metavar="SEL", help="'enabled' | 'all' | comma-separated ids")
    ap.add_argument("--menu", action="store_true", help="interactive select, then apply")
    ap.add_argument("--enable", metavar="IDS", help="mark ids enabled (persist), no apply")
    ap.add_argument("--disable", metavar="IDS", help="mark ids disabled (persist), no apply")
    ap.add_argument("--revert", action="store_true", help="restore pristine exe + DATA.VDK")
    ap.add_argument("--dry-run", action="store_true", help="show actions, change nothing")
    args = ap.parse_args()

    cfg = load_config()
    state = load_state()

    if args.enable:
        cmd_set_enabled(cfg, state, args.enable, True)
    if args.disable:
        cmd_set_enabled(cfg, state, args.disable, False)
    if args.revert:
        cmd_revert(cfg, args.dry_run)
    elif args.menu:
        cmd_menu(cfg, state, args.dry_run)
    elif args.apply:
        cmd_apply(cfg, state, args.apply, args.dry_run)
    elif args.list or not (args.enable or args.disable):
        cmd_list(cfg, state)


if __name__ == "__main__":
    main()
