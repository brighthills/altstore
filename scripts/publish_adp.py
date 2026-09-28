#!/usr/bin/env python3
"""Manage the AltStore PAL source hosted on GitHub Pages.

Subcommands:
  configure  Replace the OWNER/REPO placeholder base URL in source.json.
  validate   Check source.json for missing fields, placeholders and broken local asset links.
  publish    Fetch a notarized ADP from api.altstore.io, upload its files to a GitHub Release,
             add the version (with assetURLs) to source.json and set the app's appPermissions
             from the IPA's entitlements and privacy usage descriptions.

Standard library only; `gh` is required for `publish` unless --dry-run is given.
"""

import argparse
import json
import os
import plistlib
import re
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "source.json"
API = "https://api.altstore.io"
NOT_FOUND_GRACE = 300  # seconds of 404s before giving up on an ADP ID
PLACEHOLDER_BASE = "https://OWNER.github.io/REPO"
PLACEHOLDER_MARKERS = ("OWNER.github.io", "REPLACE_ME")

REQUIRED_SOURCE = ("name", "apps", "news")
REQUIRED_APP = ("name", "bundleIdentifier", "developerName", "localizedDescription",
                "iconURL", "versions", "appPermissions")
REQUIRED_VERSION = ("version", "buildVersion", "date", "downloadURL", "size")

INFO_PLIST = re.compile(r"^Payload/[^/]+\.app/Info\.plist$")
BUNDLE_PLIST = re.compile(r"^(Payload/.+\.(?:app|appex))/Info\.plist$")  # app, extensions, watch app

LC_CODE_SIGNATURE = 0x1D
CSMAGIC_EMBEDDED_SIGNATURE = 0xFADE0CC0
CSMAGIC_EMBEDDED_ENTITLEMENTS = 0xFADE7171
# AltStore adds these itself; sources must not list them.
IMPLICIT_ENTITLEMENTS = {"application-identifier", "com.apple.developer.team-identifier"}


# ---------------------------------------------------------------- source.json

def load_source():
    return json.loads(SOURCE.read_text(encoding="utf-8"))


def save_source(data):
    SOURCE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def site_base(src):
    """Base URL of the Pages site, inferred from the source iconURL."""
    icon = src.get("iconURL", "")
    return icon.rsplit("/assets/", 1)[0] if "/assets/" in icon else None


def validate(src):
    """Return (errors, warnings) for a parsed source."""
    errors, warnings = [], []

    text = json.dumps(src)
    for marker in PLACEHOLDER_MARKERS:
        if marker in text:
            errors.append(f"placeholder '{marker}' still present (run `configure` / fill in app details)")

    for key in REQUIRED_SOURCE:
        if key not in src:
            errors.append(f"source: missing '{key}'")

    tint = src.get("tintColor")
    if tint and not re.fullmatch(r"#?[0-9A-Fa-f]{6}", tint):
        errors.append(f"source: tintColor '{tint}' is not a 6-digit hex color")

    bundle_ids = set()
    for i, app in enumerate(src.get("apps", [])):
        label = app.get("bundleIdentifier") or f"apps[{i}]"
        for key in REQUIRED_APP:
            if key not in app:
                errors.append(f"{label}: missing '{key}'")
        if label in bundle_ids:
            errors.append(f"{label}: duplicate bundleIdentifier")
        bundle_ids.add(label)

        marketplace_id = str(app.get("marketplaceID", ""))
        if not marketplace_id.isdigit():
            errors.append(f"{label}: marketplaceID must be the numeric Apple ID from App Store Connect")

        perms = app.get("appPermissions", {})
        if not isinstance(perms.get("entitlements", []), list) or not isinstance(perms.get("privacy", {}), dict):
            errors.append(f"{label}: appPermissions must be {{'entitlements': [...], 'privacy': {{...}}}}")

        versions = app.get("versions", [])
        if not versions:
            warnings.append(f"{label}: no versions yet (publish an ADP to add one)")
        for v in versions:
            vlabel = f"{label} {v.get('version')} ({v.get('buildVersion')})"
            for key in REQUIRED_VERSION:
                if key not in v:
                    errors.append(f"{vlabel}: missing '{key}'")
            if not isinstance(v.get("size", 0), int) or v.get("size", 0) <= 0:
                errors.append(f"{vlabel}: size must be a positive integer (bytes)")
            try:
                datetime.fromisoformat(str(v.get("date", "")).replace("Z", "+00:00"))
            except ValueError:
                errors.append(f"{vlabel}: date is not ISO 8601")
            if not str(v.get("downloadURL", "")).startswith("https://"):
                errors.append(f"{vlabel}: downloadURL must be https")

    for featured in src.get("featuredApps", []):
        if featured not in bundle_ids:
            errors.append(f"featuredApps: '{featured}' is not an app in this source")

    # URLs pointing at this Pages site must exist in the repo, or they'll 404 after deploy.
    base = site_base(src)
    if base and "OWNER.github.io" not in base:
        for url in sorted(set(re.findall(r'"(https://[^"]+)"', text))):
            if url.startswith(base + "/assets/"):
                local = ROOT / url[len(base) + 1:]
                if not local.is_file():
                    errors.append(f"missing local asset for {url} (expected {local.relative_to(ROOT)})")

    return errors, warnings


def cmd_validate(_args):
    errors, warnings = validate(load_source())
    for w in warnings:
        print(f"warning: {w}")
    for e in errors:
        print(f"error: {e}")
    if errors:
        sys.exit(1)
    print("source.json OK")


def cmd_configure(args):
    if args.domain:
        base = f"https://{args.domain}"
    elif not (args.owner and args.repo):
        sys.exit("configure needs --domain, or both --owner and --repo")
    elif args.repo.lower() == f"{args.owner.lower()}.github.io":
        base = f"https://{args.owner.lower()}.github.io"
    else:
        base = f"https://{args.owner.lower()}.github.io/{args.repo}"
    text = SOURCE.read_text(encoding="utf-8")
    if PLACEHOLDER_BASE not in text:
        sys.exit(f"'{PLACEHOLDER_BASE}' not found in source.json (already configured?)")
    SOURCE.write_text(text.replace(PLACEHOLDER_BASE, base), encoding="utf-8")
    print(f"Base URL set to {base}")
    print(f"Source URL: {base}/source.json")


# ---------------------------------------------------------------- ADP fetching

def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method, headers={
        "Content-Type": "application/json",
        "User-Agent": "altstore-pal-source",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, _parse_json(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, _parse_json(e.read())


def _parse_json(raw):
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {"raw": raw.decode(errors="replace")[:500]}


def fetch_adp(adp_id, workdir, timeout, interval):
    """Ask AltStore to process the ADP, wait for it, and download it. Returns the file path."""
    status, resp = api("POST", "/adps", {"adpID": adp_id})
    print(f"Process request: HTTP {status} {resp}")

    # POST is asynchronous (202), so an unknown ADP only shows up as a lasting 404 here.
    start = time.monotonic()
    while True:
        status, resp = api("GET", f"/adps/{adp_id}")
        url = resp.get("downloadURL") if isinstance(resp, dict) else None
        if url:
            break
        state = str(resp.get("status", "")) if isinstance(resp, dict) else ""
        if re.search(r"fail|error", state, re.IGNORECASE):
            sys.exit(f"AltStore reported ADP processing {state!r}: {resp}")
        elapsed = time.monotonic() - start
        if status == 404 and elapsed > NOT_FOUND_GRACE:
            sys.exit(f"ADP {adp_id} still not found after {NOT_FOUND_GRACE}s. Check the ID and that "
                     "AltStore PAL is authorized as a marketplace for this app in App Store Connect.")
        if elapsed > timeout:
            sys.exit(f"ADP not ready after {timeout}s: HTTP {status} {resp}")
        print(f"Waiting for ADP (HTTP {status}, status={state!r})...")
        time.sleep(interval)

    dest = workdir / "adp.download"
    print("Downloading ADP...")
    req = urllib.request.Request(url, headers={"User-Agent": "altstore-pal-source"})
    with urllib.request.urlopen(req, timeout=600) as resp, open(dest, "wb") as out:
        while chunk := resp.read(1 << 20):
            out.write(chunk)
    return dest


def extract_adp(archive, workdir):
    """Unzip the ADP and return the directory holding manifest.json."""
    if not zipfile.is_zipfile(archive):
        sys.exit(f"Downloaded ADP is not a zip archive ({archive.stat().st_size} bytes)")
    out = workdir / "adp"
    with zipfile.ZipFile(archive) as z:
        z.extractall(out)
    manifests = [p for p in out.rglob("manifest.json") if "__MACOSX" not in p.parts]
    if len(manifests) != 1:
        sys.exit(f"Expected exactly one manifest.json in the ADP, found {len(manifests)}")
    return manifests[0].parent


def adp_assets(adp_root):
    """Map assetURLs keys to files: 'manifest', 'signature', and each IPA's asset ID (file stem)."""
    assets = {}
    for path in sorted(p for p in adp_root.rglob("*") if p.is_file()):
        if "__MACOSX" in path.parts or path.name == ".DS_Store":
            continue
        key = "manifest" if path.name == "manifest.json" else path.stem
        if key in assets:
            sys.exit(f"Two ADP files map to the same asset key '{key}': {assets[key]} and {path}")
        assets[key] = path
    names = [p.name for p in assets.values()]
    if len(names) != len(set(names)):
        sys.exit("ADP contains files with the same name in different folders; can't flatten into a release")
    if "signature" not in assets:
        sys.exit("ADP has no 'signature' file")
    return assets


def ipa_info(ipas):
    """Read bundle ID / version / build / min OS from the IPAs' Info.plist; all variants must agree."""
    infos = set()
    for ipa in ipas:
        with zipfile.ZipFile(ipa) as z:
            name = next((n for n in z.namelist() if INFO_PLIST.match(n)), None)
            if not name:
                sys.exit(f"{ipa.name}: no Payload/*.app/Info.plist")
            plist = plistlib.loads(z.read(name))
        infos.add((plist["CFBundleIdentifier"], plist["CFBundleShortVersionString"],
                   plist["CFBundleVersion"], plist.get("MinimumOSVersion")))
    if len(infos) != 1:
        sys.exit(f"IPA variants disagree on bundle/version: {sorted(infos)}")
    return infos.pop()


def ipa_permissions(ipas):
    """appPermissions for the app and its extensions. AltStore refuses to install on a mismatch."""
    entitlements, privacy = set(), {}
    for ipa in ipas:
        with zipfile.ZipFile(ipa) as z:
            names = set(z.namelist())
            for name in sorted(names):
                m = BUNDLE_PLIST.match(name)
                if not m:
                    continue
                plist = plistlib.loads(z.read(name))
                privacy.update({k: v for k, v in plist.items() if k.endswith("UsageDescription")})
                exe = f"{m[1]}/{plist.get('CFBundleExecutable', '')}"
                if exe in names:
                    entitlements.update(macho_entitlements(z.read(exe)))
    return {"entitlements": sorted(entitlements - IMPLICIT_ENTITLEMENTS),
            "privacy": dict(sorted(privacy.items()))}


def macho_entitlements(binary):
    """Entitlements embedded in a Mach-O's code signature (thin or universal), or {}."""
    magic = struct.unpack_from(">I", binary)[0]
    if magic == 0xCAFEBABE:
        count = struct.unpack_from(">I", binary, 4)[0]
        slices = [struct.unpack_from(">I", binary, 8 + i * 20 + 8)[0] for i in range(count)]
    elif magic == 0xCAFEBABF:
        count = struct.unpack_from(">I", binary, 4)[0]
        slices = [struct.unpack_from(">Q", binary, 8 + i * 32 + 8)[0] for i in range(count)]
    else:
        slices = [0]
    entitlements = {}
    for base in slices:
        entitlements.update(_slice_entitlements(binary, base))
    return entitlements


def _slice_entitlements(binary, base):
    header = {b"\xcf\xfa\xed\xfe": 32, b"\xce\xfa\xed\xfe": 28}.get(binary[base:base + 4])
    if header is None:
        return {}
    ncmds = struct.unpack_from("<I", binary, base + 16)[0]
    pos = base + header
    for _ in range(ncmds):
        cmd, size = struct.unpack_from("<II", binary, pos)
        if cmd == LC_CODE_SIGNATURE:
            sig = base + struct.unpack_from("<I", binary, pos + 8)[0]
            magic, _length, count = struct.unpack_from(">III", binary, sig)
            if magic != CSMAGIC_EMBEDDED_SIGNATURE:
                return {}
            for i in range(count):
                offset = struct.unpack_from(">I", binary, sig + 12 + i * 8 + 4)[0]
                blob_magic, blob_len = struct.unpack_from(">II", binary, sig + offset)
                if blob_magic == CSMAGIC_EMBEDDED_ENTITLEMENTS:
                    return plistlib.loads(binary[sig + offset + 8:sig + offset + blob_len])
            return {}
        pos += size
    return {}


# ---------------------------------------------------------------- GitHub Releases

def gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def upload_release(tag, title, notes, files):
    """Create (or update) the release and return {asset name: download URL}."""
    exists = subprocess.run(["gh", "release", "view", tag], capture_output=True).returncode == 0
    paths = [str(f) for f in files]
    if exists:
        print(f"Release {tag} exists, replacing assets")
        gh("release", "upload", tag, *paths, "--clobber")
    else:
        print(f"Creating release {tag}")
        gh("release", "create", tag, *paths, "--title", title, "--notes", notes or title)
    assets = json.loads(gh("release", "view", tag, "--json", "assets"))["assets"]
    return {a["name"]: a["url"] for a in assets}


def cmd_publish(args):
    src = load_source()
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        if args.adp_zip:
            archive = Path(args.adp_zip)
        else:
            archive = fetch_adp(args.adp_id, workdir, args.timeout, args.interval)
        adp_root = extract_adp(archive, workdir)
        assets = adp_assets(adp_root)
        ipas = [p for p in assets.values() if p.suffix == ".ipa"]
        if not ipas:
            sys.exit("ADP contains no .ipa files")
        bundle_id, version, build, min_os = ipa_info(ipas)
        print(f"ADP: {bundle_id} {version} ({build}), {len(ipas)} IPA variant(s)")
        permissions = ipa_permissions(ipas)
        print(f"appPermissions from IPA: {json.dumps(permissions, indent=2, ensure_ascii=False)}")

        app = next((a for a in src["apps"] if a.get("bundleIdentifier") == bundle_id), None)
        if app is None:
            sys.exit(f"No app with bundleIdentifier '{bundle_id}' in source.json; add its entry first")
        app["appPermissions"] = permissions

        tag = f"{bundle_id}-{version}-{build}"
        title = f"{app['name']} {version} ({build})"
        if args.dry_run:
            repo = os.environ.get("GITHUB_REPOSITORY", "OWNER/REPO")
            urls = {p.name: f"https://github.com/{repo}/releases/download/{tag}/{p.name}"
                    for p in assets.values()}
            print(f"[dry-run] would upload {len(assets)} file(s) to release {tag}")
        else:
            urls = upload_release(tag, title, args.notes, assets.values())

        missing = [p.name for p in assets.values() if p.name not in urls]
        if missing:
            sys.exit(f"Release is missing uploaded assets: {missing}")

        entry = {
            "version": version,
            "buildVersion": build,
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "localizedDescription": args.notes or None,
            "downloadURL": urls[assets["manifest"].name],
            # AltStore shows this as the download size; the largest variant is the upper bound.
            "size": max(p.stat().st_size for p in ipas),
            "minOSVersion": min_os,
            "assetURLs": {key: urls[path.name] for key, path in assets.items()},
        }
        entry = {k: v for k, v in entry.items() if v is not None}

    others = [v for v in app.get("versions", [])
              if (v.get("version"), v.get("buildVersion")) != (version, build)]
    app["versions"] = [entry] + others

    errors, _ = validate(src)
    if errors:
        print("\n".join(f"error: {e}" for e in errors))
        sys.exit("source.json would be invalid after publishing; not saved")
    save_source(src)
    print(f"source.json updated: {title}")

    if gh_output := os.environ.get("GITHUB_OUTPUT"):
        with open(gh_output, "a", encoding="utf-8") as f:
            f.write(f"summary={title}\ntag={tag}\n")


# ---------------------------------------------------------------- CLI

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("configure", help="set the GitHub Pages base URL in source.json")
    p.add_argument("--owner", help="GitHub user or organization")
    p.add_argument("--repo", help="repository name")
    p.add_argument("--domain", help="custom Pages domain; replaces --owner/--repo")
    p.set_defaults(func=cmd_configure)

    p = sub.add_parser("validate", help="check source.json")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("publish", help="publish a notarized ADP")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--adp-id", help="Alternative Distribution Package ID from App Store Connect")
    src.add_argument("--adp-zip", help="use an already downloaded ADP zip instead of the API")
    p.add_argument("--notes", default="", help="release notes shown in AltStore")
    p.add_argument("--timeout", type=int, default=1800, help="seconds to wait for ADP processing")
    p.add_argument("--interval", type=int, default=30, help="seconds between status checks")
    p.add_argument("--dry-run", action="store_true", help="skip the GitHub Release upload")
    p.set_defaults(func=cmd_publish)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
