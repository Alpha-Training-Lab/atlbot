"""Member roles. No row in member_roles means an ordinary member."""
from src.db.connection import get_conn
from src.db.members import upsert_active
from src.db.schema import STATUS_ACTIVE, STATUS_REMOVED
# ===========================================================================

MEMBER = "member"
ADMIN = "admin"
ROLES = (ADMIN,)   # grantable roles; add new ones here
FROM_OWNER = "owner"            # granted with /admin
FROM_LEADERSHIP = "leadership"  # from being in the leadership group


def get_role(user_id):
  with get_conn() as conn:
    row = conn.execute(
      "SELECT role FROM member_roles WHERE user_id = ?", (user_id,)
    ).fetchone()
  return row["role"] if row else MEMBER


def get_role_source(user_id):
  with get_conn() as conn:
    row = conn.execute(
      "SELECT source FROM member_roles WHERE user_id = ?", (user_id,)
    ).fetchone()
  return row["source"] if row else None


def grant_role(user_id, username, first_name, last_name, role, granted_by,
               source=FROM_OWNER):
  """Give a member a role, creating or refreshing their row and making them
  active. False if they already had it. An owner grant outranks a
  leadership one: the owner granting someone who's already an admin through
  leadership makes it stick even if they later leave the group.

  Also False, changing nothing, for an expelled member: only /reinstate
  undoes an expulsion. Callers check first to tell the two apart."""
  if role not in ROLES:
    raise ValueError(f"Unknown role: {role}")
  with get_conn() as conn:
    current = conn.execute(
      "SELECT role, source FROM member_roles WHERE user_id = ?", (user_id,)
    ).fetchone()
    member = conn.execute(
      "SELECT status FROM members WHERE user_id = ?", (user_id,)
    ).fetchone()
    if member is not None and member["status"] == STATUS_REMOVED:
      return False
    upsert_active(conn, user_id, username, first_name, last_name)
    if member is None or member["status"] != STATUS_ACTIVE:
      conn.execute(
        "INSERT INTO member_events (user_id, event, actor_user_id, note) "
        "VALUES (?, ?, ?, ?)",
        (user_id, f"status:{STATUS_ACTIVE}", granted_by, f"given the {role} role ({source})"),
      )
    if current is not None and current["role"] == role:
      if source == FROM_OWNER and current["source"] != FROM_OWNER:
        conn.execute(
          "UPDATE member_roles SET source = ?, granted_by = ? WHERE user_id = ?",
          (FROM_OWNER, granted_by, user_id),
        )
      return False
    conn.execute(
      """
      INSERT INTO member_roles (user_id, role, source, granted_by) VALUES (?, ?, ?, ?)
      ON CONFLICT(user_id) DO UPDATE SET
        role = excluded.role, source = excluded.source,
        granted_by = excluded.granted_by, granted_at = datetime('now')
      """,
      (user_id, role, source, granted_by),
    )
    conn.execute(
      "INSERT INTO member_events (user_id, event, actor_user_id, note) VALUES (?, ?, ?, ?)",
      (user_id, "role_granted", granted_by, f"{role} ({source})"),
    )
  return True


def revoke_role(user_id, revoked_by, only_source=None):
  """Back to ordinary member. False if they had no role, or (with
  only_source) if their role came from somewhere else."""
  with get_conn() as conn:
    row = conn.execute(
      "SELECT role, source FROM member_roles WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is None or (only_source and row["source"] != only_source):
      return False
    conn.execute("DELETE FROM member_roles WHERE user_id = ?", (user_id,))
    conn.execute(
      "INSERT INTO member_events (user_id, event, actor_user_id, note) VALUES (?, ?, ?, ?)",
      (user_id, "role_revoked", revoked_by, row["role"]),
    )
  return True


def members_with_role(role):
  with get_conn() as conn:
    return conn.execute(
      """
      SELECT r.user_id, m.username, m.first_name, m.last_name, m.status, r.source, r.granted_at
      FROM member_roles r JOIN members m ON m.user_id = r.user_id
      WHERE r.role = ? ORDER BY lower(COALESCE(m.username, m.first_name, ''))
      """,
      (role,),
    ).fetchall()
