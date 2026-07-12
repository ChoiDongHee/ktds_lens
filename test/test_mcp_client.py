"""End-to-end MCP protocol test for the web-search server.

Unlike importing server.py directly (which bypasses the MCP protocol
entirely), this launches the real server process over the sse transport and
talks to it as an actual MCP client would -- connect, initialize, list
tools, call web_search and search_stats. Kept in its own test/ folder,
separate from server.py, so test-only code never mixes into the server
source.

Requires .env's MCP_TRANSPORT=sse (or streamable-http) and a free MCP_PORT.

Run: .venv/Scripts/python.exe test/test_mcp_client.py
"""

import asyncio
import os
import subprocess
import sys
import time

from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.sse import sse_client

# Same reasoning as server.py: Windows defaults stdout to the system codepage
# (cp949 on Korean Windows), which can't represent arbitrary search-result
# Unicode -- printing raw tool results here would otherwise crash this test
# script itself, unrelated to whether the server is actually working.
sys.stdout.reconfigure(encoding="utf-8")

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_BASE_DIR, ".env"))

HOST = os.environ.get("MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("MCP_PORT", "8000"))
SSE_URL = f"http://{HOST}:{PORT}/sse"
PYTHON_EXE = os.path.join(_BASE_DIR, ".venv", "Scripts", "python.exe")
SERVER_SCRIPT = os.path.join(_BASE_DIR, "server.py")


def start_server() -> subprocess.Popen:
    env = os.environ.copy()
    env["MCP_TRANSPORT"] = "sse"
    proc = subprocess.Popen(
        [PYTHON_EXE, SERVER_SCRIPT],
        cwd=_BASE_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )
    return proc


async def wait_for_server(url: str, timeout: float = 15.0) -> None:
    import httpx

    deadline = time.monotonic() + timeout
    last_error = None
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            try:
                # SSE endpoint streams forever on success -- a connect that
                # doesn't immediately refuse/404 means the server is up.
                async with client.stream("GET", url, timeout=2.0) as r:
                    if r.status_code == 200:
                        return
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
                last_error = e
            await asyncio.sleep(0.3)
    raise RuntimeError(f"서버가 {timeout}s 안에 {url}에서 응답하지 않음: {last_error}")


async def run_checks() -> None:
    print(f"Connecting to {SSE_URL} ...")
    async with sse_client(SSE_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            tool_names = sorted(t.name for t in tools.tools)
            print("registered tools:", tool_names)
            assert "web_search" in tool_names, "web_search tool missing"
            assert "search_stats" in tool_names, "search_stats tool missing"

            print("\n--- calling web_search(query='제주도 여행', max_results=2) over real MCP protocol ---")
            result = await session.call_tool(
                "web_search", {"query": "제주도 여행", "max_results": 2}
            )
            text = "".join(block.text for block in result.content if hasattr(block, "text"))
            print(text)
            assert not result.isError, "web_search tool returned an error"
            assert text.strip(), "web_search returned empty text"

            print("\n--- calling search_stats() over real MCP protocol ---")
            stats_result = await session.call_tool("search_stats", {})
            stats_text = "".join(block.text for block in stats_result.content if hasattr(block, "text"))
            print(stats_text)
            assert not stats_result.isError, "search_stats tool returned an error"

    print("\nOK: MCP 프로토콜을 통한 실제 연결/툴 호출 테스트 통과")


def main() -> None:
    proc = start_server()
    try:
        asyncio.run(wait_for_server(SSE_URL))
        asyncio.run(run_checks())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        if proc.stdout:
            output = proc.stdout.read()
            if output:
                print("\n--- server process output ---")
                print(output)


if __name__ == "__main__":
    main()
