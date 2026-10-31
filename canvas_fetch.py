#!/usr/bin/env python3
"""
canvas_fetch.py

Pulls everything Claude needs from Canvas and packages it for upload. No AI
calls here. You drag the contents of upload_me/ into a Claude chat and ask for
the list; Claude does the interpreting and sorting.

Run
  python canvas_fetch.py                 # upload_me/ gets the manifest + files new since last run
  python canvas_fetch.py --all           # upload_me/ gets everything (use for a fresh chat)
  python canvas_fetch.py --course 43639  # one course only
"""
import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent

_env = HERE / ".env"
if _env.exists():
    for _line in _env.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip("\"'"))

CANVAS_URL = os.environ.get("CANVAS_URL", "https://ecourses.pvamu.edu").rstrip("/")
API = CANVAS_URL + "/api/v1"
DUMP = HERE / "dump"
UPLOAD = HERE / "upload_me"
STATE = HERE / "fetch_state.json"

MAX_FILE_BYTES = 30_000_000
DESC_MAX, PAGE_MAX, SYLLABUS_MAX = 1500, 4000, 6000
READABLE = {".pdf", ".pptx", ".ppt", ".docx", ".doc", ".txt", ".md", ".csv",
            ".png", ".jpg", ".jpeg"}
OFFICE = {".pptx", ".ppt", ".docx", ".doc"}

SKIP_MODULES = ["canvas: student overview"]
SKIP_TITLES = ["student guide", "additional support", "academic integrity",
               "gradebook", "grade book"]


def _head():
    return {"Authorization": f"Bearer {os.environ['CANVAS_TOKEN']}"}


def get_all(path, params=None):
    url = path if path.startswith("http") else API + path
    out = []
    while url:
        r = requests.get(url, headers=_head(), params=params, timeout=60)
        r.raise_for_status()
        data = r.json()
        out += data if isinstance(data, list) else [data]
        url = r.links.get("next", {}).get("url")
        params = None
    return out


def get_one(path):
    r = requests.get(API + path, headers=_head(), timeout=60)
    r.raise_for_status()
    return r.json()


def html_to_text(h):
    h = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", h or "")
    h = re.sub(r"<[^>]+>", " ", h)
    return re.sub(r"\s+", " ", html.unescape(h)).strip()


def clip(s, n):
    return s if len(s) <= n else s[:n].rstrip() + " ...[truncated]"


def slug(s):
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:40] or "course"


def safe_name(n):
    return re.sub(r'[\\/:*?"<>|]+', "_", n)


def fmt_due(iso):
    if not iso:
        return "no date"
    d = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    return f"{d:%a %b} {d.day}, {d.hour % 12 or 12}:{d:%M} {d:%p}"


def office_to_pdf(name, data):
    soffice = (os.environ.get("SOFFICE_PATH") or shutil.which("soffice")
               or shutil.which("libreoffice"))
    if not soffice or (not Path(soffice).exists() and not shutil.which(soffice)):
        return None
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / safe_name(Path(name).name)
        src.write_bytes(data)
        try:
            subprocess.run([soffice, "--headless", "--convert-to", "pdf",
                            "--outdir", d, str(src)],
                           check=True, capture_output=True, timeout=240)
        except Exception:
            return None
        out = src.with_suffix(".pdf")
        return out.read_bytes() if out.exists() else None


def fetch_file(cid, tag, fid, state, include_all):
    base = {"name": f"file {fid}", "link": "", "path": None, "new": False,
            "note": "", "upload_name": None}
    try:
        meta = get_one(f"/files/{fid}")
    except Exception as e:
        base["note"] = f"unavailable ({e})"
        return base

    name = meta.get("display_name") or str(fid)
    base["name"] = name
    base["link"] = f"{CANVAS_URL}/courses/{cid}/files/{fid}"
    ext = Path(name).suffix.lower()

    if ext not in READABLE:
        base["note"] = f"skipped ({ext or 'no extension'})"
        return base
    if meta.get("size", 0) > MAX_FILE_BYTES:
        base["note"] = "skipped (too large)"
        return base

    key = f"{fid}:{meta.get('modified_at')}"
    rel = state.get(key)
    if rel and (DUMP / rel).exists():
        local = DUMP / rel
    else:
        print(f"  downloading {name}")
        data = requests.get(meta["url"], timeout=180).content
        out_name = name
        if ext in OFFICE:
            pdf = office_to_pdf(name, data)
            if pdf:
                data, out_name = pdf, Path(name).stem + ".pdf"
        folder = DUMP / tag
        folder.mkdir(parents=True, exist_ok=True)
        local = folder / safe_name(out_name)
        local.write_bytes(data)
        state[key] = str(local.relative_to(DUMP))
        base["new"] = True

    base["path"] = local
    if base["new"] or include_all:
        up = UPLOAD / f"{tag}__{local.name}"
        shutil.copy(local, up)
        base["upload_name"] = up.name
    return base


def file_line(f):
    if f["path"] is None:
        return f"{f['name']} - {f['note']}"
    where = f"`{f['upload_name']}`" if f["upload_name"] else "(uploaded in an earlier batch)"
    return f"{f['name']} -> {where} [{'NEW' if f['new'] else 'seen'}]"


def load_todos():
    by_course = defaultdict(list)
    for t in get_all("/users/self/todo", {"per_page": 100}):
        a = t.get("assignment") or t.get("quiz")
        cid = t.get("course_id")
        if not a or not cid:
            continue
        by_course[cid].append({
            "name": a.get("name") or a.get("title") or "Untitled",
            "due": a.get("due_at"),
            "pts": a.get("points_possible"),
            "link": a.get("html_url") or t.get("html_url") or CANVAS_URL,
            "desc": html_to_text(a.get("description")),
        })
    return by_course


def detail_lines(cid, it):
    kind = "assignments" if it["type"] == "Assignment" else "quizzes"
    head = f"- {it['type']}: {it['title']}"
    try:
        a = get_one(f"/courses/{cid}/{kind}/{it['content_id']}")
    except Exception:
        return [head]
    pts = a.get("points_possible")
    line = f"{head} - due {fmt_due(a.get('due_at'))}"
    if pts:
        line += f" - {pts} pts"
    lines = [f"{line} ({it.get('html_url', '')})"]
    d = html_to_text(a.get("description"))
    if d:
        lines.append(f"  > {clip(d, DESC_MAX)}")
    return lines


def build_course(c, todos, state, include_all):
    cid = c["id"]
    tag = slug(c.get("course_code") or c["name"])
    L = [f"## {c['name']}", f"(course id {cid})", ""]

    try:
        syl = html_to_text(get_one(f"/courses/{cid}?include[]=syllabus_body").get("syllabus_body"))
    except Exception:
        syl = ""
    if syl:
        L += ["### Syllabus", clip(syl, SYLLABUS_MAX), ""]

    L += ["### To Do (from Canvas)", ""]
    if not todos:
        L += ["(nothing pending)", ""]
    for t in sorted(todos, key=lambda t: t["due"] or "9999"):
        pts = f" - {t['pts']} pts" if t["pts"] else ""
        L.append(f"- {t['name']} - due {fmt_due(t['due'])}{pts} ({t['link']})")
        if t["desc"]:
            L.append(f"  > {clip(t['desc'], DESC_MAX)}")
    L.append("")

    L += ["### Modules", ""]
    seen = set()
    try:
        modules = get_all(f"/courses/{cid}/modules", {"include[]": "items", "per_page": 100})
    except Exception as e:
        modules = []
        L.append(f"(modules unavailable: {e})")

    for m in modules:
        items = m.get("items") or get_all(f"/courses/{cid}/modules/{m['id']}/items",
                                          {"per_page": 100})
        for it in items:
            if it["type"] == "File" and it.get("content_id"):
                seen.add(it["content_id"])
        L.append(f"#### {m['name']}")
        if any(s in m["name"].lower() for s in SKIP_MODULES):
            L += ["(Canvas boilerplate, skipped)", ""]
            continue

        for it in items:
            t, title = it["type"], it["title"]
            if any(s in title.lower() for s in SKIP_TITLES):
                continue
            if t == "SubHeader":
                L.append(f"**{title}**")
            elif t == "File" and it.get("content_id"):
                f = fetch_file(cid, tag, it["content_id"], state, include_all)
                L.append(f"- File: {file_line(f)}")
            elif t == "Page" and it.get("page_url"):
                try:
                    p = get_one(f"/courses/{cid}/pages/{it['page_url']}")
                    text = html_to_text(p.get("body"))
                    L.append(f"- Page: {title} ({p.get('html_url', '')})")
                    if len(text) >= 80:
                        L.append(f"  > {clip(text, PAGE_MAX)}")
                except Exception:
                    L.append(f"- Page: {title}")
            elif t in ("Assignment", "Quiz") and it.get("content_id"):
                L += detail_lines(cid, it)
            elif t in ("ExternalUrl", "ExternalTool"):
                L.append(f"- Link: {title} ({it.get('external_url', '')})")
            else:
                L.append(f"- {t}: {title}")
        L.append("")

    L += ["### Loose files (not in any module)", ""]
    try:
        loose = get_all(f"/courses/{cid}/files", {"per_page": 100})
    except Exception:
        loose = []
    found = False
    for f in loose:
        if f["id"] not in seen:
            found = True
            L.append(f"- {file_line(fetch_file(cid, tag, f['id'], state, include_all))}")
    if not found:
        L.append("(none)")
    L.append("")
    return L


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="put every file in upload_me/")
    ap.add_argument("--course", type=int, help="only this course id")
    a = ap.parse_args()

    if not os.environ.get("CANVAS_TOKEN"):
        sys.exit("Missing CANVAS_TOKEN. Put it in a .env file next to this script.")

    if UPLOAD.exists():
        shutil.rmtree(UPLOAD)
    UPLOAD.mkdir()
    DUMP.mkdir(exist_ok=True)

    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    todos_by_course = load_todos()

    courses = [c for c in get_all("/courses", {"enrollment_state": "active", "per_page": 100})
               if c.get("name")]
    if a.course:
        courses = [c for c in courses if c["id"] == a.course]
        if not courses:
            sys.exit(f"Course {a.course} not found among your active courses.")

    body = []
    try:
        for c in sorted(courses, key=lambda c: c["name"]):
            print(c["name"])
            body += build_course(c, todos_by_course.get(c["id"], []), state, a.all)
    finally:
        STATE.write_text(json.dumps(state, indent=1), encoding="utf-8")

    stamp = datetime.now().strftime("%a %b %d, %I:%M %p")
    head = ["# Canvas dump", f"_Fetched {stamp}_", "",
            "Files marked NEW are in this upload. Files marked seen went up in an "
            "earlier batch, so they are only available if they are in this chat.", ""]
    manifest = "\n".join(head + body)
    (UPLOAD / "manifest.md").write_text(manifest, encoding="utf-8")
    (DUMP / "manifest.md").write_text(manifest, encoding="utf-8")

    n = len(list(UPLOAD.iterdir()))
    print(f"\nupload_me/ has {n} files (manifest.md included). Drag them into Claude.")


if __name__ == "__main__":
    main()
