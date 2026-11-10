from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import urllib.error
import urllib.request

PICOKVM_JSONRPC_URL = "http://127.0.0.1:8080/rpc"
PICOKVM_REST_URL = "http://127.0.0.1:8080/api/v1"

class MCPBridgeHandler(BaseHTTPRequestHandler):

  def do_POST(self):

    if self.path != "/mcp":
      self.send_error(404, "Not Found")
      return
    content_length = int(self.headers.get("Content-Length", 0))
    body_bytes = self.rfile.read(content_length)

    try:
      req = json.loads(body_bytes.decode("utf-8"))
    except Exception:
      self.send_error(400, "Invalid JSON")
      return

    req_id = req.get("id")
    method = req.get("method")
    params = req.get("params", {})

    response_data = None

    # init
    if method == "initialize":
      response_data = {
          "jsonrpc": "2.0",
          "id": req_id,
          "result": {
              "protocolVersion": "2024-11-05",
              "capabilities": {"tools": {}},
              "serverInfo": {"name": "std-mcp-bridge", "version": "1.0.0"},
          },
      }

    # init notif
    elif method == "notifications/initialized":
      self.send_response(200)
      self.end_headers()
      return

    # tools/list
    elif method == "tools/list":
      response_data = {
          "jsonrpc": "2.0",
          "id": req_id,
          "result": {
              "tools": [
                  {
                      "name": "kvm_send_key",
                      "description": (
                          "send key"
                      ),
                      "inputSchema": {
                          "type": "object",
                          "properties": {"key": {"type": "string"}},
                          "required": ["key"],
                      },
                  },
                  {
                      "name": "kvm_get_status",
                      "description": "get status",
                      "inputSchema": {"type": "object"},
                  },
              ]
          },
      }

    # tools/call
    elif method == "tools/call":
      tool_name = params.get("name")
      args = params.get("arguments", {})

      result_text = self.forward_request(tool_name, args)

      response_data = {
          "jsonrpc": "2.0",
          "id": req_id,
          "result": {"content": [{"type": "text", "text": result_text}]},
      }

    else:
      response_data = {
          "jsonrpc": "2.0",
          "id": req_id,
          "error": {"code": -32601, "message": "Method not found"},
      }

    # return response
    res_bytes = json.dumps(response_data).encode("utf-8")
    self.send_response(200)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(res_bytes)))
    self.end_headers()
    self.wfile.write(res_bytes)

  # forward request to picokvm
  def forward_request(self, tool_name: str, args: dict) -> str:
    try:
      # json rpc
      if tool_name == "kvm_send_key":
        payload = json.dumps({
            "jsonrpc": "2.0",
            "method": "hid.key",
            "params": {"key": args.get("key", "")},
            "id": 1,
        }).encode("utf-8")

        req = urllib.request.Request(
            PICOKVM_JSONRPC_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as res:
          return res.read().decode("utf-8")

      # rest
      elif tool_name == "kvm_get_status":
        req = urllib.request.Request(f"{PICOKVM_REST_URL}/status", method="GET")
        with urllib.request.urlopen(req, timeout=5) as res:
          return res.read().decode("utf-8")

      else:
        return f"Unknown tool: {tool_name}"

    except urllib.error.HTTPError as e:
      return f"HTTP Error {e.code}: {e.read().decode('utf-8')}"
    except urllib.error.URLError as e:
      return f"Connection Failed: {e.reason}"
    except Exception as e:
      return f"Internal Error: {str(e)}"

  # omit log
  def log_message(self, format, *args):
    print(f"[{self.log_date_time_string()}] {args[0]} {args[1]}")

def run(host="0.0.0.0", port=8000):
  server = ThreadingHTTPServer((host, port), MCPBridgeHandler)
  print(f"picokvm-mcp running on http://{host}:{port}/mcp")
  try:
    server.serve_forever()
  except KeyboardInterrupt:
    print("\nShutting down server")
    server.server_close()


if __name__ == "__main__":
  run()