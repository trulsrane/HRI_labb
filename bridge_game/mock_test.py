import http.server
import socketserver
import threading
import json
import time
import sys
import os

PORT = 8000

# A simple mock queue to simulate screen interactions
class MockQueue:
    def __init__(self):
        self.data = None
    def put(self, item):
        self.data = item
    def get_blocking(self):
        while self.data is None:
            time.sleep(0.1)
        item = self.data
        self.data = None
        return item

choice_queue = MockQueue()

# Minimal interceptor to catch button actions from your browser clicks
class MockTabletHTTPHandler(http.server.SimpleHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/tablet_input_mock":
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            payload = json.loads(post_data.decode('utf-8'))
            
            print(f"\n[MOCK ROBOT SEES TOUCH]: {payload}")
            choice_queue.put(payload)
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"success"}')
        else:
            super().do_POST()

def run_server():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    target_dir = os.path.abspath(os.path.join(script_dir, "../dashboard/static/tablet"))
    
    print(f"[MOCK] Target HTML directory configured: {target_dir}")
    
    class TargetDirHandler(MockTabletHTTPHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=target_dir, **kwargs)

    with socketserver.TCPServer(("", PORT), TargetDirHandler) as httpd:
        print(f"[MOCK] Serving HTML files locally at http://localhost:{PORT}")
        httpd.serve_forever()

def run_simulated_scenario():
    print("\n--- STARTING FLOW SIMULATION ---")
    print("Open your browser to the URLs printed below to interact with your pages!\n")
    
    # 1. Start Screen
    print(f"1. GREETING PHASE: Open -> http://localhost:{PORT}/startGame.html")
    print("Waiting for you to click 'Play' on the browser...")
    user_click = choice_queue.get_blocking()
    
    # 2. Rules Screen
    print("\n2. RULES PHASE: Open -> http://localhost:{PORT}/rules.html")
    print("Waiting for you to click 'Ready'...")
    user_click = choice_queue.get_blocking()
    
    # 3. Picked Level Screen
    params = "label=Junior&img=https://people.cs.umu.se/~id23sem/bridgegame_img/Medium.jpg"
    print(f"\n3. PICKED LEVEL PHASE: Open -> http://localhost:{PORT}/pickedLevel.html?{params}")
    print("Waiting for you to click 'Ready!'...")
    user_click = choice_queue.get_blocking()
    
    # 4. Solution Screen
    sol_params = "label=Junior&img=https://people.cs.umu.se/~id23sem/bridgegame_img/Medium_sol.jpg"
    print(f"\n4. SOLUTION CHECK PHASE: Open -> http://localhost:{PORT}/solution.html?{sol_params}")
    print("Waiting for you to choose if it matches...")
    user_click = choice_queue.get_blocking()
    
    print("\n Scenario completed")
    sys.exit(0)

if __name__ == "__main__":
    # Start the local server in the background
    srv_thread = threading.Thread(target=run_server, daemon=True)
    srv_thread.start()
    time.sleep(1)
    
    # Run the simulation sequence
    run_simulated_scenario()