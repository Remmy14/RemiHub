from datetime import date

from backend.tasks.finance_worker import upsert_manual_asset


def main() -> None:
    """
    One-off helper for creating/updating manual finance assets.

    Edit the values below, run the task, and the monthly snapshot will use
    the latest stored values.
    """
    today = date.today().isoformat()

    manual_assets = [
        {
            "label": "Via Benefits HSA",
            "asset_category": "hsa",
            "current_value": 14355.65,
            "as_of_date": today,
            "include_in_net_worth": True,
            "notes": "Manual value; Plaid institution unsupported.",
        },
        {
            "label": "T. Rowe Price 401k",
            "asset_category": "retirement",
            "current_value": 103383.89,
            "as_of_date": today,
            "include_in_net_worth": True,
            "notes": "Manual value; Plaid connectivity unavailable.",
        },
        {
            "label": "Home",
            "asset_category": "home",
            "current_value": 585900,
            "as_of_date": today,
            "include_in_net_worth": True,
            "notes": "Manual home value placeholder. Zillow provider stubbed.",
        },
        {
            "label": "2025 Kia Telluride",
            "asset_category": "vehicle",
            "current_value": 45590,
            "as_of_date": today,
            "include_in_net_worth": True,
            "notes": "Manual vehicle value placeholder. KBB provider stubbed.",
        },
        {
            "label": "2014 Toyota Camry",
            "asset_category": "vehicle",
            "current_value": 9320,
            "as_of_date": today,
            "include_in_net_worth": True,
            "notes": "Manual vehicle value placeholder. KBB provider stubbed.",
        },
    ]

    for asset in manual_assets:
        stored = upsert_manual_asset(**asset)
        print(
            f"Stored manual asset: "
            f"{stored['label']} | "
            f"{stored['asset_category']} | "
            f"${stored['current_value']:,.2f} | "
            f"as of {stored['as_of_date']}"
        )


if __name__ == "__main__":
    main()