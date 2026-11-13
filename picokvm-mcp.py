from contextlib import nullcontext
from http.client import HTTPException
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import binascii
import itertools
import json
import os
from pathlib import Path
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from picokvm_env import EnvError, load_env


REPORT_GAP = 0.015
INPUT_LOCK = threading.Lock()


class ToolError(Exception):
    pass


# Pixel clock (kHz), H sync start/end/total,
# V sync start/end/total, and EDID separate-sync polarity flags.
# Use standard DMT/CTA timings where encodable in a DTD; other 50/30Hz
# modes retain the 60Hz blanking and adjust the pixel clock.
EDID_TIMINGS = {
    (640, 480, 60): (25175, 656, 752, 800, 490, 492, 525, 0x18),
    (640, 480, 50): (21000, 656, 752, 800, 490, 492, 525, 0x18),
    (640, 480, 30): (12600, 656, 752, 800, 490, 492, 525, 0x18),
    (800, 600, 60): (40000, 840, 968, 1056, 601, 605, 628, 0x1E),
    (800, 600, 50): (33158, 840, 968, 1056, 601, 605, 628, 0x1E),
    (800, 600, 30): (19895, 840, 968, 1056, 601, 605, 628, 0x1E),
    (1024, 768, 60): (65000, 1048, 1184, 1344, 771, 777, 806, 0x18),
    (1024, 768, 50): (54163, 1048, 1184, 1344, 771, 777, 806, 0x18),
    (1024, 768, 30): (32498, 1048, 1184, 1344, 771, 777, 806, 0x18),
    (1280, 720, 60): (74250, 1390, 1430, 1650, 725, 730, 750, 0x1E),
    (1280, 720, 50): (74250, 1720, 1760, 1980, 725, 730, 750, 0x1E),
    # CTA 720p30's 1760-pixel H sync offset exceeds the DTD's 10-bit field.
    (1280, 720, 30): (37125, 1390, 1430, 1650, 725, 730, 750, 0x1E),
    (1280, 1024, 60): (108000, 1328, 1440, 1688, 1025, 1028, 1066, 0x1E),
    (1280, 1024, 50): (89970, 1328, 1440, 1688, 1025, 1028, 1066, 0x1E),
    (1280, 1024, 30): (53982, 1328, 1440, 1688, 1025, 1028, 1066, 0x1E),
    (1920, 1080, 60): (148500, 2008, 2052, 2200, 1084, 1089, 1125, 0x1E),
    (1920, 1080, 50): (148500, 2448, 2492, 2640, 1084, 1089, 1125, 0x1E),
    (1920, 1080, 30): (74250, 2008, 2052, 2200, 1084, 1089, 1125, 0x1E),
}


def resolution_edid(template, width, height, refresh_rate):
    timing = EDID_TIMINGS.get((width, height, refresh_rate))
    if timing is None:
        raise ToolError("Unsupported EDID mode; use 640x480, 800x600, 1024x768, "
                        "1280x720, 1280x1024 or 1920x1080 at 60, 50 or 30Hz")
    try:
        original = bytes.fromhex(template)
    except (TypeError, ValueError) as error:
        raise ToolError("Invalid default EDID from PicoKVM") from error
    if (len(original) < 128 or len(original) % 128
            or original[:8] != bytes.fromhex("00ffffffffffff00")
            or len(original) != (original[126] + 1) * 128
            or any(sum(original[i:i + 128]) % 256 for i in range(0, len(original), 128))):
        raise ToolError("Invalid default EDID length, header or checksum")

    clock, hs, he, ht, vs, ve, vt, flags = timing
    hb, vb = ht - width, vt - height
    h_offset, h_pulse, v_offset, v_pulse = hs - width, he - hs, vs - height, ve - vs
    dtd = bytearray(18)
    dtd[:2] = ((clock + 5) // 10).to_bytes(2, "little")
    dtd[2:5] = bytes((width & 255, hb & 255, (width >> 8) << 4 | (hb >> 8)))
    dtd[5:8] = bytes((height & 255, vb & 255, (height >> 8) << 4 | (vb >> 8)))
    dtd[8:12] = bytes((h_offset & 255, h_pulse & 255, (v_offset & 15) << 4 | (v_pulse & 15),
                      (h_offset >> 8) << 6 | (h_pulse >> 8) << 4 | (v_offset >> 4) << 2 | (v_pulse >> 4)))
    dtd[12:15] = original[66:69]  # Preserve the template's physical display size.
    dtd[17] = flags

    base = bytearray(original[:128])
    base[24] = (base[24] & 0xFC) | 2  # Preferred first DTD, no continuous-frequency modes.
    base[35:38] = bytes(3)  # Remove conflicting established modes.
    base[38:54] = b"\x01\x01" * 8  # No additional standard timings.
    base[54:72] = dtd
    base[72:90] = b"\x00\x00\x00\xfc\x00" + b"PicoKVM MCP\n "
    base[90:126] = (b"\x00\x00\x00\x10\x00" + bytes(13)) * 2

    # Keep audio/speaker capabilities and HDMI identification, without the
    # template's video modes, extension DTDs, or HDMI vendor video modes.
    audio_blocks = bytearray()
    basic_audio = 0
    for offset in range(128, len(original), 128):
        block = original[offset:offset + 128]
        if block[0] != 2:
            continue
        end = block[2]
        if end == 0:
            continue
        if not 4 <= end <= 127:
            raise ToolError("Invalid default EDID CTA data offset")
        basic_audio |= block[3] & 0x40
        index = 4
        while index < end:
            tag, size = block[index] >> 5, block[index] & 31
            next_index = index + 1 + size
            if next_index > end:
                raise ToolError("Invalid default EDID CTA data block")
            payload = block[index + 1:next_index]
            if tag in (1, 4):
                audio_blocks.extend(block[index:next_index])
            elif tag == 3 and size >= 5 and payload[:3] == b"\x03\x0c\x00":
                audio_blocks.extend(b"\x65" + payload[:5])
            index = next_index
    if len(audio_blocks) > 123:
        raise ToolError("Default EDID audio data exceeds one CTA block")
    extension = bytearray(128)
    extension[:4] = bytes((2, 3, 4 + len(audio_blocks), basic_audio))
    extension[4:4 + len(audio_blocks)] = audio_blocks
    extension[127] = -sum(extension[:127]) & 255
    base[126] = 1
    base[127] = -sum(base[:127]) & 255
    return (base + extension).hex()


def integer_schema(low, high, **extra):
    return dict(type="integer", minimum=low, maximum=high, **extra)


KEY_SCHEMA = {"type": ["string", "integer"], "minLength": 1,
              "minimum": 1, "maximum": 231}
SOURCE_SCHEMA = {"type": "string", "enum": ["storage", "sd"], "default": "storage"}
STORAGE_STATUS_FIELDS = (
    ("virtual_media", "getVirtualMediaState"),
    ("storage", "getStorageSpace"),
)
DIAGNOSTIC_FIELDS = (
    ("device_id", "getDeviceID"),
    ("version", "getLocalUpdateStatus"),
    ("network", "getNetworkState"),
    ("video", "getVideoState"),
    ("usb", "getUSBState"),
    ("edid", "getEDID"),
) + STORAGE_STATUS_FIELDS


def tool(name, description, properties=None, required=()):
    return {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": properties or {},
                        "required": list(required), "additionalProperties": False},
    }


TOOLS = [
    tool("kvm_mouse_move", "Move in screen pixels (absolute) or HID deltas (relative). "
         "Absolute requires x and y; relative requires dx and dy.", {
             "mode": {"type": "string", "enum": ["absolute", "relative"], "default": "absolute"},
             "x": integer_schema(0, 2147483647), "y": integer_schema(0, 2147483647),
             "dx": integer_schema(-2047, 2047), "dy": integer_schema(-2047, 2047),
         }),
    tool("kvm_mouse_click", "Click once. Supply both x and y in screen pixels for an "
         "absolute click; omit both to click in place using the relative mouse interface.", {
             "button": {"type": "string", "enum": ["left", "right", "middle"]},
             "x": integer_schema(0, 2147483647), "y": integer_schema(0, 2147483647),
         }, ("button",)),
    tool("kvm_mouse_wheel", "Scroll vertically (positive up) and horizontally "
         "(positive right); deltas are HID wheel units.", {
             "wheel_y": integer_schema(-127, 127, default=0),
             "wheel_x": integer_schema(-127, 127, default=0),
         }),
    tool("kvm_send_key", "Press and release one key: case-insensitive key name "
         "(Enter, F1, a, CtrlRight), a US-layout character, or numeric HID usage ID.",
         {"key": KEY_SCHEMA}, ("key",)),
    tool("kvm_send_key_combination", "Press keys together, then release all. "
         "Accepts modifier names and key names/HID usage IDs, with at most six non-modifier keys.",
         {"keys": {"type": "array", "items": KEY_SCHEMA, "minItems": 1, "maxItems": 14}}, ("keys",)),
    tool("kvm_type_text", "Type up to 1000 ASCII characters using a US keyboard layout. "
         "Printable ASCII, newline, carriage return and tab only; no Unicode/IME conversion.",
         {"text": {"type": "string", "maxLength": 1000}}, ("text",)),
    tool("kvm_get_status", "Read video input and USB attachment status."),
    tool("kvm_get_diagnostics", "Collect device ID, local app/system versions, network, video, "
         "USB, current EDID, virtual media and internal shared-storage capacity in bytes. "
         "Reads sequentially without changing settings or querying SD storage. "
         "Returns data, per-field errors and complete; partial results remain usable."),
    tool("kvm_get_storage_status", "Read the current virtual medium (null if unmounted) and "
         "internal shared-storage filesystem bytesUsed/bytesFree. Does not query or mount SD. "
         "Returns data, per-field errors and complete; partial results remain usable."),
    tool("kvm_list_storage_files", "List root-level shared-storage files, including files shared "
         "with the target host over MTP. Returns filename, size in bytes and createdAt "
         "(actually the modification time). Does not list directories or their contents. "
         "source=storage selects internal storage; source=sd selects the SD card.",
         {"source": SOURCE_SCHEMA}),
    tool("kvm_delete_storage_file", "Permanently delete one stored file from internal storage or SD, "
         "including an incomplete upload. Does not unmount virtual media.", {
             "filename": {"type": "string", "minLength": 1}, "source": SOURCE_SCHEMA,
         }, ("filename",)),
    tool("kvm_mount_iso", "Mount an existing ISO as CDROM from internal storage or SD. "
         "Replaces any currently mounted virtual medium; does not upload files.", {
             "filename": {"type": "string", "minLength": 1}, "source": SOURCE_SCHEMA,
         }, ("filename",)),
    tool("kvm_unmount_iso", "Unmount the current virtual medium. PicoKVM may also clear "
         "its matching auto-mount setting and rebuild USB if the medium is busy."),
    tool("kvm_set_screen_resolution", "Set EDID to advertise one preferred display mode: "
         "640x480, 800x600, 1024x768, 1280x720, 1280x1024 or 1920x1080 at 60, 50 or 30Hz. "
         "Persists on PicoKVM; the target OS decides when to switch resolution.", {
             "width": integer_schema(640, 1920), "height": integer_schema(480, 1080),
             "refresh_rate": integer_schema(30, 60, enum=[60, 50, 30], default=60),
         }, ("width", "height")),
    tool("kvm_reset_edid", "Restore the firmware's default EDID. The target OS may need to "
         "re-detect the display."),
    tool("kvm_capture_screenshot", "Capture a JPEG image using the device MCP server. "
         "Requires PICOKVM_MCP_API_KEY and firmware supporting Streamable HTTP. "
         "The device also saves the screenshot locally.", {
             "timeout_ms": integer_schema(1000, 30000, default=5000),
         }),
]
TOOL_BY_NAME = {item["name"]: item for item in TOOLS}
INPUT_TOOLS = {item["name"] for item in TOOLS[:6]} | {"kvm_set_screen_resolution", "kvm_reset_edid"}


def validate(value, schema, label="arguments"):
    """Validate the small JSON Schema subset used by the public tools."""
    kinds = schema["type"]
    if isinstance(kinds, str):
        kinds = [kinds]
    matches = {"object": isinstance(value, dict), "array": isinstance(value, list),
               "string": isinstance(value, str), "integer": type(value) is int}
    if not any(matches[kind] for kind in kinds):
        raise ToolError("{} must be {}".format(label, " or ".join(kinds)))
    if "enum" in schema and value not in schema["enum"]:
        raise ToolError("{} must be one of {}".format(label, schema["enum"]))
    if isinstance(value, dict):
        properties = schema["properties"]
        for key in schema.get("required", []):
            if key not in value:
                raise ToolError("Missing {}.{}".format(label, key))
        for key, child in value.items():
            if key not in properties:
                raise ToolError("Unknown {}.{}".format(label, key))
            validate(child, properties[key], "{}.{}".format(label, key))
    elif isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", len(value)):
            raise ToolError("{} has an invalid number of items".format(label))
        for child in value:
            validate(child, schema["items"], label + "[]")
    elif isinstance(value, str):
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", len(value)):
            raise ToolError("{} has an invalid length".format(label))
    elif not schema["minimum"] <= value <= schema["maximum"]:
        raise ToolError("{} must be between {} and {}".format(label, schema["minimum"], schema["maximum"]))


def validate_storage_filename(filename):
    if (filename in (".", "..") or ".." in filename or "/" in filename
            or "\\" in filename or "\x00" in filename):
        raise ToolError("filename must be a storage file name, without path components")


class PicoKVMClient:
    def __init__(self, base_url=None, password=None):
        self.base_url = (base_url if base_url is not None else
                         os.environ.get("PICOKVM_BASE_URL", "http://127.0.0.1")).rstrip("/")
        self.password = password if password is not None else os.environ.get("PICOKVM_PASSWORD")
        self.cookies = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        self.session_id = str(uuid.uuid4())
        self.request_ids = itertools.count(1)
        self.lock = threading.Lock()
        self.authenticated = False
        self.invalidated = False

    def _post(self, path, body):
        request = urllib.request.Request(
            self.base_url + path, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "X-Session-ID": self.session_id})
        try:
            with self.opener.open(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code == 401:
                self.authenticated = False
            with error:
                detail = error.read().decode("utf-8", errors="replace")[:512]
            raise ToolError("PicoKVM HTTP {}: {}".format(error.code, detail)) from error
        except urllib.error.URLError as error:
            raise ToolError("PicoKVM connection failed: {}".format(error.reason)) from error
        except (HTTPException, OSError, ValueError) as error:
            raise ToolError("PicoKVM request failed: {}".format(error)) from error

    def rpc(self, method, params=None):
        # Cookies, login and request IDs share one lock across HTTP handler threads.
        with self.lock:
            if self.invalidated:
                raise ToolError("PicoKVM HTTP session invalidated; restart the bridge to reconnect")
            if self.password is not None and not self.authenticated:
                login = self._post("/auth/login-local", {"password": self.password})
                if not isinstance(login, dict) or "error" in login:
                    raise ToolError("PicoKVM login failed")
                self.authenticated = True
            request_id = next(self.request_ids)
            response = self._post("/api/rpc", {
                "jsonrpc": "2.0", "method": method, "params": params or {}, "id": request_id})
            if not isinstance(response, dict):
                raise ToolError("Invalid PicoKVM JSON-RPC response")
            if "response" in response:
                event = response.get("event")
                if isinstance(event, dict) and event.get("method") == "sessionInvalidated":
                    self.invalidated = True
                    raise ToolError("PicoKVM HTTP session invalidated; the operation may already have run")
                response = response["response"]
            if (not isinstance(response, dict) or response.get("jsonrpc") != "2.0"
                    or response.get("id") != request_id):
                raise ToolError("Invalid PicoKVM JSON-RPC response or mismatched ID")
            if response.get("error") is not None:
                error = response["error"]
                if isinstance(error, dict) and error.get("code") == -32001:
                    self.invalidated = True
                raise ToolError("PicoKVM RPC {} failed: {}".format(method, json.dumps(error, ensure_ascii=False)))
            # Void Go handlers omit result rather than returning result: null.
            return response.get("result")


class NoMCPRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, *args, **kwargs):
        # Never forward the device API key to a redirected endpoint.
        return None


class PicoKVMMCPClient:
    """Small Streamable HTTP client, independent of the web RPC login session."""

    PROTOCOL_VERSION = "2025-03-26"

    def __init__(self, base_url=None, url=None, api_key=None):
        self.base_url = base_url if base_url is not None else os.environ.get(
            "PICOKVM_BASE_URL", "http://127.0.0.1")
        self.url = url if url is not None else os.environ.get("PICOKVM_MCP_URL")
        self.api_key = api_key if api_key is not None else os.environ.get("PICOKVM_MCP_API_KEY")
        self.opener = urllib.request.build_opener(NoMCPRedirect())
        self.lock = threading.Lock()
        self.request_ids = itertools.count(1)
        self.initialized = False
        self.session_id = None

    def _configure(self):
        if not self.api_key or any(char.isspace() for char in self.api_key):
            raise ToolError("Set PICOKVM_MCP_API_KEY to the device MCP API key")
        if self.url is None:
            try:
                host = urllib.parse.urlsplit(self.base_url).hostname
            except ValueError:
                host = None
            if not host:
                raise ToolError("Invalid PICOKVM_BASE_URL; set PICOKVM_MCP_URL explicitly")
            if ":" in host:
                host = "[{}]".format(host)
            self.url = "http://{}:8081/mcp".format(host)
        try:
            parsed = urllib.parse.urlsplit(self.url)
            valid = (parsed.scheme in ("http", "https") and parsed.hostname
                     and parsed.username is None and parsed.password is None
                     and not parsed.fragment and parsed.port != 0
                     and not any(char.isspace() for char in self.url))
        except ValueError:
            valid = False
        if not valid:
            raise ToolError("PICOKVM_MCP_URL must be a complete HTTP(S) endpoint URL")

    @staticmethod
    def _rpc_result(message, request_id):
        if (not isinstance(message, dict) or message.get("jsonrpc") != "2.0"
                or type(message.get("id")) is not int or message["id"] != request_id
                or ("result" in message) == ("error" in message)):
            raise ToolError("Invalid device MCP JSON-RPC response or mismatched ID")
        if "error" in message:
            error = message["error"]
            code = error.get("code") if isinstance(error, dict) else None
            # Do not reflect arbitrary response bodies (which may contain secrets).
            if type(code) is int:
                raise ToolError("Device MCP JSON-RPC error {}".format(code))
            raise ToolError("Invalid device MCP JSON-RPC error")
        return message["result"]

    def _read_response(self, response, request_id, deadline):
        content_type = response.headers.get_content_type()
        if content_type == "application/json":
            message = json.loads(response.read().decode("utf-8"))
            return self._rpc_result(message, request_id)
        if content_type != "text/event-stream":
            raise ToolError("Unsupported device MCP response Content-Type")

        data = []
        for index, raw_line in enumerate(response):
            if time.monotonic() > deadline:
                raise ToolError("Device MCP response timed out")
            line = raw_line.decode("utf-8-sig" if index == 0 else "utf-8").rstrip("\r\n")
            if not line:
                if data:
                    message = json.loads("\n".join(data))
                    data = []
                    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                        raise ToolError("Invalid device MCP SSE message")
                    if type(message.get("id")) is int and message["id"] == request_id:
                        return self._rpc_result(message, request_id)
                continue
            field, separator, value = line.partition(":")
            if separator and field == "data":
                data.append(value[1:] if value.startswith(" ") else value)
        raise ToolError("Device MCP SSE stream ended without a matching response")

    def _post(self, method, params=None, timeout=30, notification=False):
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notification:
            body["id"] = next(self.request_ids)
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "Authorization": "Bearer " + self.api_key,
                   "MCP-Protocol-Version": self.PROTOCOL_VERSION}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(
            self.url, data=json.dumps(body).encode("utf-8"), method="POST", headers=headers)
        deadline = time.monotonic() + timeout
        try:
            with self.opener.open(request, timeout=timeout) as response:
                if notification:
                    if response.status != 202:
                        raise ToolError("Device MCP did not accept initialization notification")
                    return None
                if response.status != 200:
                    raise ToolError("Unexpected device MCP HTTP status {}".format(response.status))
                result = self._read_response(response, body["id"], deadline)
                if method == "initialize":
                    self.session_id = response.headers.get("Mcp-Session-Id")
                return result
        except urllib.error.HTTPError as error:
            error.close()
            raise ToolError("Device MCP HTTP {}; check the API key, endpoint and MCP setting".format(
                error.code)) from error
        except urllib.error.URLError as error:
            raise ToolError("Device MCP connection failed; check PICOKVM_MCP_URL and device availability") from error
        except (HTTPException, OSError) as error:
            raise ToolError("Device MCP request failed or timed out") from error
        except (ValueError, UnicodeError) as error:
            raise ToolError("Invalid device MCP response") from error

    @staticmethod
    def _validate_capture(result):
        if (not isinstance(result, dict) or not isinstance(result.get("content"), list)
                or not result["content"]
                or type(result.get("isError", False)) is not bool):
            raise ToolError("Invalid device MCP screenshot result")
        has_image = False
        for item in result["content"]:
            if not isinstance(item, dict):
                raise ToolError("Invalid device MCP screenshot content")
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                continue
            if item.get("type") != "image" or item.get("mimeType") != "image/jpeg":
                raise ToolError("Device MCP screenshot must contain JPEG image content")
            try:
                data = base64.b64decode(item.get("data", ""), validate=True)
            except (TypeError, ValueError, binascii.Error) as error:
                raise ToolError("Invalid device MCP screenshot Base64") from error
            # Check JPEG framing without adding an image decoder dependency.
            if not data.startswith(b"\xff\xd8\xff") or not data.endswith(b"\xff\xd9"):
                raise ToolError("Device MCP screenshot contains empty or invalid JPEG data")
            has_image = True
        if not result.get("isError") and not has_image:
            raise ToolError("Device MCP screenshot returned no image")
        return {key: result[key] for key in ("content", "isError") if key in result}

    def capture_screenshot(self, timeout_ms=5000):
        with self.lock:
            self._configure()
            try:
                if not self.initialized:
                    result = self._post("initialize", {
                        "protocolVersion": self.PROTOCOL_VERSION, "capabilities": {},
                        "clientInfo": {"name": "picokvm-mcp-bridge", "version": "1.0.0"}})
                    if (not isinstance(result, dict)
                            or result.get("protocolVersion") != self.PROTOCOL_VERSION
                            or not isinstance(result.get("capabilities"), dict)
                            or "tools" not in result["capabilities"]):
                        raise ToolError("Device MCP does not support the required protocol and tools")
                    self._post("notifications/initialized", notification=True)
                    self.initialized = True
                result = self._post("tools/call", {
                    "name": "capture_screenshot", "arguments": {"timeoutMs": timeout_ms}},
                    timeout=timeout_ms / 1000 + 5)
                return self._validate_capture(result)
            except ToolError:
                # A later caller may reconnect, but never replay this capture.
                self.initialized = False
                self.session_id = None
                raise


# Mirror kvm/cli.go and kvm/input_actions.go; no Go source is needed at runtime.
KEY_CODES = {chr(ord("a") + i): 0x04 + i for i in range(26)}
KEY_CODES.update({char: 0x1E + i for i, char in enumerate("1234567890")})
KEY_CODES.update({name.lower(): code for name, code in {
    "Enter": 0x28, "Escape": 0x29, "Backspace": 0x2A, "Tab": 0x2B, "Space": 0x2C,
    "Minus": 0x2D, "Equal": 0x2E, "LeftBrace": 0x2F, "RightBrace": 0x30,
    "Backslash": 0x31, "Semicolon": 0x33, "Quote": 0x34, "Grave": 0x35,
    "Comma": 0x36, "Dot": 0x37, "Slash": 0x38, "CapsLock": 0x39,
    "PrintScreen": 0x46, "ScrollLock": 0x47, "Pause": 0x48, "Insert": 0x49,
    "Home": 0x4A, "PageUp": 0x4B, "Delete": 0x4C, "End": 0x4D,
    "PageDown": 0x4E, "Right": 0x4F, "Left": 0x50, "Down": 0x51, "Up": 0x52,
}.items()})
KEY_CODES.update({"f{}".format(i): 0x39 + i for i in range(1, 13)})
KEY_CODES.update({"arrow" + name: KEY_CODES[name] for name in ("up", "down", "left", "right")})
MODIFIERS = {}
for aliases, bit in [(("ctrl", "control"), 1), (("shift",), 2), (("alt",), 4),
                     (("meta", "win", "cmd"), 8)]:
    for alias in aliases:
        MODIFIERS[alias] = MODIFIERS[alias + "left"] = bit
        MODIFIERS[alias + "right"] = bit << 4
MODIFIERS["altgr"] = 0x40
CHAR_KEYS = {" ": (0, 0x2C), "\n": (0, 0x28), "\r": (0, 0x28), "\t": (0, 0x2B)}
for chars, codes, modifier in [
    ("-=[]\\;'`,./", (0x2D, 0x2E, 0x2F, 0x30, 0x31, 0x33, 0x34, 0x35, 0x36, 0x37, 0x38), 0),
    ("!@#$%^&*()", range(0x1E, 0x28), 2),
    ("_+{}|:\"~<>?", (0x2D, 0x2E, 0x2F, 0x30, 0x31, 0x33, 0x34, 0x35, 0x36, 0x37, 0x38), 2),
]:
    CHAR_KEYS.update({char: (modifier, code) for char, code in zip(chars, codes)})
CHAR_KEYS.update({char: (0, KEY_CODES[char]) for char in "abcdefghijklmnopqrstuvwxyz0123456789"})
CHAR_KEYS.update({char: (2, KEY_CODES[char.lower()]) for char in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"})


def parse_keys(names):
    modifier, keys = 0, []
    for name in names:
        if type(name) is int:
            if 0xE0 <= name <= 0xE7:
                modifier |= 1 << (name - 0xE0)
                continue
            code, shift = name, 0
        elif name.lower() in MODIFIERS:
            modifier |= MODIFIERS[name.lower()]
            continue
        elif name.lower() in KEY_CODES:
            code, shift = KEY_CODES[name.lower()], 0
        elif name in CHAR_KEYS:
            shift, code = CHAR_KEYS[name]
        else:
            raise ToolError("Unknown key: {}".format(name))
        modifier |= shift
        if code not in keys:
            keys.append(code)
    if len(keys) > 6:
        raise ToolError("At most six non-modifier keys can be pressed together")
    return modifier, keys


class MCPBridge:
    def __init__(self, client=None, mcp_client=None):
        self.client = client if client is not None else PicoKVMClient()
        self.mcp_client = mcp_client if mcp_client is not None else PicoKVMMCPClient(
            base_url=self.client.base_url)

    def autoset_edid(self):
        setting = os.environ.get("EDID_AUTOSET", "0").strip().lower()
        if setting in ("0", "false", "no", "off"):
            return None
        if setting not in ("1", "true", "yes", "on"):
            raise ToolError("EDID_AUTOSET must be 1/0, true/false, yes/no or on/off")
        with INPUT_LOCK:
            return self.kvm_set_screen_resolution(1024, 768)

    def invoke(self, name, arguments):
        try:
            if not isinstance(name, str) or name not in TOOL_BY_NAME:
                raise ToolError("Unknown tool: {}".format(name))
            validate(arguments, TOOL_BY_NAME[name]["inputSchema"])
            with INPUT_LOCK if name in INPUT_TOOLS else nullcontext():
                result = getattr(self, name)(**arguments)
            if name == "kvm_capture_screenshot":
                return result
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
        except (ToolError, OSError, ValueError) as error:
            return {"content": [{"type": "text", "text": str(error)}], "isError": True}

    def kvm_capture_screenshot(self, timeout_ms=5000):
        return self.mcp_client.capture_screenshot(timeout_ms)

    def _absolute_position(self, x, y):
        state = self.client.rpc("getVideoState")
        if not isinstance(state, dict):
            raise ToolError("Invalid video state")
        width, height = state.get("width"), state.get("height")
        if type(width) is not int or type(height) is not int or width < 2 or height < 2:
            raise ToolError("Video resolution unknown; cannot map pixel coordinates")
        if not 0 <= x < width or not 0 <= y < height:
            raise ToolError("Coordinates outside {}x{} screen".format(width, height))
        return ((x * 32767 + (width - 1) // 2) // (width - 1),
                (y * 32767 + (height - 1) // 2) // (height - 1))

    def _press_release(self, method, press, release):
        failure = None
        try:
            self.client.rpc(method, press)
        except ToolError as error:
            failure = error
        finally:
            # Even a timed-out press may have reached the device.
            time.sleep(REPORT_GAP)
            try:
                self.client.rpc(method, release)
            except ToolError as error:
                if failure is not None:
                    raise ToolError("{}; release also failed: {}".format(failure, error)) from error
                raise
        if failure is not None:
            raise failure

    def _tap(self, modifier, keys):
        self._press_release("keyboardReport", {"modifier": modifier, "keys": keys},
                            {"modifier": 0, "keys": []})

    def kvm_mouse_move(self, mode="absolute", x=None, y=None, dx=None, dy=None):
        if mode == "absolute":
            if x is None or y is None or dx is not None or dy is not None:
                raise ToolError("Absolute move requires x and y only")
            ax, ay = self._absolute_position(x, y)
            self.client.rpc("absMouseReport", {"x": ax, "y": ay, "buttons": 0})
            return {"success": True, "mode": mode, "x": x, "y": y}
        if dx is None or dy is None or x is not None or y is not None:
            raise ToolError("Relative move requires dx and dy only")
        remaining_x, remaining_y = dx, dy
        while remaining_x or remaining_y:
            step_x, step_y = max(-127, min(127, remaining_x)), max(-127, min(127, remaining_y))
            self.client.rpc("relMouseReport", {"dx": step_x, "dy": step_y, "buttons": 0})
            remaining_x -= step_x
            remaining_y -= step_y
        return {"success": True, "mode": mode, "dx": dx, "dy": dy}

    def kvm_mouse_click(self, button, x=None, y=None):
        if (x is None) != (y is None):
            raise ToolError("Supply both x and y, or omit both")
        if x is None:
            method, position = "relMouseReport", {"dx": 0, "dy": 0}
        else:
            ax, ay = self._absolute_position(x, y)
            method, position = "absMouseReport", {"x": ax, "y": ay}
        bit = {"left": 1, "right": 2, "middle": 4}[button]
        self._press_release(method, dict(position, buttons=bit), dict(position, buttons=0))
        return {"success": True, "button": button}

    def kvm_mouse_wheel(self, wheel_y=0, wheel_x=0):
        self.client.rpc("wheelReport", {"wheelY": wheel_y, "wheelX": wheel_x})
        return {"success": True}

    def kvm_send_key(self, key):
        self._tap(*parse_keys([key]))
        return {"success": True}

    def kvm_send_key_combination(self, keys):
        self._tap(*parse_keys(keys))
        return {"success": True}

    def kvm_type_text(self, text):
        reports = []
        for index, char in enumerate(text):
            if char not in CHAR_KEYS:
                raise ToolError("Unsupported character at index {}; only printable ASCII, newline and tab are supported".format(index))
            reports.append(CHAR_KEYS[char])
        for modifier, key in reports:
            self._tap(modifier, [key])
        return {"success": True, "characters": len(reports)}

    def kvm_get_status(self):
        return {"video": self.client.rpc("getVideoState"), "usb": self.client.rpc("getUSBState")}

    def _collect_status(self, fields):
        data, errors = {}, {}
        for key, method in fields:
            try:
                data[key] = self.client.rpc(method)
            except (ToolError, OSError, ValueError) as error:
                errors[key] = str(error)
        result = {"data": data, "errors": errors, "complete": not errors}
        if not data:
            # invoke preserves this JSON text and marks the MCP result as an error.
            raise ToolError(json.dumps(result, ensure_ascii=False))
        return result

    def kvm_get_diagnostics(self):
        return self._collect_status(DIAGNOSTIC_FIELDS)

    def kvm_get_storage_status(self):
        return self._collect_status(STORAGE_STATUS_FIELDS)

    def kvm_reset_edid(self):
        self.client.rpc("setEDID", {"edid": ""})
        return {"success": True, "edid_set": True,
                "message": "Default EDID configured; the target OS may need to re-detect the display. "
                           "Use kvm_get_status to check the actual video input resolution. "}

    def kvm_set_screen_resolution(self, width, height, refresh_rate=60):
        if (width, height, refresh_rate) not in EDID_TIMINGS:
            raise ToolError("Unsupported EDID mode; use 640x480, 800x600, 1024x768, "
                            "1280x720, 1280x1024 or 1920x1080 at 60, 50 or 30Hz")
        template = self.client.rpc("getDefaultEDID")
        edid = resolution_edid(template, width, height, refresh_rate)
        self.client.rpc("setEDID", {"edid": edid})
        return {"success": True, "edid_set": True, "width": width, "height": height,
                "refresh_rate": refresh_rate,
                "message": "EDID configured; the target OS may need to re-detect the display. "
                           "Use kvm_get_status to check the actual video input resolution."}

    def kvm_list_storage_files(self, source="storage"):
        return self.client.rpc("listStorageFiles" if source == "storage" else "listSDStorageFiles")

    def kvm_delete_storage_file(self, filename, source="storage"):
        validate_storage_filename(filename)
        self.client.rpc("deleteStorageFile" if source == "storage" else "deleteSDStorageFile",
                        {"filename": filename})
        return {"success": True, "filename": filename, "source": source}

    def kvm_mount_iso(self, filename, source="storage"):
        validate_storage_filename(filename)
        self.client.rpc("mountWithStorage" if source == "storage" else "mountWithSDStorage",
                        {"filename": filename, "mode": "CDROM"})
        return {"success": True, "filename": filename, "source": source}

    def kvm_unmount_iso(self):
        self.client.rpc("unmountImage")
        return {"success": True}


class MCPBridgeHandler(BaseHTTPRequestHandler):
    # GET is for diagnostic usage; /mcp accepts JSON-RPC POST requests.
    def do_GET(self):
        if urllib.parse.urlsplit(self.path).path != "/":
            self.send_error(404, "Not Found")
            return
        try:
            body = Path(__file__).with_name("README.md").read_bytes()
        except OSError:
            self.send_error(500, "Unable to read README.md")
            return
        self._send(body, "text/markdown; charset=utf-8")

    def _send(self, body, content_type="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/mcp":
            self.send_error(404, "Not Found")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0:
                raise ValueError("Negative content length")
            request = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeError):
            self.send_error(400, "Invalid JSON or Content-Length")
            return
        if not isinstance(request, dict):
            self._send(json.dumps({"jsonrpc": "2.0", "id": None,
                                   "error": {"code": -32600, "message": "Invalid Request"}}).encode("utf-8"))
            return
        request_id, method = request.get("id"), request.get("method")
        response = {"jsonrpc": "2.0", "id": request_id}
        params = request.get("params", {})
        if not isinstance(params, dict):
            response["error"] = {"code": -32602, "message": "params must be an object"}
        elif method == "initialize":
            response["result"] = {
                "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                "serverInfo": {"name": "std-mcp-bridge", "version": "1.0.0"}}
        elif method == "notifications/initialized":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        elif method == "ping":
            response["result"] = {}
        elif method == "tools/list":
            response["result"] = {"tools": TOOLS}
        elif method == "tools/call":
            response["result"] = self.server.bridge.invoke(params.get("name"), params.get("arguments", {}))
        else:
            response["error"] = {"code": -32601, "message": "Method not found"}
        self._send(json.dumps(response, ensure_ascii=False).encode("utf-8"))

    def log_message(self, format, *args):
        print("[{}] {}".format(self.log_date_time_string(), format % args))


def run(host=None, port=None):
    try:
        load_env(Path(__file__).resolve().with_name(".env"))
        if host is None:
            host = os.environ.get("PICOKVM_MCP_HOST", "0.0.0.0").strip()
            if not host:
                raise EnvError("PICOKVM_MCP_HOST must not be empty")
        if port is None:
            try:
                port = int(os.environ.get("PICOKVM_MCP_PORT", "8000"))
            except ValueError:
                raise EnvError("PICOKVM_MCP_PORT must be an integer from 1 to 65535") from None
            if not 1 <= port <= 65535:
                raise EnvError("PICOKVM_MCP_PORT must be an integer from 1 to 65535")
    except EnvError as error:
        print("Configuration error: {}".format(error), file=sys.stderr)
        return 1
    server = ThreadingHTTPServer((host, port), MCPBridgeHandler)
    server.bridge = MCPBridge()
    try:
        try:
            if server.bridge.autoset_edid() is not None:
                print("EDID_AUTOSET: configured 1024x768 @ 60Hz")
        except (ToolError, OSError, ValueError) as error:
            print("EDID_AUTOSET failed: {} (MCP server will continue)".format(error), file=sys.stderr)
        print("picokvm-mcp running on http://{}:{}/mcp".format(host, port))
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server")
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(run())
