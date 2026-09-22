"""One-off backfill: sum per-guild EXP into GlobalUser rows.

Offline, no Discord token needed (same pattern as smoke_test.py). Idempotent —
safe to re-run; upserts rather than insert-only. Run once at deploy time,
immediately after the schema migration (create_all()), against a copy of the
production DB first to sanity check, then for real.

Usage:
    python scripts/backfill_global_users.py [path/to/pokesketch.db]

Defaults to data/pokesketch.db relative to the repo root if no path given.
"""

from __future__ import annotations

import asyncio
import os
import sys

os.environ.setdefault("POKESKETCH_DISCORD_TOKEN", "dummy-not-used")


async def main() -> None:
    from sqlalchemy import func, select

    from pokesketch import db, leveling

    db_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join("data", "pokesketch.db")
    if not os.path.exists(db_path):
        print(f"DB not found at {db_path}")
        sys.exit(1)

    db.init_engine(db_path)
    await db.create_all()  # ensures global_users table exists

    async with db.session() as s:
        rows = (
            await s.execute(
                select(db.User.user_id, func.sum(db.User.exp)).group_by(db.User.user_id)
            )
        ).all()

        created, updated = 0, 0
        for user_id, total_exp in rows:
            total_exp = total_exp or 0
            global_user = (
                await s.execute(select(db.GlobalUser).where(db.GlobalUser.user_id == user_id))
            ).scalar_one_or_none()
            if global_user is None:
                global_user = db.GlobalUser(user_id=user_id)
                s.add(global_user)
                created += 1
            else:
                updated += 1
            global_user.exp = total_exp
            global_user.level = leveling.level_for_exp(total_exp)
            # Streak fields intentionally left at defaults (None/0/0) — not backfilled,
            # per the design doc: global streak starts counting fresh post-deploy.

        await s.commit()

    print(f"Backfilled {len(rows)} user(s): {created} created, {updated} updated.")
    await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
