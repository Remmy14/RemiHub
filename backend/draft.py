# Python Imports

# Local Imports
from backend.database.database import get_db_conn, put_db_conn
from backend.leaderboard import save_pool_standings_to_db


def set_race_draft_status(status: str, pool_id: int = 0, ):
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            if pool_id == 0:
                cur.execute("UPDATE indy_pool_draft_status SET event_status=%s;", (status, ))
            else:
                cur.execute("UPDATE indy_pool_draft_status SET event_status=%s WHERE pool_id=%s;", (status, pool_id, ))

        conn.commit()
    finally:
        put_db_conn(conn)

def get_starting_grid_status(pool_id: int) -> list[dict]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 
                    sg.car_number,
                    sg.driver_name,
                    sg.starting_position,
                    a.participant_name  -- this may be NULL if not yet assigned
                FROM indy_pool_starting_grid sg
                LEFT JOIN indy_pool_assignments a
                    ON sg.car_number = a.car_number AND a.pool_id = %s
                ORDER BY sg.starting_position ASC
            """, (pool_id,))

            rows = cur.fetchall()
            return [
                {
                    'number': row[0],
                    'name': row[1],
                    'starting_position': row[2],
                    'takenBy': row[3],  # May be None if unassigned
                    'car_image_url': f'/static/images/{row[0]}.png',
                }
                for row in rows
            ]
    finally:
        put_db_conn(conn)

def assign_driver_to_participant(pool_id: int, participant_name: str, car_number: str) -> bool:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            # Verify driver is available
            cur.execute("""
                SELECT sg.driver_name
                FROM indy_pool_starting_grid sg
                WHERE sg.car_number = %s
                AND NOT EXISTS (
                    SELECT 1 FROM indy_pool_assignments a
                    WHERE a.car_number = sg.car_number AND a.pool_id = %s
                )
            """, (car_number, pool_id))
            result = cur.fetchone()

            if not result:
                return False  # Already taken or invalid

            driver_name = result[0]

            # Insert into pool-specific assignments
            cur.execute("""
                INSERT INTO indy_pool_assignments (participant_name, car_number, driver_name, pool_id)
                VALUES (%s, %s, %s, %s)
            """, (participant_name, car_number, driver_name, pool_id))

        conn.commit()
        return True
    finally:
        put_db_conn(conn)

def get_current_draft_pick(pool_id: int, ) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            # Get current pick number
            cur.execute("SELECT current_pick FROM indy_pool_draft_status WHERE pool_id=%s LIMIT 1", (pool_id, ))
            current_pick = cur.fetchone()[0]

            # Step 2: Get the full draft order
            cur.execute("SELECT participant_name FROM indy_pool_draft_order WHERE pool_id=%s ORDER BY pick_position ASC", (pool_id, ))
            full_order = [row[0] for row in cur.fetchall()]
            participant = full_order[current_pick - 1]

            return {
                'current_pick': current_pick,
                'participant': participant
            }
    finally:
        put_db_conn(conn)

def get_current_draft_status(pool_id: int, ):
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            # Get current draft status
            cur.execute("SELECT current_pick, total_picks FROM indy_pool_draft_status WHERE pool_id=%s LIMIT 1", (pool_id, ))
            current_pick, total_picks = cur.fetchone()
            return current_pick, total_picks
    finally:
        put_db_conn(conn)

def advance_draft(pool_id: int, ):
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            # Get current draft status
            current_pick, total_picks = get_current_draft_status(pool_id)

            # Get full draft order
            cur.execute("SELECT participant_name FROM indy_pool_draft_order WHERE pool_id=%s ORDER BY pick_position ASC", (pool_id, ))
            full_order = [row[0] for row in cur.fetchall()]
            num_participants = len(full_order)

            if num_participants == 0:
                return  # No participants, nothing to do

            # Step 1: Increment total_picks
            total_picks += 1

            # Step 2: Determine round number
            round_number = (total_picks - 1) // num_participants

            # Step 3: Advance current_pick based on round
            if round_number % 2 == 0:
                # Even round → move right (increment)
                current_pick += 1
                if current_pick > num_participants:
                    current_pick = num_participants
            else:
                # Odd round → move left (decrement)
                current_pick -= 1
                if current_pick < 1:
                    current_pick = 1

            # Get max number of picks
            cur.execute("""
                SELECT COUNT(*) FROM indy_pool_starting_grid;
            """)
            max_picks = cur.fetchone()[0]
            if total_picks >= max_picks:
                # The draft is over, all cars have been chosen
                set_race_draft_status('PRE_RACE', pool_id)
                # Initialize the starting grid
                seed_leaderboard()

                # Generate the initial standings
                save_pool_standings_to_db(pool_id)

            # Step 4: Update draft status
            cur.execute("""
                UPDATE indy_pool_draft_status
                SET current_pick = %s, total_picks = %s
                WHERE pool_id = %s;
            """, (current_pick, total_picks, pool_id, ))

        conn.commit()
    finally:
        put_db_conn(conn)

def seed_leaderboard():
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO indy_pool_leaderboard (
                    car_number,
                    position,
                    status,
                    laps_completed,
                    updated_at
                )
                SELECT
                    car_number,
                    starting_position AS position,
                    'Not Started' AS status,
                    0 AS laps_completed,
                    NOW() AS updated_at
                FROM indy_pool_starting_grid;
            """)
        conn.commit()
    finally:
        put_db_conn(conn)

def reset_draft(pool_id: int, pick_order: list[dict]):
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            # Clear existing data
            cur.execute("DELETE FROM indy_pool_draft_order WHERE pool_id = %s", (pool_id,))

            # Insert new draft order using provided positions
            for entry in pick_order:
                name = entry['name']
                position = entry['position']
                cur.execute("""
                    INSERT INTO indy_pool_draft_order (participant_name, pick_position, pool_id)
                    VALUES (%s, %s, %s)
                """, (name, position, pool_id))

            # Reset draft status
            cur.execute("DELETE FROM indy_pool_draft_status WHERE pool_id = %s", (pool_id,))
            cur.execute("""INSERT INTO indy_pool_draft_status 
                    (current_pick, total_picks, event_status, pool_id) 
                    VALUES 
                    (1, 0, 'DRAFT_READY', %s)
                """, (pool_id, ))

        conn.commit()
    finally:
        put_db_conn(conn)

def make_pick(pool_id: int, car_number: str) -> bool:
    state = get_current_draft_pick(pool_id)
    participant = state['participant']

    success = assign_driver_to_participant(pool_id, participant, car_number)
    if success:
        # This is where we can hook in and check if the draft is completed
        advance_draft(pool_id)
    return success

def get_draft_order(pool_id: int, ) -> list[dict]:
    conn = get_db_conn()
    try:
        order = []
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM indy_pool_draft_order WHERE pool_id=%s", (pool_id, ))
            results = cur.fetchall()

            for entry in results:
                order.append({
                    "name": entry[1],
                    "position": entry[2],
                })
            return order
    except:
        pass
    finally:
        put_db_conn(conn)

def reset_draft_to_square_one():
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            # Step 0: Clear previous pool assignments
            print('Deleting Pool Assignments')
            cur.execute("""
                DELETE FROM indy_pool_assignments;
            """)

            # NO LONGER NEEDED WITH MULTI-POOLS
            # # Step 1: Clear out "taken_by" in starting grid
            # cur.execute("""
            #     UPDATE indy_pool_starting_grid
            #     SET taken_by = NULL
            # """)

            print('Resetting Draft Statuses')
            # Step 2: Reset current_pick to 1
            cur.execute("""
                UPDATE indy_pool_draft_status
                SET current_pick = 1, total_picks = 0;
            """)

            print('Clearing draft order')
            # Step 3: Clear out draft order
            cur.execute("""
                TRUNCATE indy_pool_draft_order;
            """)

            print('Clearing leaderboard')
            # Step 4: Clear out driver leaderboard
            cur.execute("""
                TRUNCATE indy_pool_leaderboard;
            """)

            print('Clearing Pool standings cache')
            # Step 5: Clear out pool leaderboard cache
            cur.execute("""
                TRUNCATE indy_pool_standings_cache;
            """)

            print('Removing pools')
            # Step 6: Clear out pools
            cur.execute("""
                DELETE FROM indy_pools;
            """)
            print('Finished')


        """
            # Get all pool ids in order to reset them
            cur.execute("SELECT id FROM indy_pools")
            pool_ids = [row[0] for row in cur.fetchall()]
        conn.commit()

        # Set status for each pool
        for pool_id in pool_ids:
            set_race_draft_status("NOT_INITIALIZED", pool_id)
        """

        # set_race_draft_status('DRAFT_READY')
        conn.commit()
        print("Draft reset successfully.")
    except Exception as e:
        print(f'Error resetting to square one: {e}')
    finally:
        put_db_conn(conn)

def start_draft(pool_id: int, ):
    # First, get the current status.
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT event_status FROM indy_pool_draft_status WHERE pool_id=%s LIMIT 1;", (pool_id, ))
            status = cur.fetchone()[0]
    finally:
        put_db_conn(conn)

    # Check to see if our draft is ready
    if status != 'DRAFT_READY':
        return {"success": False, "message": "Draft not yet ready to be started."}

    set_race_draft_status("DRAFT_ACTIVE", pool_id)
    return {"success": True, "message": "Draft has started!"}

def get_on_deck_picks(full_order: list[str], pool_id: int, num_on_deck: int = 7, max_picks: int = 33) -> list[str]:
    """
    Given the full draft order and current_pick (already adjusted for snake logic),
    return the next `num_on_deck` participants.
    """
    if not full_order:
        return []

    num_participants = len(full_order)
    picks = []

    # total_picks_so_far means how many picks have already happened
    current_pick, total_picks_so_far = get_current_draft_status(pool_id)

    # Skip the current picker
    total_picks_so_far += 1

    round_number = (total_picks_so_far) // num_participants
    index_in_round = (total_picks_so_far) % num_participants

    for _ in range(num_on_deck):
        if total_picks_so_far >= max_picks:
            break

        if round_number % 2 == 0:
            idx = index_in_round
        else:
            idx = num_participants - 1 - index_in_round

        if idx < 0 or idx >= len(full_order):
            break

        picks.append(full_order[idx])

        # Advance
        index_in_round += 1
        if index_in_round == num_participants:
            index_in_round = 0
            round_number += 1

        total_picks_so_far += 1

    return picks

def get_draft_status(pool_id: int, ):
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            # Get our Event Status
            cur.execute("""
                SELECT event_status FROM indy_pool_draft_status WHERE pool_id=%s; 
            """, (pool_id, ))
            status = cur.fetchone()[0]
            if status in ['NOT_INITIALIZED', 'DRAFT_READY', 'PRE_RACE', 'RACE_ACTIVE', 'RACE_COMPLETED', ]:
                return {
                    "status": status,
                    "current_picker": "",
                    "on_deck": [],
                    "total_picks": 0,
                }

            # Get current pick number
            cur.execute("SELECT current_pick, total_picks FROM indy_pool_draft_status WHERE pool_id=%s;", (pool_id, ))
            result = cur.fetchone()
            if not result:
                return {
                    "status": status,
                    "current_picker": "",
                    "on_deck": [],
                    "total_picks": 0,
                }
            current_pick, total_picks = result

            # Get full draft order
            cur.execute("""
                SELECT participant_name
                FROM indy_pool_draft_order
                WHERE pool_id=%s
                ORDER BY pick_position ASC
            """, (pool_id, ))
            full_order = [row[0] for row in cur.fetchall()]

            # Get max number of picks
            cur.execute("""
                SELECT COUNT(*) FROM indy_pool_starting_grid;
            """)
            max_picks = cur.fetchone()[0]

            # Draft in Progress Case: show current picker and on-deck list
            current_picker = full_order[current_pick - 1]
            on_deck = get_on_deck_picks(full_order, pool_id, num_on_deck=7, max_picks=max_picks)

            return {
                "status": status,
                "current_picker": current_picker,
                "on_deck": on_deck,
                "total_picks": total_picks,
            }
    finally:
        put_db_conn(conn)

if __name__ == '__main__':
    reset_draft_to_square_one()
