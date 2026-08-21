"""
Slash-command prefix lookup against capture_shortcuts.

Single entry point used by every capture source (Telegram, app, share-to, Ask Brain).
Never raises — returns None on any DB error so callers always fall through silently.
"""
import logging
import os
from typing import Optional

import psycopg
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


def lookup_shortcut(alias: str) -> Optional[dict]:
    """
    SELECT * FROM capture_shortcuts WHERE alias = alias.
    Returns {alias, category, subcategory, agent, notebook_id} or None.
    """
    url = os.environ.get("BRAIN_DB_URL", "")
    if not url:
        return None
    try:
        with psycopg.connect(url) as conn:
            row = conn.execute(
                """SELECT alias, category, subcategory, agent, notebook_id
                     FROM capture_shortcuts
                    WHERE alias = %s
                    LIMIT 1""",
                (alias,),
            ).fetchone()
        if not row:
            return None
        return {
            "alias": row[0],
            "category": row[1],
            "subcategory": row[2],
            "agent": row[3],
            "notebook_id": row[4],
        }
    except Exception:
        logger.exception("capture_shortcuts lookup failed for alias=%r", alias)
        return None


def parse_slash(content: str) -> Optional[str]:
    """
    If content starts with '/', return the lowercased alias (first word after '/').
    Otherwise return None. Strips but does not modify the rest of the content.
    """
    stripped = content.strip()
    if not stripped.startswith("/"):
        return None
    rest = stripped[1:]
    if not rest or rest[0].isspace():
        return None
    return rest.split()[0].lower()
