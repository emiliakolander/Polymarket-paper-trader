"""Contract test: the research request goes through the REAL anthropic SDK to a
local fake server. Verifies request shape + response parsing. No network, no cost."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

anthropic = pytest.importorskip("anthropic")

from paperbot.budget import AIBudget  # noqa: E402
from paperbot.config import load_config  # noqa: E402
from paperbot.db import Database  # noqa: E402
from paperbot.research import ResearchAgent  # noqa: E402

from .conftest import NOW  # noqa: E402
from .test_strategies import GOOD, market_with_books  # noqa: E402

CAPTURED: list[dict] = []

RESPONSE = {
    "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
    "stop_reason": "end_turn", "stop_sequence": None,
    "content": [
        {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search",
         "input": {"query": "event 101"}},
        {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [
            {"type": "web_search_result", "url": "https://example.org/a", "title": "A",
             "encrypted_content": "x", "page_age": None}]},
        {"type": "text", "text": json.dumps(GOOD)},
    ],
    "usage": {"input_tokens": 12000, "output_tokens": 900, "cache_read_input_tokens": 0,
              "cache_creation_input_tokens": 0, "server_tool_use": {"web_search_requests": 1}},
}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        CAPTURED.append({"path": self.path, "body": json.loads(body),
                         "auth": self.headers.get("x-api-key")})
        out = json.dumps(RESPONSE).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def test_research_request_through_real_sdk(tmp_path):
    server = HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        cfg = load_config(overrides={"database_path": str(tmp_path / "c.sqlite"),
                                     "research": {"enabled": True}})
        db = Database(cfg.database_path)
        factory = lambda: anthropic.Anthropic(api_key="sk-ant-test", max_retries=0,
                                              base_url=f"http://127.0.0.1:{server.server_port}")
        agent = ResearchAgent(cfg, db, AIBudget(db, cfg.budget, cfg.research), factory)
        m = market_with_books(cfg, yes_asks=[(0.55, 500)], no_asks=[(0.47, 500)])
        r = agent.research(m, NOW, "Resolves YES if X happens.")
    finally:
        server.shutdown()

    assert r.status == "OK", r.error
    assert r.probability == 0.70 and len(r.evidence) == 2
    body = CAPTURED[-1]["body"]
    assert CAPTURED[-1]["path"] == "/v1/messages"
    assert body["model"] == "claude-opus-5-5"
    assert body["tools"] == [{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}]
    assert body["output_config"] == {"effort": "low"}
    assert "thinking" not in body  # Opus 5.5 rejects disabled thinking; we omit it
    expected = 12000 * 4e-6 + 900 * 20e-6 + 1 * 0.01
    assert r.cost_usd == pytest.approx(expected)
    assert db.ai_spend_total() == pytest.approx(expected)
