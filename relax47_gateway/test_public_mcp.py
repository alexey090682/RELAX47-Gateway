#!/usr/bin/env python3
"""Offline protocol/security regression tests for RELAX47 public MCP."""
from __future__ import annotations

import importlib
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path


class PublicMCPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tempdir = tempfile.TemporaryDirectory()
        os.environ.update({
            "RELAX47_PUBLIC_BASE_URL": "https://relax47.example",
            "RELAX47_OAUTH_ACTIVATION_CODE": "test-activation-code",
            "RELAX47_OAUTH_STATE_FILE": str(Path(cls.tempdir.name) / "oauth.json"),
            "RELAX47_CONNECTION_AUDIT_FILE": str(Path(cls.tempdir.name) / "connections.jsonl"),
            "RELAX47_AUDIT_PATH": str(Path(cls.tempdir.name) / "changes.jsonl"),
        })
        cls.mcp = importlib.import_module("public_mcp")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), cls.mcp.PublicMCPHandler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.tempdir.cleanup()

    def request(self, path, *, method="GET", payload=None, headers=None):
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                raw = response.read()
                return response.status, dict(response.headers), json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, dict(exc.headers), json.loads(raw) if raw else None

    def token(self, client_name="Claude"):
        registration = self.mcp.register_client({"client_name": client_name, "redirect_uris": ["https://client.example/callback"]})
        token = self.mcp.issue_tokens(client_id=registration["client_id"], scopes=["mcp:read", "mcp:write", "mcp:admin"])["access_token"]
        return registration, token

    def initialize(self, token, client_name):
        return self.request("/mcp", method="POST", headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, payload={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": client_name, "version": "1"}}})

    def test_metadata_and_unauthorized_challenge(self):
        status, _, metadata = self.request("/.well-known/oauth-protected-resource/mcp")
        self.assertEqual(status, 200)
        self.assertEqual(metadata["resource"], "https://relax47.example/mcp")
        status, headers, _ = self.request("/mcp", method="POST", payload={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(status, 401)
        self.assertIn("resource_metadata", headers["WWW-Authenticate"])

    def test_independent_sessions_and_tool_list(self):
        _, token_a = self.token("Claude")
        _, token_b = self.token("ChatGPT")
        status_a, headers_a, _ = self.initialize(token_a, "Claude")
        status_b, headers_b, _ = self.initialize(token_b, "ChatGPT")
        self.assertEqual((status_a, status_b), (200, 200))
        self.assertNotEqual(headers_a["Mcp-Session-Id"], headers_b["Mcp-Session-Id"])
        status, _, body = self.request("/mcp", method="POST", headers={"Authorization": f"Bearer {token_a}", "Mcp-Session-Id": headers_a["Mcp-Session-Id"], "Content-Type": "application/json"}, payload={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        self.assertEqual(status, 200)
        self.assertTrue(body["result"]["tools"])

    def test_session_cannot_cross_clients(self):
        _, token_a = self.token("Claude")
        _, token_b = self.token("code")
        _, headers, _ = self.initialize(token_a, "Claude")
        status, _, body = self.request("/mcp", method="POST", headers={"Authorization": f"Bearer {token_b}", "Mcp-Session-Id": headers["Mcp-Session-Id"], "Content-Type": "application/json"}, payload={"jsonrpc": "2.0", "id": 3, "method": "ping"})
        self.assertEqual(status, 404)
        self.assertIn("session", body["error"]["message"].lower())

    def test_namespaced_tool_call_is_accepted(self):
        _, token = self.token("ChatGPT")
        _, headers, _ = self.initialize(token, "ChatGPT")
        status, _, body = self.request(
            "/mcp", method="POST",
            headers={"Authorization": f"Bearer {token}", "Mcp-Session-Id": headers["Mcp-Session-Id"], "Content-Type": "application/json"},
            payload={"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "relax47_mcp.gateway_status", "arguments": {}}},
        )
        self.assertEqual(status, 200)
        self.assertIn("result", body)

    def test_loopback_redirect_is_allowed_but_public_http_is_not(self):
        record = self.mcp.register_client({"redirect_uris": ["http://127.0.0.1:7777/callback"]})
        self.assertTrue(record["client_id"])
        with self.assertRaises(ValueError):
            self.mcp.register_client({"redirect_uris": ["http://public.example/callback"]})

    def test_terminal_is_non_shell_and_blocks_credentials(self):
        root = Path(self.tempdir.name)
        self.mcp.gateway.TERMINAL_ROOTS = (*self.mcp.gateway.TERMINAL_ROOTS, root)
        result = self.mcp.gateway.tool_terminal_exec({"argv": ["ls", "-1"], "cwd": str(root)})
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["host_shell"])
        with self.assertRaises(PermissionError):
            self.mcp.gateway.tool_terminal_exec({"argv": ["head", "secrets.yaml"], "cwd": "/homeassistant"})
        with self.assertRaises(PermissionError):
            self.mcp.gateway.tool_terminal_exec({"argv": ["sh", "-c", "id"]})


if __name__ == "__main__":
    unittest.main(verbosity=2)
