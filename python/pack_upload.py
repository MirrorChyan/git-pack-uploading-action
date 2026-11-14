"""Upload the snapshot of a git commit to MirrorChyan as a version of a pack resource.

The snapshot is the commit with its whole tree and no history, written as a non-thin pack.
The version name is the committer time of the commit in UTC (%Y%m%d%H%M%S): clients
only have their HEAD, and must reach the same name from it to ask for an incremental
package, so CI and clients share this one rule.

Only the standard library is used, so the action runs on any runner without installing
packages.
"""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

PACK_MAGIC = b"PACK"
CHANNELS = ("stable", "beta", "alpha")

# polling states of the storage task, see misc.PollingStatus in resource-backend
STATUS_PENDING = 1
STATUS_COMPLETED = 2
STATUS_FAILED = 3

UPLOAD_ATTEMPTS = 3

# failures of the connection itself, worth another attempt
NETWORK_ERRORS = (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError)


def log(msg: object) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} | {msg}", flush=True)


def annotate(level: str, msg: str) -> None:
    """Prints a workflow command so the message shows up on the run summary."""
    print(f"::{level}::{msg}", flush=True)


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------


class GitError(RuntimeError):
    pass


def _git(args: list[str], cwd: str | None, stdin: bytes | None = None) -> bytes:
    result = subprocess.run(["git", *args], cwd=cwd, input=stdin, capture_output=True)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git {' '.join(args)} exited with {result.returncode}: {stderr}")
    return result.stdout


def git(*args: str, cwd: str | None = None) -> str:
    return _git(list(args), cwd).decode("utf-8", "replace").strip()


def head_commit(cwd: str | None = None) -> str:
    return git("rev-parse", "--verify", "HEAD^{commit}", cwd=cwd)


def remote_branch_tip(branch: str, remote: str = "origin", cwd: str | None = None) -> str | None:
    """Returns the commit the branch points to on the remote, None when it has no such branch."""
    ref = f"refs/heads/{branch}"
    result = subprocess.run(
        ["git", "ls-remote", "--exit-code", remote, ref],
        cwd=cwd,
        capture_output=True,
    )
    # --exit-code exits with 2 when no ref matches
    if result.returncode == 2:
        return None
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git ls-remote {remote} {ref} exited with {result.returncode}: {stderr}")
    # the pattern matches ref name tails, so pick the exact ref
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == ref:
            return parts[0]
    return None


def committer_time(commit: str, cwd: str | None = None) -> int:
    """Reads the time from the commit object itself: log and show also load the parents,
    which a snapshot or a shallow repository may not have."""
    raw = git("cat-file", "commit", commit, cwd=cwd)
    for line in raw.splitlines():
        if not line:
            break  # end of the header
        if line.startswith("committer "):
            # committer <name> <<email>> <unix time> <offset>
            return int(line.rsplit(" ", 2)[1])
    raise GitError(f"commit {commit} has no committer")


def version_name(commit: str, cwd: str | None = None) -> str:
    """The committer time of the commit in UTC; the time zone a commit records varies
    by committer, so a local time would not sort with the commit order."""
    return datetime.fromtimestamp(committer_time(commit, cwd), timezone.utc).strftime("%Y%m%d%H%M%S")


def build_snapshot_pack(commit: str, dest: str, cwd: str | None = None) -> int:
    """Writes the commit with its whole tree, and none of its history, as a non-thin pack,
    and returns its size. A shallow checkout of depth 1 is enough."""
    objects = _git(["rev-list", "--objects", "--no-walk", commit], cwd)
    with open(dest, "wb") as out:
        result = subprocess.run(
            ["git", "pack-objects", "-q", "--stdout"],
            cwd=cwd,
            input=objects,
            stdout=out,
            stderr=subprocess.PIPE,
        )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git pack-objects exited with {result.returncode}: {stderr}")
    with open(dest, "rb") as f:
        if f.read(len(PACK_MAGIC)) != PACK_MAGIC:
            raise GitError("git pack-objects did not write a pack")
    return os.path.getsize(dest)


def _subject(message: str) -> str:
    lines = message.strip().splitlines()
    return lines[0].strip() if lines else ""


def build_release_note(commit: str, cwd: str | None, event_name: str, event_path: str | None) -> str:
    """Lists the commits a push brought; any other trigger only knows the commit itself."""
    entries: list[str] = []
    if event_name == "push" and event_path and os.path.isfile(event_path):
        with open(event_path, encoding="utf-8") as f:
            event = json.load(f)
        # a push of another commit (the job no longer runs on the tip) says nothing about this one
        if event.get("after") == commit:
            for c in event.get("commits") or []:
                subject = _subject(c.get("message") or "")
                if subject:
                    entries.append(f"- {subject} ({(c.get('id') or '')[:7]})")
    if entries:
        return "\n".join(entries)
    return f"- {git('log', '-1', '--format=%s', commit, cwd=cwd)} ({commit[:7]})"


# ---------------------------------------------------------------------------
# MirrorChyan API
# ---------------------------------------------------------------------------


class ApiError(RuntimeError):
    def __init__(self, step: str, status: int | None, detail: object, retryable: bool):
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", "replace")
        detail = str(detail).strip()[:500]
        super().__init__(f"{step} failed" + (f" ({status})" if status else "") + f": {detail}")
        self.status = status
        self.retryable = retryable


def encode_multipart(fields: dict[str, str], file_field: str, filename: str, data: bytes) -> tuple[bytes, str]:
    """Encodes a multipart/form-data body; the file comes last, as OSS PostObject requires."""
    boundary = uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    chunks.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
    )
    chunks.append(data)
    chunks.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _send(req: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


@dataclass
class Uploader:
    api_base: str
    rid: str
    token: str
    poll_interval: float = 5
    max_polls: int = 60
    timeout: float = 120
    upload_timeout: float = 900
    retry_delay: float = 15

    def _url(self, path: str = "", query: dict[str, str] | None = None) -> str:
        url = f"{self.api_base.rstrip('/')}/resources/{urllib.parse.quote(self.rid, safe='')}/versions{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return url

    def _call(self, step: str, method: str, url: str, body: bytes | None = None, content_type: str | None = None) -> dict:
        headers = {"Authorization": self.token.strip(), "Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            status, raw = _send(req, self.timeout)
        except NETWORK_ERRORS as e:
            raise ApiError(step, None, getattr(e, "reason", e), retryable=True) from e
        if status != 200:
            # 4xx means the request itself is wrong (token, name conflict, ...): sending it again won't help
            raise ApiError(step, status, raw, retryable=status >= 500 or status == 429)
        try:
            payload = json.loads(raw)
        except ValueError as e:
            raise ApiError(step, status, raw, retryable=False) from e
        if payload.get("code", 0) != 0:
            raise ApiError(step, status, payload.get("msg") or raw, retryable=False)
        return payload.get("data") or {}

    def _post_form(self, step: str, url: str, form: dict[str, str]) -> dict:
        body = urllib.parse.urlencode(form).encode()
        return self._call(step, "POST", url, body, "application/x-www-form-urlencoded")

    def _upload_to_oss(self, policy: dict, pack_path: str, download_name: str) -> None:
        fields = {
            "success_action_status": "200",
            "name": policy["name"],
            "signature": policy["signature"],
            "key": policy["key"],
            "policy": policy["policy"],
            "OSSAccessKeyId": policy["access_key"],
            "Content-Disposition": f'attachment; filename="{download_name}"',
        }
        with open(pack_path, "rb") as f:
            body, content_type = encode_multipart(fields, "file", download_name, f.read())
        req = urllib.request.Request(policy["host"], data=body, method="POST", headers={"Content-Type": content_type})
        try:
            status, raw = _send(req, self.upload_timeout)
        except NETWORK_ERRORS as e:
            raise ApiError("upload to oss", None, getattr(e, "reason", e), retryable=True) from e
        if status != 200:
            # the policy may have expired; a new attempt asks for a new one
            raise ApiError("upload to oss", status, raw, retryable=True)

    def _wait_processed(self, status_key: str) -> None:
        for i in range(self.max_polls):
            time.sleep(self.poll_interval)
            try:
                data = self._call("poll status", "GET", self._url("/status", {"key": status_key}))
            except ApiError as e:
                if e.retryable:
                    log(f"{e}, polling again")
                    continue
                raise
            status = data.get("status")
            if status == STATUS_COMPLETED:
                return
            if status == STATUS_FAILED:
                raise ApiError("processing", None, "the server failed to process the snapshot", retryable=False)
            if status != STATUS_PENDING:
                raise ApiError("processing", None, f"unexpected status {status}", retryable=False)
            if (i + 1) % 6 == 0:
                log(f"still processing ({(i + 1) * self.poll_interval:.0f}s)")
        # the task may still finish; uploading again would only race with it
        raise ApiError("processing", None, "timed out waiting for the server", retryable=False)

    def _upload_once(self, name: str, channel: str, pack_path: str, download_name: str) -> None:
        form = {"name": name, "os": "", "arch": "", "channel": channel}
        policy = self._post_form("create version", self._url(), {**form, "filename": download_name})
        log("got upload policy")
        self._upload_to_oss(policy, pack_path, download_name)
        log("uploaded to oss")
        data = self._post_form("callback", self._url("/callback"), {**form, "key": policy["key"]})
        status_key = data.get("status_key") or ""
        if not status_key:
            log("the server has a package for this version already, nothing to process")
            return
        log("waiting for the server to process the snapshot")
        self._wait_processed(status_key)
        log("processed")

    def upload(self, name: str, channel: str, pack_path: str, download_name: str) -> None:
        for attempt in range(1, UPLOAD_ATTEMPTS + 1):
            try:
                self._upload_once(name, channel, pack_path, download_name)
                return
            except ApiError as e:
                if not e.retryable or attempt == UPLOAD_ATTEMPTS:
                    raise
                delay = self.retry_delay * attempt
                log(f"{e}, retrying in {delay:.0f}s ({attempt}/{UPLOAD_ATTEMPTS})")
                time.sleep(delay)

    def set_release_note(self, name: str, channel: str, content: str) -> None:
        body = json.dumps({"version_name": name, "channel": channel, "content": content}).encode()
        self._call("set release note", "PUT", self._url("/release-note"), body, "application/json")


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------


def write_outputs(values: dict[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    for key, value in values.items():
        log(f"output {key}={value}")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for key, value in values.items():
            f.write(f"{key}={value}\n")


def env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


def run() -> int:
    rid = os.environ.get("MC_RID", "").strip()
    token = os.environ.get("MC_TOKEN", "")
    branch = os.environ.get("MC_BRANCH", "").strip()
    channel = os.environ.get("MC_CHANNEL", "").strip() or "stable"
    api_base = os.environ.get("MC_API_BASE", "").strip() or "https://mirrorchyan.com/api"
    dry_run = env_flag("MC_DRY_RUN", False)

    if not rid:
        annotate("error", "mirrorchyan_rid is required")
        return 1
    if not token.strip() and not dry_run:
        annotate("error", "upload_token is required")
        return 1
    if not branch:
        annotate("error", "branch is required")
        return 1
    if channel not in CHANNELS:
        annotate("error", f"channel must be one of {', '.join(CHANNELS)}, got {channel!r}")
        return 1

    commit = head_commit()
    tip = remote_branch_tip(branch)
    if tip is None:
        annotate("error", f"origin has no branch {branch!r}; set the branch input to the branch the resource tracks")
        return 1
    if tip != commit:
        # a newer push owns the upload; uploading this commit could make an old commit the latest
        log(f"HEAD {commit[:8]} is no longer the tip of {branch} ({tip[:8]}), skip")
        write_outputs({"skipped": "true", "commit": commit, "version_name": ""})
        return 0

    name = version_name(commit)
    download_name = f"{rid}-{name}.pack"
    log(f"commit {commit}, version {name}, channel {channel}")

    note = os.environ.get("MC_RELEASE_NOTE", "")
    if not note.strip() and env_flag("MC_AUTO_RELEASE_NOTE", True):
        note = build_release_note(
            commit,
            None,
            os.environ.get("GITHUB_EVENT_NAME", ""),
            os.environ.get("GITHUB_EVENT_PATH"),
        )

    with tempfile.TemporaryDirectory(prefix="mirrorchyan-pack-") as tmp:
        pack_path = os.path.join(tmp, "snapshot.pack")
        size = build_snapshot_pack(commit, pack_path)
        log(f"snapshot pack: {size} bytes")

        if dry_run:
            log(f"dry run, would upload {download_name} to {api_base}")
            if note.strip():
                log("release note:\n" + note)
            write_outputs({"skipped": "false", "commit": commit, "version_name": name})
            return 0

        uploader = Uploader(api_base=api_base, rid=rid, token=token)
        try:
            uploader.upload(name, channel, pack_path, download_name)
        except ApiError as e:
            annotate("error", str(e))
            return 1

        if note.strip():
            # set once the server took the snapshot, so a rejected upload leaves the version's note alone
            try:
                uploader.set_release_note(name, channel, note)
                log("release note set")
            except ApiError as e:
                annotate("warning", f"uploaded, but {e}")

    write_outputs({"skipped": "false", "commit": commit, "version_name": name})
    return 0


def main() -> None:
    try:
        sys.exit(run())
    except GitError as e:
        annotate("error", str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
