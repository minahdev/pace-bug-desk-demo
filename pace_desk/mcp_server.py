"""PACE 기록부 도구를 MCP 서버로 내놓는다 (6단계, Function calling).

Claude Code 헤드리스 모드(claude -p)가 --mcp-config로 이 프로그램을 켜고, Haiku가 도구를 부르면 여기서 답한다.
MCP는 한 줄에 JSON 하나씩 주고받는 약속(JSON-RPC 2.0)이다. 외부 라이브러리 없이 필요한 부분만 만들었다.
  - initialize: 서버 소개와 할 수 있는 일(tools)을 알린다
  - tools/list: 도구 목록과 각 도구의 입력 모양(JSON Schema)을 알린다
  - tools/call: 도구를 실행하고 결과를 JSON 글로 돌려준다
도구 함수는 tools.py에 있다.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tools  # noqa: E402

TOOLS = [
    {
        "name": "search_known_issues",
        "description": "제보 내용과 비슷한 PACE의 알려진 이슈를 점수 순으로 찾는다. 증상을 짧게 적은 query를 준다. "
                       "플랫폼을 알면 platform에 'Android 앱' 또는 '웹'을 준다.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "찾을 증상이나 요청 (예: 걸음 수가 삼성헬스보다 적게 나옴)"},
                "platform": {"type": "string", "enum": ["Android 앱", "웹"], "description": "제보의 플랫폼. 모르면 빼기"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10, "description": "몇 개까지 (기본 5)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_issue",
        "description": "이슈 하나의 자세한 정보(증상, 상태, 고친 버전과 날짜, 우회 방법, 비슷한 짝)를 준다.",
        "inputSchema": {
            "type": "object",
            "properties": {"issue_id": {"type": "string", "description": "이슈 ID (예: PACE-107)"}},
            "required": ["issue_id"],
        },
    },
    {
        "name": "get_releases",
        "description": "버전 기록(어느 버전에서 어떤 이슈를 고쳤나)과 플랫폼별 최신 버전을 준다.",
        "inputSchema": {
            "type": "object",
            "properties": {"platform": {"type": "string", "enum": ["Android 앱", "웹", "서버"]}},
        },
    },
]


def handle(message):
    method = message.get("method")
    params = message.get("params") or {}
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "pace", "version": "1.0"}}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        try:
            result = tools.call(params.get("name"), params.get("arguments") or {})
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "isError": False}
        except Exception as exc:  # 도구 안의 오류는 Haiku에게 글로 알려 준다
            return {"content": [{"type": "text", "text": f"도구 오류: {exc}"}], "isError": True}
    if method == "ping":
        return {}
    return None


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" not in message:  # 알림(notification)에는 답하지 않는다
            continue
        result = handle(message)
        if result is None:
            reply = {"jsonrpc": "2.0", "id": message["id"],
                     "error": {"code": -32601, "message": f"모르는 요청: {message.get('method')}"}}
        else:
            reply = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
