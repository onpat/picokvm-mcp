#!/usr/bin/env python3
"""Upload a file to PicoKVM using bounded HTTP requests (standard library only)."""

import argparse
from http.client import HTTPException
from http.cookiejar import CookieJar
import itertools
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


CHUNK_SIZE = 256 * 1024 * 1024
READ_SIZE = 64 * 1024
TIMEOUT = 120


class UploadError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise UploadError("HTTP redirect refused; set PICOKVM_BASE_URL to the final device URL")


class Client:
    def __init__(self, base_url, password=None):
        parsed = urllib.parse.urlsplit(base_url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment):
            raise UploadError("PICOKVM_BASE_URL must be an HTTP(S) device URL without credentials, query or fragment")
        self.base_url = base_url.rstrip("/")
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()), NoRedirect())
        self.session_id = str(uuid.uuid4())
        self.ids = itertools.count(1)
        if password is not None:
            self.post("/auth/login-local", {"password": password})

    def request(self, path, data, content_type, length):
        request = urllib.request.Request(
            self.base_url + path, data=data, method="POST",
            headers={"Content-Type": content_type, "Content-Length": str(length),
                     "Accept": "application/json", "X-Session-ID": self.session_id})
        try:
            with self.opener.open(request, timeout=TIMEOUT) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            with error:
                detail = error.read(1024).decode("utf-8", errors="replace")
            raise UploadError("HTTP {}: {}".format(error.code, detail)) from error
        except (ValueError, UnicodeError) as error:
            raise UploadError("Invalid JSON response from PicoKVM") from error
        if not isinstance(result, dict):
            raise UploadError("Unexpected response from PicoKVM")
        return result

    def post(self, path, body):
        data = json.dumps(body).encode("utf-8")
        return self.request(path, data, "application/json", len(data))

    def rpc(self, method, params=None):
        request_id = next(self.ids)
        result = self.post("/api/rpc", {
            "jsonrpc": "2.0", "id": request_id,
            "method": method, "params": params or {}})
        # The HTTP RPC transport can wrap a response together with a UI event.
        result = result.get("response", result)
        if not isinstance(result, dict) or result.get("id") != request_id:
            raise UploadError("Invalid JSON-RPC response")
        if "error" in result:
            raise UploadError("{}: {}".format(method, result["error"]))
        if "result" not in result:
            raise UploadError("Missing JSON-RPC result for " + method)
        return result["result"]

    def upload(self, upload_id, blocks, length):
        query = urllib.parse.urlencode({"uploadId": upload_id})
        result = self.request("/storage/upload?" + query, blocks,
                              "application/octet-stream", length)
        if "error" in result:
            raise UploadError("Upload failed: {}".format(result["error"]))


def byte_count(value, label):
    if type(value) is not int or value < 0:
        raise UploadError("Invalid {} returned by PicoKVM".format(label))
    return value


class Storage:
    def __init__(self, client, source):
        self.client = client
        self.suffix = "SDStorage" if source == "sd" else "Storage"

    def files(self):
        result = self.client.rpc("list" + self.suffix + "Files")
        if not isinstance(result, dict) or not isinstance(result.get("files"), list):
            raise UploadError("Invalid storage file list")
        files = {}
        for item in result["files"]:
            if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
                raise UploadError("Invalid storage file entry")
            files[item["filename"]] = byte_count(item.get("size"), "file size")
        return files

    def free_space(self):
        result = self.client.rpc("get" + self.suffix + "Space")
        if not isinstance(result, dict):
            raise UploadError("Invalid storage space response")
        return byte_count(result.get("bytesFree"), "free space")

    def start(self, filename, size):
        result = self.client.rpc("start" + self.suffix + "FileUpload",
                                 {"filename": filename, "size": size})
        if not isinstance(result, dict):
            raise UploadError("Invalid upload start response")
        offset = byte_count(result.get("alreadyUploadedBytes"), "upload offset")
        upload_id = result.get("dataChannel")
        if not isinstance(upload_id, str) or not upload_id.startswith("upload_"):
            raise UploadError("Invalid upload ID")
        return offset, upload_id

    def confirm(self, filename, expected, total):
        deadline = time.monotonic() + 5
        while True:
            files = self.files()
            if filename in files:
                if expected == total and files[filename] == total:
                    return
                raise UploadError("Unexpected completed file or completed file size")
            partial = files.get(filename + ".incomplete")
            if expected < total:
                if partial != expected:
                    raise UploadError("Received size mismatch: expected {}, got {}".format(expected, partial))
                return
            if partial is not None and partial != expected:
                raise UploadError("Received size mismatch: expected {}, got {}".format(expected, partial))
            if time.monotonic() >= deadline:
                raise UploadError("Completed file was not confirmed within 5 seconds")
            time.sleep(0.2)


class Progress:
    def __init__(self, total, stream=None):
        self.total = total
        self.stream = stream if stream is not None else sys.stderr
        self.last_update = float("-inf")

    def update(self, sent, confirmed=False, done=False):
        now = time.monotonic()
        if not confirmed and not done and now - self.last_update < 0.5:
            return
        self.last_update = now
        percent = 100 * sent / self.total if self.total else 100
        status = "Complete" if done else "Confirmed" if confirmed else "Sending"
        self.stream.write("\r{:9s} {:6.2f}%  {:.2f} / {:.2f} MiB   {}".format(
            status, percent, sent / (1024 * 1024), self.total / (1024 * 1024),
            "\n" if done else ""))
        self.stream.flush()


def file_blocks(file, offset, length, progress):
    """An iterable body with a known Content-Length; never buffer a whole chunk."""
    file.seek(offset)
    sent = 0
    while sent < length:
        block = file.read(min(READ_SIZE, length - sent))
        if not block:
            raise UploadError("Local file became shorter during upload")
        yield block
        sent += len(block)
        progress.update(offset + sent)


def upload_file(client, filename, source="internal", progress=None):
    path = Path(filename)
    if ".." in path.name or path.name.endswith(".incomplete"):
        raise UploadError("Filename must not contain '..' or end in '.incomplete'")
    if not path.is_file():
        raise UploadError("Not a regular file: {}".format(path))
    storage = Storage(client, source)
    with path.open("rb") as file:
        initial = os.fstat(file.fileno())
        total = initial.st_size
        progress = progress if progress is not None else Progress(total)
        files = storage.files()
        if path.name in files:
            raise UploadError("Completed file already exists; it will not be overwritten: " + path.name)
        offset = files.get(path.name + ".incomplete", 0)
        if offset > total:
            raise UploadError("Partial file is larger than the local file")
        if storage.free_space() < total - offset:
            raise UploadError("Not enough free space for the remaining file data")
        progress.update(offset, confirmed=True)
        # A zero-byte POST also finalizes an empty file or a fully received partial.
        while True:
            current = os.fstat(file.fileno())
            if (current.st_size, current.st_mtime_ns) != (total, initial.st_mtime_ns):
                raise UploadError("Local file changed during upload")
            received, upload_id = storage.start(path.name, total)
            if received != offset:
                raise UploadError("Upload offset changed unexpectedly: expected {}, got {}".format(offset, received))
            length = min(CHUNK_SIZE, total - offset)
            client.upload(upload_id, file_blocks(file, offset, length, progress), length)
            current = os.fstat(file.fileno())
            if (current.st_size, current.st_mtime_ns) != (total, initial.st_mtime_ns):
                raise UploadError("Local file changed during upload")
            offset += length
            storage.confirm(path.name, offset, total)
            progress.update(offset, confirmed=True)
            if offset == total:
                break
        progress.update(total, done=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("filename", help="local file to upload (saved with the same basename)")
    parser.add_argument("--source", choices=("internal", "sd"), default="internal",
                        help="destination storage (default: internal)")
    args = parser.parse_args(argv)
    base_url = os.environ.get("PICOKVM_BASE_URL", "").strip()
    if not base_url:
        parser.error("set PICOKVM_BASE_URL first, e.g. export PICOKVM_BASE_URL='http://192.168.1.100'")
    try:
        client = Client(base_url, os.environ.get("PICOKVM_PASSWORD"))
        upload_file(client, args.filename, args.source)
    except KeyboardInterrupt:
        print("\nInterrupted. Partial data is kept; rerun with the same file to resume.", file=sys.stderr)
        return 130
    except (UploadError, OSError, urllib.error.URLError, HTTPException) as error:
        print("\nUpload failed: {}\nPartial data is kept. After resolving the error, "
              "rerun with the same file to resume.".format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
