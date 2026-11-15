"""本物の Claude Code / Codex を動かすための、LLM API の代わり (tests/test_agents_e2e.py が使う)。

どちらも決まった 1 往復だけを返す:
  1 回目: 思考 → 「確認します。」→ シェルのコマンドを 1 つ実行させる
  2 回目 (ツールの結果を受け取った後): 「完了しました。」
"""

import http.server
import json
import threading

USAGE_OPENAI = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
                "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}


def sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events).encode("utf-8")


class MockServer(http.server.ThreadingHTTPServer):
    def __init__(self, command):
        self.command = command   # エージェントに実行させるシェルのコマンド
        self.requests = []
        srv = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, body, content_type):
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                srv.requests.append((self.path, body))
                srv.handle_post(self, body)

        super().__init__(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server_address[1]}"
        threading.Thread(target=self.serve_forever, daemon=True).start()


class AnthropicMock(MockServer):
    """Messages API (Claude Code 用)。"""

    def handle_post(self, h, body):
        if "count_tokens" in h.path:
            return h.send(json.dumps({"input_tokens": 10}).encode(), "application/json")
        msgs = body.get("messages") or []
        last = msgs[-1].get("content") if msgs else None
        has_result = isinstance(last, list) and any(isinstance(c, dict) and c.get("type") == "tool_result" for c in last)
        if not body.get("tools"):   # タイトルの生成などの補助的な呼び出し
            blocks, stop = [{"type": "text", "text": "テスト"}], "end_turn"
        elif not has_result:
            blocks = [{"type": "thinking", "thinking": "まずコマンドを実行する"}, {"type": "text", "text": "確認します。"},
                      {"type": "tool_use", "id": "toolu_1", "name": "Bash",
                       "input": {"command": self.command, "description": "run the command"}}]
            stop = "tool_use"
        else:
            blocks, stop = [{"type": "text", "text": "完了しました。"}], "end_turn"
        if not body.get("stream"):
            return h.send(json.dumps({"id": "msg_mock", "type": "message", "role": "assistant", "model": "claude-mock",
                                      "content": blocks, "stop_reason": stop, "stop_sequence": None,
                                      "usage": {"input_tokens": 1, "output_tokens": 1}}, ensure_ascii=False).encode(),
                          "application/json")
        events = [{"type": "message_start", "message": {
            "id": "msg_mock", "type": "message", "role": "assistant", "model": "claude-mock", "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}}]
        for i, b in enumerate(blocks):
            if b["type"] == "text":
                events += [{"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}},
                           {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": b["text"]}}]
            elif b["type"] == "thinking":
                events += [{"type": "content_block_start", "index": i,
                            "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
                           {"type": "content_block_delta", "index": i,
                            "delta": {"type": "thinking_delta", "thinking": b["thinking"]}},
                           {"type": "content_block_delta", "index": i, "delta": {"type": "signature_delta", "signature": "sig"}}]
            else:
                events += [{"type": "content_block_start", "index": i,
                            "content_block": {"type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}},
                           {"type": "content_block_delta", "index": i,
                            "delta": {"type": "input_json_delta", "partial_json": json.dumps(b["input"])}}]
            events.append({"type": "content_block_stop", "index": i})
        events += [{"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                    "usage": {"output_tokens": 1}}, {"type": "message_stop"}]
        h.send(sse(events), "text/event-stream")


class OpenAIResponsesMock(MockServer):
    """Responses API (Codex 用)。"""

    def handle_post(self, h, body):
        n = len(self.requests)
        has_output = any(isinstance(i, dict) and i.get("type") in ("function_call_output", "custom_tool_call_output")
                         for i in body.get("input") or [])
        if not has_output:
            items = [{"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "まずコマンドを実行する"}],
                      "encrypted_content": None},
                     {"type": "message", "role": "assistant", "id": "msg_0", "content": [{"type": "output_text", "text": "確認します。"}]},
                     {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "exec_command",
                      "arguments": json.dumps({"cmd": self.command})}]
        else:
            items = [{"type": "message", "role": "assistant", "id": "msg_1",
                      "content": [{"type": "output_text", "text": "完了しました。"}]}]
        events = [{"type": "response.created", "response": {"id": f"resp_{n}"}}]
        events += [{"type": "response.output_item.done", "output_index": i, "item": it} for i, it in enumerate(items)]
        events.append({"type": "response.completed", "response": {"id": f"resp_{n}", "usage": USAGE_OPENAI}})
        h.send(sse(events), "text/event-stream")
