from backend.tasks.finance_worker import discover_plaid_accounts


def main() -> None:
    accounts = discover_plaid_accounts()

    print(f"Discovered/upserted {len(accounts)} Plaid account(s).")
    print()

    for account in accounts:
        include_flag = "INCLUDED" if account["include_in_net_worth"] else "excluded"
        balance = account.get("balance_current")

        if balance is None:
            balance_text = "unknown"
        else:
            balance_text = f"${balance:,.2f}"

        print(
            f"- {account['institution_label']} | "
            f"{account['name']} | "
            f"{account['type']}/{account['subtype']} | "
            f"{account['asset_category']} | "
            f"****{account.get('mask') or '----'} | "
            f"{balance_text} | "
            f"{include_flag}"
        )


if __name__ == "__main__":
    main()
