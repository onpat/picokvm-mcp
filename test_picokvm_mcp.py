"""Offline protocol and HID report tests; no PicoKVM or listening sockets needed."""

from email.message import Message
from http.client import IncompleteRead, RemoteDisconnected
import importlib.util
import io
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request
import urllib.response


ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("picokvm_mcp", ROOT / "picokvm-mcp.py")
bridge_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge_module)


# PicoKVM's getDefaultEDID fixture, including its original audio/video CTA block.
DEFAULT_EDID = (
    "00ffffffffffff0052620188008888881c150103800000780a0dc9a05747982712484c00"
    "000001010101010101010101010101010101023a801871382d40582c4500c48e2100001e"
    "011d007251d01e206e285500c48e2100001e000000fc00543734392d6648443732300a20"
    "000000fd00147801ff1d000a202020202020017b02031a71479004020111220523090701"
    "8301000065030c0010008c0ad08a20e02d10103e9600138e2100001ed80980a020e02d10"
    "1060a200c48e210000188c0ad090204031200c405500483900000018011d801871382d40"
    "582c4500c06c00000018011d8018711c1620582c2500c06c000000180000000000000000"
    "00000032"
)


class EDIDTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.rpc.side_effect = lambda method, *args: DEFAULT_EDID if method == "getDefaultEDID" else None
        self.bridge = bridge_module.MCPBridge(self.client)

    def set_resolution(self, width=1024, height=768, refresh_rate=60):
        result = self.bridge.invoke("kvm_set_screen_resolution", {
            "width": width, "height": height, "refresh_rate": refresh_rate})
        self.assertNotIn("isError", result, result)
        self.assertEqual(self.client.rpc.call_args_list[0][0], ("getDefaultEDID",))
        call_args = self.client.rpc.call_args_list[1][0]
        self.assertEqual(call_args[0], "setEDID")
        return bytes.fromhex(call_args[1]["edid"]), json.loads(result["content"][0]["text"])

    def test_1024_timing_and_checksums(self):
        edid, result = self.set_resolution()
        self.assertEqual(len(edid), 256)
        self.assertEqual(edid[:8], bytes.fromhex("00ffffffffffff00"))
        self.assertEqual(edid[126], 1)
        self.assertEqual(sum(edid[:128]) % 256, 0)
        self.assertEqual(sum(edid[128:]) % 256, 0)
        # Independent DTD fixture: 65MHz, 1024+320 x 768+38, -HSync/-VSync.
        self.assertEqual(edid[54:66].hex(), "641900404100263018883600")
        self.assertEqual(edid[71], 0x18)
        self.assertTrue(edid[24] & 2)
        self.assertTrue(result["edid_set"])
        self.assertEqual((result["width"], result["height"], result["refresh_rate"]), (1024, 768, 60))
        self.assertIn("target OS", result["message"])

    def test_all_supported_modes_decode_to_requested_dimensions(self):
        for width, height in [(640, 480), (800, 600), (1024, 768),
                              (1280, 720), (1280, 1024), (1920, 1080)]:
            with self.subTest(width=width, height=height):
                self.client.reset_mock()
                edid, _ = self.set_resolution(width, height)
                dtd = edid[54:72]
                active_x = dtd[2] | (dtd[4] & 0xF0) << 4
                active_y = dtd[5] | (dtd[7] & 0xF0) << 4
                blank_x = dtd[3] | (dtd[4] & 15) << 8
                blank_y = dtd[6] | (dtd[7] & 15) << 8
                rate = int.from_bytes(dtd[:2], "little") * 10000 / ((active_x + blank_x) * (active_y + blank_y))
                self.assertEqual((active_x, active_y), (width, height))
                self.assertAlmostEqual(rate, 60, delta=0.4)
                self.assertEqual(sum(edid[:128]) % 256, 0)
                self.assertEqual(sum(edid[128:]) % 256, 0)

    def test_other_modes_removed_and_audio_preserved(self):
        edid, _ = self.set_resolution()
        self.assertEqual(edid[35:38], bytes(3))
        self.assertEqual(edid[38:54], b"\x01\x01" * 8)
        self.assertEqual(edid[90:126], (b"\0\0\0\x10\0" + bytes(13)) * 2)
        extension = edid[128:]
        self.assertEqual(extension[:4], bytes.fromhex("02031240"))
        self.assertEqual(extension[4:18].hex(), "230907018301000065030c001000")
        self.assertEqual(extension[18:127], bytes(109))

    def test_unsupported_modes_fail_without_rpc(self):
        for args in [{"width": 1024, "height": 720}, {"width": 1024, "height": 768, "refresh_rate": 75},
                     {"width": True, "height": 768}, {"width": 1024},
                     {"width": 1920, "height": 1200}, {"width": 1024.0, "height": 768}]:
            with self.subTest(args=args):
                self.assertTrue(self.bridge.invoke("kvm_set_screen_resolution", args)["isError"])
                self.client.rpc.assert_not_called()

    def test_invalid_template_prevents_write(self):
        invalid_checksum = bytearray.fromhex(DEFAULT_EDID)
        invalid_checksum[127] ^= 1
        invalid_cta = bytearray.fromhex(DEFAULT_EDID)
        invalid_cta[130] = 3
        invalid_cta[255] = -sum(invalid_cta[128:255]) & 255
        for template in [None, "not hex", "00", DEFAULT_EDID[:256],
                         invalid_checksum.hex(), invalid_cta.hex()]:
            with self.subTest(template=str(template)[:20]):
                self.client.reset_mock()
                self.client.rpc.side_effect = None
                self.client.rpc.return_value = template
                result = self.bridge.invoke("kvm_set_screen_resolution", {"width": 1024, "height": 768})
                self.assertTrue(result["isError"])
                self.assertEqual(self.client.rpc.call_count, 1)

    def test_rpc_errors_report_failure_and_do_not_retry(self):
        for effects in [[bridge_module.ToolError("read failed")],
                        [DEFAULT_EDID, bridge_module.ToolError("write failed")]]:
            with self.subTest(effects=effects):
                self.client.reset_mock()
                self.client.rpc.side_effect = effects
                result = self.bridge.invoke("kvm_set_screen_resolution", {"width": 1024, "height": 768})
                self.assertTrue(result["isError"])
                self.assertEqual(self.client.rpc.call_count, len(effects))

    def test_autoset_defaults_on_and_accepts_explicit_enabled_values(self):
        for setting in [None, "1", "true", "YES", " on "]:
            with self.subTest(setting=setting):
                self.client.reset_mock()
                with patch.dict(os.environ, {} if setting is None else {"EDID_AUTOSET": setting}, clear=True):
                    result = self.bridge.autoset_edid()
                self.assertEqual((result["width"], result["height"], result["refresh_rate"]), (1024, 768, 60))
                self.assertEqual(self.client.rpc.call_count, 2)

    def test_autoset_disabled_does_not_contact_device(self):
        for setting in ["0", "false", "NO", " off "]:
            with self.subTest(setting=setting):
                with patch.dict(os.environ, {"EDID_AUTOSET": setting}):
                    self.assertIsNone(self.bridge.autoset_edid())
                self.client.rpc.assert_not_called()
        with patch.dict(os.environ, {"EDID_AUTOSET": "invalid"}):
            with self.assertRaisesRegex(bridge_module.ToolError, "EDID_AUTOSET"):
                self.bridge.autoset_edid()
        self.client.rpc.assert_not_called()

    def test_run_applies_once_before_serving_and_closes_server(self):
        server = Mock()
        server.serve_forever.side_effect = KeyboardInterrupt
        with patch.object(bridge_module, "ThreadingHTTPServer", return_value=server), \
                patch.object(bridge_module, "MCPBridge", return_value=self.bridge), \
                patch.dict(os.environ, {}, clear=True), patch("sys.stdout", new_callable=io.StringIO):
            server.serve_forever.side_effect = lambda: self.assertEqual(self.client.rpc.call_count, 2)
            bridge_module.run()
        self.assertEqual(self.client.rpc.call_count, 2)
        server.serve_forever.assert_called_once()
        server.server_close.assert_called_once()

    def test_run_reports_startup_failure_and_continues_serving(self):
        server = Mock()
        self.client.rpc.side_effect = bridge_module.ToolError("unavailable")
        with patch.object(bridge_module, "ThreadingHTTPServer", return_value=server), \
                patch.object(bridge_module, "MCPBridge", return_value=self.bridge), \
                patch.dict(os.environ, {}, clear=True), patch("sys.stdout", new_callable=io.StringIO), \
                patch("sys.stderr", new_callable=io.StringIO) as stderr:
            bridge_module.run()
        self.assertIn("EDID_AUTOSET failed", stderr.getvalue())
        server.serve_forever.assert_called_once()
        server.server_close.assert_called_once()


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.rpc.return_value = None
        self.bridge = bridge_module.MCPBridge(self.client)
        self.sleep = patch.object(bridge_module.time, "sleep").start()
        self.addCleanup(patch.stopall)

    def invoke(self, name, args=None):
        result = self.bridge.invoke(name, {} if args is None else args)
        self.assertNotIn("isError", result, result)
        return json.loads(result["content"][0]["text"])

    def error(self, name, args, message=None):
        result = self.bridge.invoke(name, args)
        self.assertTrue(result.get("isError"), result)
        if message:
            self.assertIn(message, result["content"][0]["text"])

    def reports(self):
        return [(call[0][0], call[0][1] if len(call[0]) > 1 else {})
                for call in self.client.rpc.call_args_list]

    def test_absolute_pixels_endpoints_and_rounding(self):
        self.client.rpc.return_value = {"width": 1920, "height": 1080}
        for x, y in [(0, 0), (1919, 1079), (960, 540)]:
            with self.subTest(x=x, y=y):
                self.client.reset_mock()
                self.invoke("kvm_mouse_move", {"x": x, "y": y})
                self.assertEqual(self.reports(), [
                    ("getVideoState", {}),
                    ("absMouseReport", {"x": (x * 32767 + 959) // 1919,
                                        "y": (y * 32767 + 539) // 1079, "buttons": 0}),
                ])

    def test_absolute_rejects_unknown_resolution_and_outside_screen(self):
        for state, position in [({"width": 0, "height": 1080}, {"x": 0, "y": 0}),
                                ({"width": 1, "height": 1}, {"x": 0, "y": 0}),
                                ({"width": 1920, "height": 1080}, {"x": 1920, "y": 0}),
                                ({"width": 1920, "height": 1080}, {"x": 0, "y": 1080}),
                                (None, {"x": 0, "y": 0})]:
            with self.subTest(state=state, position=position):
                self.client.reset_mock()
                self.client.rpc.return_value = state
                self.error("kvm_mouse_move", position)
                self.assertEqual(self.reports(), [("getVideoState", {})])

    def test_relative_move_splits_without_losing_distance(self):
        self.invoke("kvm_mouse_move", {"mode": "relative", "dx": 300, "dy": -130})
        self.assertEqual(self.reports(), [
            ("relMouseReport", {"dx": 127, "dy": -127, "buttons": 0}),
            ("relMouseReport", {"dx": 127, "dy": -3, "buttons": 0}),
            ("relMouseReport", {"dx": 46, "dy": 0, "buttons": 0}),
        ])
        self.client.reset_mock()
        self.invoke("kvm_mouse_move", {"mode": "relative", "dx": 0, "dy": 0})
        self.client.rpc.assert_not_called()

    def test_all_relative_clicks_press_then_release_in_place(self):
        for button, bit in [("left", 1), ("right", 2), ("middle", 4)]:
            with self.subTest(button=button):
                self.client.reset_mock()
                self.invoke("kvm_mouse_click", {"button": button})
                self.assertEqual(self.reports(), [
                    ("relMouseReport", {"dx": 0, "dy": 0, "buttons": bit}),
                    ("relMouseReport", {"dx": 0, "dy": 0, "buttons": 0}),
                ])
                self.sleep.assert_called_with(0.015)

    def test_absolute_click_uses_same_position_for_release(self):
        self.client.rpc.side_effect = [{"width": 1920, "height": 1080}, None, None]
        self.invoke("kvm_mouse_click", {"button": "right", "x": 1919, "y": 1079})
        self.assertEqual(self.reports()[1:], [
            ("absMouseReport", {"x": 32767, "y": 32767, "buttons": 2}),
            ("absMouseReport", {"x": 32767, "y": 32767, "buttons": 0}),
        ])

    def test_wheel_both_axes_and_defaults(self):
        self.invoke("kvm_mouse_wheel", {"wheel_y": -127, "wheel_x": 127})
        self.invoke("kvm_mouse_wheel")
        self.assertEqual(self.reports(), [
            ("wheelReport", {"wheelY": -127, "wheelX": 127}),
            ("wheelReport", {"wheelY": 0, "wheelX": 0}),
        ])

    def test_single_named_numeric_and_modifier_keys(self):
        for key, modifier, codes in [("eNtEr", 0, [0x28]), (0x52, 0, [0x52]),
                                     ("CtrlRight", 0x10, []), (0xE5, 0x20, []),
                                     ("+", 2, [0x2E]), ("ArrowLeft", 0, [0x50])]:
            with self.subTest(key=key):
                self.client.reset_mock()
                self.invoke("kvm_send_key", {"key": key})
                self.assertEqual(self.reports(), [
                    ("keyboardReport", {"modifier": modifier, "keys": codes}),
                    ("keyboardReport", {"modifier": 0, "keys": []}),
                ])

    def test_combination_and_six_key_limit(self):
        self.invoke("kvm_send_key_combination", {"keys": ["ctrl", "alt", "Delete"]})
        self.assertEqual(self.reports()[0], ("keyboardReport", {"modifier": 5, "keys": [0x4C]}))
        self.client.reset_mock()
        self.invoke("kvm_send_key_combination", {"keys": ["CtrlRight", "+"]})
        self.assertEqual(self.reports()[0], ("keyboardReport", {"modifier": 0x12, "keys": [0x2E]}))
        self.client.reset_mock()
        self.invoke("kvm_send_key_combination", {"keys": ["ctrl", "a", "b", "c", "d", "e", "f"]})
        self.assertEqual(len(self.reports()[0][1]["keys"]), 6)
        self.client.reset_mock()
        self.error("kvm_send_key_combination", {"keys": list("abcdefg")}, "six")
        self.client.rpc.assert_not_called()

    def test_text_ascii_case_punctuation_newline_tab(self):
        self.invoke("kvm_type_text", {"text": "aA!>\n\t\r"})
        pressed = [(params["modifier"], params["keys"]) for _, params in self.reports()[::2]]
        self.assertEqual(pressed, [(0, [4]), (2, [4]), (2, [0x1E]), (2, [0x37]),
                                   (0, [0x28]), (0, [0x2B]), (0, [0x28])])
        self.assertEqual(self.sleep.call_count, 7)
        for _, report in self.reports()[1::2]:
            self.assertEqual(report, {"modifier": 0, "keys": []})

    def test_all_printable_ascii_is_supported(self):
        result = self.invoke("kvm_type_text", {"text": "".join(chr(i) for i in range(32, 127))})
        self.assertEqual(result["characters"], 95)
        # Independent spot checks for US punctuation, not just table completeness.
        self.assertEqual(self.reports()[2 * (ord('@') - 32)][1], {"modifier": 2, "keys": [0x1F]})
        self.assertEqual(self.reports()[2 * (ord('\\') - 32)][1], {"modifier": 0, "keys": [0x31]})

    def test_text_validated_in_full_before_typing(self):
        for text in ["abc日本語", "abc\x00", "abc\x7f", "a" * 1001]:
            with self.subTest(text=text[:12]):
                self.error("kvm_type_text", {"text": text})
                self.client.rpc.assert_not_called()
        self.invoke("kvm_type_text", {"text": ""})
        self.client.rpc.assert_not_called()

    def test_release_after_press_failure_and_report_both_failures(self):
        for name, args in [("kvm_send_key", {"key": "Enter"}),
                           ("kvm_mouse_click", {"button": "left"})]:
            with self.subTest(name=name):
                self.client.reset_mock()
                self.client.rpc.side_effect = [bridge_module.ToolError("press failed"), None]
                self.error(name, args, "press failed")
                self.assertEqual(len(self.reports()), 2)
                self.assertEqual(list(self.reports()[1][1].values())[-1], [] if name == "kvm_send_key" else 0)
                self.client.reset_mock()
                self.client.rpc.side_effect = [bridge_module.ToolError("press failed"), bridge_module.ToolError("release failed")]
                self.error(name, args, "release also failed")
                self.assertEqual(len(self.reports()), 2)

    def test_release_failure_stops_text_and_is_not_retried(self):
        self.client.rpc.side_effect = [None, bridge_module.ToolError("release failed")]
        self.error("kvm_type_text", {"text": "abc"}, "release failed")
        self.assertEqual(len(self.reports()), 2)

    def test_invalid_arguments_are_rejected_before_network(self):
        cases = [
            ("missing", {}), ("kvm_send_key", {"key": "UnknownKey"}),
            ("kvm_send_key", {"key": True}), ("kvm_send_key", {"key": 0}),
            ("kvm_send_key", {"key": 232}), ("kvm_send_key", {"key": 4.0}),
            ("kvm_send_key", {"key": ""}), ("kvm_send_key", {}),
            ("kvm_send_key_combination", {"keys": []}),
            ("kvm_send_key_combination", {"keys": ["ctrl", "nope"]}),
            ("kvm_mouse_move", {}), ("kvm_mouse_move", {"x": 0}),
            ("kvm_mouse_move", {"mode": "relative", "dx": 0}),
            ("kvm_mouse_move", {"x": 0, "y": 0, "dx": 1}),
            ("kvm_mouse_move", {"mode": "relative", "dx": 2048, "dy": 0}),
            ("kvm_mouse_move", {"mode": "relative", "dx": -2048, "dy": 0}),
            ("kvm_mouse_move", {"mode": "bad", "x": 0, "y": 0}),
            ("kvm_mouse_move", {"x": -1, "y": 0}),
            ("kvm_mouse_move", {"x": True, "y": 0}),
            ("kvm_mouse_click", {"button": "left", "x": 0}),
            ("kvm_mouse_click", {"button": "bad"}),
            ("kvm_mouse_wheel", {"wheel_y": 128}),
            ("kvm_mouse_wheel", {"wheel_x": -128}),
            ("kvm_list_storage_files", {"source": "bad"}),
            ("kvm_mount_iso", {"filename": "../test.iso"}),
            ("kvm_mount_iso", {"filename": "/test.iso"}),
            ("kvm_mount_iso", {"filename": "a\\test.iso"}),
            ("kvm_mount_iso", {"filename": "test\x00.iso"}),
            ("kvm_mount_iso", {"filename": ""}),
            ("kvm_get_status", {"unknown": 1}),
            ("kvm_get_status", None), ("kvm_get_status", []),
        ]
        for name, args in cases:
            with self.subTest(name=name, args=args):
                self.error(name, args)
                self.client.rpc.assert_not_called()

    def test_status_and_storage_mapping_preserve_results(self):
        self.client.rpc.side_effect = [{"width": 1920, "height": 1080}, "configured"]
        self.assertEqual(self.invoke("kvm_get_status"),
                         {"video": {"width": 1920, "height": 1080}, "usb": "configured"})
        self.assertEqual(self.reports(), [("getVideoState", {}), ("getUSBState", {})])
        self.client.rpc.side_effect = None
        files = {"files": [{"filename": "test.iso", "size": 4096, "createdAt": "2026-11-11T00:00:00Z"}]}
        self.client.rpc.return_value = files
        for source, listing, mount in [("storage", "listStorageFiles", "mountWithStorage"),
                                       ("sd", "listSDStorageFiles", "mountWithSDStorage")]:
            with self.subTest(source=source):
                self.client.reset_mock()
                args = {} if source == "storage" else {"source": source}
                self.assertEqual(self.invoke("kvm_list_storage_files", args), files)
                self.invoke("kvm_mount_iso", dict(args, filename="test.iso"))
                self.invoke("kvm_unmount_iso")
                self.assertEqual(self.reports(), [(listing, {}),
                    (mount, {"filename": "test.iso", "mode": "CDROM"}), ("unmountImage", {})])

    def test_input_calls_are_serialized_across_bridge_instances(self):
        entered, unblock, second_started, second_entered = [threading.Event() for _ in range(4)]
        first_client, second_client = Mock(), Mock()

        def first_rpc(*args):
            entered.set()
            if not unblock.wait(2):
                raise bridge_module.ToolError("test timed out")

        first_client.rpc.side_effect = first_rpc
        second_client.rpc.side_effect = lambda *args: second_entered.set()
        first = threading.Thread(target=bridge_module.MCPBridge(first_client).invoke,
                                 args=("kvm_send_key", {"key": "a"}))

        def second_call():
            second_started.set()
            bridge_module.MCPBridge(second_client).invoke("kvm_send_key", {"key": "b"})

        second = threading.Thread(target=second_call)
        first.start()
        try:
            self.assertTrue(entered.wait(2))
            second.start()
            self.assertTrue(second_started.wait(2))
            self.assertFalse(second_entered.wait(0.05))
        finally:
            unblock.set()
            first.join(2)
            if second.ident is not None:
                second.join(2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertTrue(second_entered.is_set())


class FakeHTTPHandler(urllib.request.HTTPHandler):
    """Keep urllib's CookieJar and status processing, replacing only network I/O."""
    def __init__(self, responder):
        super().__init__()
        self.responder = responder
        self.requests = []

    def http_open(self, request):
        self.requests.append(request)
        code, payload, headers = self.responder(request)
        info = Message()
        for key, value in headers.items():
            info[key] = value
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        response = urllib.response.addinfourl(io.BytesIO(body), info, request.full_url, code)
        response.msg = "OK" if code == 200 else "Error"
        return response


class ClientTests(unittest.TestCase):
    def make_client(self, responder, password=None):
        with patch.dict(os.environ, {}, clear=True):
            client = bridge_module.PicoKVMClient("http://picokvm.test/", password)
        handler = FakeHTTPHandler(responder)
        client.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), handler, urllib.request.HTTPCookieProcessor(client.cookies))
        return client, handler.requests

    @staticmethod
    def success(request, result="pong"):
        body = json.loads(request.data)
        return 200, {"jsonrpc": "2.0", "id": body["id"], "result": result}, {}

    def test_defaults_and_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            client = bridge_module.PicoKVMClient()
            self.assertEqual(client.base_url, "http://127.0.0.1")
            self.assertIsNone(client.password)
        with patch.dict(os.environ, {"PICOKVM_BASE_URL": "http://device:8080/", "PICOKVM_PASSWORD": "secret"}):
            client = bridge_module.PicoKVMClient()
            self.assertEqual(client.base_url, "http://device:8080")
            self.assertEqual(client.password, "secret")

    def test_login_cookie_retained_and_session_id_stable(self):
        def responder(request):
            if request.full_url.endswith("/auth/login-local"):
                self.assertEqual(json.loads(request.data), {"password": "secret"})
                return 200, {"message": "Login successful"}, {"Set-Cookie": "authToken=session-token; Path=/; HttpOnly"}
            self.assertEqual(request.get_header("Cookie"), "authToken=session-token")
            return self.success(request)

        client, requests = self.make_client(responder, "secret")
        self.assertEqual(client.rpc("ping"), "pong")
        client.rpc("getUSBState")
        self.assertEqual(len(requests), 3)
        self.assertEqual([req.full_url for req in requests], ["http://picokvm.test/auth/login-local",
                          "http://picokvm.test/api/rpc", "http://picokvm.test/api/rpc"])
        self.assertEqual({req.get_header("X-session-id") for req in requests}, {client.session_id})
        self.assertEqual([json.loads(req.data)["id"] for req in requests[1:]], [1, 2])
        self.assertTrue(all(req.get_method() == "POST" for req in requests))

    def test_no_password_skips_login_and_preserves_request_parameters(self):
        client, requests = self.make_client(self.success)
        client.rpc("wheelReport", {"wheelY": 1, "wheelX": 0})
        self.assertEqual(len(requests), 1)
        self.assertEqual(json.loads(requests[0].data), {"jsonrpc": "2.0", "id": 1,
                         "method": "wheelReport", "params": {"wheelY": 1, "wheelX": 0}})

    def test_wrapped_and_void_success_responses(self):
        def responder(request):
            response = {"jsonrpc": "2.0", "id": json.loads(request.data)["id"]}
            return 200, {"response": response, "event": {"method": "otherSessionConnected"}}, {}
        client, _ = self.make_client(responder)
        self.assertIsNone(client.rpc("unmountImage"))

    def test_rpc_error_and_session_invalidation_are_not_retried(self):
        for wrapped, code in [(False, -32603), (True, -32603), (True, -32001), (False, -32001)]:
            with self.subTest(wrapped=wrapped, code=code):
                def responder(request):
                    response = {"jsonrpc": "2.0", "id": json.loads(request.data)["id"],
                                "error": {"code": code, "message": "failure", "data": "details"}}
                    return 200, {"response": response} if wrapped else response, {}
                client, requests = self.make_client(responder)
                with self.assertRaisesRegex(bridge_module.ToolError, "details"):
                    client.rpc("mountWithStorage", {"filename": "test.iso", "mode": "CDROM"})
                self.assertEqual(len(requests), 1)
                if code == -32001:
                    with self.assertRaisesRegex(bridge_module.ToolError, "invalidated"):
                        client.rpc("ping")
                    self.assertEqual(len(requests), 1)

    def test_session_invalidated_event_is_an_error(self):
        def responder(request):
            _, response, _ = self.success(request)
            return 200, {"response": response, "event": {"method": "sessionInvalidated"}}, {}
        client, _ = self.make_client(responder)
        with self.assertRaisesRegex(bridge_module.ToolError, "invalidated"):
            client.rpc("ping")

    def test_auth_failure_prevents_operation(self):
        client, requests = self.make_client(lambda request: (401, {"error": "Invalid password"}, {}), "wrong")
        result = bridge_module.MCPBridge(client).invoke("kvm_mount_iso", {"filename": "test.iso"})
        self.assertTrue(result["isError"])
        self.assertIn("401", result["content"][0]["text"])
        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0].full_url.endswith("/auth/login-local"))

    def test_http_error_does_not_replay_operation(self):
        client, requests = self.make_client(lambda request: (500, {"error": "failure"}, {}))
        result = bridge_module.MCPBridge(client).invoke("kvm_unmount_iso", {})
        self.assertTrue(result["isError"])
        self.assertEqual(len(requests), 1)

    def test_expired_login_reauthenticates_only_on_next_call(self):
        fail = [True]
        def responder(request):
            if request.full_url.endswith("/auth/login-local"):
                return 200, {"message": "Login successful"}, {"Set-Cookie": "authToken=new; Path=/"}
            if fail[0]:
                fail[0] = False
                return 401, {"error": "Unauthorized"}, {}
            return self.success(request)
        client, requests = self.make_client(responder, "secret")
        with self.assertRaises(bridge_module.ToolError):
            client.rpc("unmountImage")
        self.assertEqual(len(requests), 2)
        self.assertEqual(client.rpc("ping"), "pong")
        self.assertEqual(len(requests), 4)
        self.assertEqual(json.loads(requests[1].data)["method"], "unmountImage")
        self.assertEqual(json.loads(requests[3].data)["method"], "ping")

    def test_malformed_json_response_and_mismatched_id(self):
        for payload in [b"<html>not JSON</html>", [], {"response": []},
                        {"jsonrpc": "2.0", "id": 999}, {"id": 1}]:
            with self.subTest(payload=payload):
                client, requests = self.make_client(lambda request: (200, payload, {}))
                result = bridge_module.MCPBridge(client).invoke("kvm_unmount_iso", {})
                self.assertTrue(result["isError"])
                self.assertEqual(len(requests), 1)

    def test_connection_and_timeout_errors(self):
        for error in [urllib.error.URLError("offline"), TimeoutError("timed out"),
                      IncompleteRead(b"partial", 100), RemoteDisconnected("disconnected")]:
            with self.subTest(error=error):
                client, _ = self.make_client(self.success)
                client.opener.open = Mock(side_effect=error)
                result = bridge_module.MCPBridge(client).invoke("kvm_unmount_iso", {})
                self.assertTrue(result["isError"])
                client.opener.open.assert_called_once()


class MemoryConnection:
    def __init__(self, request):
        self.input = io.BytesIO(request)
        self.output = io.BytesIO()

    def makefile(self, *args):
        return self.input

    def sendall(self, data):
        self.output.write(data)


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.rpc.return_value = {"files": []}
        self.server = SimpleNamespace(bridge=bridge_module.MCPBridge(self.client))

    def request(self, method, path, payload=None, body=None, content_length=None):
        if body is None:
            body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        connection = MemoryConnection(
            ("{} {} HTTP/1.0\r\nContent-Length: {}\r\n\r\n".format(
                method, path, len(body) if content_length is None else content_length)).encode() + body)
        with patch.object(bridge_module.MCPBridgeHandler, "log_message"):
            bridge_module.MCPBridgeHandler(connection, ("127.0.0.1", 12345), self.server)
        raw_headers, body = connection.output.getvalue().split(b"\r\n\r\n", 1)
        lines = raw_headers.decode().split("\r\n")
        headers = dict(line.split(": ", 1) for line in lines[1:])
        return int(lines[0].split()[1]), headers, body

    def rpc(self, method, params=None):
        status, headers, body = self.request("POST", "/mcp", {
            "jsonrpc": "2.0", "id": 42, "method": method, "params": {} if params is None else params})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(int(headers["Content-Length"]), len(body))
        response = json.loads(body)
        self.assertEqual(response["id"], 42)
        return response

    def test_initialize_and_list_publish_only_supported_tools(self):
        self.assertEqual(self.rpc("initialize")["result"]["capabilities"], {"tools": {}})
        names = {tool["name"] for tool in self.rpc("tools/list")["result"]["tools"]}
        self.assertEqual(names, {"kvm_mouse_move", "kvm_mouse_click", "kvm_mouse_wheel", "kvm_send_key",
                                "kvm_send_key_combination", "kvm_type_text", "kvm_get_status",
                                "kvm_list_storage_files", "kvm_mount_iso", "kvm_unmount_iso",
                                "kvm_set_screen_resolution"})
        self.assertEqual(self.rpc("ping")["result"], {})
        self.assertEqual(self.rpc("unknown")["error"]["code"], -32601)

    def test_tools_call_success_and_errors(self):
        result = self.rpc("tools/call", {"name": "kvm_list_storage_files"})["result"]
        self.assertEqual(json.loads(result["content"][0]["text"]), {"files": []})
        for args in [{"name": "kvm_send_key", "arguments": {"key": True}},
                     {"name": "screenshot"}, {"name": []},
                     {"name": "kvm_get_status", "arguments": None}]:
            self.assertTrue(self.rpc("tools/call", args)["result"]["isError"])
        self.assertEqual(self.rpc("tools/call", [1])["error"]["code"], -32602)

    def test_notification_acknowledgment(self):
        status, headers, body = self.request("POST", "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(status, 202)
        self.assertEqual(headers["Content-Length"], "0")
        self.assertEqual(body, b"")

    def test_get_readme_independent_of_working_directory(self):
        expected = (ROOT / "README.md").read_bytes()
        current = os.getcwd()
        try:
            os.chdir("/tmp")
            for path in ["/", "/?diagnostic=1"]:
                status, headers, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(body, expected)
                self.assertEqual(headers["Content-Type"], "text/markdown; charset=utf-8")
                self.assertEqual(int(headers["Content-Length"]), len(expected))
        finally:
            os.chdir(current)
        with patch.object(bridge_module.Path, "read_bytes", side_effect=OSError):
            self.assertEqual(self.request("GET", "/")[0], 500)

    def test_http_paths_and_invalid_requests(self):
        for path in ["/mcp", "/README.md", "/missing"]:
            self.assertEqual(self.request("GET", path)[0], 404)
        self.assertEqual(self.request("POST", "/")[0], 404)
        for body, length in [(b"{", None), (b"{}", "bad"), (b"{}", -1), (b"\xff", None)]:
            self.assertEqual(self.request("POST", "/mcp", body=body, content_length=length)[0], 400)
        status, _, body = self.request("POST", "/mcp", payload=[])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["error"]["code"], -32600)


if __name__ == "__main__":
    unittest.main()
