import os
from flask import Flask, jsonify, request, render_template_string
from dotenv import load_dotenv

from plaid.api import plaid_api
from plaid.api_client import ApiClient
from plaid.configuration import Configuration
from plaid.model.country_code import CountryCode
from plaid.model.products import Products
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.accounts_get_request import AccountsGetRequest

# RemiHub helpers for existing encrypted Plaid Items.
# Run this from the RemiHub project root so `backend.*` imports resolve.
from backend.tasks.finance_worker import (
    decrypt_access_token,
    get_enabled_plaid_items,
    get_plaid_client,
    mark_plaid_item_error,
    mark_plaid_item_success,
)


load_dotenv(dotenv_path='config/remihub.env')

app = Flask(__name__)

PLAID_ENV = os.environ.get("PLAID_ENV", "sandbox")
PLAID_CLIENT_ID = os.environ["PLAID_CLIENT_ID"]
PLAID_SECRET = os.environ["PLAID_SECRET"]
PLAID_PRODUCTS = [
    Products(product.strip())
    for product in os.environ.get("PLAID_PRODUCTS", "transactions").split(",")
]
PLAID_COUNTRY_CODES = [
    CountryCode(code.strip())
    for code in os.environ.get("PLAID_COUNTRY_CODES", "US").split(",")
]

HOSTS = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}

configuration = Configuration(
    host=HOSTS[PLAID_ENV],
    api_key={
        "clientId": PLAID_CLIENT_ID,
        "secret": PLAID_SECRET,
    },
)

client = plaid_api.PlaidApi(ApiClient(configuration))

LAST_ACCESS_TOKEN = None


HTML = """
<!doctype html>
<html>
  <head>
    <title>RemiHub Plaid POC</title>
    <style>
      body {
        font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        max-width: 900px;
        margin: 32px auto;
        padding: 0 16px;
        line-height: 1.45;
      }
      button, select {
        font: inherit;
        padding: 8px 12px;
        margin: 4px 0;
      }
      section {
        border: 1px solid #ccc;
        border-radius: 8px;
        padding: 16px;
        margin: 16px 0;
      }
      pre {
        background: #111;
        color: #eee;
        padding: 16px;
        border-radius: 8px;
        overflow: auto;
        min-height: 120px;
      }
      .row {
        display: flex;
        gap: 8px;
        align-items: center;
        flex-wrap: wrap;
      }
    </style>
  </head>
  <body>
    <h1>RemiHub Plaid POC</h1>
    <p>Environment for new connections: {{ env }}</p>
    <p>Products for new connections: {{ products }}</p>

    <section>
      <h2>Add new account</h2>
      <p>
        This is the original flow. It creates a new Plaid Item, exchanges the
        public token, and prints the access token to the server console.
      </p>
      <button id="connect">Connect new account</button>
    </section>

    <section>
      <h2>Repair existing account</h2>
      <p>
        Use this for ITEM_LOGIN_REQUIRED. It launches Plaid Link in update mode
        for an existing Item from finance_plaid_items.
      </p>
      <div class="row">
        <select id="plaid-item-select">
          <option value="">Loading Plaid Items...</option>
        </select>
        <button id="refresh-items">Refresh list</button>
        <button id="repair-item">Repair selected account</button>
      </div>
    </section>

    <pre id="output"></pre>

    <script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
    <script>
      const output = document.getElementById("output");
      const itemSelect = document.getElementById("plaid-item-select");

      function show(value) {
        output.textContent = typeof value === "string"
          ? value
          : JSON.stringify(value, null, 2);
      }

      async function loadPlaidItems() {
        show("Loading Plaid Items...");

        const response = await fetch("/plaid_items");
        const data = await response.json();

        itemSelect.innerHTML = "";

        if (!data.items || data.items.length === 0) {
          const option = document.createElement("option");
          option.value = "";
          option.textContent = "No enabled Plaid Items found";
          itemSelect.appendChild(option);
          show(data);
          return;
        }

        for (const item of data.items) {
          const option = document.createElement("option");
          option.value = item.id;
          option.textContent = `${item.label} (${item.environment || "production"})`;
          itemSelect.appendChild(option);
        }

        show({ status: "loaded", items: data.items });
      }

      document.getElementById("connect").onclick = async () => {
        show("Creating link token for new account...");

        const linkTokenResponse = await fetch("/create_link_token", {
          method: "POST"
        });

        const linkTokenData = await linkTokenResponse.json();

        if (!linkTokenData.link_token) {
          show(linkTokenData);
          return;
        }

        const handler = Plaid.create({
          token: linkTokenData.link_token,
          onSuccess: async (public_token, metadata) => {
            show("Exchanging public token...");

            const exchangeResponse = await fetch("/exchange_public_token", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ public_token, metadata })
            });

            const exchangeData = await exchangeResponse.json();
            show(exchangeData);
          },
          onExit: (err, metadata) => {
            show({ err, metadata });
          }
        });

        handler.open();
      };

      document.getElementById("refresh-items").onclick = loadPlaidItems;

      document.getElementById("repair-item").onclick = async () => {
        const plaidItemId = itemSelect.value;

        if (!plaidItemId) {
          show("Select a Plaid Item first.");
          return;
        }

        show("Creating update-mode link token...");

        const linkTokenResponse = await fetch("/create_update_link_token", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ plaid_item_id: plaidItemId })
        });

        const linkTokenData = await linkTokenResponse.json();

        if (!linkTokenData.link_token) {
          show(linkTokenData);
          return;
        }

        const handler = Plaid.create({
          token: linkTokenData.link_token,
          onSuccess: async (_public_token, metadata) => {
            show("Update-mode flow completed. Verifying account access...");

            // In update mode, do NOT exchange public_token. The existing
            // access_token remains the token for this Item.
            const completeResponse = await fetch("/plaid_update_complete", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                plaid_item_id: linkTokenData.plaid_item_id,
                metadata
              })
            });

            const completeData = await completeResponse.json();
            show(completeData);
          },
          onExit: (err, metadata) => {
            show({ err, metadata });
          }
        });

        handler.open();
      };

      loadPlaidItems();
    </script>
  </body>
</html>
"""


def _safe_accounts(accounts):
    return [
        {
            "name": account.get("name"),
            "official_name": account.get("official_name"),
            "type": account.get("type"),
            "subtype": account.get("subtype"),
            "mask": account.get("mask"),
            "balances": account.get("balances"),
        }
        for account in accounts
    ]


def _get_enabled_plaid_item_by_id(plaid_item_id: str) -> dict | None:
    for item in get_enabled_plaid_items():
        if item["id"] == plaid_item_id:
            return item
    return None


@app.get("/")
def index():
    return render_template_string(
        HTML,
        env=PLAID_ENV,
        products=", ".join(str(product) for product in PLAID_PRODUCTS),
    )


@app.get("/plaid_items")
def plaid_items():
    items = get_enabled_plaid_items()

    return jsonify(
        {
            "items": [
                {
                    "id": item["id"],
                    "label": item["label"],
                    "item_id": item["item_id"],
                    "environment": item.get("environment") or "production",
                    "enabled": item["enabled"],
                }
                for item in items
            ]
        }
    )


@app.post("/create_link_token")
def create_link_token():
    request_data = LinkTokenCreateRequest(
        products=PLAID_PRODUCTS,
        client_name="RemiHub Finance",
        country_codes=PLAID_COUNTRY_CODES,
        language="en",
        user=LinkTokenCreateRequestUser(
            client_user_id="remihub-admin"
        ),
    )

    response = client.link_token_create(request_data)
    return jsonify(response.to_dict())


@app.post("/create_update_link_token")
def create_update_link_token():
    data = request.get_json() or {}
    plaid_item_id = data.get("plaid_item_id")

    if not plaid_item_id:
        return jsonify({"error": "plaid_item_id is required"}), 400

    item = _get_enabled_plaid_item_by_id(plaid_item_id)

    if not item:
        return jsonify({"error": "Enabled Plaid Item not found"}), 404

    environment = item.get("environment") or "production"
    access_token = decrypt_access_token(item["access_token_encrypted"])
    item_client = get_plaid_client(environment)

    # Update mode: include access_token and do not include products.
    request_data = LinkTokenCreateRequest(
        access_token=access_token,
        client_name="RemiHub Finance",
        country_codes=PLAID_COUNTRY_CODES,
        language="en",
        user=LinkTokenCreateRequestUser(
            client_user_id="remihub-admin"
        ),
    )

    response = item_client.link_token_create(request_data)
    response_dict = response.to_dict()

    return jsonify(
        {
            "link_token": response_dict["link_token"],
            "expiration": response_dict.get("expiration"),
            "plaid_item_id": plaid_item_id,
            "label": item["label"],
            "environment": environment,
        }
    )


@app.post("/exchange_public_token")
def exchange_public_token():
    global LAST_ACCESS_TOKEN

    data = request.get_json()
    public_token = data["public_token"]

    exchange_request = ItemPublicTokenExchangeRequest(
        public_token=public_token
    )

    exchange_response = client.item_public_token_exchange(exchange_request)
    exchange_dict = exchange_response.to_dict()

    LAST_ACCESS_TOKEN = exchange_dict["access_token"]

    accounts_response = client.accounts_get(
        AccountsGetRequest(access_token=LAST_ACCESS_TOKEN)
    )

    accounts = accounts_response.to_dict().get("accounts", [])

    print("\n=== PLAID ITEM CONNECTED ===")
    print(f"item_id: {exchange_dict.get('item_id')}")
    print("access_token:")
    print(LAST_ACCESS_TOKEN)
    print("=== END PLAID ITEM ===\n")

    return jsonify(
        {
            "status": "connected",
            "item_id": exchange_dict.get("item_id"),
            "accounts": _safe_accounts(accounts),
            "note": "Access token printed to server console. Do not paste it into chat.",
        }
    )


@app.post("/plaid_update_complete")
def plaid_update_complete():
    data = request.get_json() or {}
    plaid_item_id = data.get("plaid_item_id")

    if not plaid_item_id:
        return jsonify({"error": "plaid_item_id is required"}), 400

    item = _get_enabled_plaid_item_by_id(plaid_item_id)

    if not item:
        return jsonify({"error": "Enabled Plaid Item not found"}), 404

    environment = item.get("environment") or "production"
    access_token = decrypt_access_token(item["access_token_encrypted"])
    item_client = get_plaid_client(environment)

    try:
        accounts_response = item_client.accounts_get(
            AccountsGetRequest(access_token=access_token)
        )
        accounts = accounts_response.to_dict().get("accounts", [])

        mark_plaid_item_success(plaid_item_id)

        return jsonify(
            {
                "status": "updated_and_verified",
                "label": item["label"],
                "environment": environment,
                "accounts": _safe_accounts(accounts),
                "note": "Update mode completed. Existing access token was reused; no token exchange was performed.",
            }
        )

    except Exception as exc:
        mark_plaid_item_error(plaid_item_id, str(exc))
        return jsonify(
            {
                "status": "update_completed_but_verification_failed",
                "label": item["label"],
                "environment": environment,
                "error": str(exc),
            }
        ), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8765, debug=False)
