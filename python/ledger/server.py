import json
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer
from python.ledger.chain import LedgerChain

logger = logging.getLogger(__name__)

class LedgerHTTPRequestHandler(BaseHTTPRequestHandler):
    # Ensure all instances share the same chain for local development
    chain = LedgerChain()

    def do_POST(self):
        if self.path == "/chain/tx" or self.path == "/chain/import":
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            
            try:
                data = json.loads(post_data.decode('utf-8'))
                
                if self.path == "/chain/tx":
                    node_id = data.get("id")
                    core_hash = data.get("hash")
                    tx_type = data.get("type", "html_nn_anchor")
                    parent_chaincode_ids = data.get("parent_chaincode_ids")
                    
                    if not node_id or not core_hash:
                        self._send_response(400, {"error": "Missing id or hash"})
                        return
                        
                    result = self.chain.append_transaction(
                        node_id=node_id, 
                        core_hash=core_hash, 
                        tx_type=tx_type,
                        parent_chaincode_ids=parent_chaincode_ids
                    )
                    
                    chaincode_id = result["chaincode_id"]
                    tx_id = result["tx_id"]
                    
                    self._send_response(200, {
                        "tx_id": chaincode_id,
                        "raw_tx_id": tx_id,
                        "status": "anchored",
                        "block": result["block"]
                    })
                    
                elif self.path == "/chain/import":
                    if not isinstance(data, list):
                        self._send_response(400, {"error": "Expected a list of blocks"})
                        return
                        
                    result = self.chain.import_blocks(data)
                    if result["success"]:
                        self._send_response(200, result)
                    else:
                        self._send_response(400, result)
                        
            except Exception as e:
                logger.error(f"Error processing POST {self.path}: {e}")
                self._send_response(500, {"error": str(e)})

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        
        try:
            if path == "/chain/blocks" or path == "/chain/export":
                blocks = self.chain.get_all_blocks()
                self._send_response(200, {"blocks": blocks, "height": self.chain.height})
                
            elif path == "/chain/status":
                self._send_response(200, {
                    "height": self.chain.height,
                    "client_id": self.chain.client_id,
                    "latest_block_hash": self.chain.last_block_hash,
                    "public_key": self.chain.crypto.get_public_key_hex()
                })
                
            elif path.startswith("/chain/tx"):
                qs = parse_qs(parsed_url.query)
                target_id = qs.get("id", [None])[0]
                
                if not target_id:
                    # Also try /chain/tx/<id>
                    parts = path.split("/")
                    if len(parts) == 4:
                        target_id = parts[3]
                        
                if not target_id:
                    self._send_response(400, {"error": "Missing tx id parameter"})
                    return
                    
                # Search all blocks for the transaction
                blocks = self.chain.get_all_blocks()
                for block in blocks:
                    for tx in block.get("transactions", []):
                        tx_chaincode_id = f"tala-chain:{self.chain.client_id}:{block['height']}:{block['block_hash']}:{tx['tx_id']}"
                        if tx["tx_id"] == target_id or tx_chaincode_id == target_id:
                            self._send_response(200, {"transaction": tx, "block_hash": block["block_hash"], "height": block["height"]})
                            return
                self._send_response(404, {"error": "Transaction not found"})
                
            else:
                self._send_response(404, {"error": "Not Found"})
                
        except Exception as e:
            logger.error(f"Error processing GET {self.path}: {e}")
            self._send_response(500, {"error": str(e)})

    def _send_response(self, status_code: int, payload: dict):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

def run_server(host: str = "127.0.0.1", port: int = 8332):
    logging.basicConfig(level=logging.INFO)
    logger.info(f"Starting Tala Local Ledger on http://{host}:{port}")
    server = HTTPServer((host, port), LedgerHTTPRequestHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == "__main__":
    run_server()
