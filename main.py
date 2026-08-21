import asyncio
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from starlette.requests import Request
import whisper
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, Security, UploadFile
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
ONEMIN_MODEL = "claude-haiku-4-5-20251001"
FALLBACK_MODEL = "gpt-4o-mini"


class CaptureInput(BaseModel):
    content: str
    source: str
    capture_type: str = "text"
    lat: Optional[float] = None
    lng: Optional[float] = None
    metadata: Optional[dict] = {}
    category: Optional[str] = None


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

    return f"""Classify this capture. Return ONLY valid JSON, no other text, no markdown.

Content: {content[:1000]}
Source: {source}
Type: {capture_type}
{type_context}
{quote_hint}

Return exactly this structure:
{{
  "category": "learning|thoughts|work|life|resources|health",
  "subcategory": "wayclear|accrediq|finance|quotes|grocery|tech|hacks|music|null",
  "tags": ["tag1", "tag2", "tag3"],
  "summary": "under 50 words",
  "action_class": "record|task|agent|alert|build"
}}

Context: User is Ashish, solo founder building WayClear (road safety) and AccredIQ (IRC accreditation).
SMS debits = life/finance. WhatsApp = life/people. URLs = resources. Tasks/todos = work."""


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
    On any failure: log full traceback at ERROR with item_id, then swallow so the stored item is unaffected.
    """
    import traceback
    from graph import build_graph
    from langgraph.checkpoint.postgres import PostgresSaver

    db_url = os.environ.get("BRAIN_DB_URL", "")
    if not db_url:
        logger.error("_invoke_graph_bg skipped: BRAIN_DB_URL not set", extra={"ctx": {"item_id": item_id}})
        return
    try:
        with PostgresSaver.from_conn_string(db_url) as checkpointer:
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
    task = asyncio.create_task(batch_classification_loop())
    yield
    task.cancel()


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
async def capture(data: CaptureInput, bg: BackgroundTasks):
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
    result = supabase.table("items").insert(insert_payload).execute()

    item_id = result.data[0]["id"]

    if shortcut:
        bg.add_task(_invoke_graph_bg, item_id, content, data.source)
    elif is_active:
        bg.add_task(classify_single, item_id)
        bg.add_task(_invoke_graph_bg, item_id, content, data.source)  # shadow: capture_agent runs alongside classify_single

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
               "plan_order", "status", "reviewed", "raw_content", "ai_summary"}
    filtered = {k: v for k, v in updates.items() if k in allowed}
    if not filtered:
        raise HTTPException(400, "No valid fields to update")
    result = supabase.table("items").update(filtered).eq("id", item_id).execute()
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
async def upload_pdf(bg: BackgroundTasks, file: UploadFile = File(...), source: str = "web_upload"):
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
        bg.add_task(classify_single, item_id)

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


@app.post("/upload-audio", dependencies=[Depends(verify_api_key)])
async def upload_audio(
    file: UploadFile = File(...),
    source: str = Form("app_voice"),
    capture_type: str = Form("voice"),
    bg: BackgroundTasks = BackgroundTasks()
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
        bg.add_task(classify_single, item_id)

    return {"status": "captured", "id": item_id, "transcript": transcript}


@app.delete("/items/{item_id}", dependencies=[Depends(verify_api_key)])
async def delete_item(item_id: str):
    supabase.table("items").update({"status": "deleted"}).eq("id", item_id).execute()
    return {"status": "deleted"}
