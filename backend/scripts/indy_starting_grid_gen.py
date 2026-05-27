# backend/scripts/update_indy_pool_starting_grid.py

import re
import requests
from bs4 import BeautifulSoup

from backend.database.database import get_db_conn


STARTING_GRID_URL = "https://www.indycar.com/Schedule/2026/Indianapolis-500/starting-grid"


POSITION_RE = re.compile(r"^(\d+)(?:ST|ND|RD|TH)$")
FIRST_NAME_RE = re.compile(r"^#(?P<number>\d+) driver first name:\s*(?P<first>.+)$")
LAST_NAME_RE = re.compile(r"^#(?P<number>\d+) driver last name:\s*(?P<last>.+)$")


def fetch_starting_grid() -> list[dict]:
    response = requests.get(STARTING_GRID_URL, timeout=20)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    entries = []

    for card in soup.select("div.driver-profile-container"):
        position_tag = card.select_one("h3.starting-position")
        first_tag = card.select_one('span.sr-only:-soup-contains("driver first name:")')
        last_tag = card.select_one('span.sr-only:-soup-contains("driver last name:")')

        if not position_tag or not first_tag or not last_tag:
            continue

        position_text = position_tag.get_text("", strip=True)
        position_match = POSITION_RE.match(position_text.upper())

        if not position_match:
            continue

        first_parent = first_tag.parent
        last_parent = last_tag.parent

        first_name = first_parent.get_text(" ", strip=True).replace(
            first_tag.get_text(" ", strip=True),
            "",
        ).strip()

        last_name = last_parent.get_text(" ", strip=True).replace(
            last_tag.get_text(" ", strip=True),
            "",
        ).strip()

        number_match = re.search(r"#(\d+)", first_tag.get_text(" ", strip=True))
        if not number_match:
            continue

        entries.append({
            "starting_position": int(position_match.group(1)),
            "car_number": number_match.group(1),
            "driver_name": f"{first_name} {last_name}",
        })

    if len(entries) != 33:
        raise ValueError(f"Expected 33 starting grid entries, found {len(entries)}")

    return sorted(entries, key=lambda entry: entry["starting_position"])


def update_starting_grid(entries: list[dict]) -> None:
    conn = get_db_conn()

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "TRUNCATE TABLE indy_pool_starting_grid RESTART IDENTITY"
                )

                cur.executemany(
                    """
                    INSERT INTO indy_pool_starting_grid
                        (starting_position, car_number, driver_name)
                    VALUES
                        (%s, %s, %s)
                    """,
                    [
                        (
                            entry["starting_position"],
                            entry["car_number"],
                            entry["driver_name"],
                        )
                        for entry in entries
                    ],
                )
    finally:
        conn.close()


def main() -> None:
    entries = fetch_starting_grid()
    update_starting_grid(entries)

    print(f"Updated indy_pool_starting_grid with {len(entries)} drivers.")
    for entry in entries:
        print(
            f'{entry["starting_position"]:>2}. '
            f'#{entry["car_number"]} {entry["driver_name"]}'
        )


if __name__ == "__main__":
    main()
