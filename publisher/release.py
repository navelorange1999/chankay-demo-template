"""Verify a static GitHub Actions artifact and publish it to R2 using conditional writes."""

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urljoin, urlsplit

BUCKET = "chankay-demo-assets"
MAX_FILE = 8 * 1024 * 1024
MAX_RELEASE = 24 * 1024 * 1024
MAX_ARCHIVE = MAX_RELEASE + 1024 * 1024
MAX_FILES = 256
MAX_INVENTORY = 64 * 1024
MIME = {
    "html": "text/html", "js": "application/javascript", "mjs": "application/javascript",
    "css": "text/css", "json": "application/json", "webmanifest": "application/manifest+json",
    "svg": "image/svg+xml", "ico": "image/x-icon", "png": "image/png", "webp": "image/webp",
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "avif": "image/avif",
    "woff": "font/woff", "woff2": "font/woff2", "wasm": "application/wasm",
}


class ReleaseError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ReleaseError(message)


class Shell(HTMLParser):
    def __init__(self):
        super().__init__()
        self.section = None
        self.markers = []
        self.assets = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("head", "body"):
            self.section = tag
        if tag == "meta" and attrs.get("name") == "chankay-shell":
            self.markers.append((self.section, attrs.get("content")))
        if "data-shell-slot" in attrs:
            self.markers.append((self.section, attrs["data-shell-slot"]))

        if tag == "base" and "href" in attrs:
            raise ReleaseError("HTML base overrides the gateway release URLs")
        if tag == "link" and set((attrs.get("rel") or "").lower().split()) & {
            "stylesheet", "icon", "apple-touch-icon", "manifest", "modulepreload", "preload", "prefetch"
        }:
            self.assets.append(attrs.get("href"))
        if tag in {"img", "script", "source", "audio", "video", "track"} and "src" in attrs:
            self.assets.append(attrs["src"])
        if tag == "video" and "poster" in attrs:
            self.assets.append(attrs["poster"])
        if tag in {"img", "source"} and "srcset" in attrs:
            self.assets.extend((candidate.strip().split() or [None])[0] for candidate in attrs["srcset"].split(","))

    def handle_endtag(self, tag):
        if tag in ("head", "body"):
            self.section = None


def valid_path(path):
    segments = path.split("/")
    return len(path) <= 512 and all(
        re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", s)
        and ".." not in s
        and not s.lower().startswith(("credentials", "secrets."))
        and s.lower() not in ("id_rsa", "id_ed25519", "id_ecdsa")
        and not s.lower().endswith((".key", ".pem", ".pfx", ".p12"))
        for s in segments
    )


GATEWAY_ORIGINS = {"demos.chankay.com", "chankay-demos.navelorange1999.workers.dev"}


def check_asset(url, base, prefix, files):
    require(isinstance(url, str) and 0 < len(url) <= 2048 and url.strip() == url
            and all(ord(char) >= 32 for char in url) and chr(92) not in url, "Invalid HTML asset URL")
    resolved = urlsplit(urljoin(base, url))
    require(resolved.scheme == "https" and resolved.netloc in GATEWAY_ORIGINS
            and resolved.path.startswith(prefix), "Asset URL must use this immutable demo release")
    path = resolved.path[len(prefix):]
    require(path in files and files[path][1] != "text/html", "Referenced asset is missing from this release")


def read_artifact(stream, locale, slug, release):
    require(locale in ("en", "zh-CN"), "Unsupported locale")
    files = {}
    entries = []
    references = []
    total = 0
    supplied = None
    with zipfile.ZipFile(stream) as bundle:
        members = bundle.infolist()
        require(0 < len(members) <= MAX_FILES + 1, "Invalid artifact file count")
        for info in sorted(members, key=lambda item: item.filename):
            path = info.filename
            require(valid_path(path) and path == info.orig_filename, "Unsafe artifact path")
            mode = stat.S_IFMT(info.external_attr >> 16)
            require(mode in (0, stat.S_IFREG) and not info.is_dir(), "Only regular files are allowed")
            require(not info.flag_bits & 1 and info.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED),
                    "Unsupported archive member")
            if path == "release.json":
                require(supplied is None and info.file_size <= MAX_INVENTORY, "Invalid supplied inventory")
                supplied = bundle.read(info)
                continue
            mime = MIME.get(path.rsplit(".", 1)[-1])
            require("." in path and mime and path not in files and len(files) < MAX_FILES, "Unsupported artifact entry")
            total += info.file_size
            require(info.file_size <= MAX_FILE and total <= MAX_RELEASE, "Build exceeds release limits")
            data = bundle.read(info)
            require(len(data) == info.file_size, "Invalid artifact size")
            if mime == "text/html":
                parser = Shell()
                parser.feed(data.decode("utf-8"))
                parser.close()
                require(parser.markers == [("head", "v1"), ("body", "header"), ("body", "footer")],
                        "HTML requires a v1 shell marker and header/footer slots in order")
                page_url = "https://demos.chankay.com/" + slug + "/" + path
                references.extend((url, page_url) for url in parser.assets)
            if mime == "application/manifest+json":
                manifest = json.loads(data)
                require(isinstance(manifest, dict) and isinstance(manifest.get("icons", []), list), "Invalid web manifest")
                manifest_url = f"https://demos.chankay.com/{slug}/_r/{release}/{path}"
                for icon in manifest.get("icons", []):
                    require(isinstance(icon, dict), "Invalid web manifest icon")
                    references.append((icon.get("src"), manifest_url))
            files[path] = (data, mime)
            entries.append(dict(path=path, size=len(data), sha256=hashlib.sha256(data).hexdigest(), contentType=mime))
    require("index.html" in files, "Missing index.html")
    prefix = f"/{slug}/_r/{release}/"
    for url, base in references:
        check_asset(url, base, prefix, files)
    inventory = {"locale": locale, "files": entries}
    encoded = (json.dumps(inventory, indent=2) + "\n").encode()
    if supplied is not None:
        # Existing releases may use a different ordering or whitespace. Preserve
        # their exact immutable bytes after verifying every independently derived entry.
        caller = json.loads(supplied)
        require(isinstance(caller, dict) and set(caller) == {"locale", "files"}
                and caller["locale"] == locale and isinstance(caller["files"], list)
                and all(isinstance(item, dict) and isinstance(item.get("path"), str)
                        and type(item.get("size")) is int for item in caller["files"]),
                "Invalid supplied inventory")
        require(sorted(caller["files"], key=lambda item: item["path"]) == entries,
                "Caller inventory differs from independently verified artifact")
        encoded = supplied
    require(len(encoded) <= MAX_INVENTORY, "Inventory exceeds limit")
    files["release.json"] = (encoded, "application/json")
    return files


def settings(env):
    name = env["GITHUB_REPOSITORY"].split("/")[-1]
    slug = env.get("DEMO_SLUG") or name
    allowed = env.get("ALLOWED_SLUG") or name
    branch = env.get("ALLOWED_BRANCH") or env["DEFAULT_BRANCH"]
    require(slug == allowed and slug != "api" and len(slug) <= 63
            and re.fullmatch("[a-z0-9]+(?:-[a-z0-9]+)*", slug), "Unauthorized project slug")
    require(branch and env["GITHUB_REF"] == "refs/heads/" + branch
            and env["GITHUB_EVENT_NAME"] in {"push", "workflow_dispatch"}, "Unauthorized release ref or event")
    sha = env["GITHUB_SHA"]
    require(re.fullmatch("[0-9a-f]{40}", sha), "Invalid commit SHA")
    return slug, sha


class Object(NamedTuple):
    data: bytes
    mime: str
    etag: str


class R2:
    def __init__(self, endpoint):
        require(re.fullmatch(r"https://[a-f0-9]{32}(?:\.(?:eu|fedramp))?\.r2\.cloudflarestorage\.com/?", endpoint),
                "Invalid R2 account endpoint")
        require(os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"),
                "Configure R2 access in the release environment")
        self.endpoint = endpoint

    def call(self, args, expected):
        command = ["aws", "--no-cli-pager", "--region", "auto", "--endpoint-url", self.endpoint, "s3api", *args]
        reply = subprocess.run(command, capture_output=True, timeout=90)
        if reply.returncode:
            code = re.search(rb"An error occurred \(([A-Za-z0-9]+)\)", reply.stderr)
            if code and code[1].decode() in expected:
                return None
            raise ReleaseError("R2 request failed; check access, endpoint and AWS CLI conditional write support")
        return json.loads(reply.stdout or b"{}")

    def get(self, key):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "object"
            result = self.call(["get-object", "--bucket", BUCKET, "--key", key, str(path)], {"NoSuchKey", "404"})
            if result is None:
                return None
            require(path.stat().st_size <= MAX_FILE, "Unexpected remote object size")
            return Object(path.read_bytes(), result.get("ContentType"), result["ETag"])

    def put(self, key, data, mime, etag=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "object"
            path.write_bytes(data)
            condition = ["--if-match", etag] if etag is not None else ["--if-none-match", "*"]
            result = self.call(["put-object", "--bucket", BUCKET, "--key", key, "--body", str(path),
                                "--content-type", mime, *condition], {"PreconditionFailed", "412", "ConditionalRequestConflict"})
            return result is not None


def is_descendant(repo, base, head):
    require(re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repo)
            and repo.split("/", 1)[1] not in {".", ".."}, "Invalid repository")
    require(all(re.fullmatch("[0-9a-f]{40}", sha) for sha in (base, head)), "Invalid comparison commit SHA")
    endpoint = f"repos/{repo}/compare/{base}...{head}"
    reply = subprocess.run(["gh", "api", "--hostname", "github.com", "--method", "GET", endpoint,
                            "-f", "per_page=1", "--jq", ".status"], capture_output=True, timeout=30)
    require(reply.returncode == 0, "GitHub commit comparison failed")
    return reply.stdout.strip() == b"ahead"


def pointer_state(store, key, release, compare_commits):
    current = store.get(key)
    if current is None:
        return None, False
    pointer = json.loads(current.data)
    active = pointer.get("release") if isinstance(pointer, dict) else None
    require(isinstance(active, str) and re.fullmatch("[a-f0-9]{40}", active), "Invalid current pointer")
    if active == release:
        return current.etag, True
    require(compare_commits(active, release), "Release commit does not descend from the active commit")
    return current.etag, False


def publish(store, slug, release, files, compare_commits):
    root = "projects/" + slug + "/"
    pointer = root + "current.json"
    pointer_state(store, pointer, release, compare_commits)
    for name, (data, mime) in files.items():
        key = root + "releases/" + release + "/" + name
        store.put(key, data, mime)
        stored = store.get(key)
        require(stored and stored.data == data and stored.mime == mime, "Immutable object read-back mismatch")
    for _ in range(4):
        etag, promoted = pointer_state(store, pointer, release, compare_commits)
        if promoted:
            return
        payload = json.dumps({"release": release}).encode()
        if store.put(pointer, payload, "application/json", etag):
            _, promoted = pointer_state(store, pointer, release, compare_commits)
            require(promoted, "Pointer read-back mismatch")
            return
    raise ReleaseError("Concurrent pointer update, retry this run")


def fetch_artifact(env, destination):
    # Run on a fresh runner. Only GitHub read access is available in this step.
    repo = env["GITHUB_REPOSITORY"]
    artifact = env["ARTIFACT_ID"]
    require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
            and re.fullmatch("[1-9][0-9]{0,19}", artifact), "Invalid repository or artifact ID")
    endpoint = f"repos/{repo}/actions/artifacts/{artifact}"
    def get(route):
        proc = subprocess.run(["gh", "api", route], capture_output=True, timeout=90)
        require(proc.returncode == 0, "GitHub artifact request failed")
        return proc.stdout
    meta = json.loads(get(endpoint))
    origin = meta.get("workflow_run", {})
    attempt = re.fullmatch(r"demo-dist-([1-9][0-9]*)-([1-9][0-9]*)", meta.get("name", ""))
    require(attempt and attempt[1] == env["GITHUB_RUN_ID"] and int(attempt[2]) <= int(env["GITHUB_RUN_ATTEMPT"])
            and str(meta.get("id")) == artifact and meta.get("expired") is False
            and str(origin.get("id")) == env["GITHUB_RUN_ID"] and origin.get("head_sha") == env["GITHUB_SHA"]
            and str(origin.get("head_repository_id")) == env["GITHUB_REPOSITORY_ID"]
            and str(origin.get("repository_id")) == env["GITHUB_REPOSITORY_ID"], "Artifact provenance mismatch")
    require(0 < meta["size_in_bytes"] <= MAX_ARCHIVE, "Invalid artifact size")
    data = get(endpoint + "/zip")
    require(len(data) == meta["size_in_bytes"]
            and "sha256:" + hashlib.sha256(data).hexdigest() == meta.get("digest"), "Artifact archive digest mismatch")
    destination.write_bytes(data)


if __name__ == "__main__":
    import sys
    try:
        slug, sha = settings(os.environ)
        archive_path = Path(os.environ["RUNNER_TEMP"]) / "demo-release-artifact.zip"
        if sys.argv[1:] == ["fetch"]:
            fetch_artifact(os.environ, archive_path)
        elif sys.argv[1:] == ["publish"]:
            require(archive_path.stat().st_size <= MAX_ARCHIVE, "Invalid archive size")
            with archive_path.open("rb") as stream:
                files = read_artifact(stream, os.environ["DEMO_LOCALE"], slug, sha)
            repo = os.environ["GITHUB_REPOSITORY"]
            publish(R2(os.environ["R2_ENDPOINT_URL"]), slug, sha, files,
                    lambda base, head: is_descendant(repo, base, head))
            print(f"Verified {len(files) - 1} files and promoted {slug} at {sha}.")
        else:
            raise ReleaseError("Invalid release command")
    except (ValueError, RuntimeError, OSError, KeyError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
        # Never echo artifact-controlled text, subprocess output or credential values.
        sys.exit(str(error) if isinstance(error, ReleaseError) else "Invalid release inputs or service response")
