import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from pathlib import Path
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location("release", Path(__file__).parents[1] / "publisher/release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

PAGE = b'<html><head><meta name="chankay-shell" content="v1"></head><body><div data-shell-slot="header"></div><main></main><div data-shell-slot="footer"></div></body></html>'
SHA = "a" * 40


def read_artifact(stream, locale):
    return release.read_artifact(stream, locale, "demo", SHA)


def archive(files=None):
    source = io.BytesIO()
    with zipfile.ZipFile(source, "w") as bundle:
        for path, data in (files or {"index.html": PAGE, "main.js": b"console.log(1)"}).items():
            bundle.writestr(path, data)
    source.seek(0)
    return source


class Store:
    def __init__(self):
        self.objects = {}
        self.writes = []

    def get(self, key):
        return self.objects.get(key)

    def put(self, key, data, mime, etag=None):
        current = self.get(key)
        if (current.etag if current else None) != etag:
            return False
        self.writes.append(key)
        self.objects[key] = SimpleNamespace(data=data, mime=mime, etag=hashlib.md5(data).hexdigest())
        return True


class ReleaseTests(unittest.TestCase):
    def test_inventory_and_shell(self):
        files = read_artifact(archive(), "en")
        inventory = json.loads(files["release.json"][0])
        self.assertEqual(inventory["locale"], "en")
        self.assertEqual(inventory["files"][1], dict(path="main.js", size=14, contentType="application/javascript",
                                                    sha256=hashlib.sha256(b"console.log(1)").hexdigest()))
        self.assertEqual(read_artifact(archive({k:v[0] for k,v in files.items()}),"en"), files)

    def test_rejects_invalid_paths_and_missing_slots(self):
        for path in ("../bad.js", "/abs.js", "a//x.js", "run.py", ".env", "secrets.json", "Credentials.json"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                read_artifact(archive({"index.html": PAGE, path: b""}), "en")
        with self.assertRaises(ValueError):
            read_artifact(archive({"index.html": b"<html></html>"}), "en")

    def test_html_references_require_the_right_release_and_an_included_file(self):
        prefix = "/demo/_r/" + SHA + "/"
        for url in (prefix + "main.js", "https://demos.chankay.com" + prefix + "main.js",
                    "/main.js", prefix + "missing.js", "/demo/_r/" + "b" * 40 + "/main.js"):
            with self.subTest(url=url):
                page = PAGE.replace(b"</body>", f'<script src="{url}"></script></body>'.encode())
                bundle = archive({"index.html": page, "main.js": b"valid"})
                if url.endswith(prefix + "main.js"):
                    read_artifact(bundle, "en")
                else:
                    with self.assertRaises(ValueError):
                        read_artifact(bundle, "en")

    def test_rejects_inventory_mismatch(self):
        with self.assertRaises(ValueError):
            read_artifact(archive({"index.html": PAGE, "release.json": b"{}"}), "en")

    def test_promotes_only_complete_immutable_release(self):
        store = Store()
        files = read_artifact(archive(), "en")
        release.publish(store, "demo", SHA, 3, files)
        self.assertEqual(store.writes[-2:], ["projects/demo/releases/" + SHA + "/release.json", "projects/demo/current.json"])
        count = len(store.writes)
        release.publish(store, "demo", SHA, 3, files)
        self.assertEqual(len(store.writes), count)
        changed = read_artifact(archive({"index.html": PAGE, "main.js": b"other"}), "en")
        with self.assertRaises(ValueError):
            release.publish(store, "demo", SHA, 4, changed)
        for stale in (2, 3):
            with self.assertRaises(ValueError):
                release.publish(store, "demo", "b" * 40, stale, files)
        self.assertEqual(json.loads(store.get("projects/demo/current.json").data), {"release": SHA, "run": 3})

    def test_legacy_inventory_preserves_exact_immutable_bytes(self):
        files = read_artifact(archive(), "en")
        supplied = json.loads(files["release.json"][0])
        supplied["files"].reverse()
        original = json.dumps(supplied).encode()
        contents = {key: value[0] for key, value in files.items()}
        contents["release.json"] = original
        self.assertEqual(read_artifact(archive(contents), "en")["release.json"][0], original)

    def test_symlink_and_oversized_file_are_rejected(self):
        link = zipfile.ZipInfo("link.js")
        link.external_attr = 0o120777 << 16
        with self.assertRaises(ValueError):
            read_artifact(archive({"index.html": PAGE, link: b"other"}), "en")
        with self.assertRaises(ValueError):
            read_artifact(archive({"index.html": PAGE, "large.js": b"X" * (release.MAX_FILE + 1)}), "en")

    def test_artifact_provenance_and_digest(self):
        data = archive().getvalue()
        env = dict(GITHUB_REPOSITORY="owner/demo", ARTIFACT_ID="99", GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT="2",
                   GITHUB_REPOSITORY_ID="456", GITHUB_SHA=SHA)
        origin = dict(id=123, head_repository_id=456, repository_id=456, head_sha=SHA)
        meta = dict(id=99, name="demo-dist-123-1", expired=False, workflow_run=origin, size_in_bytes=len(data),
                    digest="sha256:" + hashlib.sha256(data).hexdigest())
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / "artifact.zip"
            for changes in ({}, {"workflow_run": origin | {"id": 321}}, {"workflow_run": origin | {"head_sha":"c"*40}},
                            {"workflow_run": origin | {"head_repository_id": 111}}, {"expired":True},
                            {"name":"demo-dist-123-3"}, {"digest":"sha256:wrong"}):
                command = [SimpleNamespace(returncode=0, stdout=json.dumps(meta | changes).encode()),
                           SimpleNamespace(returncode=0, stdout=data)]
                with self.subTest(changes=changes), patch.object(release.subprocess, "run", side_effect=command):
                    if changes:
                        with self.assertRaises(ValueError):
                            release.fetch_artifact(env, dest)
                    else:
                        release.fetch_artifact(env, dest)
                        self.assertEqual(dest.read_bytes(), data)

    def test_partial_or_corrupted_readback_cannot_promote(self):
        store = Store()
        def corrupt(key, data, mime, etag=None):
            store.objects[key] = SimpleNamespace(data=b"damaged", mime=mime, etag="bad")
            return True
        store.put = corrupt
        with self.assertRaises(ValueError):
            release.publish(store, "demo", SHA, 1, read_artifact(archive(),"en"))
        self.assertNotIn("projects/demo/current.json", store.objects)

    def test_concurrent_promotion_preserves_winner(self):
        store = Store()
        put = store.put
        def race(key, data, mime, etag=None):
            if key.endswith("current.json"):
                put(key, json.dumps({"release": "b" * 40, "run": 5}).encode(), mime, etag)
            return put(key, data, mime, etag)
        store.put = race
        with self.assertRaises(ValueError):
            release.publish(store, "demo", SHA, 3, read_artifact(archive(),"en"))
        self.assertEqual(json.loads(store.get("projects/demo/current.json").data)["run"], 5)

    def test_environment_protects_slug_and_branch(self):
        env = dict(GITHUB_REPOSITORY="owner/demo", GITHUB_REF="refs/heads/main", DEFAULT_BRANCH="main",
                   GITHUB_EVENT_NAME="push", GITHUB_SHA=SHA, GITHUB_RUN_NUMBER="1", DEMO_SLUG="demo")
        self.assertEqual(release.settings(env), ("demo", SHA, 1))
        for overrides in ({"DEMO_SLUG":"other"}, {"GITHUB_REF":"refs/heads/feature"}, {"GITHUB_EVENT_NAME":"pull_request"},
                          {"ALLOWED_SLUG":"api", "DEMO_SLUG":"api"}, {"GITHUB_SHA":"main"}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                release.settings(env | overrides)
        self.assertEqual(release.settings(env | dict(GITHUB_REF="refs/heads/pilot", ALLOWED_BRANCH="pilot",
                                                     DEMO_SLUG="alternate", ALLOWED_SLUG="alternate")),
                         ("alternate", SHA, 1))
