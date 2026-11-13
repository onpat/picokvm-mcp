"""Offline transfer and HTTP protocol tests for the standalone downloader."""

from email.message import Message
from http.client import IncompleteRead
from http.cookiejar import CookieJar
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.parse
import urllib.request
import urllib.response


ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("picokvm_download", ROOT / "picokvm-download.py")
download = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(download)
MODIFIED = "2026-11-12T12:00:00Z"


class Response(io.BytesIO):
    def __init__(self, body, length):
        super().__init__(body)
        self.headers = Message()
        if length is not None:
            self.headers["Content-Length"] = str(length)
        self.read_sizes = []

    def read(self, size=-1):
        if not 0 < size <= 64 * 1024:
            raise AssertionError("File data must be read in bounded blocks")
        self.read_sizes.append(size)
        return super().read(size)


class FakeClient:
    def __init__(self, body=b"sample data", filename="report.zip"):
        self.filename = filename
        self.size = len(body)
        self.modified = MODIFIED
        self.response = Response(body, len(body))
        self.calls = []
        self.after_transfer = None

    def rpc(self, method):
        self.calls.append((method,))
        if self.response.closed and self.after_transfer:
            self.after_transfer()
        return {"files": [{"filename": self.filename, "size": self.size, "createdAt": self.modified}]}

    def download(self, filename, source):
        self.calls.append(("download", filename, source))
        return self.response


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.parent = Path(self.directory.name)
        self.output = self.parent / "saved.zip"
        self.messages = io.StringIO()

    def run_download(self, client, source="internal"):
        return download.download_file(client, client.filename, source, self.output,
                                      download.Progress(client.size, self.messages))

    def assert_no_files(self):
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_internal_and_sd_downloads_validate_before_publishing(self):
        for source, method in (("internal", "listStorageFiles"), ("sd", "listSDStorageFiles")):
            with self.subTest(source=source):
                client = FakeClient()

                def check_not_published():
                    self.assertFalse(self.output.exists())
                    self.assertEqual(len(list(self.parent.glob("*.part"))), 1)

                client.after_transfer = check_not_published
                self.assertEqual(self.run_download(client, source), self.output)
                self.assertEqual(self.output.read_bytes(), b"sample data")
                self.assertEqual(client.calls, [(method,), ("download", "report.zip", source), (method,)])
                self.assertTrue(client.response.closed)
                self.assertEqual(list(self.parent.iterdir()), [self.output])
                self.output.unlink()
        self.assertIn("Complete", self.messages.getvalue())
        self.assertIn("100.00%", self.messages.getvalue())

    def test_binary_data_is_read_in_64k_blocks(self):
        body = bytes(range(256)) * 1025
        client = FakeClient(body)
        self.run_download(client)
        self.assertEqual(self.output.read_bytes(), body)
        self.assertGreater(len(client.response.read_sizes), 4)
        self.assertEqual(set(client.response.read_sizes), {64 * 1024})

    def test_empty_file_and_absent_content_length(self):
        for body in (b"", b"hello"):
            with self.subTest(body=body):
                client = FakeClient(body)
                client.response = Response(body, None)
                self.run_download(client)
                self.assertEqual(self.output.read_bytes(), body)
                self.output.unlink()

    def test_default_output_is_remote_name_in_current_directory(self):
        with patch.object(download, "Path", wraps=Path) as path_type:
            # Map relative paths into the test directory without changing cwd.
            path_type.side_effect = lambda value: self.parent / value
            saved = download.download_file(FakeClient(), "report.zip", progress=Mock())
        self.assertEqual(saved, self.parent / "report.zip")
        self.assertEqual(saved.read_bytes(), b"sample data")

    def test_existing_file_directory_and_dangling_symlink_are_never_overwritten(self):
        client = FakeClient()
        for kind in ("file", "directory", "symlink"):
            with self.subTest(kind=kind):
                if kind == "file":
                    self.output.write_bytes(b"keep")
                elif kind == "directory":
                    self.output.mkdir()
                else:
                    self.output.symlink_to(self.parent / "missing")
                with self.assertRaisesRegex(download.DownloadError, "already exists"):
                    self.run_download(client)
                if kind == "file":
                    self.assertEqual(self.output.read_bytes(), b"keep")
                self.output.rmdir() if kind == "directory" else self.output.unlink()
        self.assertEqual(client.calls, [])

    def test_destination_created_during_transfer_is_preserved(self):
        client = FakeClient()
        client.after_transfer = lambda: self.output.write_bytes(b"other process")
        with self.assertRaisesRegex(download.DownloadError, "already exists"):
            self.run_download(client)
        self.assertEqual(self.output.read_bytes(), b"other process")
        self.assertEqual(list(self.parent.iterdir()), [self.output])

    def test_missing_output_directory_and_invalid_inputs_fail_before_network(self):
        client = Mock()
        with self.assertRaisesRegex(download.DownloadError, "directory does not exist"):
            download.download_file(client, "report.zip", output=self.parent / "missing" / "file")
        for filename in ("", ".", "..", "../file", "dir/file", "dir\\file", "a..b", "bad\0name", None):
            with self.subTest(filename=filename), self.assertRaises(download.DownloadError):
                download.download_file(client, filename, output=self.output)
        with self.assertRaisesRegex(download.DownloadError, "source"):
            download.download_file(client, "report.zip", "other", self.output)
        self.assertEqual(client.mock_calls, [])
        self.assert_no_files()

    def test_missing_or_malformed_metadata_never_starts_get(self):
        for result in (None, {}, {"files": None}, {"files": []}, {"files": [None]},
                       {"files": [{"filename": 1}]},
                       *({"files": [{"filename": "report.zip", "size": size, "createdAt": stamp}]}
                         for size, stamp in ((True, MODIFIED), (-1, MODIFIED), (1.5, MODIFIED), (1, None), (1, "")))):
            with self.subTest(result=result):
                client = Mock()
                client.rpc.return_value = result
                with self.assertRaises(download.DownloadError):
                    download.download_file(client, "report.zip", output=self.output)
                client.download.assert_not_called()
                self.assert_no_files()

    def test_invalid_or_mismatched_content_length_cleans_temporary_file(self):
        for length in ("bad", "-1", "0", "999"):
            with self.subTest(length=length):
                client = FakeClient()
                client.response = Response(b"sample data", length)
                with self.assertRaisesRegex(download.DownloadError, "Content-Length"):
                    self.run_download(client)
                self.assertEqual(client.response.read_sizes, [])
                self.assertTrue(client.response.closed)
                self.assert_no_files()

    def test_short_or_excess_body_is_not_published(self):
        for body in (b"short", b"sample data plus extra"):
            with self.subTest(body=body):
                client = FakeClient()
                client.response = Response(body, None)
                with self.assertRaises(download.DownloadError):
                    self.run_download(client)
                self.assertEqual(len(client.calls), 2)
                self.assert_no_files()

    def test_connection_failure_interrupt_and_disk_error_remove_partial_data(self):
        for error in (OSError("connection lost"), IncompleteRead(b"x"), KeyboardInterrupt()):
            with self.subTest(error=type(error)):
                client = FakeClient()
                client.response.read = Mock(side_effect=[b"sam", error])
                with self.assertRaises(type(error)):
                    self.run_download(client)
                self.assertTrue(client.response.closed)
                self.assert_no_files()
        original_temporary_file = download.tempfile.NamedTemporaryFile

        def failing_writer(**kwargs):
            file = original_temporary_file(**kwargs)
            file.write = Mock(side_effect=OSError("disk full"))
            return file

        with patch.object(download.tempfile, "NamedTemporaryFile", side_effect=failing_writer):
            with self.assertRaisesRegex(OSError, "disk full"):
                download.download_file(FakeClient(), "report.zip", output=self.output, progress=Mock())
        self.assert_no_files()

    def test_file_update_or_failed_final_check_discards_download(self):
        for field, value in (("size", 20), ("modified", "2026-11-13T12:00:00Z"), ("filename", "gone.zip")):
            with self.subTest(field=field):
                client = FakeClient()
                client.after_transfer = lambda: setattr(client, field, value)
                with self.assertRaises(download.DownloadError):
                    self.run_download(client)
                self.assert_no_files()
        client = FakeClient()
        client.after_transfer = Mock(side_effect=download.DownloadError("session invalidated"))
        with self.assertRaisesRegex(download.DownloadError, "session invalidated"):
            self.run_download(client)
        self.assert_no_files()

    def test_publish_failure_cleans_temp_and_leaves_no_completed_file(self):
        with patch.object(download.os, "link", side_effect=OSError("unsupported")):
            with self.assertRaisesRegex(download.DownloadError, "hard links"):
                self.run_download(FakeClient())
        self.assert_no_files()

    def test_sizes_over_4gb_do_not_overflow_or_buffer_whole_file(self):
        client = FakeClient()
        client.size = 5_000_000_000
        client.response = Response(b"", client.size)
        client.response.read = Mock(side_effect=[b"x" * download.READ_SIZE, OSError("lost")])
        progress = Mock()
        with self.assertRaisesRegex(OSError, "lost"):
            download.download_file(client, client.filename, output=self.output, progress=progress)
        self.assertEqual(client.response.read.call_args_list[0][0], (64 * 1024,))
        self.assert_no_files()


class FakeHTTPHandler(urllib.request.HTTPHandler):
    handler_order = 100

    def __init__(self, responder):
        super().__init__()
        self.responder = responder
        self.requests = []

    def http_open(self, request):
        self.requests.append(request)
        status, result, headers = self.responder(request)
        body = result if isinstance(result, bytes) else json.dumps(result).encode("utf-8")
        info = Message()
        for key, value in headers.items():
            info[key] = value
        if "Content-Length" not in info:
            info["Content-Length"] = str(len(body))
        response = urllib.response.addinfourl(io.BytesIO(body), info, request.full_url, status)
        response.msg = "Result"
        return response


class HTTPTests(unittest.TestCase):
    def client(self, responder, password=None):
        handler = FakeHTTPHandler(responder)
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()), download.NoRedirect(), handler)
        with patch.object(download.urllib.request, "build_opener", return_value=opener):
            client = download.Client("http://device", password)
        return client, handler.requests

    def test_cookie_session_filename_encoding_and_metadata_checks(self):
        filename = "結果 & report +#.zip"

        def responder(request):
            path = urllib.parse.urlsplit(request.full_url)
            if path.path == "/auth/login-local":
                self.assertEqual(json.loads(request.data), {"password": "secret"})
                return 200, {"message": "Login successful"}, {"Set-Cookie": "authToken=test; Path=/"}
            if request.get_header("Cookie") != "authToken=test":
                return 401, {"error": "Unauthorized"}, {}
            if path.path == "/api/rpc":
                rpc = json.loads(request.data)
                self.assertEqual(rpc["method"], "listSDStorageFiles")
                result = {"jsonrpc": "2.0", "id": rpc["id"], "result": {
                    "files": [{"filename": filename, "size": 3, "createdAt": MODIFIED}]}}
                return 200, {"response": result, "event": {}}, {}
            self.assertEqual(path.path, "/storage/sd-download")
            self.assertEqual(urllib.parse.parse_qs(path.query), {"file": [filename]})
            self.assertEqual(request.get_method(), "GET")
            self.assertIsNone(request.data)
            return 200, b"abc", {}

        client, requests = self.client(responder, "secret")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "saved.zip"
            download.download_file(client, filename, "sd", output, Mock())
            self.assertEqual(output.read_bytes(), b"abc")
        self.assertEqual(len(requests), 4)
        self.assertEqual(len({req.get_header("X-session-id") for req in requests}), 1)
        self.assertIsNotNone(requests[0].get_header("X-session-id"))

    def test_direct_rpc_result_and_internal_get(self):
        def responder(request):
            if request.data:
                rpc = json.loads(request.data)
                return 200, {"jsonrpc": "2.0", "id": rpc["id"], "result": {"files": []}}, {}
            self.assertEqual(request.full_url, "http://device/storage/download?file=empty.txt")
            return 200, b"", {}
        client, _ = self.client(responder)
        self.assertEqual(client.rpc("listStorageFiles"), {"files": []})
        with client.download("empty.txt", "internal") as response:
            self.assertEqual(response.read(64 * 1024), b"")

    def test_http_errors_and_redirects_do_not_retry_or_follow(self):
        for status in (401, 404, 500, 302):
            with self.subTest(status=status):
                client, requests = self.client(lambda req: (
                    status, {"error": "failure"}, {"Location": "http://other/file"}))
                with self.assertRaises(download.DownloadError):
                    client.download("report.zip", "internal")
                self.assertEqual(len(requests), 1)
        client, _ = self.client(lambda req: (206, b"partial", {}))
        with self.assertRaisesRegex(download.DownloadError, "206"):
            client.download("report.zip", "internal")

    def test_rpc_errors_invalid_json_and_session_invalidation(self):
        for result in (b"not json", [], {"jsonrpc": "2.0", "id": 99, "result": {}},
                       {"jsonrpc": "2.0", "id": 1},
                       {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "unsupported"}},
                       {"response": None},
                       {"event": {"method": "sessionInvalidated"}, "response": {}}):
            with self.subTest(result=result):
                client, requests = self.client(lambda req: (200, result, {}))
                with self.assertRaises(download.DownloadError):
                    client.rpc("listStorageFiles")
                self.assertEqual(len(requests), 1)

    def test_login_error_and_invalid_base_url(self):
        with self.assertRaisesRegex(download.DownloadError, "login failed"):
            self.client(lambda req: (200, {"error": "bad password"}, {}), "wrong")
        for url in ("", "file:///tmp", "http://user:pass@device", "http://device?query", "http://device/#fragment"):
            with self.subTest(url=url), self.assertRaises(download.DownloadError):
                download.Client(url)

    def test_requests_use_120_second_timeout(self):
        client = download.Client("http://device")
        response = Mock()
        response.getcode.return_value = 200
        client.opener = Mock()
        client.opener.open.return_value = response
        self.assertIs(client.download("report.zip", "internal"), response)
        self.assertEqual(client.opener.open.call_args[1], {"timeout": 120})


class CLITests(unittest.TestCase):
    def setUp(self):
        env_loader = patch.object(download, "load_env")
        env_loader.start()
        self.addCleanup(env_loader.stop)

    def test_success_forwards_arguments_and_prints_saved_path(self):
        with patch.dict(os.environ, {"PICOKVM_BASE_URL": "http://device", "PICOKVM_PASSWORD": "secret"}), \
                patch.object(download, "Client") as client, \
                patch.object(download, "download_file", return_value=Path("saved.zip")) as transfer, \
                patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(download.main(["report.zip", "--source", "sd", "--output", "saved.zip"]), 0)
        client.assert_called_once_with("http://device", "secret")
        transfer.assert_called_once_with(client.return_value, "report.zip", "sd", "saved.zip")
        self.assertEqual(stdout.getvalue(), "saved.zip\n")

    def test_missing_configuration_and_bad_cli_options_return_2(self):
        cases = [({}, ["report.zip"]),
                 ({"PICOKVM_BASE_URL": "http://device"}, []),
                 ({"PICOKVM_BASE_URL": "http://device"}, ["report.zip", "--source", "other"]),
                 ({"PICOKVM_BASE_URL": "http://device"}, ["dir/report.zip"]),
                 ({"PICOKVM_BASE_URL": "http://device"}, ["--list"])]
        for environment, arguments in cases:
            with self.subTest(arguments=arguments), patch.dict(os.environ, environment, clear=True), \
                    patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as error:
                download.main(arguments)
            self.assertEqual(error.exception.code, 2)

    def test_failure_and_interrupt_return_1_and_130_without_success_output(self):
        for error, code in ((download.DownloadError("offline"), 1), (OSError("disk full"), 1),
                            (urllib.error.URLError("offline"), 1), (IncompleteRead(b"x"), 1),
                            (KeyboardInterrupt(), 130)):
            with self.subTest(error=type(error)), patch.dict(os.environ, {"PICOKVM_BASE_URL": "http://device"}), \
                    patch.object(download, "Client", side_effect=error), \
                    patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                    patch("sys.stderr", new_callable=io.StringIO) as stderr:
                self.assertEqual(download.main(["report.zip"]), code)
                self.assertEqual(stdout.getvalue(), "")
                self.assertTrue(stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
