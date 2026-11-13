"""Offline .env parsing and startup tests, isolated from the user's .env."""

import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from picokvm_env import EnvError, load_env


ROOT = Path(__file__).resolve().parent
SCRIPTS = ("picokvm-mcp.py", "picokvm-upload.py", "picokvm-download.py")


def load_script(filename):
    spec = importlib.util.spec_from_file_location(filename[:-3].replace("-", "_"), ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EnvTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / ".env"
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def write(self, text):
        self.path.write_text(text, encoding="utf-8")

    def test_assignments_comments_quotes_and_literal_values(self):
        self.write("""# comment

  # indented comment
 URL = http://device/path#fragment # trailing comment
PASSWORD='  a # b=c  ' # keep the spaces and hash
DOUBLE="a 'quote' # b" # comment
EMPTY=
EMPTY_COMMENT= # comment
EMPTY_QUOTED=''
HASH=#literal
LITERAL=$HOME ${OTHER} $(command) `command` \\n
UNICODE=パスワード
_KEY_1=value
""")
        load_env(self.path)
        self.assertEqual(dict(os.environ), {
            "URL": "http://device/path#fragment", "PASSWORD": "  a # b=c  ",
            "DOUBLE": "a 'quote' # b", "EMPTY": "", "EMPTY_COMMENT": "",
            "EMPTY_QUOTED": "", "HASH": "#literal",
            "LITERAL": "$HOME ${OTHER} $(command) `command` \\n",
            "UNICODE": "パスワード", "_KEY_1": "value",
        })

    def test_existing_environment_and_empty_values_win_and_first_definition_wins(self):
        self.write("URL=http://file\nEMPTY=from-file\nREPEATED=first\nREPEATED=second\n")
        os.environ.update(URL="http://exported", EMPTY="")
        load_env(self.path)
        self.assertEqual(dict(os.environ), {
            "URL": "http://exported", "EMPTY": "", "REPEATED": "first",
        })

    def test_missing_file_leaves_environment_unchanged(self):
        os.environ["KEEP"] = "value"
        load_env(self.path)
        self.assertEqual(dict(os.environ), {"KEEP": "value"})

    def test_utf8_bom_and_crlf(self):
        self.path.write_bytes(b"\xef\xbb\xbfFIRST=one\r\nSECOND=two\r\n")
        load_env(self.path)
        self.assertEqual(dict(os.environ), {"FIRST": "one", "SECOND": "two"})

    def test_invalid_entries_report_location_without_values_or_partial_updates(self):
        invalid = (
            "secret-without-equals", "BAD-NAME=secret", "1KEY=secret",
            "=secret", "export KEY=secret", "KEY='secret", 'KEY="secret',
            'KEY="secret"suffix', 'KEY="secret"#comment', "KEY=secret\0value",
        )
        for line in invalid:
            with self.subTest(line=line):
                self.write("FIRST=valid\n" + line + "\n")
                with self.assertRaises(EnvError) as error:
                    load_env(self.path)
                self.assertIn("{}:2:".format(self.path), str(error.exception))
                self.assertNotIn("secret", str(error.exception))
                self.assertEqual(dict(os.environ), {})

    def test_multiline_quotes_are_rejected(self):
        self.write('KEY="first\nsecond"\n')
        with self.assertRaisesRegex(EnvError, ":1: unclosed quoted value"):
            load_env(self.path)

    def test_unreadable_file_and_invalid_utf8_are_configuration_errors(self):
        self.path.mkdir()
        with self.assertRaisesRegex(EnvError, "cannot read .env file"):
            load_env(self.path)
        self.path.rmdir()
        self.path.write_bytes(b"KEY=secret\xff")
        with self.assertRaises(EnvError) as error:
            load_env(self.path)
        self.assertIn("UTF-8", str(error.exception))
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual(dict(os.environ), {})


class StartupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.scripts = self.root / "scripts"
        self.scripts.mkdir()
        self.cwd = self.root / "other"
        self.cwd.mkdir()
        (self.cwd / ".env").write_text("PICOKVM_BASE_URL=http://wrong-directory\n", encoding="utf-8")
        self.env_path = self.scripts / ".env"
        self.env_path.write_text(
            "PICOKVM_BASE_URL=http://from-dotenv\nPICOKVM_PASSWORD='test password'\n"
            "PICOKVM_MCP_URL=http://screenshot:8081/mcp\n"
            "PICOKVM_MCP_API_KEY=test-key\n", encoding="utf-8")
        original_cwd = Path.cwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, original_cwd)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_transfer_startup_loads_configuration_before_constructing_client(self):
        for filename, transfer_name in (("picokvm-upload.py", "upload_file"),
                                        ("picokvm-download.py", "download_file")):
            for exported in (False, True):
                with self.subTest(filename=filename, exported=exported), \
                        patch.dict(os.environ, {}, clear=True):
                    if exported:
                        os.environ.update(PICOKVM_BASE_URL="http://exported", PICOKVM_PASSWORD="")
                    module = load_script(filename)
                    with patch.object(module, "__file__", str(self.scripts / filename)), \
                            patch.object(module, "Client") as client, \
                            patch.object(module, transfer_name, return_value=Path("saved.zip")), \
                            patch("sys.stdout", new_callable=io.StringIO):
                        self.assertEqual(module.main(["report.zip"]), 0)
                    client.assert_called_once_with(
                        "http://exported" if exported else "http://from-dotenv",
                        "" if exported else "test password")

    def test_mcp_startup_configures_clients_and_edid_before_serving(self):
        module = load_script("picokvm-mcp.py")
        server = Mock()
        with patch.object(module, "__file__", str(self.scripts / "picokvm-mcp.py")), \
                patch.object(module, "ThreadingHTTPServer", return_value=server), \
                patch.object(module.PicoKVMClient, "rpc") as rpc, \
                patch("sys.stdout", new_callable=io.StringIO):
            module.run()
        self.assertEqual(server.bridge.client.base_url, "http://from-dotenv")
        self.assertEqual(server.bridge.client.password, "test password")
        self.assertEqual(server.bridge.mcp_client.url, "http://screenshot:8081/mcp")
        self.assertEqual(server.bridge.mcp_client.api_key, "test-key")
        rpc.assert_not_called()
        server.serve_forever.assert_called_once()
        server.server_close.assert_called_once()

    def test_bad_configuration_stops_all_entrypoints_before_network(self):
        self.env_path.write_text("PASSWORD='secret\n", encoding="utf-8")
        for filename in SCRIPTS:
            module = load_script(filename)
            network = "ThreadingHTTPServer" if filename == "picokvm-mcp.py" else "Client"
            with self.subTest(filename=filename), \
                    patch.object(module, "__file__", str(self.scripts / filename)), \
                    patch.object(module, network) as connect, \
                    patch("sys.stderr", new_callable=io.StringIO) as stderr:
                result = module.run() if filename == "picokvm-mcp.py" else module.main(["report.zip"])
            self.assertEqual(result, 1)
            connect.assert_not_called()
            self.assertIn("{}:1:".format(self.env_path), stderr.getvalue())
            self.assertNotIn("secret", stderr.getvalue())

    def test_absolute_script_invocations_from_another_directory_and_help(self):
        for filename in (*SCRIPTS, "picokvm_env.py"):
            shutil.copyfile(ROOT / filename, self.scripts / filename)
        self.env_path.write_text("PASSWORD='secret\n", encoding="utf-8")
        for filename in SCRIPTS:
            with self.subTest(filename=filename):
                result = subprocess.run(
                    [sys.executable, str(self.scripts / filename)]
                    + ([] if filename == "picokvm-mcp.py" else ["report.zip"]),
                    cwd=self.cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    universal_newlines=True, timeout=10)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("{}:1:".format(self.env_path), result.stderr)
                self.assertNotIn("secret", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                if filename != "picokvm-mcp.py":
                    help_result = subprocess.run(
                        [sys.executable, str(self.scripts / filename), "--help"],
                        cwd=self.cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        universal_newlines=True, timeout=10)
                    self.assertEqual(help_result.returncode, 0, help_result.stderr)
                    self.assertIn("usage:", help_result.stdout)
                    self.assertEqual(help_result.stderr, "")


if __name__ == "__main__":
    unittest.main()
