"""brain-builder — Aider worker for pending_approvals. Standalone; never imported by main.py."""
import asyncio
import json
import logging
import os
import pathlib
import re
import shutil
import signal
import tempfile
import time
import uuid

import psycopg
from psycopg.types.json import Jsonb
from dotenv import load_dotenv

load_dotenv()


class _JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "msg": record.getMessage(),
            **getattr(record, "ctx", {}),
        })


_handler = logging.StreamHandler()
_handler.setFormatter(_JsonFormatter())
logger = logging.getLogger("brain-builder")
logger.setLevel(logging.INFO)
logger.addHandler(_handler)
logger.propagate = False

WORKER_ID = f"builder-{uuid.uuid4().hex[:8]}"
AIDER_PATH = "/home/ashish/.local/bin/aider"
AIDER_MODEL = "gemini/gemini-3.6-flash"
AIDER_TIMEOUT = 600.0  # 10 minutes hard kill
POLL_INTERVAL = 5
LOG_BASE = pathlib.Path("/opt/brain/logs/builds")
DIFF_MAX_BYTES = 100_000  # ~100 KB; truncated with marker, full diff stays on disk


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _db_url() -> str:
    url = os.environ.get("BRAIN_DB_URL", "")
    if not url:
        raise RuntimeError("BRAIN_DB_URL not set")
    return url


def _tg_chat_id() -> str:
    return os.environ.get("TELEGRAM_CHAT_ID", "")


def _recover_stale_locks() -> int:
    with psycopg.connect(_db_url()) as conn:
        cur = conn.execute(
            """UPDATE pending_approvals
               SET status='pending', locked_by=NULL, locked_at=NULL
               WHERE status='running'
                 AND locked_at < now() - INTERVAL '10 minutes'"""
        )
        conn.commit()
        return cur.rowcount


def _claim_next_approval() -> dict | None:
    with psycopg.connect(_db_url()) as conn:
        row = conn.execute(
            """UPDATE pending_approvals
               SET status='running', locked_by=%s, locked_at=now()
               WHERE id = (
                   SELECT id FROM pending_approvals
                   WHERE status='pending'
                     AND (expires_at IS NULL OR expires_at > now())
                   ORDER BY created_at
                   LIMIT 1
                   FOR UPDATE SKIP LOCKED
               )
               RETURNING id, project_id, spec""",
            (WORKER_ID,),
        ).fetchone()
        conn.commit()
    if row is None:
        return None
    return {"id": str(row[0]), "project_id": str(row[1]), "spec": row[2]}


def _get_project(project_id: str) -> dict:
    with psycopg.connect(_db_url()) as conn:
        row = conn.execute(
            "SELECT local_path, default_branch, repo_url FROM projects WHERE id = %s",
            (project_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError(f"project {project_id!r} not found")
    return {"local_path": row[0], "default_branch": row[1], "repo_url": row[2]}


def _get_next_attempt(approval_id: str) -> int:
    with psycopg.connect(_db_url()) as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(attempt), 0) + 1 FROM step_executions WHERE approval_id = %s",
            (approval_id,),
        ).fetchone()
    return row[0]


def _set_approval_status(approval_id: str, status: str, *,
                          diff_text: str | None = None,
                          branch: str | None = None) -> None:
    terminal = status in ("approved", "killed", "expired", "failed")
    with psycopg.connect(_db_url()) as conn:
        conn.execute(
            """UPDATE pending_approvals
               SET status=%s,
                   diff_text=COALESCE(%s, diff_text),
                   branch=COALESCE(%s, branch),
                   resolved_at=CASE WHEN %s THEN now() ELSE resolved_at END
               WHERE id=%s""",
            (status, diff_text, branch, terminal, approval_id),
        )
        conn.commit()


def _write_outbox(message: str, *, reply_markup: dict | None = None) -> None:
    chat_id = _tg_chat_id()
    if not chat_id:
        logger.warning("TELEGRAM_CHAT_ID not set; skipping outbox write")
        return
    try:
        with psycopg.connect(_db_url()) as conn:
            conn.execute(
                """INSERT INTO outbox (channel, recipient, message, reply_markup)
                   VALUES ('telegram', %s, %s, %s)""",
                (chat_id, message, Jsonb(reply_markup) if reply_markup else None),
            )
            conn.commit()
    except Exception:
        logger.exception("outbox write failed")


def _parse_files_changed(diff_text: str) -> list[str]:
    return re.findall(r"^diff --git a/(.+?) b/", diff_text or "", re.MULTILINE)


def _repo_to_https(repo_url: str) -> str:
    m = re.match(r"git@github\.com:(.+?)(?:\.git)?$", repo_url or "")
    if m:
        return f"https://github.com/{m.group(1)}"
    return (repo_url or "").removesuffix(".git")


# ---------------------------------------------------------------------------
# Step execution
# ---------------------------------------------------------------------------

async def _run_step(
    approval_id: str,
    step_no: int,
    attempt: int,
    cmd: list[str],
    log_path: pathlib.Path,
    cwd: str,
    *,
    timeout: float | None = None,
) -> tuple[int, bytes]:
    """Run cmd, log stdout+stderr to log_path, record a step_executions row."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command_str = " ".join(cmd)
    logger.info("step start", extra={"ctx": {
        "approval_id": approval_id, "attempt": attempt, "step_no": step_no, "cmd": command_str,
    }})

    with psycopg.connect(_db_url()) as conn:
        step_id = str(conn.execute(
            """INSERT INTO step_executions (approval_id, attempt, step_no, command, stdout_ref, started_at)
               VALUES (%s, %s, %s, %s, %s, now()) RETURNING id""",
            (approval_id, attempt, step_no, command_str, str(log_path)),
        ).fetchone()[0])
        conn.commit()

    exit_code = 0
    with open(log_path, "wb") as log_file:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=log_file,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
            start_new_session=True,
        )
        try:
            if timeout is not None:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            else:
                await proc.wait()
            exit_code = proc.returncode
        except asyncio.TimeoutError:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            exit_code = -1
            logger.error("step timed out", extra={"ctx": {
                "approval_id": approval_id, "step_no": step_no,
            }})

    with psycopg.connect(_db_url()) as conn:
        conn.execute(
            "UPDATE step_executions SET exit_code=%s, finished_at=now() WHERE id=%s",
            (exit_code, step_id),
        )
        conn.commit()

    logger.info("step done", extra={"ctx": {
        "approval_id": approval_id, "step_no": step_no, "exit_code": exit_code,
    }})
    return exit_code, log_path.read_bytes()


# ---------------------------------------------------------------------------
# Startup checks
# ---------------------------------------------------------------------------

async def _probe_aider_model() -> None:
    """Fail loud at startup if the model doesn't produce real work.

    Aider exits 0 even on LLM errors (e.g. 404). So we verify it actually
    created a file and committed — not just that the process exited cleanly.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = pathlib.Path(tmp)

        for git_cmd in (
            ["git", "init", tmp],
            ["git", "-C", tmp, "config", "user.email", "probe@brain"],
            ["git", "-C", tmp, "config", "user.name", "probe"],
        ):
            p = await asyncio.create_subprocess_exec(
                *git_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await p.wait()

        proc = await asyncio.create_subprocess_exec(
            AIDER_PATH, "--model", AIDER_MODEL, "--yes",
            "--message", "Create a file named probe.txt containing the single line: PROBE_OK",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=tmp,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise RuntimeError(f"Aider model probe timed out — {AIDER_MODEL}")

        tail = stdout.decode(errors="replace")

        failures = []
        if proc.returncode != 0:
            failures.append(f"exit code {proc.returncode}")

        probe_file = tmp_path / "probe.txt"
        if not probe_file.exists():
            failures.append("probe.txt not created")
        elif "PROBE_OK" not in probe_file.read_text():
            failures.append("probe.txt missing PROBE_OK")

        log_proc = await asyncio.create_subprocess_exec(
            "git", "-C", tmp, "log", "--oneline",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        log_out, _ = await log_proc.communicate()
        if not log_out.strip():
            failures.append("aider made no git commit")

        if failures:
            raise RuntimeError(
                f"Aider model probe failed ({'; '.join(failures)}) for {AIDER_MODEL}:\n"
                f"{tail[-600:]}"
            )

    logger.info("aider model probe ok", extra={"ctx": {"model": AIDER_MODEL}})


def _cleanup_old_build_logs() -> int:
    """Remove build log dirs older than 30 days. Same pattern as backup rotation."""
    if not LOG_BASE.exists():
        return 0
    cutoff = time.time() - 30 * 86400
    removed = 0
    for entry in LOG_BASE.iterdir():
        if entry.is_dir() and entry.stat().st_mtime < cutoff:
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    return removed


# ---------------------------------------------------------------------------
# Build logic
# ---------------------------------------------------------------------------

async def _build_one(approval: dict) -> None:
    approval_id = approval["id"]
    short_id = approval_id.replace("-", "")[:8]
    branch_name = f"agent/{short_id}"
    task = approval["spec"].get("task", "")

    project = _get_project(approval["project_id"])
    local_path = project["local_path"]
    default_branch = project["default_branch"]

    attempt = _get_next_attempt(approval_id)
    log_dir = LOG_BASE / approval_id
    step_no = 0

    async def run(cmd: list[str], *, timeout: float | None = None) -> tuple[int, bytes]:
        nonlocal step_no
        step_no += 1
        return await _run_step(
            approval_id, step_no, attempt, cmd,
            log_dir / f"step{step_no}-attempt{attempt}.log",
            local_path, timeout=timeout,
        )

    # Step 1: fetch
    code, _ = await run(["git", "-C", local_path, "fetch", "origin"])
    if code != 0:
        _set_approval_status(approval_id, "failed")
        _write_outbox(f"Build FAILED (git fetch) — `{short_id}`")
        return

    # Step 2: checkout clean branch
    code, _ = await run([
        "git", "-C", local_path, "checkout", "-B", branch_name,
        f"origin/{default_branch}",
    ])
    if code != 0:
        _set_approval_status(approval_id, "failed")
        _write_outbox(f"Build FAILED (git checkout) — `{short_id}`")
        return

    # Step 3: aider
    code, _ = await run(
        [AIDER_PATH, "--yes", "--model", AIDER_MODEL, "--message", task],
        timeout=AIDER_TIMEOUT,
    )
    if code != 0:
        _set_approval_status(approval_id, "failed")
        _write_outbox(
            f"Build FAILED (aider exit {code}) — `{short_id}`\n"
            f"task: {task[:200]}"
        )
        return

    # Step 4: diff — read output for diff_text
    code, diff_bytes = await run([
        "git", "-C", local_path, "diff", f"origin/{default_branch}...HEAD",
    ])
    if code != 0:
        _set_approval_status(approval_id, "failed")
        _write_outbox(f"Build FAILED (git diff) — `{short_id}`")
        return

    if not diff_bytes.strip():
        _set_approval_status(approval_id, "failed")
        _write_outbox(
            f"Build FAILED (aider produced no changes) — `{short_id}`\ntask: {task[:200]}"
        )
        return

    diff_text = diff_bytes.decode(errors="replace")
    if len(diff_bytes) > DIFF_MAX_BYTES:
        diff_text = (
            diff_text[:DIFF_MAX_BYTES]
            + f"\n\n[truncated — full diff at {log_dir}/step{step_no}-attempt{attempt}.log]"
        )

    # Step 5: push
    code, _ = await run([
        "git", "-C", local_path, "push", "--force-with-lease", "origin", branch_name,
    ])
    if code != 0:
        _set_approval_status(approval_id, "failed", diff_text=diff_text, branch=branch_name)
        _write_outbox(f"Build FAILED (git push) — `{short_id}`\nbranch: `{branch_name}`")
        return

    _set_approval_status(approval_id, "awaiting_review", diff_text=diff_text, branch=branch_name)

    files = _parse_files_changed(diff_text)
    listed = files[:8]
    overflow = len(files) - len(listed)
    files_lines = "\n".join(f"  • `{f}`" for f in listed)
    if overflow:
        files_lines += f"\n  _+{overflow} more_"
    gh_url = _repo_to_https(project["repo_url"])
    branch_link = f"[{branch_name}]({gh_url}/tree/{branch_name})"
    notification = (
        f"🔨 *Build ready* — `{short_id}`\n"
        f"*task:* {task[:200]}\n"
        f"*branch:* {branch_link}\n"
        f"*changed:* {len(files)} file{'s' if len(files) != 1 else ''}\n"
        f"{files_lines}"
    )
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Promote", "callback_data": f"promote:{approval_id}"},
        {"text": "❌ Kill",    "callback_data": f"kill:{approval_id}"},
    ]]}
    _write_outbox(notification, reply_markup=keyboard)
    logger.info("build complete", extra={"ctx": {
        "approval_id": approval_id, "branch": branch_name,
    }})


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

async def builder_worker() -> None:
    import traceback

    await _probe_aider_model()

    recovered = _recover_stale_locks()
    if recovered:
        logger.info("stale lock recovery", extra={"ctx": {"recovered": recovered}})

    cleaned = _cleanup_old_build_logs()
    if cleaned:
        logger.info("old build logs cleaned", extra={"ctx": {"removed_dirs": cleaned}})

    while True:
        try:
            approval = _claim_next_approval()
            if approval is None:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            logger.info("approval claimed", extra={"ctx": {"approval_id": approval["id"]}})
            try:
                await _build_one(approval)
            except Exception as exc:
                tb = traceback.format_exc()
                logger.error("build crashed", extra={"ctx": {
                    "approval_id": approval["id"],
                    "exc_type": type(exc).__name__,
                    "exc_msg": str(exc)[:200],
                    "traceback": tb[:2000],
                }})
                try:
                    _set_approval_status(approval["id"], "failed")
                    _write_outbox(
                        f"Build CRASHED — `{approval['id'][:8]}`\n"
                        f"{type(exc).__name__}: {str(exc)[:200]}"
                    )
                except Exception:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("worker loop error", extra={"ctx": {"error": str(e)}})
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(builder_worker())
