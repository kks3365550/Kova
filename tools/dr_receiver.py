# -*- coding: utf-8 -*-
"""
dr_receiver.py - VM 재해복구(DR) 결과 실시간 수신 경량 HTTP 서버
VM(100.127.224.56)으로부터 복원 로그 및 3-Tier 채점표를 수신하여 data/dr_result/에 저장합니다.
"""

import os
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "dr_result")
os.makedirs(OUTPUT_DIR, exist_ok=True)

class DRHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        
        path = self.path.strip("/")
        if path == "log":
            target = os.path.join(OUTPUT_DIR, "dr_restore_log.txt")
            with open(target, "wb") as f:
                f.write(body)
            print(f"[+] 복원 로그 수신 완료: {len(body)} bytes -> {target}", flush=True)
        elif path == "scorecard":
            target = os.path.join(OUTPUT_DIR, "dr_scorecard.txt")
            with open(target, "wb") as f:
                f.write(body)
            print(f"[+] 채점표 수신 완료: {len(body)} bytes -> {target}", flush=True)
        elif path == "json":
            target = os.path.join(OUTPUT_DIR, "dr_scorecard.json")
            with open(target, "wb") as f:
                f.write(body)
            print(f"[+] JSON 채점표 수신 완료: {len(body)} bytes -> {target}", flush=True)
        else:
            print(f"[?] 알 수 없는 POST 경로: {path}", flush=True)

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK\n")

    def do_GET(self):
        path = self.path.strip("/")
        if path == "done":
            done_file = os.path.join(OUTPUT_DIR, "dr_done.flag")
            with open(done_file, "w", encoding="utf-8") as f:
                f.write("DONE\n")
            print("[🎉] 전체 복원 및 채점 완료 신호 수신! dr_done.flag 생성됨.", flush=True)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"DR_COMPLETED\n")
            # 잠시 후 서버 정상 종료
            sys.exit(0)
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"DR Receiver is RUNNING\n")

    def log_message(self, format, *args):
        # 불필요한 HTTP 액세스 로그 억제
        pass

def main():
    port = 8999
    server = HTTPServer(("0.0.0.0", port), DRHandler)
    print(f"[*] DR 결과 수신 서버 기동: 0.0.0.0:{port} (저장소: {OUTPUT_DIR})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except SystemExit:
        pass
    print("[*] DR 수신 서버 정상 종료.", flush=True)

if __name__ == "__main__":
    main()
