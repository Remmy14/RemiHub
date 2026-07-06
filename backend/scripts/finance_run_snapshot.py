from backend.tasks.finance_worker import create_monthly_finance_snapshot


def main() -> None:
    result = create_monthly_finance_snapshot(force=True)

    print("Finance snapshot result:")
    for key, value in result.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
