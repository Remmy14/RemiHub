import json
from pathlib import Path

from backend.tasks.finance_worker import upsert_plaid_item


DEFAULT_INPUT_FILE = Path("/home/alex/plaid-poc/plaid_items.json")


def seed_plaid_items(input_file: Path = DEFAULT_INPUT_FILE) -> None:
    if not input_file.exists():
        raise FileNotFoundError(f"Plaid items file not found: {input_file}")

    data = json.loads(input_file.read_text())

    items = data.get("items", [])
    if not items:
        print("No Plaid items found.")
        return

    for item in items:
        label = item["label"]
        item_id = item["item_id"]
        access_token = item["access_token"]

        stored = upsert_plaid_item(
            label=label,
            item_id=item_id,
            access_token=access_token,
            environment=item.get("environment", "production"),
            enabled=item.get("enabled", True),
        )

        print(
            f"Stored Plaid Item: "
            f"{stored['label']} | {stored['item_id']} | enabled={stored['enabled']}"
        )

    print(f"Done. Seeded {len(items)} Plaid item(s).")


if __name__ == "__main__":
    seed_plaid_items()
