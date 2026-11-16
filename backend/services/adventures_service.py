"""Owner-scoped PostgreSQL operations and calendar rules for Adventures."""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg2.extras import RealDictCursor

from backend.database.database import get_db_conn, put_db_conn


class NotFound(ValueError):
    pass


class Conflict(ValueError):
    pass


class ConfigurationError(ValueError):
    pass


@contextmanager
def transaction():
    conn = get_db_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)


def today() -> date:
    name = os.environ.get("REMIHUB_ADVENTURES_TIMEZONE")
    if not name:
        raise ConfigurationError("REMIHUB_ADVENTURES_TIMEZONE is not configured")
    try:
        return datetime.now(ZoneInfo(name)).date()
    except ZoneInfoNotFoundError as exc:
        raise ConfigurationError("REMIHUB_ADVENTURES_TIMEZONE is invalid") from exc


def dates_valid(arrival, departure):
    if isinstance(arrival, str):
        arrival = date.fromisoformat(arrival)
    if isinstance(departure, str):
        departure = date.fromisoformat(departure)
    if (arrival is None) != (departure is None):
        raise ValueError("arrival_date and departure_date must both be set or both be null")
    if arrival is not None and departure < arrival:
        raise ValueError("departure_date must be on or after arrival_date")


def present_trip(row: dict, local_today: date) -> dict:
    item = dict(row)
    arrival, departure, intent = item["arrival_date"], item["departure_date"], item["intent"]
    item["days_until_arrival"] = (arrival - local_today).days if arrival else None
    item["nights"] = (departure - arrival).days if arrival else None
    item["calendar_days"] = item["nights"] + 1 if arrival else None
    item["trip_day"] = (local_today - arrival).days + 1 if arrival and arrival <= local_today <= departure and intent != "Cancelled" else None
    if intent == "Cancelled":
        item["state"], item["classification"] = "Cancelled", "cancelled"
    elif arrival is None:
        item["state"], item["classification"] = intent, "undated"
    elif local_today < arrival:
        item["state"], item["classification"] = intent, "upcoming"
    elif local_today <= departure:
        item["state"], item["classification"] = "In Progress", "active"
    else:
        item["state"], item["classification"] = "Completed", "historical"
    return item


TRIP_SELECT = """SELECT t.*, c.name AS campground_name FROM public.adventures_trips t
 LEFT JOIN public.adventures_campgrounds c ON c.id=t.campground_id"""


def _one(cur, query, args, message="Resource not found"):
    cur.execute(query, args)
    row = cur.fetchone()
    if row is None:
        raise NotFound(message)
    return dict(row)


def _page(cur, select, where, args, order, limit, offset, projection="*"):
    cur.execute("SELECT count(*) AS count " + select + " WHERE " + where, args)
    total = cur.fetchone()["count"]
    cur.execute("SELECT " + projection + " " + select + " WHERE " + where + " ORDER BY " + order + " LIMIT %s OFFSET %s", (*args, limit, offset))
    return {"items": [dict(row) for row in cur.fetchall()], "total": total, "limit": limit, "offset": offset}


def assert_trip(cur, user_id, trip_id, lock=False):
    suffix = " FOR UPDATE OF t" if lock else ""
    return _one(cur, TRIP_SELECT + " WHERE t.user_id=%s AND t.id=%s" + suffix,
                (user_id, trip_id), "Trip not found")


def assert_campground(cur, user_id, campground_id, lock=None):
    suffix = " FOR " + lock if lock else ""
    return _one(cur, "SELECT * FROM public.adventures_campgrounds WHERE user_id=%s AND id=%s" + suffix,
                (user_id, campground_id), "Campground not found")


def _insert(cur, table, values):
    columns = list(values)
    cur.execute("INSERT INTO public." + table + " (" + ",".join(columns) + ") VALUES (" + ",".join(["%s"] * len(columns)) + ") RETURNING *", list(values.values()))
    return dict(cur.fetchone())


def _update(cur, table, values, where, args):
    required = {
        "adventures_trips": ("title", "intent", "electricity"),
        "adventures_campgrounds": ("name",),
        "adventures_activities": ("title", "completed", "display_order"),
        "adventures_template_items": ("text", "category", "display_order"),
        "adventures_trip_items": ("text", "category", "display_order", "completed"),
        "adventures_memories": ("title",),
        "adventures_photos": ("favorite", "display_order"),
    }
    if any(k in values and values[k] is None for k in required.get(table, ())):
        raise ValueError("Required fields cannot be null")
    if not values:
        return _one(cur, "SELECT * FROM public." + table + " WHERE " + where, args)
    columns = list(values)
    cur.execute("UPDATE public." + table + " SET " + ",".join(c + "=%s" for c in columns) +
                ",updated_at=now() WHERE " + where + " RETURNING *", (*values.values(), *args))
    row = cur.fetchone()
    if row is None:
        raise NotFound("Resource not found")
    return dict(row)


def _delete(cur, table, where, args):
    cur.execute("DELETE FROM public." + table + " WHERE " + where, args)
    if cur.rowcount == 0:
        raise NotFound("Resource not found")


def _check_campground(cur, user_id, values):
    if values.get("campground_id") is not None:
        assert_campground(cur, user_id, values["campground_id"], "KEY SHARE")


def create_trip(user_id, values):
    today()  # Fail before writing if lifecycle timezone is not configured.
    dates_valid(values.get("arrival_date"), values.get("departure_date"))
    with transaction() as cur:
        _check_campground(cur, user_id, values)
        row = _insert(cur, "adventures_trips", {"user_id": user_id, **values})
    return get_trip(user_id, row["id"])


def get_trip(user_id, trip_id):
    local_today = today()
    with transaction() as cur:
        return present_trip(assert_trip(cur, user_id, trip_id), local_today)


def update_trip(user_id, trip_id, values):
    today()
    with transaction() as cur:
        current = assert_trip(cur, user_id, trip_id, lock=True)
        dates_valid(values.get("arrival_date", current["arrival_date"]),
                    values.get("departure_date", current["departure_date"]))
        _check_campground(cur, user_id, values)
        _update(cur, "adventures_trips", values, "user_id=%s AND id=%s", (user_id, trip_id))
    return get_trip(user_id, trip_id)


def delete_trip(user_id, trip_id):
    # Queue file removals in the same transaction as relational deletion.
    with transaction() as cur:
        assert_trip(cur, user_id, trip_id, lock=True)
        cur.execute("INSERT INTO public.adventures_photo_cleanup(storage_key) SELECT storage_key FROM public.adventures_photos WHERE trip_id=%s ON CONFLICT DO NOTHING", (trip_id,))
        cur.execute("UPDATE public.adventures_trips SET cover_photo_id=NULL WHERE id=%s", (trip_id,))
        _delete(cur, "adventures_trips", "user_id=%s AND id=%s", (user_id, trip_id))


def list_trips(user_id, kind, year, limit, offset):
    local_today = today()
    where, args = ["t.user_id=%s"], [user_id]
    if kind == "upcoming":
        where += ["t.intent <> 'Cancelled' AND t.arrival_date > %s"]
        args.append(local_today)
        order = "t.arrival_date ASC,t.id"
    elif kind == "active":
        where += ["t.intent <> 'Cancelled' AND t.arrival_date <= %s AND t.departure_date >= %s"]
        args += [local_today, local_today]
        order = "t.arrival_date ASC,t.id"
    elif kind == "historical":
        where += ["t.intent <> 'Cancelled' AND t.departure_date < %s"]
        args.append(local_today)
        order = "t.departure_date DESC,t.id"
    elif kind == "cancelled":
        where += ["t.intent = 'Cancelled'"]
        order = "t.updated_at DESC,t.id"
    elif kind == "undated":
        where += ["t.arrival_date IS NULL AND t.intent <> 'Cancelled'"]
        order = "t.created_at DESC,t.id"
    else:
        order = "t.created_at DESC,t.id"
    if year is not None:
        where += ["t.arrival_date >= %s AND t.arrival_date < %s"]
        args += [date(year, 1, 1), date(year + 1, 1, 1)]
    with transaction() as cur:
        page = _page(cur, "FROM public.adventures_trips t LEFT JOIN public.adventures_campgrounds c ON c.id=t.campground_id",
                     " AND ".join(where), args, order, limit, offset, "t.*,c.name AS campground_name")
    page["items"] = [present_trip(x, local_today) for x in page["items"]]
    return page


def create_campground(user_id, values):
    with transaction() as cur:
        return _insert(cur, "adventures_campgrounds", {"user_id": user_id, **values})


def get_campground(user_id, campground_id):
    with transaction() as cur:
        return assert_campground(cur, user_id, campground_id)


def update_campground(user_id, campground_id, values):
    with transaction() as cur:
        assert_campground(cur, user_id, campground_id)
        return _update(cur, "adventures_campgrounds", values, "user_id=%s AND id=%s", (user_id, campground_id))


def delete_campground(user_id, campground_id):
    with transaction() as cur:
        assert_campground(cur, user_id, campground_id, "UPDATE")
        cur.execute("SELECT 1 FROM public.adventures_trips WHERE user_id=%s AND campground_id=%s LIMIT 1", (user_id, campground_id))
        if cur.fetchone():
            raise Conflict("Campground is referenced by trips")
        _delete(cur, "adventures_campgrounds", "user_id=%s AND id=%s", (user_id, campground_id))


def list_campgrounds(user_id, limit, offset):
    with transaction() as cur:
        return _page(cur, "FROM public.adventures_campgrounds", "user_id=%s", [user_id], "name,id", limit, offset)


def campground_visits(user_id, campground_id, limit, offset):
    local_today = today()
    with transaction() as cur:
        assert_campground(cur, user_id, campground_id)
        page = _page(cur, "FROM public.adventures_trips t LEFT JOIN public.adventures_campgrounds c ON c.id=t.campground_id",
                     "t.user_id=%s AND t.campground_id=%s AND t.intent<>'Cancelled' AND t.departure_date<%s",
                     [user_id, campground_id, local_today], "t.arrival_date DESC,t.id", limit, offset, "t.*,c.name AS campground_name")
        visits = []
        for trip in page["items"]:
            cur.execute("SELECT * FROM public.adventures_ratings WHERE trip_id=%s", (trip["id"],))
            rating = cur.fetchone()
            visits.append({"trip": present_trip(trip, local_today), "rating": dict(rating) if rating else None})
        cur.execute("SELECT avg(r.overall) AS average,count(r.overall) AS count FROM public.adventures_trips t JOIN public.adventures_ratings r ON r.trip_id=t.id WHERE t.user_id=%s AND t.campground_id=%s AND t.intent<>'Cancelled' AND t.departure_date<%s", (user_id, campground_id, local_today))
        aggregate = cur.fetchone()
    return {"visits": {**page, "items": visits}, "overall_average": aggregate["average"], "overall_recorded_count": aggregate["count"]}


def calendar(user_id, start, end):
    if end < start or (end - start).days > 366:
        raise ValueError("Calendar range must be ordered and at most 367 inclusive days")
    local_today = today()
    with transaction() as cur:
        cur.execute(TRIP_SELECT + " WHERE t.user_id=%s AND t.arrival_date<=%s AND t.departure_date>=%s ORDER BY t.arrival_date,t.id LIMIT 1000", (user_id, end, start))
        rows = [dict(row) for row in cur.fetchall()]
    if len(rows) == 1000:
        raise Conflict("Calendar period contains too many trips; request a shorter range")
    fields = ("id", "title", "arrival_date", "departure_date", "intent", "state", "destination", "campground_name", "cover_photo_id")
    return [{k: present_trip(row, local_today)[k] for k in fields} for row in rows]


def dashboard(user_id, year, upcoming_limit):
    local_today = today()
    with transaction() as cur:
        cur.execute(TRIP_SELECT + " WHERE t.user_id=%s AND t.intent<>'Cancelled' AND t.arrival_date>%s ORDER BY t.arrival_date,t.id LIMIT %s", (user_id, local_today, upcoming_limit))
        upcoming = [present_trip(dict(x), local_today) for x in cur.fetchall()]
        cur.execute(TRIP_SELECT + " WHERE t.user_id=%s AND t.intent<>'Cancelled' AND t.arrival_date<=%s AND t.departure_date>=%s ORDER BY t.arrival_date,t.id", (user_id, local_today, local_today))
        active = [present_trip(dict(x), local_today) for x in cur.fetchall()]
        cur.execute("""SELECT count(*) AS total_planned,
            count(*) FILTER (WHERE arrival_date>%s) AS upcoming,
            count(*) FILTER (WHERE arrival_date<=%s AND departure_date>=%s) AS active,
            count(*) FILTER (WHERE departure_date<%s) AS completed,
            coalesce(sum(departure_date-arrival_date) FILTER (WHERE departure_date>=%s),0) AS planned_nights,
            coalesce(sum(departure_date-arrival_date) FILTER (WHERE departure_date<%s),0) AS completed_nights,
            count(DISTINCT campground_id) FILTER (WHERE departure_date<%s) AS distinct_visited_campgrounds
            FROM public.adventures_trips WHERE user_id=%s AND intent<>'Cancelled' AND arrival_date>=%s AND arrival_date<%s""",
            (local_today,local_today,local_today,local_today,local_today,local_today,local_today,user_id,date(year,1,1),date(year+1,1,1)))
        summary = {"year": year, **dict(cur.fetchone())}
    return {"next_trip": upcoming[0] if upcoming else None, "active": active, "upcoming": upcoming, "summary": summary}


def list_child(user_id, trip_id, table, order, limit=100, offset=0):
    with transaction() as cur:
        assert_trip(cur, user_id, trip_id)
        return _page(cur, "FROM public." + table, "trip_id=%s", [trip_id], order, limit, offset)


def create_child(user_id, trip_id, table, values):
    with transaction() as cur:
        assert_trip(cur, user_id, trip_id)
        return _insert(cur, table, {"trip_id": trip_id, **values})


def get_child(user_id, trip_id, table, child_id):
    with transaction() as cur:
        assert_trip(cur, user_id, trip_id)
        return _one(cur, "SELECT * FROM public." + table + " WHERE trip_id=%s AND id=%s", (trip_id, child_id))


def update_child(user_id, trip_id, table, child_id, values):
    with transaction() as cur:
        assert_trip(cur, user_id, trip_id)
        return _update(cur, table, values, "trip_id=%s AND id=%s", (trip_id, child_id))


def delete_child(user_id, trip_id, table, child_id):
    with transaction() as cur:
        assert_trip(cur, user_id, trip_id)
        _delete(cur, table, "trip_id=%s AND id=%s", (trip_id, child_id))


def list_template(user_id, limit=200, offset=0):
    with transaction() as cur:
        page = _page(cur, "FROM public.adventures_template_items", "user_id=%s", [user_id], "category,display_order,id", limit, offset)
    for item in page["items"]:
        item["completed"] = False
    return page


def create_template_item(user_id, values):
    with transaction() as cur:
        row = _insert(cur, "adventures_template_items", {"user_id": user_id, **values})
    return {**row, "completed": False}


def get_template_item(user_id, item_id):
    with transaction() as cur:
        row = _one(cur, "SELECT * FROM public.adventures_template_items WHERE user_id=%s AND id=%s", (user_id, item_id))
    return {**row, "completed": False}


def update_template_item(user_id, item_id, values):
    with transaction() as cur:
        row = _update(cur, "adventures_template_items", values, "user_id=%s AND id=%s", (user_id, item_id))
    return {**row, "completed": False}


def delete_template_item(user_id, item_id):
    with transaction() as cur:
        _delete(cur, "adventures_template_items", "user_id=%s AND id=%s", (user_id, item_id))


def instantiate_checklist(user_id, trip_id):
    with transaction() as cur:
        assert_trip(cur, user_id, trip_id, lock=True)
        cur.execute("INSERT INTO public.adventures_trip_checklists(trip_id) VALUES (%s) ON CONFLICT DO NOTHING RETURNING trip_id", (trip_id,))
        if cur.fetchone():
            cur.execute("""INSERT INTO public.adventures_trip_items(trip_id,text,category,display_order,notes)
                SELECT %s,text,category,display_order,notes FROM public.adventures_template_items WHERE user_id=%s ORDER BY category,display_order,id""", (trip_id,user_id))
    return get_checklist(user_id, trip_id)


def get_checklist(user_id, trip_id):
    with transaction() as cur:
        assert_trip(cur, user_id, trip_id)
        cur.execute("SELECT 1 FROM public.adventures_trip_checklists WHERE trip_id=%s", (trip_id,))
        instantiated = bool(cur.fetchone())
        cur.execute("SELECT * FROM public.adventures_trip_items WHERE trip_id=%s ORDER BY category,display_order,id", (trip_id,))
        items = [dict(x) for x in cur.fetchall()]
    count = sum(bool(x["completed"]) for x in items)
    return {"instantiated": instantiated, "items": items, "completed_count": count,
            "total_count": len(items), "percent": round(100 * count / len(items)) if items else 0}


def create_trip_item(user_id, trip_id, values):
    with transaction() as cur:
        assert_trip(cur, user_id, trip_id)
        cur.execute("SELECT 1 FROM public.adventures_trip_checklists WHERE trip_id=%s", (trip_id,))
        if not cur.fetchone():
            raise Conflict("Instantiate the trip checklist first")
        return _insert(cur, "adventures_trip_items", {"trip_id": trip_id, **values})


def reset_checklist(user_id, trip_id):
    with transaction() as cur:
        assert_trip(cur, user_id, trip_id)
        cur.execute("UPDATE public.adventures_trip_items SET completed=false,updated_at=now() WHERE trip_id=%s", (trip_id,))
    return get_checklist(user_id, trip_id)


def get_singleton(user_id, trip_id, table):
    with transaction() as cur:
        assert_trip(cur, user_id, trip_id)
        cur.execute("SELECT * FROM public." + table + " WHERE trip_id=%s", (trip_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def upsert_singleton(user_id, trip_id, table, values):
    with transaction() as cur:
        assert_trip(cur, user_id, trip_id)
        columns = list(values)
        if not columns:
            cur.execute("INSERT INTO public." + table + "(trip_id) VALUES (%s) ON CONFLICT DO NOTHING", (trip_id,))
        else:
            cur.execute("INSERT INTO public." + table + "(trip_id," + ",".join(columns) + ") VALUES (%s," + ",".join(["%s"]*len(columns)) + ") ON CONFLICT (trip_id) DO UPDATE SET " + ",".join(c + "=EXCLUDED." + c for c in columns) + ",updated_at=now()", (trip_id, *values.values()))
        return _one(cur, "SELECT * FROM public." + table + " WHERE trip_id=%s", (trip_id,))


def _memory_photos(cur, trip_id, memory_id, photo_ids):
    if photo_ids is None:
        return
    if len(set(photo_ids)) != len(photo_ids):
        raise ValueError("Duplicate photo IDs")
    if photo_ids:
        cur.execute("SELECT id FROM public.adventures_photos WHERE trip_id=%s AND id=ANY(%s::uuid[])", (trip_id, [str(x) for x in photo_ids]))
        if len(cur.fetchall()) != len(photo_ids):
            raise NotFound("Memory photo not found in trip")
    cur.execute("DELETE FROM public.adventures_memory_photos WHERE memory_id=%s", (memory_id,))
    for photo_id in photo_ids:
        cur.execute("INSERT INTO public.adventures_memory_photos(trip_id,memory_id,photo_id) VALUES (%s,%s,%s)", (trip_id,memory_id,photo_id))


def _present_memory(cur, row):
    cur.execute("SELECT photo_id FROM public.adventures_memory_photos WHERE memory_id=%s ORDER BY photo_id", (row["id"],))
    return {**dict(row), "photo_ids": [x["photo_id"] for x in cur.fetchall()]}


def create_memory(user_id, trip_id, values):
    photos = values.pop("photo_ids", [])
    with transaction() as cur:
        assert_trip(cur, user_id, trip_id)
        row = _insert(cur, "adventures_memories", {"trip_id": trip_id, **values})
        _memory_photos(cur, trip_id, row["id"], photos)
        return _present_memory(cur, row)


def update_memory(user_id, trip_id, memory_id, values):
    photo_ids_supplied = "photo_ids" in values
    photos = values.pop("photo_ids", None)
    if photo_ids_supplied and photos is None:
        photos = []
    with transaction() as cur:
        assert_trip(cur, user_id, trip_id)
        row = _update(cur, "adventures_memories", values, "trip_id=%s AND id=%s", (trip_id, memory_id))
        _memory_photos(cur, trip_id, memory_id, photos)
        return _present_memory(cur, row)


def list_memories(user_id, trip_id, limit, offset):
    with transaction() as cur:
        assert_trip(cur, user_id, trip_id)
        page = _page(cur, "FROM public.adventures_memories", "trip_id=%s", [trip_id], "memory_date DESC NULLS LAST,created_at DESC,id", limit, offset)
        page["items"] = [_present_memory(cur, x) for x in page["items"]]
        return page


def get_memory(user_id, trip_id, memory_id):
    with transaction() as cur:
        assert_trip(cur, user_id, trip_id)
        return _present_memory(cur, _one(cur, "SELECT * FROM public.adventures_memories WHERE trip_id=%s AND id=%s", (trip_id,memory_id)))
