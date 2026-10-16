#!/usr/bin/env python3
"""Prepare and publish immutable, incremental efootxi resource releases.

Preparation reads the supplied ZIP directly. It never relies on the old source
inventory. Publishing is an explicit second command and updates channel.json last.
See docs/resource-updates.md for the protocol and examples.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import quote, urlparse

CHUNK = 1024 * 1024
REPO = "maxlzyver/efootxi"
PACKAGE = "com.efootball.revival.local"
GAME_VERSION = 311010101
MAX_ASSET = 2 * 1024**3 - 1
HASH = re.compile(r"[0-9a-f]{64}")
TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
ASSET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}")
ALLOWED_REPOS = {"maxlzyver/efootxi", "maxlzyver/73efootball"}


def sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(CHUNK), b""):
            result.update(block)
    return result.hexdigest()


def write_json(path: Path, value: object) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def valid_path(path: str) -> None:
    if (not isinstance(path, str) or "\\" in path or ":" in path or "\0" in path
            or path.split("/", 1)[0] not in ("assetpacks", "Download")
            or len(path.split("/")) < 2
            or any(part in ("", ".", "..") for part in path.split("/"))):
        raise ValueError(f"Unsafe resource path: {path!r}")


def valid_url(url: str) -> None:
    parsed = urlparse(url)
    parts = parsed.path.split("/")
    if (parsed.scheme != "https" or parsed.hostname != "github.com"
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.query or parsed.fragment
            or len(parts) != 7 or "/".join(parts[1:3]) not in ALLOWED_REPOS
            or parts[3:5] != ["releases", "download"]
            or not TAG.fullmatch(parts[5]) or not ASSET.fullmatch(parts[6])
            or ".." in parts[5] or ".." in parts[6]):
        raise ValueError(f"Expected an ASCII HTTPS Release asset URL in the efootxi repository: {url!r}")


def release_base(repo: str, tag: str) -> str:
    if repo not in ALLOWED_REPOS or not TAG.fullmatch(tag) or ".." in tag:
        raise ValueError("Invalid repository or release tag")
    return f"https://github.com/{repo}/releases/download/{tag}/"


def validate_entry(entry: dict) -> None:
    valid_path(entry["path"])
    if type(entry["bytes"]) is not int or entry["bytes"] < 0 or not HASH.fullmatch(entry["sha256"]):
        raise ValueError("Invalid resource entry size or SHA-256")


def index_previous(manifest: dict | None, base_url: str | None) -> tuple[list[dict], dict]:
    if manifest is None:
        return [], {}
    if manifest["schemaVersion"] != 1 or manifest["packageName"] != PACKAGE:
        raise ValueError("Previous manifest schema or package mismatch")
    packs, entries, names = [], {}, set()
    for original in manifest["files"]:
        pack = dict(original)
        if pack["name"] in names:
            raise ValueError("Duplicate package name in previous manifest")
        names.add(pack["name"])
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\.zip", pack["name"]):
            raise ValueError("Invalid previous resource package name")
        if (type(pack["bytes"]) is not int or not 0 < pack["bytes"] <= MAX_ASSET
                or not HASH.fullmatch(pack["sha256"])):
            raise ValueError("Invalid previous resource package metadata")
        pack["url"] = pack.get("url") or (base_url or "") + quote(pack["name"])
        valid_url(pack["url"])
        for entry in pack["entries"]:
            validate_entry(entry)
            if entry["path"] in entries:
                raise ValueError("Duplicate resource path in previous manifest")
            entries[entry["path"]] = (entry, pack)
        packs.append(pack)
    return packs, entries


def scan_archive(path: Path) -> list[dict]:
    entries, seen, folded = [], set(), set()
    with zipfile.ZipFile(path) as archive:
        for item in archive.infolist():
            if item.is_dir():
                continue
            valid_path(item.filename)
            if item.filename in seen or item.filename.casefold() in folded:
                raise ValueError(f"Duplicate or case-colliding source path: {item.filename}")
            if item.flag_bits & 1 or ((item.external_attr >> 16) & 0o170000) == 0o120000:
                raise ValueError(f"Encrypted or symbolic-link ZIP member: {item.filename}")
            seen.add(item.filename)
            folded.add(item.filename.casefold())
            digest, size = hashlib.sha256(), 0
            with archive.open(item) as source:
                for block in iter(lambda: source.read(CHUNK), b""):
                    digest.update(block)
                    size += len(block)
            if size != item.file_size:
                raise ValueError(f"Source size mismatch: {item.filename}")
            entries.append({"path": item.filename, "bytes": size, "sha256": digest.hexdigest()})
    if not entries:
        raise ValueError("Source ZIP contains no resource files")
    return sorted(entries, key=lambda entry: entry["path"])


def create_pack(source: zipfile.ZipFile, entry: dict, output: Path, base: str) -> dict:
    if entry["bytes"] + 98 + 2 * len(entry["path"].encode("utf-8")) > MAX_ASSET:
        raise ValueError(f"Resource file is too large for a single Release asset: {entry['path']}")
    identity = hashlib.sha256((entry["path"] + "\0" + entry["sha256"]).encode()).hexdigest()
    name = "resource-" + identity + ".zip"
    path = output / name
    temporary = path.with_suffix(".zip.tmp")
    info = zipfile.ZipInfo(entry["path"], (2024, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.file_size = entry["bytes"]
    digest = hashlib.sha256()
    with zipfile.ZipFile(temporary, "w", allowZip64=False) as target:
        with source.open(entry["path"]) as reader, target.open(info, "w") as writer:
            for block in iter(lambda: reader.read(CHUNK), b""):
                digest.update(block)
                writer.write(block)
    if digest.hexdigest() != entry["sha256"] or not 0 < temporary.stat().st_size <= MAX_ASSET:
        raise ValueError(f"Input changed or resource exceeds GitHub asset size: {entry['path']}")
    with zipfile.ZipFile(temporary) as check:
        if check.testzip() is not None:
            raise ValueError(f"Built ZIP CRC check failed: {name}")
    temporary.replace(path)
    return {"name": name, "bytes": path.stat().st_size, "sha256": sha256(path),
            "url": base + name, "expandedBytes": entry["bytes"], "entries": [entry]}


def validate_apk(apk: dict) -> None:
    required = ("buildRevision", "versionCode", "versionName", "url", "sha256", "bytes", "signerCertificateSha256", "minSdk", "abi")
    if any(key not in apk for key in required):
        raise ValueError("APK metadata is incomplete")
    valid_url(apk["url"])
    if (type(apk["buildRevision"]) is not int or apk["buildRevision"] < 1
            or type(apk["versionCode"]) is not int or apk["versionCode"] <= 0
            or type(apk["bytes"]) is not int or not 0 < apk["bytes"] <= MAX_ASSET
            or not HASH.fullmatch(apk["sha256"]) or not HASH.fullmatch(apk["signerCertificateSha256"])
            or type(apk["minSdk"]) is not int or apk["minSdk"] < 31
            or apk["abi"] != "arm64-v8a"):
        raise ValueError("Invalid APK metadata")


def prepare(args: argparse.Namespace) -> None:
    base = release_base(args.repo, args.tag)
    if args.tag == "updates" or args.revision < 1:
        raise ValueError("Use an immutable resource tag and positive channel revision")
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("Output directory must be empty; use a new directory for each release")
    output.mkdir(parents=True, exist_ok=True)
    source = args.source.resolve(strict=True) if args.source else None
    before = source.stat() if source else None
    apk = read_json(args.apk_json)
    if "apk" in apk:
        apk = apk["apk"]
    validate_apk(apk)
    previous_path = args.prepare_existing or args.previous_manifest
    previous = read_json(previous_path) if previous_path else None
    old_packs, old_entries = index_previous(previous, args.previous_base_url)
    entries = scan_archive(source) if source else sorted(
        [dict(entry) for entry, _ in old_entries.values()], key=lambda entry: entry["path"])
    if not entries:
        raise ValueError("Existing manifest contains no entries")
    unchanged, changed = {}, []
    for entry in entries:
        old = old_entries.get(entry["path"])
        if old and (old[0]["bytes"], old[0]["sha256"]) == (entry["bytes"], entry["sha256"]):
            unchanged[entry["path"]] = entry
        else:
            changed.append(entry)
    removed = sorted(set(old_entries) - {entry["path"] for entry in entries})
    if (previous and args.resource_version == previous["resourceVersion"]
            and (changed or removed)):
        raise ValueError("Changed resources require a new resourceVersion")
    packs = []
    for old in old_packs:
        retained = [unchanged[item["path"]] for item in old["entries"] if item["path"] in unchanged]
        if retained:
            packs.append({**old, "entries": retained, "expandedBytes": sum(item["bytes"] for item in retained),
                          "allowSubset": bool(old.get("allowSubset", False) or len(retained) != len(old["entries"]))})
    created = []
    if source:
        with zipfile.ZipFile(source) as archive:
            for entry in changed:
                pack = create_pack(archive, entry, output, base)
                packs.append(pack)
                created.append({key: pack[key] for key in ("name", "bytes", "sha256")})
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("Source ZIP changed during preparation")
    if len(created) + 3 > 1000:
        raise ValueError("Release would exceed 1,000 assets; split the resource update")
    manifest = {"schemaVersion": 1, "packageName": PACKAGE, "versionCode": args.game_version_code,
                "resourceVersion": args.resource_version or args.tag, "files": packs}
    manifest_path = output / "manifest.json"
    write_json(manifest_path, manifest)
    channel = {"schemaVersion": 1, "revision": args.revision, "packageName": PACKAGE,
               "resources": {"resourceVersion": manifest["resourceVersion"], "manifestUrl": base + "manifest.json",
                             "sha256": sha256(manifest_path), "bytes": manifest_path.stat().st_size,
                             "baseUrl": base, "gameVersionCode": args.game_version_code}, "apk": apk}
    write_json(output / "channel.json", channel)
    created.append({"name": "manifest.json", "bytes": manifest_path.stat().st_size, "sha256": sha256(manifest_path)})
    report = {"schemaVersion": 1, "repo": args.repo, "tag": args.tag, "channelRevision": args.revision,
              "source": str(source) if source else str(previous_path),
              "sourceBytes": before.st_size if before else None, "entryCount": len(entries),
              "reusedEntryCount": len(unchanged), "newOrChangedEntryCount": len(changed),
              "removedPaths": removed,
              "uploadBytes": sum(item["bytes"] for item in created), "assets": created,
              "channelSha256": sha256(output / "channel.json")}
    write_json(output / "publish-plan.json", report)
    (output / "release-notes.md").write_text(
        f"efootxi 资源 {manifest['resourceVersion']}\n\n"
        f"资源文件 {len(entries)} 个；新增或变更 {len(changed)} 个，复用 {len(unchanged)} 个。\n"
        "本版本由更新清单引用。请保留仍被当前清单引用的历史资源附件。\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("repo", "tag", "entryCount", "reusedEntryCount",
          "newOrChangedEntryCount", "uploadBytes")}, ensure_ascii=False, indent=2))


def gh(executable: str, *arguments: str, json_result: bool = False):
    result = subprocess.run([executable, *arguments], check=False, stdout=subprocess.PIPE,
                            encoding="utf-8", stderr=subprocess.PIPE)
    if result.returncode:
        detail = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", result.stderr).strip()
        detail = re.sub(r"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]+", "[redacted]", detail)
        raise RuntimeError(f"GitHub CLI {arguments[0]} failed (exit {result.returncode}): {detail[:1800]}")
    return json.loads(result.stdout) if json_result else result.stdout


def list_releases(executable: str, repo: str) -> list[dict]:
    pages = gh(executable, "api", "--paginate", "--slurp", f"repos/{repo}/releases?per_page=100", json_result=True)
    return [release for page in pages for release in page]


def channel_asset(release: dict) -> dict | None:
    return next((asset for asset in release["assets"] if asset["name"] == "channel.json"), None)


def asset_fingerprint(asset: dict | None) -> tuple | None:
    if asset is None:
        return None
    return asset.get("id"), asset.get("digest"), asset.get("size")


def verify_remote_reference(executable: str, url: str, size: int, digest: str, cache: dict) -> None:
    valid_url(url)
    owner, repository, _, _, tag, name = urlparse(url).path.strip("/").split("/")
    endpoint = f"repos/{owner}/{repository}/releases/tags/{tag}"
    if endpoint not in cache:
        cache[endpoint] = gh(executable, "api", endpoint, json_result=True)
    release = cache[endpoint]
    if release["draft"]:
        raise ValueError(f"Referenced asset is not public yet: {url}")
    found = next((asset for asset in release["assets"] if asset["name"] == name), None)
    if (not found or found.get("state") != "uploaded" or found.get("size") != size
            or found.get("digest") != "sha256:" + digest):
        raise ValueError(f"Referenced asset is absent or has a different GitHub digest: {url}")


def publish(args: argparse.Namespace) -> None:
    directory = args.directory.resolve(strict=True)
    plan = read_json(directory / "publish-plan.json")
    channel = read_json(directory / "channel.json")
    if sha256(directory / "channel.json") != plan["channelSha256"]:
        raise ValueError("Prepared channel changed; prepare a new update")
    repo, tag = plan["repo"], plan["tag"]
    release_base(repo, tag)
    for asset in plan["assets"]:
        if Path(asset["name"]).name != asset["name"]:
            raise ValueError("Invalid asset filename in publication plan")
        path = directory / asset["name"]
        if path.stat().st_size != asset["bytes"] or sha256(path) != asset["sha256"]:
            raise ValueError(f"Prepared asset changed: {path.name}")
    # Reading current channel through the release API also proves repository access.
    control = gh(args.gh, "api", f"repos/{repo}/releases/tags/updates", json_result=True)
    existing = channel_asset(control)
    initial_channel_fingerprint = asset_fingerprint(existing)
    if existing:
        with tempfile.TemporaryDirectory(prefix="efootxi-channel-") as temporary:
            gh(args.gh, "release", "download", "updates", "--repo", repo, "--pattern", "channel.json", "--dir", temporary)
            old_path = Path(temporary) / "channel.json"
            if (existing.get("size") != old_path.stat().st_size
                    or existing.get("digest") != "sha256:" + sha256(old_path)):
                raise ValueError("Published channel changed during preparation; rerun against the current channel")
            old = read_json(old_path)
        if channel["revision"] <= old["revision"]:
            raise ValueError("Channel revision must be greater than the published revision")
        if channel["apk"]["versionCode"] < old["apk"]["versionCode"]:
            raise ValueError("Channel would downgrade the APK")
        if channel["apk"]["buildRevision"] < old["apk"]["buildRevision"]:
            raise ValueError("Channel would downgrade the APK build revision")
        if (channel["apk"]["buildRevision"] == old["apk"]["buildRevision"]
                and channel["apk"]["sha256"] != old["apk"]["sha256"]):
            raise ValueError("Changed APK requires a higher buildRevision")
        if channel["apk"]["signerCertificateSha256"] != old["apk"]["signerCertificateSha256"]:
            raise ValueError("APK signing certificate changed; existing installs could not update")
    releases = list_releases(args.gh, repo)
    if any(release["tag_name"] == tag for release in releases):
        raise ValueError("Immutable release tag already exists; choose a new tag")
    manifest = read_json(directory / "manifest.json")
    cache = {}
    verify_remote_reference(args.gh, channel["apk"]["url"], channel["apk"]["bytes"], channel["apk"]["sha256"], cache)
    new_assets = {asset["name"] for asset in plan["assets"]}
    for pack in manifest["files"]:
        if pack["name"] not in new_assets:
            verify_remote_reference(args.gh, pack["url"], pack["bytes"], pack["sha256"], cache)
    stage = "create resource draft"
    channel_replacement_started = False
    try:
        gh(args.gh, "release", "create", tag, "--repo", repo, "--draft", "--title", f"efootxi 资源 {tag}",
           "--notes-file", str(directory / "release-notes.md"))
        # GitHub's release-by-tag route may return 404 for a draft whose tag has
        # not been created yet. Authenticated release listing includes the draft.
        draft = next((release for release in list_releases(args.gh, repo) if release["tag_name"] == tag), None)
        if not draft or not draft.get("draft") or type(draft.get("id")) is not int:
            raise ValueError("New resource draft was not found in the authenticated release list")
        release_id = draft["id"]
        stage = "upload and verify resource assets"
        for asset in plan["assets"]:
            gh(args.gh, "release", "upload", tag, str(directory / asset["name"]), "--repo", repo)
        remote = gh(args.gh, "api", f"repos/{repo}/releases/{release_id}", json_result=True)
        assets = {asset["name"]: asset for asset in remote["assets"]}
        for local in plan["assets"]:
            observed = assets.get(local["name"], {})
            if observed.get("size") != local["bytes"] or observed.get("digest") != "sha256:" + local["sha256"]:
                raise ValueError(f"Uploaded asset digest differs: {local['name']}")
        # Resources become public before any client can see the new channel.
        stage = "publish verified resources"
        gh(args.gh, "release", "edit", tag, "--repo", repo, "--draft=false", "--latest=false")
        stage = "recheck current update channel"
        latest_control = gh(args.gh, "api", f"repos/{repo}/releases/tags/updates", json_result=True)
        if asset_fingerprint(channel_asset(latest_control)) != initial_channel_fingerprint:
            raise ValueError("Another publication changed the channel; prepared resources are public but this channel was not submitted")
        stage = "replace update channel"
        channel_replacement_started = True
        gh(args.gh, "release", "upload", "updates", str(directory / "channel.json"), "--repo", repo, "--clobber")
        stage = "verify published update channel"
        updated = gh(args.gh, "api", f"repos/{repo}/releases/tags/updates", json_result=True)
        asset = next(asset for asset in updated["assets"] if asset["name"] == "channel.json")
        if asset.get("digest") != "sha256:" + plan["channelSha256"]:
            raise ValueError("Published channel digest differs")
    except Exception:
        print(f"Publication stopped during: {stage}. Resource release: {repo} / {tag}.", file=sys.stderr)
        print("The channel replacement has started; inspect updates/channel.json before retrying."
              if channel_replacement_started else "The existing update channel was not changed.", file=sys.stderr)
        print("Inspect the draft/resource release; do not overwrite immutable resource assets.", file=sys.stderr)
        raise
    write_json(directory / "publication.json", {"repo": repo, "tag": tag, "revision": channel["revision"],
               "channelSha256": plan["channelSha256"], "verified": True})
    print(f"Published {repo}: {tag}; channel revision {channel['revision']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare", help="Build and verify locally; no network writes")
    source = prep.add_mutually_exclusive_group(required=True)
    source.add_argument("--source", type=Path, help="Complete desired resource ZIP, scanned directly")
    source.add_argument("--prepare-existing", type=Path, help="Reuse a verified manifest without rebuilding its resource ZIPs")
    prep.add_argument("--output", type=Path, required=True)
    prep.add_argument("--repo", default=REPO)
    prep.add_argument("--tag", required=True)
    prep.add_argument("--resource-version")
    prep.add_argument("--revision", type=int, required=True)
    prep.add_argument("--game-version-code", type=int, default=GAME_VERSION)
    prep.add_argument("--previous-manifest", type=Path)
    prep.add_argument("--previous-base-url")
    prep.add_argument("--apk-json", type=Path, required=True, help="Previous channel.json or standalone APK metadata")
    prep.set_defaults(handler=prepare)
    pub = commands.add_parser("publish", help="Upload immutable release, verify digests, then advance channel")
    pub.add_argument("--directory", type=Path, required=True)
    pub.add_argument("--gh", default="gh")
    pub.set_defaults(handler=publish)
    args = parser.parse_args()
    try:
        args.handler(args)
    except (ValueError, OSError, RuntimeError, KeyError, zipfile.BadZipFile) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
