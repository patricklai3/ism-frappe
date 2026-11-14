#!/usr/bin/env python3
"""Frappe v16 Native MCP Protocol Bridge & Stdio Server.

Provides a robust Stdio JSON-RPC 2.0 interface for AI assistants (Antigravity,
Claude Code, Cursor, Claude Desktop) communicating with Frappe v16's native
Streamable HTTP MCP endpoint (/api/v2/mcp).

Usage:
  # Stdio server mode (for mcp_config.json):
  py -3 scripts/frappe_mcp_bridge.py

  # Diagnostic / Self-Test mode:
  py -3 scripts/frappe_mcp_bridge.py --test
"""

import sys
import os
import json
import argparse
import urllib.request
import urllib.error

# Ensure UTF-8 I/O on Windows console
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def parse_args():
    parser = argparse.ArgumentParser(description="Frappe MCP Bridge")
    parser.add_argument(
        "--url",
        default=os.environ.get("FRAPPE_URL", "http://localhost:8000"),
        help="Frappe instance base URL (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--key",
        default=os.environ.get("FRAPPE_API_KEY", "53e6659484dcc74"),
        help="Frappe API key",
    )
    parser.add_argument(
        "--secret",
        default=os.environ.get("FRAPPE_API_SECRET", "f259e4d2239015d"),
        help="Frappe API secret",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run self-test and report Frappe MCP status",
    )
    return parser.parse_args()


def post_mcp(endpoint: str, auth_header: str, payload: dict) -> tuple[int, dict | str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": auth_header,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode("utf-8")
            if not body:
                return resp.status, ""
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    except Exception as e:
        return 500, {"error": str(e)}


def run_test(endpoint: str, auth_header: str):
    print("=" * 60)
    print("FRAPPE v16 NATIVE MCP PROTOCOL VERIFICATION")
    print(f"Target Endpoint: {endpoint}")
    print("=" * 60)

    # 1. Ping
    print("\n[1/3] Testing Ping...")
    status, body = post_mcp(endpoint, auth_header, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    if status == 200:
        print("  ✓ Ping successful (200 OK)")
    else:
        print(f"  ✗ Ping failed: HTTP {status} — {body}")
        return False

    # 2. Initialize
    print("\n[2/3] Negotiating Handshake (initialize)...")
    status, body = post_mcp(
        endpoint,
        auth_header,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "frappe-mcp-bridge", "version": "1.0.0"},
            },
        },
    )
    if status == 200 and isinstance(body, dict):
        result = body.get("result", {})
        server_info = result.get("serverInfo", {})
        print(f"  ✓ Connected to Frappe v{server_info.get('version', 'unknown')} ({server_info.get('name', 'frappe')})")
        print(f"  ✓ Protocol Version negotiated: {result.get('protocolVersion')}")
    else:
        print(f"  ✗ Handshake failed: HTTP {status} — {body}")
        return False

    # 3. Discover Tools
    print("\n[3/3] Discovering MCP Tools (tools/list)...")
    status, body = post_mcp(endpoint, auth_header, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    if status == 200 and isinstance(body, dict):
        tools = body.get("result", {}).get("tools", [])
        tool_names = [t.get("name") for t in tools]
        print(f"  ✓ Found {len(tools)} native MCP tools:")
        for name in tool_names:
            print(f"    - {name}")
    else:
        print(f"  ✗ tools/list failed: HTTP {status} — {body}")
        return False

    print("\n" + "=" * 60)
    print("ALL CHECKS PASSED: Frappe MCP is fully operational!")
    print("=" * 60)
    return True


def run_stdio_server(endpoint: str, auth_header: str):
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except Exception as e:
            continue

        req_id = payload.get("id")
        status, resp_data = post_mcp(endpoint, auth_header, payload)

        if req_id is not None:
            if isinstance(resp_data, dict):
                sys.stdout.write(json.dumps(resp_data) + "\n")
            else:
                sys.stdout.write(
                    json.dumps({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32603, "message": str(resp_data)},
                    })
                    + "\n"
                )
            sys.stdout.flush()


def main():
    args = parse_args()
    endpoint = f"{args.url.rstrip('/')}/api/v2/mcp"
    auth_header = f"token {args.key}:{args.secret}"

    if args.test:
        success = run_test(endpoint, auth_header)
        sys.exit(0 if success else 1)
    else:
        run_stdio_server(endpoint, auth_header)


if __name__ == "__main__":
    main()
