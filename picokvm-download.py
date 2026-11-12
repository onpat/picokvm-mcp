#!/usr/bin/env python3
"""Download one MTP-shared PicoKVM file over HTTP (standard library only)."""

import argparse
from http.client import HTTPException
from http.cookiejar import CookieJar
import itertools
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


READ_SIZE = 64 * 1024
TIMEOUT = 120


class DownloadError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DownloadError("HTTP redirect refused; set PICOKVM_BASE_URL to the final device URL")


class Client:
    def __init__(self, base_url, password=None):
        parsed = urllib.parse.urlsplit(base_url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment):
            raise DownloadError("PICOKVM_BASE_URL must be an HTTP(S) device URL without credentials, query or fragment")
        self.base_url = base_url.rstrip("/")
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()), NoRedirect())
        self.session_id = str(uuid.uuid4())
        self.ids = itertools.count(1)
        if password is not None:
            login = self.post("/auth/login-local", {"password": password})
            if "error" in login:
                raise DownloadError("PicoKVM login failed")

    def open(self, path, body=None):
        headers = {"X-Session-ID": self.session_id}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers.update({"Content-Type": "application/json", "Accept": "application/json"})
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers)
        try:
            response = self.opener.open(request, timeout=TIMEOUT)
        except urllib.error.HTTPError as error:
            with error:
                detail = error.read(1024).decode("utf-8", errors="replace")
            raise DownloadError("HTTP {}: {}".format(error.code, detail)) from error
        if response.getcode() != 200:
            status = response.getcode()
            response.close()
            raise DownloadError("Unexpected HTTP status: {}".format(status))
        return response

    def post(self, path, body):
        with self.open(path, body) as response:
            try:
                result = json.loads(response.read().decode("utf-8"))
            except (ValueError, UnicodeError) as error:
                raise DownloadError("Invalid JSON response from PicoKVM") from error
        if not isinstance(result, dict):
            raise DownloadError("Unexpected JSON response from PicoKVM")
        return result

    def rpc(self, method):
        request_id = next(self.ids)
        result = self.post("/api/rpc", {
            "jsonrpc": "2.0", "id": request_id, "method": method, "params": {}})
        event = result.get("event")
        if isinstance(event, dict) and event.get("method") == "sessionInvalidated":
            raise DownloadError("PicoKVM HTTP session invalidated")
        result = result.get("response", result)
        if (not isinstance(result, dict) or result.get("jsonrpc") != "2.0"
                or result.get("id") != request_id):
            raise DownloadError("Invalid JSON-RPC response")
        if result.get("error") is not None:
            raise DownloadError("{}: {}".format(method, result["error"]))
        if "result" not in result:
            raise DownloadError("Missing JSON-RPC result for " + method)
        return result["result"]

    def download(self, filename, source):
        path = "/storage/sd-download" if source == "sd" else "/storage/download"
        return self.open(path + "?" + urllib.parse.urlencode({"file": filename}))


def validate_filename(filename):
    if (not isinstance(filename, str) or not filename or ".." in filename
            or filename == "." or "/" in filename or "\\" in filename or "\x00" in filename):
        raise DownloadError("filename must be a shared-storage file name, without path components or '..'")


class Storage:
    def __init__(self, client, source):
        if source not in ("internal", "sd"):
            raise DownloadError("source must be internal or sd")
        self.client = client
        self.method = "listSDStorageFiles" if source == "sd" else "listStorageFiles"

    def info(self, filename):
        result = self.client.rpc(self.method)
        if not isinstance(result, dict) or not isinstance(result.get("files"), list):
            raise DownloadError("Invalid storage file list")
        for item in result["files"]:
            if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
                raise DownloadError("Invalid storage file entry")
            if item["filename"] == filename:
                size, modified = item.get("size"), item.get("createdAt")
                if type(size) is not int or size < 0 or not isinstance(modified, str) or not modified:
                    raise DownloadError("Invalid file size or modification time")
                return size, modified
        raise DownloadError("File not found in shared storage: " + filename)


class Progress:
    def __init__(self, total, stream=None):
        self.total = total
        self.stream = stream if stream is not None else sys.stderr
        self.last_update = float("-inf")

    def update(self, received, done=False):
        now = time.monotonic()
        if not done and now - self.last_update < 0.5:
            return
        self.last_update = now
        percent = 100 * received / self.total if self.total else 100
        self.stream.write("\r{:11s} {:6.2f}%  {:.2f} / {:.2f} MiB{}".format(
            "Complete" if done else "Downloading", percent,
            received / (1024 * 1024), self.total / (1024 * 1024), "\n" if done else ""))
        self.stream.flush()


def download_file(client, filename, source="internal", output=None, progress=None):
    validate_filename(filename)
    destination = Path(filename if output is None else output)
    if os.path.lexists(destination):
        raise DownloadError("Destination already exists; it will not be overwritten: {}".format(destination))
    if not destination.parent.is_dir():
        raise DownloadError("Destination directory does not exist: {}".format(destination.parent))
    storage = Storage(client, source)
    initial = storage.info(filename)
    total = initial[0]
    progress = progress if progress is not None else Progress(total)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=str(destination.parent),
                                         prefix=".picokvm-download-", suffix=".part", delete=False) as file:
            temporary = Path(file.name)
            with client.download(filename, source) as response:
                raw_length = response.headers.get("Content-Length")
                if raw_length is not None:
                    try:
                        length = int(raw_length)
                    except ValueError as error:
                        raise DownloadError("Invalid Content-Length") from error
                    if length < 0 or length != total:
                        raise DownloadError("Content-Length does not match the listed file size")
                received = 0
                progress.update(received)
                while True:
                    block = response.read(READ_SIZE)
                    if not block:
                        break
                    received += len(block)
                    if received > total:
                        raise DownloadError("Received more data than the listed file size")
                    file.write(block)
                    progress.update(received)
                if received != total:
                    raise DownloadError("Received size mismatch: expected {}, got {}".format(total, received))
        if storage.info(filename) != initial:
            raise DownloadError("Remote file changed during download; wait for the MTP copy to finish")
        # Linking within the destination directory publishes the complete file
        # atomically and fails if another process has created the destination.
        try:
            os.link(temporary, destination)
        except FileExistsError as error:
            raise DownloadError("Destination already exists; it will not be overwritten: {}".format(destination)) from error
        except OSError as error:
            raise DownloadError("Cannot publish download; destination filesystem must support hard links: {}".format(error)) from error
    finally:
        if temporary is not None:
            temporary.unlink()
    progress.update(total, done=True)
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("filename", help="file name in the root of PicoKVM shared storage")
    parser.add_argument("--source", choices=("internal", "sd"), default="internal",
                        help="source storage (default: internal)")
    parser.add_argument("--output", help="destination file path (default: same name in the current directory)")
    args = parser.parse_args(argv)
    base_url = os.environ.get("PICOKVM_BASE_URL", "").strip()
    if not base_url:
        parser.error("set PICOKVM_BASE_URL first, e.g. export PICOKVM_BASE_URL='http://192.168.1.100'")
    try:
        validate_filename(args.filename)
    except DownloadError as error:
        parser.error(str(error))
    try:
        client = Client(base_url, os.environ.get("PICOKVM_PASSWORD"))
        saved = download_file(client, args.filename, args.source, args.output)
    except KeyboardInterrupt:
        print("\nInterrupted. Temporary data is removed; rerun to download from the beginning.", file=sys.stderr)
        return 130
    except (DownloadError, OSError, urllib.error.URLError, HTTPException) as error:
        print("\nDownload failed: {}".format(error), file=sys.stderr)
        return 1
    print(saved)
    return 0


if __name__ == "__main__":
    sys.exit(main())
