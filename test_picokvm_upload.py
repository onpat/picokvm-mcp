"""Standard-library tests for the standalone uploader; no device required."""

import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("picokvm_upload", ROOT / "picokvm-upload.py")
upload = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upload)


class FakeClient:
    def __init__(self, partial=0, consume=True):
        self.offset = partial
        self.files = {"disc.iso.incomplete": partial} if partial else {}
        self.calls = []
        self.lengths = []
        self.bodies = []
        self.free = 10**10
        self.consume = consume
        self.serial = 0
        self.active = None

    def rpc(self, method, params=None):
        self.calls.append((method, params))
        if method.startswith("list"):
            return {"files": [{"filename": name, "size": size} for name, size in self.files.items()]}
        if method.startswith("get"):
            return {"bytesFree": self.free}
        if method.startswith("start"):
            self.total = params["size"]
            self.serial += 1
            self.active = "upload_{}".format(self.serial)
            return {"alreadyUploadedBytes": self.offset, "dataChannel": self.active}
        raise AssertionError(method)

    def upload(self, upload_id, blocks, length):
        assert upload_id == self.active
        self.active = None
        self.lengths.append(length)
        if self.consume:
            body = b"".join(blocks)
            assert len(body) == length
            self.bodies.append(body)
        self.offset += length
        if self.offset == self.total:
            self.files = {"disc.iso": self.total}
        else:
            self.files = {"disc.iso.incomplete": self.offset}


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "disc.iso"
        self.path.write_bytes(b"0123456789")
        self.output = io.StringIO()

    def run_upload(self, client, source="internal"):
        upload.upload_file(client, self.path, source,
                           upload.Progress(self.path.stat().st_size, self.output))

    def test_split_and_confirm(self):
        client = FakeClient()
        with patch.object(upload, "CHUNK_SIZE", 4):
            self.run_upload(client)
        self.assertEqual(client.lengths, [4, 4, 2])
        self.assertEqual(b"".join(client.bodies), self.path.read_bytes())
        self.assertEqual(client.serial, 3)
        self.assertIn("Complete", self.output.getvalue())
        self.assertIn("100.00%", self.output.getvalue())
        self.assertEqual(sum(name == "listStorageFiles" for name, _ in client.calls), 4)

    def test_resume_sd(self):
        client = FakeClient(partial=3)
        with patch.object(upload, "CHUNK_SIZE", 4):
            self.run_upload(client, "sd")
        self.assertEqual(b"".join(client.bodies), b"3456789")
        self.assertEqual(client.lengths, [4, 3])
        methods = {name for name, _ in client.calls}
        self.assertEqual(methods, {"listSDStorageFiles", "getSDStorageSpace", "startSDStorageFileUpload"})

    def test_exact_chunk_boundary(self):
        client = FakeClient()
        with patch.object(upload, "CHUNK_SIZE", 5):
            self.run_upload(client)
        self.assertEqual(client.lengths, [5, 5])

    def test_empty_and_fully_received_partial(self):
        for size in (0, 10):
            with self.subTest(size=size):
                self.path.write_bytes(b"x" * size)
                client = FakeClient(partial=size)
                self.run_upload(client)
                self.assertEqual(client.lengths, [0])
                self.assertEqual(client.files, {"disc.iso": size})

    def test_existing_and_oversized_and_no_space(self):
        for kind in ("complete", "oversized", "no_space"):
            with self.subTest(kind=kind):
                client = FakeClient(partial=11 if kind == "oversized" else 0)
                if kind == "complete":
                    client.files = {"disc.iso": 10}
                if kind == "no_space":
                    client.free = 9
                with self.assertRaises(upload.UploadError):
                    self.run_upload(client)
                self.assertEqual(client.serial, 0)

    def test_size_mismatch_never_reports_complete(self):
        client = FakeClient()
        original = client.upload
        def wrong_size(*args):
            original(*args)
            client.files = {"disc.iso": 9}
        client.upload = wrong_size
        with self.assertRaisesRegex(upload.UploadError, "size"):
            self.run_upload(client)
        self.assertNotIn("Complete", self.output.getvalue())

    def test_offset_changed(self):
        client = FakeClient()
        original = client.rpc
        def changed_offset(method, params=None):
            result = original(method, params)
            if method.startswith("start"):
                result["alreadyUploadedBytes"] = 1
            return result
        client.rpc = changed_offset
        with self.assertRaisesRegex(upload.UploadError, "offset changed"):
            self.run_upload(client)
        self.assertFalse(client.lengths)

    def test_no_automatic_retry(self):
        client = FakeClient()
        client.upload = Mock(side_effect=OSError("connection lost"))
        with self.assertRaises(OSError):
            self.run_upload(client)
        self.assertEqual(client.upload.call_count, 1)
        self.assertNotIn("Complete", self.output.getvalue())

    def test_dvd_size_arithmetic_without_reading_gigabytes(self):
        size = 4_700_000_000
        with self.path.open("wb") as file:
            file.truncate(size)  # Sparse local fixture, no payload allocation.
        client = FakeClient(consume=False)
        self.run_upload(client)
        self.assertEqual(sum(client.lengths), size)
        self.assertEqual(len(client.lengths), 18)
        self.assertLessEqual(max(client.lengths), 256 * 1024 * 1024)
        starts = [params for name, params in client.calls if name.startswith("start")]
        self.assertTrue(all(params["size"] == size for params in starts))

    def test_reader_is_lazy_and_bounded(self):
        file = Mock()
        file.read.side_effect = lambda n: b"x" * n
        blocks = upload.file_blocks(file, 4_500_000_000, upload.CHUNK_SIZE, Mock())
        self.assertEqual(file.read.call_count, 0)
        for _ in range(3):
            self.assertEqual(len(next(blocks)), upload.READ_SIZE)
        blocks.close()
        self.assertEqual(file.read.call_count, 3)
        file.seek.assert_called_once_with(4_500_000_000)
        self.assertTrue(all(call[0][0] <= 64 * 1024 for call in file.read.call_args_list))

    def test_truncated_reader(self):
        with self.assertRaisesRegex(upload.UploadError, "shorter"):
            list(upload.file_blocks(io.BytesIO(b"x"), 0, 2, Mock()))

    def test_finalization_poll_and_timeout(self):
        storage = upload.Storage(Mock(), "internal")
        storage.files = Mock(side_effect=[{"disc.iso.incomplete": 10}, {"disc.iso": 10}])
        with patch.object(upload.time, "sleep"):
            storage.confirm("disc.iso", 10, 10)
        storage.files = Mock(return_value={"disc.iso.incomplete": 10})
        with patch.object(upload.time, "monotonic", side_effect=[0, 6]):
            with self.assertRaisesRegex(upload.UploadError, "5 seconds"):
                storage.confirm("disc.iso", 10, 10)

    def test_main_errors(self):
        with patch.dict(os.environ, {"PICOKVM_BASE_URL": ""}), patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                upload.main([str(self.path)])
        self.assertEqual(error.exception.code, 2)
        for exception, code in [(KeyboardInterrupt(), 130), (OSError("offline"), 1)]:
            with patch.dict(os.environ, {"PICOKVM_BASE_URL": "http://device"}), \
                    patch.object(upload, "Client", side_effect=exception), \
                    patch("sys.stderr", io.StringIO()):
                self.assertEqual(upload.main([str(self.path)]), code)


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.received = []
        # Exercise urllib/http.client's real cookie handling and wire encoding,
        # replacing only the socket so these tests also work without networking.
        class WireSocket:
            def __init__(self):
                self.wire = bytearray()

            def sendall(self, data):
                self.wire.extend(data)

            def close(self):
                pass

            def makefile(self, mode):
                wire = io.BytesIO(self.wire)
                path = wire.readline().decode().split()[1]
                headers = http.client.parse_headers(wire)
                body = wire.read()
                assert len(body) == int(headers["Content-Length"])
                cls.received.append((path, headers, body))
                status = 200
                extra = b""
                if path == "/auth/login-local":
                    extra = b"Set-Cookie: authToken=test; Path=/; HttpOnly\r\n"
                    result = {"message": "Login successful"}
                elif headers.get("Cookie") != "authToken=test":
                    status = 401
                    result = {"error": "Unauthorized"}
                elif path == "/api/rpc":
                    request = json.loads(body)
                    if request["method"] == "fail":
                        result = {"id": request["id"], "error": {"message": "failure"}}
                    else:
                        result = {"response": {"id": request["id"], "result": {}}, "event": {}}
                else:
                    result = {"message": "Upload completed"}
                payload = json.dumps(result).encode()
                return io.BytesIO("HTTP/1.1 {} Result\r\nContent-Length: {}\r\n".format(
                    status, len(payload)).encode() + extra + b"\r\n" + payload)

        def connect(connection):
            connection.sock = WireSocket()

        cls.patcher = patch.object(http.client.HTTPConnection, "connect", connect)
        cls.patcher.start()
        cls.url = "http://127.0.0.1:12345"

    @classmethod
    def tearDownClass(cls):
        cls.patcher.stop()

    def test_cookie_session_and_streaming_content_length(self):
        client = upload.Client(self.url, "secret")
        self.assertEqual(client.rpc("ping"), {})
        client.upload("upload_123", iter([b"abc", b"def"]), 6)
        login, rpc, data = self.received[-3:]
        self.assertEqual(json.loads(login[2]), {"password": "secret"})
        self.assertEqual(data[2], b"abcdef")
        self.assertEqual(data[1]["Content-Length"], "6")
        self.assertIsNone(data[1].get("Transfer-Encoding"))
        self.assertEqual(data[1]["Content-Type"], "application/octet-stream")
        self.assertEqual(data[1]["X-Session-ID"], rpc[1]["X-Session-ID"])

    def test_http_and_rpc_errors(self):
        with self.assertRaisesRegex(upload.UploadError, "401"):
            upload.Client(self.url).rpc("ping")
        client = upload.Client(self.url, "secret")
        with self.assertRaisesRegex(upload.UploadError, "failure"):
            client.rpc("fail")


if __name__ == "__main__":
    unittest.main()
