import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pack_upload as pu  # noqa: E402

GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "a",
    "GIT_AUTHOR_EMAIL": "a@example.com",
    "GIT_COMMITTER_NAME": "a",
    "GIT_COMMITTER_EMAIL": "a@example.com",
}


def git(*args, cwd=None, env=None):
    result = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-c", "init.defaultBranch=master", *args],
        cwd=cwd,
        env=env or GIT_ENV,
        capture_output=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {args} failed: {result.stderr.decode()}")
    return result.stdout.decode().strip()


def commit(repo, files, message, date="2026-11-01T10:00:00+08:00"):
    for name, content in files.items():
        path = os.path.join(repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    git("add", "-A", cwd=repo)
    env = {**GIT_ENV, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    git("commit", "-q", "-m", message, cwd=repo, env=env)
    return git("rev-parse", "HEAD", cwd=repo)


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pack-upload-test-")
        # registered first so it runs last, after a test has left the directory
        self.addCleanup(self._remove_tmp)

    def _remove_tmp(self):
        # git marks pack files read-only on Windows
        def onexc(func, path, _):
            os.chmod(path, 0o700)
            func(path)

        shutil.rmtree(self.tmp, onexc=onexc)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def make_origin(self):
        """A bare origin and a working clone of it with one pushed commit."""
        origin = self.path("origin.git")
        git("init", "-q", "--bare", origin)
        work = self.path("work")
        git("clone", "-q", origin, work)
        c = commit(work, {"a.txt": "1", "d/b.txt": "x"}, "c1")
        git("push", "-q", "origin", "HEAD:master", cwd=work)
        return origin, work, c


class VersionNameTest(TempDirTest):
    def test_utc_keeps_commit_order_across_time_zones(self):
        repo = self.path("r")
        git("init", "-q", repo)
        # the two consecutive NKAS master commits whose local-time names sort backwards
        first = commit(repo, {"a": "1"}, "a4d4c93", "2026-10-31T01:22:49+08:00")
        second = commit(repo, {"a": "2"}, "a72ed7f", "2026-10-30T22:48:45+00:00")
        self.assertEqual(pu.version_name(first, cwd=repo), "20261030172249")
        self.assertEqual(pu.version_name(second, cwd=repo), "20261030224845")
        self.assertLess(pu.version_name(first, cwd=repo), pu.version_name(second, cwd=repo))


class TipTest(TempDirTest):
    def test_tip_matches_head_until_a_newer_push(self):
        origin, work, c1 = self.make_origin()
        self.assertEqual(pu.remote_branch_tip("master", cwd=work), c1)

        other = self.path("other")
        git("clone", "-q", origin, other)
        c2 = commit(other, {"a.txt": "2"}, "c2")
        git("push", "-q", "origin", "HEAD:master", cwd=other)

        self.assertEqual(pu.head_commit(cwd=work), c1)
        self.assertEqual(pu.remote_branch_tip("master", cwd=work), c2)

    def test_missing_branch(self):
        _, work, _ = self.make_origin()
        self.assertIsNone(pu.remote_branch_tip("dev", cwd=work))

    def test_exact_ref_only(self):
        origin, work, c1 = self.make_origin()
        c2 = commit(work, {"a.txt": "nested"}, "nested")
        git("push", "-q", "origin", "HEAD:refs/heads/x/refs/heads/master", cwd=work)
        self.assertEqual(pu.remote_branch_tip("master", cwd=work), c1)
        self.assertNotEqual(c1, c2)


class SnapshotPackTest(TempDirTest):
    def test_pack_from_shallow_clone_is_self_contained(self):
        origin, work, _ = self.make_origin()
        c2 = commit(work, {"a.txt": "2", "d/c.txt": "y"}, "c2")
        git("push", "-q", "origin", "HEAD:master", cwd=work)

        shallow = self.path("shallow")
        git("clone", "-q", "--depth=1", "file://" + origin.replace("\\", "/"), shallow)
        pack = self.path("snapshot.pack")
        size = pu.build_snapshot_pack(pu.head_commit(cwd=shallow), pack, cwd=shallow)
        self.assertEqual(size, os.path.getsize(pack))

        # what the server does on import: --strict would also demand the parent commits
        fresh = self.path("fresh.git")
        git("init", "-q", "--bare", fresh)
        with open(pack, "rb") as f:
            subprocess.run(["git", "index-pack", "--fsck-objects", "--stdin"], cwd=fresh, stdin=f, check=True, capture_output=True)
        kinds = git("cat-file", "--batch-all-objects", "--batch-check=%(objecttype)", cwd=fresh).split()
        self.assertEqual(kinds.count("commit"), 1)
        git("rev-list", "--objects", "--no-walk", c2, cwd=fresh)
        self.assertEqual(git("show", f"{c2}:d/c.txt", cwd=fresh), "y")


class ReleaseNoteTest(TempDirTest):
    def write_event(self, event):
        path = self.path("event.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(event, f)
        return path

    def test_lists_pushed_commits(self):
        repo = self.path("r")
        git("init", "-q", repo)
        c = commit(repo, {"a": "1"}, "head subject")
        event = self.write_event({
            "after": c,
            "commits": [
                {"id": "1" * 40, "message": "修复友情点招募\n\ndetails"},
                {"id": "2" * 40, "message": ""},
                {"id": c, "message": "head subject"},
            ],
        })
        note = pu.build_release_note(c, repo, "push", event)
        self.assertEqual(note, f"- 修复友情点招募 (1111111)\n- head subject ({c[:7]})")

    def test_falls_back_to_the_commit(self):
        repo = self.path("r")
        git("init", "-q", repo)
        c = commit(repo, {"a": "1"}, "ZH: 更新 NIKKE 咨询对话 (#338)")
        event = self.write_event({"after": "f" * 40, "commits": [{"id": "1" * 40, "message": "other"}]})
        expected = f"- ZH: 更新 NIKKE 咨询对话 (#338) ({c[:7]})"
        self.assertEqual(pu.build_release_note(c, repo, "push", event), expected)
        self.assertEqual(pu.build_release_note(c, repo, "workflow_run", event), expected)


class MultipartTest(unittest.TestCase):
    def test_fields_then_file(self):
        body, content_type = pu.encode_multipart(
            {"key": "k/1", "Content-Disposition": 'attachment; filename="r-1.pack"'},
            "file",
            "r-1.pack",
            b"PACK\x00\x01binary",
        )
        msg = BytesParser(policy=HTTP).parsebytes(f"Content-Type: {content_type}\r\n\r\n".encode() + body)
        parts = list(msg.iter_parts())
        self.assertEqual([p.get_param("name", header="content-disposition") for p in parts], ["key", "Content-Disposition", "file"])
        self.assertEqual(parts[0].get_content(), "k/1")
        self.assertEqual(parts[2].get_filename(), "r-1.pack")
        self.assertEqual(parts[2].get_payload(decode=True), b"PACK\x00\x01binary")


# ---------------------------------------------------------------------------
# fake MirrorChyan
# ---------------------------------------------------------------------------


class FakeMirrorChyan:
    """Answers the upload endpoints; each scripted step pops its next answer."""

    def __init__(self):
        self.requests = []
        self.create_answers = []
        self.callback_status_key = "status-1"
        self.statuses = [pu.STATUS_PENDING, pu.STATUS_COMPLETED]
        self.uploaded = None
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, payload):
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self):
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def do_POST(self):
                body = self._body()
                fake.requests.append(("POST", self.path, dict(self.headers), body))
                if self.path == "/oss":
                    msg = BytesParser(policy=HTTP).parsebytes(
                        f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode() + body
                    )
                    fake.uploaded = {p.get_param("name", header="content-disposition"): p for p in msg.iter_parts()}
                    self.send_response(200)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/api/resources/NKAS/versions":
                    status, payload = fake.create_answers.pop(0) if fake.create_answers else (200, None)
                    if payload is None:
                        payload = {"code": 0, "msg": "success", "data": {
                            "host": f"http://127.0.0.1:{fake.port}/oss",
                            "name": "n", "signature": "s", "key": "NKAS/1/snapshot.pack",
                            "policy": "p", "access_key": "ak",
                        }}
                    self._reply(status, payload)
                    return
                if self.path == "/api/resources/NKAS/versions/callback":
                    self._reply(200, {"code": 0, "msg": "success", "data": {"status_key": fake.callback_status_key}})
                    return
                self._reply(404, {"code": 1, "msg": "not found"})

            def do_GET(self):
                fake.requests.append(("GET", self.path, dict(self.headers), b""))
                if self.path.startswith("/api/resources/NKAS/versions/status"):
                    self._reply(200, {"code": 0, "msg": "success", "data": {"status": fake.statuses.pop(0)}})
                    return
                self._reply(404, {"code": 1, "msg": "not found"})

            def do_PUT(self):
                body = self._body()
                fake.requests.append(("PUT", self.path, dict(self.headers), body))
                self._reply(200, {"code": 0, "msg": "success", "data": None})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.api_base = f"http://127.0.0.1:{self.port}/api"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def paths(self):
        return [(m, p.split("?")[0]) for m, p, _, _ in self.requests]


class FakeServerTest(TempDirTest):
    def setUp(self):
        super().setUp()
        # a system proxy must not see requests to the fake server
        urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))
        self.fake = FakeMirrorChyan()
        self.sleep = mock.patch.object(pu.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.fake.close)
        self.pack = self.path("snapshot.pack")
        with open(self.pack, "wb") as f:
            f.write(b"PACK-content")

    def uploader(self):
        return pu.Uploader(api_base=self.fake.api_base, rid="NKAS", token=" tok ")


class UploaderTest(FakeServerTest):
    def test_upload_flow(self):
        self.uploader().upload("20261113011648", "stable", self.pack, "NKAS-20261113011648.pack")
        self.assertEqual(self.fake.paths(), [
            ("POST", "/api/resources/NKAS/versions"),
            ("POST", "/oss"),
            ("POST", "/api/resources/NKAS/versions/callback"),
            ("GET", "/api/resources/NKAS/versions/status"),
            ("GET", "/api/resources/NKAS/versions/status"),
        ])
        _, _, headers, body = self.fake.requests[0]
        self.assertEqual(headers["Authorization"], "tok")
        self.assertEqual(urllib.parse.parse_qs(body.decode(), keep_blank_values=True), {
            "name": ["20261113011648"], "os": [""], "arch": [""], "channel": ["stable"],
            "filename": ["NKAS-20261113011648.pack"],
        })
        self.assertEqual(self.fake.uploaded["file"].get_payload(decode=True), b"PACK-content")
        self.assertEqual(self.fake.uploaded["key"].get_content(), "NKAS/1/snapshot.pack")
        callback = urllib.parse.parse_qs(self.fake.requests[2][3].decode(), keep_blank_values=True)
        self.assertEqual(callback["key"], ["NKAS/1/snapshot.pack"])
        self.assertNotIn("filename", callback)
        self.assertIn("key=status-1", self.fake.requests[3][1])

    def test_client_error_is_not_retried(self):
        self.fake.create_answers = [(409, {"code": 1, "msg": "version name under the current platform architecture already exists"})]
        with self.assertRaises(pu.ApiError) as ctx:
            self.uploader().upload("1", "stable", self.pack, "NKAS-1.pack")
        self.assertFalse(ctx.exception.retryable)
        self.assertIn("409", str(ctx.exception))
        self.assertEqual(len(self.fake.requests), 1)

    def test_server_error_is_retried(self):
        self.fake.create_answers = [(503, {"code": -1, "msg": "busy"})]
        self.uploader().upload("1", "stable", self.pack, "NKAS-1.pack")
        self.assertEqual(self.fake.paths()[:2], [
            ("POST", "/api/resources/NKAS/versions"),
            ("POST", "/api/resources/NKAS/versions"),
        ])
        self.sleep.assert_any_call(15)

    def test_failed_processing_is_not_retried(self):
        self.fake.statuses = [pu.STATUS_FAILED]
        with self.assertRaises(pu.ApiError):
            self.uploader().upload("1", "stable", self.pack, "NKAS-1.pack")
        self.assertEqual(sum(1 for m, p in self.fake.paths() if p.endswith("/versions")), 1)

    def test_no_status_key_skips_polling(self):
        self.fake.callback_status_key = ""
        self.uploader().upload("1", "stable", self.pack, "NKAS-1.pack")
        self.assertEqual(self.fake.paths()[-1], ("POST", "/api/resources/NKAS/versions/callback"))

    def test_release_note(self):
        self.uploader().set_release_note("1", "stable", "- 修复 (abc1234)")
        method, path, _, body = self.fake.requests[0]
        self.assertEqual((method, path), ("PUT", "/api/resources/NKAS/versions/release-note"))
        self.assertEqual(json.loads(body), {"version_name": "1", "channel": "stable", "content": "- 修复 (abc1234)"})


class RunTest(FakeServerTest):
    def setUp(self):
        super().setUp()
        self.origin, self.work, self.c1 = self.make_origin()
        self.outputs = self.path("outputs")
        env = {
            "MC_RID": "NKAS", "MC_TOKEN": "tok", "MC_BRANCH": "master", "MC_CHANNEL": "stable",
            "MC_API_BASE": self.fake.api_base, "MC_RELEASE_NOTE": "", "MC_AUTO_RELEASE_NOTE": "true",
            "MC_DRY_RUN": "false", "GITHUB_OUTPUT": self.outputs, "GITHUB_EVENT_NAME": "schedule",
        }
        mock.patch.dict(os.environ, env).start()
        cwd = os.getcwd()
        os.chdir(self.work)
        self.addCleanup(os.chdir, cwd)

    def read_outputs(self):
        with open(self.outputs, encoding="utf-8") as f:
            return dict(line.split("=", 1) for line in f.read().splitlines())

    def test_uploads_tip(self):
        self.assertEqual(pu.run(), 0)
        name = pu.version_name(self.c1)
        self.assertEqual(self.read_outputs(), {"skipped": "false", "commit": self.c1, "version_name": name})
        self.assertTrue(self.fake.uploaded["file"].get_payload(decode=True).startswith(b"PACK"))
        method, path, _, body = self.fake.requests[-1]
        self.assertEqual((method, path), ("PUT", "/api/resources/NKAS/versions/release-note"))
        self.assertEqual(json.loads(body)["content"], f"- c1 ({self.c1[:7]})")

    def test_skips_when_no_longer_tip(self):
        other = self.path("other")
        git("clone", "-q", self.origin, other)
        commit(other, {"a.txt": "2"}, "c2")
        git("push", "-q", "origin", "HEAD:master", cwd=other)
        self.assertEqual(pu.run(), 0)
        self.assertEqual(self.read_outputs()["skipped"], "true")
        self.assertEqual(self.fake.requests, [])

    def test_dry_run(self):
        os.environ["MC_DRY_RUN"] = "true"
        self.assertEqual(pu.run(), 0)
        self.assertEqual(self.read_outputs()["skipped"], "false")
        self.assertEqual(self.fake.requests, [])

    def test_unknown_branch_fails(self):
        os.environ["MC_BRANCH"] = "v1.0.0"
        self.assertEqual(pu.run(), 1)

    def test_upload_rejected_fails(self):
        self.fake.create_answers = [(409, {"code": 1, "msg": "conflict"})]
        self.assertEqual(pu.run(), 1)
        self.assertFalse(os.path.exists(self.outputs))


if __name__ == "__main__":
    unittest.main()
