"""Mock OpenAI-compatible LLM server for the live e2e (tests/live_e2e.sh).

Speaks just enough of /chat/completions to drive synth (propose + merge) and
the now-page generator, with deterministic Portuguese content. Port from argv.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prompt = body["messages"][0]["content"]
        if "Reply with STRICT JSON" in prompt:          # synth: propose
            content = json.dumps({
                "skip": False, "slug": "pipeline-vendas-dedup",
                "title": "Dedup no pipeline de vendas", "section": "topics",
                "tags": ["pipeline", "vendas", "dedup"], "related": [],
                "distill": "Dedup de pedidos por order_id + janela de 24h no pipeline de vendas.",
            })
        elif "WORKING-MEMORY" in prompt:                # now-page
            content = ("## Contexto\nPipeline de vendas: dedup de pedidos duplicados.\n\n"
                       "## Estado atual\nRegra order_id + janela 24h definida e validada.\n\n"
                       "## Próximos passos\n- [ ] aplicar a regra no job noturno\n\n"
                       "## Arquivos-chave\n- etl/dedup.sql — a regra\n")
        else:                                           # synth: merge
            content = ("## Decisão\nDeduplicar por `order_id` + janela de 24h.\n\n"
                       "Motivo: reprocessamentos criavam pedidos duplicados no dashboard.\n")
        payload = json.dumps({"choices": [{"message": {"content": content}}]})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload.encode())

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
