import asyncio
import io
import json
import logging
import os
import pathlib
import psycopg
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date
from typing import Optional

import httpx
from starlette.requests import Request
import whisper
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Security, UploadFile
from fastapi.responses import JSONResponse
from fastapi.security.api_key import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import Client, create_client

from shortcuts import lookup_shortcut, parse_slash

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
logger = logging.getLogger("brain")
logger.setLevel(logging.INFO)
logger.addHandler(_handler)
logger.propagate = False
API_KEY = os.getenv("API_KEY")
if not API_KEY:
    raise RuntimeError("API_KEY not set")

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(api_key: str = Security(api_key_header)):
    if api_key == API_KEY:
        return api_key
    raise HTTPException(
        status_code=403,
        detail="Invalid or missing API key"
    )


supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_KEY"),
)

ACTIVE_SOURCES = {"app_voice", "app_text", "app_share", "app_photo", "web_upload", "telegram"}
# Claude models are not supported for UNIFY_CHAT_WITH_AI on this 1min.ai account/plan —
# confirmed via direct API testing. Both models below are verified working.
# ⚠ gpt-4.1-nano deprecationDate: 2026-10-21 — replace fallback before that date.
ONEMIN_MODEL = "gpt-4o-mini"
FALLBACK_MODEL = "gpt-4.1-nano"

_WORKER_ID = f"worker-{uuid.uuid4().hex[:8]}"


def _db_url() -> str:
    url = os.environ.get("BRAIN_DB_URL", "")
    if not url:
        raise RuntimeError("BRAIN_DB_URL not set")
    return url

# Fail loud at startup if the prompt file is missing — better than silent runtime failures.
_CLASSIFY_TEMPLATE: str = (
    pathlib.Path(__file__).parent / "prompts" / "classify_single.txt"
).read_text()


class CaptureInput(BaseModel):
    content: str
    source: str
    capture_type: str = "text"
    lat: Optional[float] = None
    lng: Optional[float] = None
    metadata: Optional[dict] = {}
    category: Optional[str] = None
    title: Optional[str] = None


async def call_1minai(prompt: str, model: str = ONEMIN_MODEL) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{os.getenv('ONEMIN_API_URL')}/api/chat-with-ai",
            headers={
                "API-KEY": os.getenv("ONEMIN_API_KEY"),
                "Content-Type": "application/json",
            },
            json={
                "type": "UNIFY_CHAT_WITH_AI",
                "model": model,
                "promptObject": {"prompt": prompt},
            },
        )
        r.raise_for_status()
        data = r.json()
        return data["aiRecord"]["aiRecordDetail"]["resultObject"][0]


async def classify_with_fallback(prompt: str) -> str:
    try:
        return await call_1minai(prompt, ONEMIN_MODEL)
    except Exception as e:
        print(f"[1min.ai] failed: {e}, trying fallback")
        return await call_1minai(prompt, FALLBACK_MODEL)


def _detect_quote(content: str) -> tuple[bool, str | None]:
    """Return (is_quote, author_or_None)."""
    stripped = content.strip()

    # Pattern 1: attribution at end — "— Name" or "~ Name" or "- Name"
    attribution = re.search(r'[—~\-]\s*([A-Z][^\n\-~—]{1,50}?)\s*$', stripped)
    if attribution:
        # Must also have quote markers or be reasonably short standalone text
        has_quotes = any(c in stripped for c in ('"', '"', '"', '\u201c', '\u201d'))
        if has_quotes or len(stripped) < 300:
            return True, attribution.group(1).strip()

    # Pattern 2: starts and ends with quote characters
    if (stripped.startswith(('"', '\u201c')) and stripped.endswith(('"', '\u201d'))):
        return True, None
    if stripped.startswith("'") and stripped.endswith("'") and len(stripped) > 10:
        return True, None

    return False, None


def build_single_prompt(content: str, source: str, capture_type: str = "text") -> str:
    type_context = ""
    if capture_type == "url":
        type_context = "This is extracted text from a web page. Classify by the page content, not the URL string."
    elif capture_type == "youtube":
        type_context = "This is a YouTube video transcript. Classify by the video content/topic."
    elif capture_type == "pdf":
        type_context = "This is text extracted from a PDF document. Classify by document content."

    quote_hint = ""
    if _detect_quote(content)[0]:
        quote_hint = "If this looks like a quote or saying, set category=learning, subcategory=quotes, and include the author in tags if identifiable."

    return _CLASSIFY_TEMPLATE.format(
        content=content[:1000],
        source=source,
        capture_type=capture_type,
        type_context=type_context,
        quote_hint=quote_hint,
    )


def build_batch_prompt(items: list) -> str:
    items_text = ""
    for i, item in enumerate(items):
        ctype = item.get("capture_type", "text")
        type_hint = ""
        if ctype == "url":
            type_hint = "[web page content] "
        elif ctype == "youtube":
            type_hint = "[youtube transcript] "
        elif ctype == "pdf":
            type_hint = "[pdf document] "
        items_text += f"\n[ITEM {i+1}] source={item['source']} type={ctype}\n{type_hint}{(item['raw_content'] or '')[:500]}\n"

    return f"""Classify each item below. Return ONLY a valid JSON array, no other text, no markdown.

Each object must have:
  "item_index": number (1-based),
  "category": "learning|thoughts|work|life|resources|health",
  "subcategory": "wayclear|accrediq|finance|quotes|grocery|tech|hacks|music" or null,
  "tags": ["3 to 5 tags"],
  "summary": "under 50 words",
  "action_class": "record|task|agent|alert|build"

Context: Solo founder Ashish. WayClear=road safety, AccredIQ=IRC accreditation.
SMS debits=life/finance. Location pings=record only. Screenshots=classify by content.
For url/youtube/pdf types: classify by the extracted content, not the URL or filename.

{items_text}

Return ONLY the JSON array."""


def _strip_html(html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    return m.group(1).strip() if m else ""


def _extract_github_readme(text: str) -> str:
    """Extract README section from stripped GitHub page text."""
    lower = text.lower()
    idx = lower.find("readme")
    if idx == -1:
        return text[:8000]
    # Start from the README marker
    section = text[idx:]
    # Cut at the next major boundary (another heading-like all-caps word or long gap)
    # Heuristic: take up to 6000 chars from README onwards
    return section[:6000]


async def fetch_url_content(url: str) -> tuple[str, str]:
    """Fetch a URL and return (text_content, page_title)."""
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as client:
        r = await client.get(url)
        r.raise_for_status()
        html = r.text
    title = _extract_title(html)
    text = _strip_html(html)
    if "github.com" in url:
        text = _extract_github_readme(text)
    else:
        text = text[:8000]
    return text, title


async def fetch_youtube_transcript(url: str) -> str:
    """Extract transcript from YouTube using yt-dlp."""
    yt_dlp_path = os.path.expanduser("~/.local/bin/yt-dlp")
    if not os.path.exists(yt_dlp_path):
        yt_dlp_path = "yt-dlp"

    with tempfile.TemporaryDirectory() as tmpdir:
        proc = await asyncio.create_subprocess_exec(
            yt_dlp_path, "--write-auto-sub", "--sub-lang", "en", "--skip-download",
            "--sub-format", "vtt", "-o", os.path.join(tmpdir, "video"), url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise ValueError(f"yt-dlp timed out for {url}")
        stderr_text = stderr.decode(errors="replace")
        vtt_files = [f for f in os.listdir(tmpdir) if f.endswith(".vtt")]
        if not vtt_files:
            raise ValueError(f"No transcript found for {url}. yt-dlp output: {stderr_text[:300]}")

        with open(os.path.join(tmpdir, vtt_files[0])) as f:
            vtt = f.read()

    # Strip VTT markup and deduplicate lines
    lines = []
    seen = set()
    for line in vtt.splitlines():
        line = line.strip()
        if not line or line.startswith("WEBVTT") or "-->" in line or re.match(r"^\d+$", line):
            continue
        clean = re.sub(r"<[^>]+>", "", line).strip()
        if clean and clean not in seen:
            seen.add(clean)
            lines.append(clean)

    return " ".join(lines)[:8000]


async def _invoke_graph_bg(item_id: str, content: str, source: str) -> None:
    """
    Fire the LangGraph personal-agent pipeline for a shortcut-routed item.
    classify_single is NOT called for these items — category is already set from capture_shortcuts.
    On failure: logs full traceback at ERROR then re-raises so the worker can apply retry logic.
    """
    import traceback
    from graph import build_graph
    from langgraph.checkpoint.postgres import PostgresSaver

    try:
        with PostgresSaver.from_conn_string(_db_url()) as checkpointer:
            graph = build_graph(checkpointer)
            graph.invoke(
                {
                    "raw_input": content,
                    "source": source,
                    "capture_uuid": item_id,
                    "routed_to": None,
                    "specialist_result": None,
                },
                config={"configurable": {"thread_id": item_id}},
            )
        logger.info("graph invoked", extra={"ctx": {"item_id": item_id, "source": source}})
    except Exception:
        logger.error(
            "graph invocation failed\n" + traceback.format_exc(),
            extra={"ctx": {"item_id": item_id, "source": source}},
        )
        raise


async def classify_single(item_id: str):
    try:
        result = supabase.table("items").select("*").eq("id", item_id).single().execute()
        item = result.data
        user_category = item.get("category")
        supabase.table("items").update({"classification_status": "processing"}).eq("id", item_id).execute()

        raw_content = item["raw_content"] or ""
        is_quote, author = _detect_quote(raw_content)

        if is_quote:
            tags = ["quote", "inspiration"]
            if author:
                tags.append(author.lower().replace(" ", "-"))
            supabase.table("items").update({
                "category": user_category or "learning",
                "subcategory": "quotes",
                "ai_tags": tags,
                "ai_summary": raw_content,
                "action_class": "record",
                "classification_status": "done",
            }).eq("id", item_id).execute()
            print(f"[classify_single] quote detected: {item_id} author={author!r}")
        else:
            raw = await classify_with_fallback(build_single_prompt(raw_content, item["source"], item.get("capture_type", "text")))
            clean = raw.strip().replace("```json", "").replace("```", "").strip()
            cls = json.loads(clean)

            supabase.table("items").update({
                "category": user_category or cls.get("category", "thoughts"),
                "subcategory": cls.get("subcategory"),
                "ai_tags": cls.get("tags", []),
                "ai_summary": cls.get("summary", ""),
                "action_class": cls.get("action_class", "record"),
                "classification_status": "done",
            }).eq("id", item_id).execute()

            print(f"[classify_single] done: {item_id} → {cls.get('category')}/{cls.get('action_class')}")

    except Exception as e:
        print(f"[classify_single] failed for {item_id}: {e}")
        supabase.table("items").update({"classification_status": "failed"}).eq("id", item_id).execute()


async def watch_eval_loop():
    """Evaluate watch rules every hour. Uses the same graph path as /watch slash command."""
    await asyncio.sleep(300)  # let startup settle before first run
    while True:
        try:
            await _invoke_graph_bg("watch-cron", "/watch", "system")
        except Exception as e:
            logger.error("watch_eval_loop error", extra={"ctx": {"error": str(e)}})
        await asyncio.sleep(3600)


async def batch_classification_loop():
    await asyncio.sleep(30)
    while True:
        await asyncio.sleep(120)
        try:
            result = supabase.table("items")\
                .select("id, raw_content, source, capture_type")\
                .in_("classification_status", ["queued", "failed"])\
                .order("created_at")\
                .limit(50)\
                .execute()

            pending = result.data
            if not pending:
                continue

            print(f"[batch] processing {len(pending)} items")
            ids = [p["id"] for p in pending]

            for item_id in ids:
                supabase.table("items").update({"classification_status": "processing"}).eq("id", item_id).execute()

            raw = await classify_with_fallback(build_batch_prompt(pending))
            clean = raw.strip().replace("```json", "").replace("```", "").strip()
            classifications = json.loads(clean)

            if len(classifications) != len(pending):
                print(f"[batch] MISMATCH: sent {len(pending)} items, got {len(classifications)} classifications — marking batch failed for retry")
                for item_id in ids:
                    supabase.table("items").update({"classification_status": "failed"}).eq("id", item_id).execute()
                continue
            for item, cls in zip(pending, classifications):
                supabase.table("items").update({
                    "category": cls.get("category", "thoughts"),
                    "subcategory": cls.get("subcategory"),
                    "ai_tags": cls.get("tags", []),
                    "ai_summary": cls.get("summary", ""),
                    "action_class": cls.get("action_class", "record"),
                    "classification_status": "done",
                }).eq("id", item["id"]).execute()

            print(f"[batch] done: {len(classifications)} items classified")

        except Exception as e:
            print(f"[batch_loop] error: {e}")
            if 'ids' in locals():
                for item_id in ids:
                    supabase.table("items").update({"classification_status": "queued"}).eq("id", item_id).execute()


# ---------------------------------------------------------------------------
# Job queue worker
# ---------------------------------------------------------------------------

def _enqueue_graph_invoke(item_id: str, content: str, source: str) -> None:
    with psycopg.connect(_db_url()) as conn:
        conn.execute(
            "INSERT INTO job_queue (job_type, payload) VALUES ('graph_invoke', %s)",
            (json.dumps({"item_id": item_id, "content": content, "source": source}),),
        )
        conn.commit()


def _recover_stale_locks() -> int:
    with psycopg.connect(_db_url()) as conn:
        cur = conn.execute(
            """UPDATE job_queue
               SET status='queued', locked_by=NULL, locked_at=NULL
               WHERE status='running'
                 AND locked_at < now() - INTERVAL '10 minutes'"""
        )
        conn.commit()
        return cur.rowcount


def _claim_next_job() -> dict | None:
    with psycopg.connect(_db_url()) as conn:
        row = conn.execute(
            """UPDATE job_queue
               SET status='running', locked_by=%s, locked_at=now(),
                   attempts=attempts+1, started_at=now()
               WHERE id = (
                   SELECT id FROM job_queue
                   WHERE status='queued' AND scheduled_at <= now()
                   ORDER BY priority, scheduled_at
                   LIMIT 1
                   FOR UPDATE SKIP LOCKED
               )
               RETURNING id, job_type, payload, attempts, max_attempts""",
            (_WORKER_ID,),
        ).fetchone()
        conn.commit()
    if row is None:
        return None
    return {"id": str(row[0]), "job_type": row[1], "payload": row[2],
            "attempts": row[3], "max_attempts": row[4]}


def _mark_job_done(job_id: str) -> None:
    with psycopg.connect(_db_url()) as conn:
        conn.execute(
            "UPDATE job_queue SET status='done', finished_at=now() WHERE id=%s",
            (job_id,),
        )
        conn.commit()


def _mark_job_retry_or_dead(job_id: str, attempts: int, max_attempts: int, error: str) -> None:
    status = "dead" if attempts >= max_attempts else "queued"
    with psycopg.connect(_db_url()) as conn:
        conn.execute(
            """UPDATE job_queue
               SET status=%s, locked_by=NULL, locked_at=NULL,
                   finished_at=CASE WHEN %s='dead' THEN now() END,
                   error=%s
               WHERE id=%s""",
            (status, status, error[:2000], job_id),
        )
        conn.commit()


async def job_queue_worker() -> None:
    import traceback
    recovered = _recover_stale_locks()
    if recovered:
        logger.info("stale lock recovery", extra={"ctx": {"recovered": recovered}})

    while True:
        try:
            job = _claim_next_job()
            if job is None:
                await asyncio.sleep(2)
                continue

            logger.info("job claimed", extra={"ctx": {"job_id": job["id"], "job_type": job["job_type"]}})
            try:
                if job["job_type"] == "graph_invoke":
                    p = job["payload"]
                    await _invoke_graph_bg(p["item_id"], p["content"], p["source"])
                else:
                    raise ValueError(f"unknown job_type: {job['job_type']!r}")
                _mark_job_done(job["id"])
                logger.info("job done", extra={"ctx": {"job_id": job["id"]}})
            except Exception:
                _mark_job_retry_or_dead(job["id"], job["attempts"], job["max_attempts"],
                                        traceback.format_exc())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("worker loop error", extra={"ctx": {"error": str(e)}})
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Outbox delivery loop
# ---------------------------------------------------------------------------

def _poll_outbox() -> list[dict]:
    with psycopg.connect(_db_url()) as conn:
        rows = conn.execute(
            """SELECT id, channel, recipient, message, attempts
               FROM outbox WHERE status='pending'
               ORDER BY created_at LIMIT 10"""
        ).fetchall()
    return [{"id": str(r[0]), "channel": r[1], "recipient": r[2],
             "message": r[3], "attempts": r[4]} for r in rows]


def _mark_outbox_sent(outbox_id: str) -> None:
    with psycopg.connect(_db_url()) as conn:
        conn.execute(
            "UPDATE outbox SET status='sent', delivered_at=now() WHERE id=%s",
            (outbox_id,),
        )
        conn.commit()


def _mark_outbox_attempt(outbox_id: str, error: str) -> None:
    with psycopg.connect(_db_url()) as conn:
        conn.execute(
            """UPDATE outbox
               SET attempts=attempts+1, error=%s,
                   status=CASE WHEN attempts+1 >= 5 THEN 'failed' ELSE status END
               WHERE id=%s""",
            (error[:500], outbox_id),
        )
        conn.commit()


async def outbox_delivery_loop() -> None:
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_base = f"https://api.telegram.org/bot{tg_token}"

    while True:
        try:
            rows = _poll_outbox()
            for row in rows:
                try:
                    if row["channel"] == "telegram":
                        async with httpx.AsyncClient(timeout=10) as client:
                            r = await client.post(
                                f"{tg_base}/sendMessage",
                                json={"chat_id": int(row["recipient"]),
                                      "text": row["message"],
                                      "parse_mode": "Markdown"},
                            )
                            r.raise_for_status()
                        _mark_outbox_sent(row["id"])
                        logger.info("outbox sent", extra={"ctx": {"outbox_id": row["id"],
                                                                   "channel": row["channel"]}})
                    else:
                        raise ValueError(f"unknown channel: {row['channel']!r}")
                except Exception as e:
                    logger.error("outbox delivery failed",
                                 extra={"ctx": {"outbox_id": row["id"], "error": str(e)}})
                    _mark_outbox_attempt(row["id"], str(e))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("outbox loop error", extra={"ctx": {"error": str(e)}})
        await asyncio.sleep(5)


_model_ready: bool = False
whisper_model = None


async def _load_whisper():
    import torch
    torch.backends.mkldnn.enabled = False
    global whisper_model, _model_ready
    loop = asyncio.get_event_loop()
    whisper_model = await loop.run_in_executor(None, lambda: whisper.load_model("tiny"))
    _model_ready = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(_load_whisper())
    batch_task = asyncio.create_task(batch_classification_loop())
    watch_task = asyncio.create_task(watch_eval_loop())
    worker_task = asyncio.create_task(job_queue_worker())
    outbox_task = asyncio.create_task(outbox_delivery_loop())
    yield
    batch_task.cancel()
    watch_task.cancel()
    worker_task.cancel()
    outbox_task.cancel()
    await asyncio.gather(worker_task, outbox_task, return_exceptions=True)


app = FastAPI(title="Brain API", version="1.0", lifespan=lifespan)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    rid = uuid.uuid4().hex[:8]
    t0 = time.monotonic()
    response = await call_next(request)
    logger.info("request", extra={"ctx": {
        "request_id": rid,
        "path": request.url.path,
        "method": request.method,
        "status": response.status_code,
        "latency_ms": round((time.monotonic() - t0) * 1000),
    }})
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    checks: dict = {}
    failed: list[str] = []

    if not _model_ready:
        checks["whisper"] = {"status": "critical", "detail": "model still loading"}
        failed.append("whisper")
    else:
        checks["whisper"] = {"status": "ok"}

    try:
        await asyncio.to_thread(
            lambda: supabase.table("items").select("id").limit(1).execute()
        )
        checks["postgres"] = {"status": "ok"}
    except Exception:
        checks["postgres"] = {"status": "critical", "detail": "unreachable"}
        failed.append("postgres")

    try:
        await asyncio.to_thread(
            lambda: supabase.table("items").select("embedding").limit(1).execute()
        )
        checks["pgvector"] = {"status": "ok"}
    except Exception:
        checks["pgvector"] = {"status": "critical", "detail": "vector type unavailable"}
        failed.append("pgvector")

    disk = shutil.disk_usage("/")
    free_gb = round(disk.free / 1024**3, 1)
    checks["disk"] = {
        "status": "warn" if free_gb < 2.0 else "ok",
        "free_gb": free_gb,
        "total_gb": round(disk.total / 1024**3, 1),
    }

    return JSONResponse(
        status_code=503 if failed else 200,
        content={
            "status": "degraded" if failed else "ok",
            "failed": failed,
            "checks": checks,
        },
    )


@app.post("/capture", dependencies=[Depends(verify_api_key)])
async def capture(data: CaptureInput):
    is_active = data.source in ACTIVE_SOURCES
    content = data.content
    capture_type = data.capture_type
    metadata = dict(data.metadata or {})

    # Deterministic slash-command check — BEFORE any LLM path. Zero cost.
    # /alias [rest] → lookup alias in capture_shortcuts → if found, skip classification.
    # Not found → fall through silently; normal classify path takes over.
    slash_alias = parse_slash(content)
    shortcut = lookup_shortcut(slash_alias) if slash_alias else None

    # Auto-detect URL captures
    stripped = content.strip()
    if stripped.lower().startswith(("http://", "https://")):
        url = stripped.split()[0]  # take first token in case of trailing text
        is_twitter = any(d in url for d in ("twitter.com", "x.com"))
        is_youtube = any(h in url for h in ("youtube.com", "youtu.be"))
        if is_twitter:
            content = f"Twitter/X post: {url}"
            capture_type = "url"
            metadata.update({"url": url})
            print(f"[capture] twitter url, skipping fetch: {url}")
        else:
            capture_type = "youtube" if is_youtube else "url"
            try:
                if is_youtube:
                    content = await fetch_youtube_transcript(url)
                    metadata.update({"url": url})
                    print(f"[capture] youtube transcript fetched: {url}")
                else:
                    text, title = await fetch_url_content(url)
                    content = text
                    metadata.update({"url": url, "title": title})
                    print(f"[capture] url fetched: {url} title={title!r}")
            except Exception as e:
                print(f"[capture] url/yt fetch failed ({url}): {e} — storing raw URL")
                content = url
                metadata.update({"url": url, "fetch_error": str(e)})

    # Shortcut hit → skip LLM classification entirely
    if shortcut:
        classification_status = "shortcut"
    elif is_active:
        classification_status = "instant"
    else:
        classification_status = "queued"

    insert_payload = {
        "raw_content": content,
        "source": data.source,
        "capture_type": capture_type,
        "classification_status": classification_status,
        "location_lat": data.lat,
        "location_lng": data.lng,
        "metadata": metadata,
    }
    if data.category:
        insert_payload["category"] = data.category
    elif shortcut and shortcut.get("category"):
        insert_payload["category"] = shortcut["category"]
    if data.title:
        insert_payload["title"] = data.title
    result = supabase.table("items").insert(insert_payload).execute()

    item_id = result.data[0]["id"]

    if shortcut or is_active:
        _enqueue_graph_invoke(item_id, content, data.source)

    path = "shortcut" if shortcut else ("instant" if is_active else "queued")
    response = {"status": "captured", "id": item_id, "path": path, "capture_type": capture_type}
    if shortcut:
        response["alias"] = slash_alias
    return response


@app.get("/items", dependencies=[Depends(verify_api_key)])
async def get_items(
    q: Optional[str] = None,
    cat: Optional[str] = None,
    tag: Optional[str] = None,
    limit: int = Query(default=50, le=200),
):
    query = supabase.table("items").select("*").eq("status", "active")
    if cat:
        query = query.eq("category", cat)
    result = query.order("created_at", desc=True).limit(limit).execute()
    items = result.data

    if tag:
        items = [i for i in items if tag in (i.get("ai_tags") or [])]
    if q:
        q_lower = q.lower()
        items = [i for i in items if q_lower in (i.get("raw_content") or "").lower()
                 or q_lower in (i.get("ai_summary") or "").lower()]

    return {"items": items, "count": len(items)}


@app.get("/items/{item_id}", dependencies=[Depends(verify_api_key)])
async def get_item(item_id: str):
    result = supabase.table("items").select("*").eq("id", item_id).single().execute()
    if not result.data:
        raise HTTPException(404, "Item not found")
    return result.data


@app.patch("/items/{item_id}", dependencies=[Depends(verify_api_key)])
async def update_item(item_id: str, updates: dict):
    allowed = {"category", "subcategory", "ai_tags", "task_status",
               "task_deadline", "task_progress", "plan_bucket",
               "plan_order", "plan_date", "rollover_note",
               "status", "reviewed", "raw_content", "ai_summary"}
    filtered = {k: v for k, v in updates.items() if k in allowed}
    if not filtered:
        raise HTTPException(400, "No valid fields to update")
    result = supabase.table("items").update(filtered).eq("id", item_id).execute()
    if "task_status" in filtered:
        _enqueue_graph_invoke(item_id, "/plan", "system")
    return result.data[0]


@app.get("/tasks", dependencies=[Depends(verify_api_key)])
async def get_tasks(status: Optional[str] = None):
    query = supabase.table("items").select("*").eq("action_class", "task")
    if status:
        if status == "pending":
            # include rows where task_status is null OR pending
            result_null = query.is_("task_status", "null").order("created_at", desc=True).execute()
            result_pend = supabase.table("items").select("*").eq("action_class", "task")\
                .eq("task_status", "pending").order("created_at", desc=True).execute()
            tasks = result_null.data + result_pend.data
            tasks.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            return {"tasks": tasks}
        else:
            query = query.eq("task_status", status)
    result = query.order("created_at", desc=True).execute()
    tasks = result.data
    # Normalise null task_status to "pending"
    for t in tasks:
        if not t.get("task_status"):
            t["task_status"] = "pending"
    return {"tasks": tasks}


@app.get("/planner/counts", dependencies=[Depends(verify_api_key)])
async def get_planner_counts():
    counts = {}
    for b in ("today", "this_week", "this_month", "someday"):
        r = supabase.table("items").select("id", count="exact").eq("plan_bucket", b).eq("status", "active").execute()
        counts[b] = r.count or 0
    unplanned = supabase.table("items").select("id", count="exact")\
        .is_("plan_bucket", "null").eq("action_class", "task").eq("status", "active").execute()
    counts["unplanned"] = unplanned.count or 0
    return counts


@app.get("/planner", dependencies=[Depends(verify_api_key)])
async def get_planner(bucket: Optional[str] = None):
    query = supabase.table("items").select("*")
    if bucket:
        query = query.eq("plan_bucket", bucket)
    else:
        query = query.is_("plan_bucket", "null").eq("action_class", "task").eq("status", "active")
    result = query.order("plan_order").order("created_at", desc=True).execute()
    return {"items": result.data}


class MovePlannerInput(BaseModel):
    bucket: str


@app.patch("/planner/{item_id}/move", dependencies=[Depends(verify_api_key)])
async def move_planner(item_id: str, body: MovePlannerInput):
    bucket = body.bucket
    valid = {"today", "this_week", "this_month", "someday", "unplanned"}
    if bucket not in valid:
        raise HTTPException(400, f"bucket must be one of {valid}")
    result = supabase.table("items").update({
        "plan_bucket": None if bucket == "unplanned" else bucket
    }).eq("id", item_id).execute()
    return result.data[0]

@app.post("/upload", dependencies=[Depends(verify_api_key)])
async def upload_pdf(file: UploadFile = File(...), source: str = "web_upload"):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are supported")

    try:
        from pypdf import PdfReader
    except ImportError:
        raise HTTPException(500, "pypdf not installed")

    contents = await file.read()
    reader = PdfReader(io.BytesIO(contents))
    pages_text = []
    for page in reader.pages:
        pages_text.append(page.extract_text() or "")
    text = "\n".join(pages_text).strip()

    if not text:
        raise HTTPException(422, "Could not extract text from PDF")

    is_active = source in ACTIVE_SOURCES
    result = supabase.table("items").insert({
        "raw_content": text[:10000],
        "source": source,
        "capture_type": "pdf",
        "classification_status": "instant" if is_active else "queued",
        "metadata": {"filename": file.filename, "pages": len(reader.pages)},
    }).execute()

    item_id = result.data[0]["id"]
    if is_active:
        _enqueue_graph_invoke(item_id, text[:10000], source)

    return {"status": "captured", "id": item_id, "filename": file.filename, "pages": len(reader.pages), "path": "instant" if is_active else "queued"}


class JournalInput(BaseModel):
    mood_score: int
    energy_score: int
    content: str


@app.post("/journal", dependencies=[Depends(verify_api_key)])
async def journal(data: JournalInput):
    if not (1 <= data.mood_score <= 5 and 1 <= data.energy_score <= 5):
        raise HTTPException(400, "mood_score and energy_score must be between 1 and 5")

    result = supabase.table("items").insert({
        "raw_content": data.content,
        "source": "journal",
        "capture_type": "text",
        "category": "thoughts",
        "subcategory": "journal",
        "action_class": "record",
        "mood_score": data.mood_score,
        "energy_score": data.energy_score,
        "ai_summary": data.content[:100],
        "classification_status": "done",
    }).execute()

    item_id = result.data[0]["id"]
    return {"status": "saved", "id": item_id}


@app.get("/finance", dependencies=[Depends(verify_api_key)])
async def get_finance():
    # Single query: category+subcategory match OR any finance-related tag
    r = supabase.table("items").select("*")\
        .eq("status", "active")\
        .or_('and(category.eq.life,subcategory.eq.finance),ai_tags.cs.["finance"],ai_tags.cs.["transaction"],ai_tags.cs.["debit"],ai_tags.cs.["credit"]')\
        .order("created_at", desc=True).limit(200).execute()
    items = r.data or []

    # Group by month
    monthly = {}
    for item in items:
        created = item.get("created_at", "")
        if created:
            month_key = created[:7]  # "2026-04"
            if month_key not in monthly:
                from datetime import datetime
                dt = datetime.strptime(month_key, "%Y-%m")
                monthly[month_key] = {
                    "total_items": 0,
                    "month_label": dt.strftime("%B %Y"),
                }
            monthly[month_key]["total_items"] += 1

    return {"items": items, "monthly": monthly}


@app.get("/finance/infra-cost", dependencies=[Depends(verify_api_key)])
async def finance_infra_cost():
    try:
        with _db_conn() as conn:
            rows = conn.execute(
                "SELECT item, amount_inr, note FROM infra_costs ORDER BY amount_inr DESC, item"
            ).fetchall()
    except Exception:
        logger.exception("finance_infra_cost: DB query failed")
        raise HTTPException(status_code=500, detail="query failed")

    return {
        "monthly_fixed_costs": [
            {"item": r[0], "amount_inr": str(r[1]), "note": r[2]}
            for r in rows
        ],
        "total_monthly_inr": str(sum(r[1] for r in rows)),
        "note": (
            "Static figures from PROJECT_BIBLE §15, converted to INR at 94.49 INR/USD (2026-09-06). "
            "Real per-call LLM cost tracking is deliberately deferred — "
            "v1 is static config, not computed spend."
        ),
    }


@app.get("/finance/calendar", dependencies=[Depends(verify_api_key)])
async def finance_calendar(month: str = Query(default=None)):
    today = date.today()
    if month is None:
        month = f"{today.year}-{today.month:02d}"
    try:
        if len(month) != 7 or month[4] != "-":
            raise ValueError
        year, mon = int(month[:4]), int(month[5:7])
        if not (1 <= mon <= 12):
            raise ValueError
    except (ValueError, IndexError):
        raise HTTPException(status_code=400, detail="month must be YYYY-MM")

    month_start = date(year, mon, 1)
    month_end = date(year + (mon == 12), 1 if mon == 12 else mon + 1, 1)

    try:
        with _db_conn() as conn:
            rows = conn.execute(
                """SELECT
                       transaction_date,
                       json_agg(
                           json_build_object(
                               'id',        id,
                               'amount',    amount::text,
                               'direction', direction,
                               'merchant',  merchant,
                               'category',  category
                           )
                           ORDER BY id
                       )                                                            AS transactions,
                       COALESCE(SUM(amount) FILTER (WHERE direction = 'debit'),  0) AS total_debit,
                       COALESCE(SUM(amount) FILTER (WHERE direction = 'credit'), 0) AS total_credit
                   FROM transactions
                   WHERE transaction_date >= %s
                     AND transaction_date <  %s
                   GROUP BY transaction_date
                   ORDER BY transaction_date""",
                (month_start, month_end),
            ).fetchall()
    except Exception:
        logger.exception("finance_calendar: DB query failed")
        raise HTTPException(status_code=500, detail="query failed")

    days = [
        {
            "date": str(r[0]),
            "transactions": r[1],
            "total_debit": str(r[2]),
            "total_credit": str(r[3]),
        }
        for r in rows
    ]
    month_total_debit = str(sum(r[2] for r in rows))
    month_total_credit = str(sum(r[3] for r in rows))

    return {
        "month": month,
        "days": days,
        "month_total_debit": month_total_debit,
        "month_total_credit": month_total_credit,
    }


@app.post("/upload-audio", dependencies=[Depends(verify_api_key)])
async def upload_audio(
    file: UploadFile = File(...),
    source: str = Form("app_voice"),
    capture_type: str = Form("voice"),
):
    if not _model_ready:
        raise HTTPException(status_code=503, detail="whisper model loading")

    content = await file.read()

    suffix = os.path.splitext(file.filename or "audio.m4a")[1] or ".m4a"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        loop = asyncio.get_event_loop()
        result_w = await loop.run_in_executor(None, lambda: whisper_model.transcribe(tmp_path))
        transcript = result_w["text"].strip()
    finally:
        os.unlink(tmp_path)

    is_active = source in ACTIVE_SOURCES
    result = supabase.table("items").insert({
        "raw_content": transcript,
        "source": source,
        "capture_type": capture_type,
        "classification_status": "instant" if is_active else "queued",
        "metadata": {"audio_size": len(content), "format": suffix.lstrip(".")}
    }).execute()

    item_id = result.data[0]["id"]
    if is_active:
        _enqueue_graph_invoke(item_id, transcript, source)

    return {"status": "captured", "id": item_id, "transcript": transcript}


@app.delete("/items/{item_id}", dependencies=[Depends(verify_api_key)])
async def delete_item(item_id: str):
    supabase.table("items").update({"status": "deleted"}).eq("id", item_id).execute()
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Notebooks endpoints
# ---------------------------------------------------------------------------

class NotebookCreateInput(BaseModel):
    name: str
    notebook_type: str = "gate_subject"


_VALID_NOTEBOOK_TYPES = {"gate_subject", "general", "project"}


@app.post("/notebooks", dependencies=[Depends(verify_api_key)], status_code=201)
async def create_notebook(data: NotebookCreateInput):
    if data.notebook_type not in _VALID_NOTEBOOK_TYPES:
        raise HTTPException(400, f"notebook_type must be one of {sorted(_VALID_NOTEBOOK_TYPES)}")
    name = data.name.strip()
    if not name:
        raise HTTPException(400, "name must not be blank")
    with _db_conn() as conn:
        row = conn.execute(
            """INSERT INTO notebooks (name, notebook_type)
               VALUES (%s, %s)
               ON CONFLICT ON CONSTRAINT uq_notebooks_name_type
               DO UPDATE SET archived_at = NULL
               RETURNING id, name, notebook_type, created_at, archived_at""",
            (name, data.notebook_type),
        ).fetchone()
        conn.commit()
    return {
        "id": row[0], "name": row[1], "notebook_type": row[2],
        "created_at": row[3].isoformat(), "archived_at": row[4],
    }


@app.get("/notebooks", dependencies=[Depends(verify_api_key)])
async def list_notebooks(
    notebook_type: Optional[str] = None,
    include_archived: bool = False,
):
    with _db_conn() as conn:
        wheres = [] if include_archived else ["archived_at IS NULL"]
        params: list = []
        if notebook_type:
            wheres.append("notebook_type = %s")
            params.append(notebook_type)
        where_clause = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        rows = conn.execute(
            f"SELECT id, name, notebook_type, created_at, archived_at "
            f"FROM notebooks {where_clause} ORDER BY notebook_type, name",
            params,
        ).fetchall()
    return {
        "notebooks": [
            {
                "id": r[0], "name": r[1], "notebook_type": r[2],
                "created_at": r[3].isoformat(), "archived_at": r[4],
            }
            for r in rows
        ]
    }


# ---------------------------------------------------------------------------
# Backlinks + graph endpoints (raw psycopg — thought_links requires joins
# the Supabase client can't express)
# ---------------------------------------------------------------------------

def _db_conn() -> psycopg.Connection:
    url = os.environ.get("BRAIN_DB_URL", "")
    if not url:
        raise HTTPException(500, "BRAIN_DB_URL not set")
    return psycopg.connect(url)


def _summary_snippet(text: str | None, max_len: int = 100) -> str | None:
    if not text:
        return None
    return text[:max_len] + ("…" if len(text) > max_len else "")


def _mention_snippet(raw: str, title: str, window: int = 120) -> str:
    idx = raw.lower().find(title.lower())
    if idx == -1:
        return raw[:window]
    start = max(0, idx - 40)
    end = min(len(raw), idx + len(title) + 80)
    snippet = raw[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(raw):
        snippet = snippet + "…"
    return snippet


@app.get("/items/{item_id}/backlinks", dependencies=[Depends(verify_api_key)])
async def get_backlinks(item_id: str):
    with _db_conn() as conn:
        rows = conn.execute(
            """SELECT i.id, i.title, i.category, i.ai_summary,
                      tl.link_type, tl.wikilink_text, tl.similarity_score
                 FROM thought_links tl
                 JOIN items i ON i.id = tl.source_item_id
                WHERE tl.target_item_id = %s
                  AND i.status = 'active'
                ORDER BY tl.created_at DESC""",
            (item_id,),
        ).fetchall()

    backlinks = [
        {
            "source_id":        str(r[0]),
            "title":            r[1],
            "category":         r[2],
            "summary":          _summary_snippet(r[3]),
            "link_type":        r[4],
            "wikilink_text":    r[5],
            "similarity_score": r[6],
        }
        for r in rows
    ]
    return {"item_id": item_id, "backlinks": backlinks, "count": len(backlinks)}


@app.get("/items/{item_id}/unlinked-mentions", dependencies=[Depends(verify_api_key)])
async def get_unlinked_mentions(item_id: str):
    with _db_conn() as conn:
        item = conn.execute(
            "SELECT title FROM items WHERE id = %s AND status = 'active'",
            (item_id,),
        ).fetchone()

    if not item:
        raise HTTPException(404, "Item not found")
    title = item[0]
    if not title:
        raise HTTPException(404, "Item has no title — unlinked-mentions only applies to titled items")

    with _db_conn() as conn:
        rows = conn.execute(
            """SELECT id, title, category, ai_summary, raw_content
                 FROM items
                WHERE raw_content ILIKE '%%' || %s || '%%'
                  AND id != %s
                  AND status = 'active'
                  AND id NOT IN (
                      SELECT source_item_id FROM thought_links
                       WHERE target_item_id = %s
                         AND link_type = 'wikilink'
                  )
                ORDER BY created_at DESC""",
            (title, item_id, item_id),
        ).fetchall()

    mentions = [
        {
            "id":       str(r[0]),
            "title":    r[1],
            "category": r[2],
            "summary":  _summary_snippet(r[3]),
            "snippet":  _mention_snippet(r[4] or "", title),
        }
        for r in rows
    ]
    return {"item_id": item_id, "title": title, "mentions": mentions, "count": len(mentions)}


@app.post("/items/{item_id}/link-mentions", dependencies=[Depends(verify_api_key)])
async def link_mentions(item_id: str):
    with _db_conn() as conn:
        item = conn.execute(
            "SELECT title FROM items WHERE id = %s AND status = 'active'",
            (item_id,),
        ).fetchone()

        if not item:
            raise HTTPException(404, "Item not found")
        title = item[0]
        if not title:
            raise HTTPException(404, "Item has no title — link-mentions only applies to titled items")

        rows = conn.execute(
            """SELECT id FROM items
                WHERE raw_content ILIKE '%%' || %s || '%%'
                  AND id != %s
                  AND status = 'active'
                  AND id NOT IN (
                      SELECT source_item_id FROM thought_links
                       WHERE target_item_id = %s
                         AND link_type = 'wikilink'
                  )""",
            (title, item_id, item_id),
        ).fetchall()

        linked = skipped = 0
        for (source_id,) in rows:
            result = conn.execute(
                """INSERT INTO thought_links
                       (source_item_id, target_item_id, link_type, wikilink_text)
                   VALUES (%s, %s, 'wikilink', %s)
                   ON CONFLICT (source_item_id, target_item_id, link_type) DO NOTHING""",
                (str(source_id), item_id, title),
            )
            if result.rowcount == 1:
                linked += 1
            else:
                skipped += 1
        conn.commit()

    return {"item_id": item_id, "title": title, "linked": linked, "skipped": skipped}


@app.get("/graph", dependencies=[Depends(verify_api_key)])
async def get_graph():
    NODE_CAP = 500
    with _db_conn() as conn:
        node_rows = conn.execute(
            """SELECT DISTINCT i.id, i.title, i.category, i.ai_summary
                 FROM items i
                WHERE i.status = 'active'
                  AND i.id IN (
                      SELECT source_item_id FROM thought_links
                      UNION
                      SELECT target_item_id FROM thought_links
                  )
                LIMIT %s""",
            (NODE_CAP + 1,),
        ).fetchall()

        capped = len(node_rows) > NODE_CAP
        node_rows = node_rows[:NODE_CAP]
        node_ids = {r[0] for r in node_rows}

        edge_rows = conn.execute(
            """SELECT source_item_id, target_item_id, link_type
                 FROM thought_links
                WHERE source_item_id = ANY(%s)
                  AND target_item_id = ANY(%s)""",
            (list(node_ids), list(node_ids)),
        ).fetchall()

    nodes = [
        {
            "id":       str(r[0]),
            "title":    r[1],
            "category": r[2],
            "summary":  _summary_snippet(r[3], max_len=80),
        }
        for r in node_rows
    ]
    edges = [
        {"source": str(r[0]), "target": str(r[1]), "link_type": r[2]}
        for r in edge_rows
    ]
    return {
        "nodes":      nodes,
        "edges":      edges,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "capped":     capped,
    }


@app.get("/items/{item_id}/graph", dependencies=[Depends(verify_api_key)])
async def get_item_graph(item_id: str):
    with _db_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM items WHERE id = %s AND status = 'active'",
            (item_id,),
        ).fetchone()
        if not exists:
            raise HTTPException(404, "Item not found")

        node_rows = conn.execute(
            """SELECT DISTINCT i.id, i.title, i.category, i.ai_summary
                 FROM items i
                WHERE i.status = 'active'
                  AND (
                      i.id = %s
                      OR i.id IN (
                          SELECT target_item_id FROM thought_links WHERE source_item_id = %s
                          UNION
                          SELECT source_item_id FROM thought_links WHERE target_item_id = %s
                      )
                  )""",
            (item_id, item_id, item_id),
        ).fetchall()

        node_ids = {r[0] for r in node_rows}
        edge_rows = conn.execute(
            """SELECT source_item_id, target_item_id, link_type
                 FROM thought_links
                WHERE source_item_id = ANY(%s)
                  AND target_item_id = ANY(%s)""",
            (list(node_ids), list(node_ids)),
        ).fetchall()

    nodes = [
        {
            "id":       str(r[0]),
            "title":    r[1],
            "category": r[2],
            "summary":  _summary_snippet(r[3], max_len=80),
        }
        for r in node_rows
    ]
    edges = [
        {"source": str(r[0]), "target": str(r[1]), "link_type": r[2]}
        for r in edge_rows
    ]
    return {
        "focal_id":   item_id,
        "nodes":      nodes,
        "edges":      edges,
        "node_count": len(nodes),
        "edge_count": len(edges),
    }
