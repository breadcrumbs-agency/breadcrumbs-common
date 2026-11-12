"""breadcrumbs/launch-mcp.sh: the one launcher of the MCP server that the plugins of the harnesses (Claude Code,
Codex, OpenCode, Grok) share. It picks a Python of 3.11 or newer and runs `python -m breadcrumbs core mcp`."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

PKG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "breadcrumbs")
SCRIPT = os.path.join(PKG, "launch-mcp.sh")


@unittest.skipUnless(os.path.exists("/bin/sh"), "a POSIX shell is needed")
class LaunchMcpTest(unittest.TestCase):
    def run_script(self, env_extra, request_lines):
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, PYTHONPATH=os.path.dirname(PKG), HOME=home, BREADCRUMBS_HOME=home, **env_extra)
            return subprocess.run(["/bin/sh", SCRIPT], input="\n".join(request_lines) + "\n", capture_output=True,
                                  text=True, timeout=30, env=env)

    def test_it_is_executable_and_ships_with_the_package(self):
        self.assertTrue(os.access(SCRIPT, os.X_OK))

    def test_it_starts_the_mcp_server_and_answers_initialize_and_tools_list(self):
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
        tools = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        p = self.run_script({"BREADCRUMBS_PY": sys.executable}, [json.dumps(init), json.dumps(tools)])
        replies = [json.loads(line) for line in p.stdout.splitlines() if line.strip()]
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "breadcrumbs")
        self.assertEqual([t["name"] for t in replies[1]["result"]["tools"]][:2], ["find", "open"])

    def test_without_a_python_of_3_11_it_says_so_and_fails(self):
        p = self.run_script({"BREADCRUMBS_PY": "/nonexistent/python", "PATH": "/nonexistent"}, [])
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("3.11", p.stderr)


if __name__ == "__main__":
    unittest.main()
