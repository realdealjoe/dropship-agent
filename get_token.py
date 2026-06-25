#!/usr/bin/env python3
"""
Run this once to get your Shopify API access token via OAuth.
Usage: python3 get_token.py <client_secret>
"""
import sys
import json
import webbrowser
import urllib.parse
import httpx
from http.server import HTTPServer, BaseHTTPRequestHandler

STORE = "8k0gdf-iz.myshopify.com"
CLIENT_ID = "9700bc86846ff8258d362cc51edd0539"
SCOPES = "read_products,write_products,read_orders,write_orders,read_fulfillments,write_fulfillments,read_themes,write_themes,read_customers,write_customers,read_inventory,write_inventory,read_content,write_content,read_discounts,write_discounts,read_price_rules,write_price_rules,read_metafields,write_metafields,read_locations,read_gift_cards,write_gift_cards"
REDIRECT_URI = "http://localhost:8080/callback"

_client_secret = None
_access_token = [None]  # mutable so handler can write to it


class OAuthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return

        params = urllib.parse.parse_qs(parsed.query)
        code = params.get("code", [None])[0]

        if not code:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Missing code parameter")
            return

        r = httpx.post(
            f"https://{STORE}/admin/oauth/access_token",
            json={"client_id": CLIENT_ID, "client_secret": _client_secret, "code": code},
            timeout=15,
        )
        data = r.json()
        token = data.get("access_token")
        _access_token[0] = token

        if token:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"""
                <html><body style="font-family:sans-serif;padding:40px;background:#1a1a1a;color:#fff">
                <h2 style="color:#4ade80">Success! Access token captured.</h2>
                <p>You can close this tab and return to the terminal.</p>
                </body></html>
            """)
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(f"Error: {data}".encode())

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 get_token.py <client_secret>")
        sys.exit(1)

    _client_secret = sys.argv[1]

    auth_url = (
        f"https://{STORE}/admin/oauth/authorize"
        f"?client_id={CLIENT_ID}"
        f"&scope={urllib.parse.quote(SCOPES)}"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI, safe='')}"
        f"&state=dropship_setup"
    )

    print("\nOpening browser for Shopify authorization...")
    print(f"If it doesn't open automatically, visit:\n{auth_url}\n")
    webbrowser.open(auth_url)

    print("Waiting for you to click 'Install' in the browser...")
    server = HTTPServer(("localhost", 8080), OAuthHandler)
    server.handle_request()

    token = _access_token[0]
    if token:
        env_path = "/Users/josephcook/dropship-agent/.env"
        try:
            with open(env_path) as f:
                content = f.read()
        except FileNotFoundError:
            with open("/Users/josephcook/dropship-agent/.env.example") as f:
                content = f.read()

        lines = content.splitlines()
        replaced = False
        for i, line in enumerate(lines):
            if line.startswith("SHOPIFY_ACCESS_TOKEN="):
                lines[i] = f"SHOPIFY_ACCESS_TOKEN={token}"
                replaced = True
        if not replaced:
            lines.append(f"SHOPIFY_ACCESS_TOKEN={token}")

        with open(env_path, "w") as f:
            f.write("\n".join(lines))

        print(f"\nToken saved to .env automatically.")
        print(f"Token: {token}")
    else:
        print("Failed to get access token.")
