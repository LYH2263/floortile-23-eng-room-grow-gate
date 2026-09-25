from app.db import connect


def list_rooms():
    conn = connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM rooms ORDER BY id").fetchall()]
    finally:
        conn.close()


def get_room(room_id: int):
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_room(room_id: int, *, length=None, width=None):
    """Partially update room dimensions; returns the updated row or None."""
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
        if not row:
            return None
        cur = dict(row)
        if length is not None:
            cur["length"] = float(length)
        if width is not None:
            cur["width"] = float(width)
        conn.execute(
            "UPDATE rooms SET length=?, width=? WHERE id=?",
            (cur["length"], cur["width"], room_id),
        )
        conn.commit()
        return cur
    finally:
        conn.close()
