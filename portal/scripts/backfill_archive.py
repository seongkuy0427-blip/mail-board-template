#!/usr/bin/env python3
"""과거 메일 점진 백필(포털판) — board.json 에 지난 메일을 '이전 기록(archived)'으로 채운다.

`sync_mail.py` 가 '새 메일'을 처리한다면, 이 모듈은 10분 워커가 새 메일 동기화를 끝낸 뒤
한 배치(기본 100건)씩 웹메일 목록을 최신순으로 거슬러 올라가며 지난 메일을 보드에 더한다.

왜 목록 미리보기(data-preview)인가
  · 목록 한 페이지(100건)를 한 번의 요청으로 받으므로 9,000건도 90번 남짓이면 닿는다.
  · 편지읽기 페이지를 열지 않으므로 **사용자 편지함이 '읽음'으로 바뀌지 않는다**.
  · 미리보기 본문을 summary/bodyText 로 넣어, 화면의 '검색'과 '유사 사례' 색인이 실제
    문장 근거로 동작한다(제목만 있던 예전 아카이브보다 쓸모가 크다).

지키는 선
  · 항목은 `archived: true` 로 들어간다. 대시보드는 archived 항목을 기본 목록·KPI·캘린더에서
    빼고, 검색('이전 기록')과 유사 사례 색인에만 쓴다.
  · 진행 위치(cursor)를 `board["backfill"]` 에 남겨 서버를 껐다 켜도 이어서 채운다.
  · 이미 과거 기록을 가진 보드(예: 2년치가 들어 있는 보드)는 `should_skip()` 이 걸러
    중복 백필을 하지 않는다.

이 파일은 서버(server.py)가 라이브러리로만 쓴다. 단독 실행은 하지 않는다.
"""
from __future__ import annotations

import datetime as dt
import re
import time
from typing import Any

import sync_mail as sm

ARCHIVE_IN_ID = "a-%d"        # 받은편지함 과거 기록
ARCHIVE_OUT_ID = "as-%d"      # 보낸편지함 과거 기록 ('as' 는 max_mf_no 정규식 [msa]- 와 겹치지 않는다)
PREVIEW_CAP = 600             # 저장할 본문(미리보기) 최대 길이
SUMMARY_CAP = 240             # 한 줄 요약 최대 길이
BOXES = ("inbox", "sent")

_CATEGORY_HINTS = (("출하리스트", "출하/보급"), ("납입요청", "납입요청"), ("미상차", "현황/보고"),
                   ("재고", "재고/실사"), ("실사", "재고/실사"), ("ptr", "PTR"), ("eop", "승인/검토"),
                   ("용기", "승인/검토"), ("승인", "승인/검토"), ("결품", "결품/보고"),
                   ("불량", "불량/품질"), ("품질", "불량/품질"), ("일정", "일정/공유"),
                   ("공지", "공지/안전"), ("안전", "공지/안전"), ("요청", "자료요청"))


def _stamp(now: dt.datetime | None = None) -> str:
    return (now or sm.kst_now()).strftime("%Y-%m-%d %H:%M")


def _clean(text: str, cap: int | None = None) -> str:
    s = re.sub(r"\s+", " ", str(text or "").replace("\xa0", " ")).strip()
    return s[:cap] if cap else s


# ────────────────────────────────────────────────────────────── 보드 상태
def item_mf(it: dict) -> tuple[str, int] | None:
    """항목 → (편지함, mf_no). id 규칙(m-/s-/a-/as-)과 mailLink 둘 다 본다."""
    ident = str(it.get("id") or "")
    m = re.fullmatch(r"(as|a|s|m)-(\d+)", ident)
    if m:
        box = "sent" if m.group(1) in ("as", "s") else "inbox"
        return box, int(m.group(2))
    link = str(it.get("mailLink") or "")
    mf = re.search(r"mf_no=(\d+)", link)
    if mf:
        box = re.search(r"mb_id=(inbox|sent)", link)
        return (box.group(1) if box else "inbox"), int(mf.group(1))
    return None


def existing_keys(board: dict) -> set[tuple[str, int]]:
    out: set[tuple[str, int]] = set()
    for it in board.get("items") or []:
        got = item_mf(it)
        if got:
            out.add(got)
    return out


def archived_items(board: dict) -> list[dict]:
    return [it for it in (board.get("items") or []) if it.get("archived")]


def archive_count(board: dict) -> int:
    return sum(1 for it in (board.get("items") or []) if it.get("archived"))


def _seed_cutoff(now: dt.datetime, seed_days: int) -> str:
    return (now - dt.timedelta(days=max(1, int(seed_days)))).strftime("%Y-%m-%d")


def should_skip(board: dict, *, seed_days: int = 14, min_existing: int = 500,
                now: dt.datetime | None = None) -> tuple[bool, str]:
    """이 보드는 과거 백필이 필요한가 → (건너뛸지, 이유).

    이유: "done"(이미 끝남) · "existing"(처음부터 과거 기록이 충분한 보드) · ""(진행해야 함)
    """
    st = board.get("backfill")
    if isinstance(st, dict) and st.get("done"):
        return True, "done"
    if isinstance(st, dict) and st.get("startedAt"):
        return False, ""                      # 이미 진행 중 — 커서대로 이어서
    now = now or sm.kst_now()
    cutoff = _seed_cutoff(now, seed_days)
    items = board.get("items") or []
    old = [it for it in items if str(it.get("receivedAt") or "")[:10] and
           str(it.get("receivedAt") or "")[:10] < cutoff]
    if len(items) >= min_existing and len(old) >= min_existing:
        return True, "existing"               # 2년치가 이미 들어 있는 보드 등
    return False, ""


def mark_done(board: dict, *, reason: str = "", now: dt.datetime | None = None) -> None:
    st = _state(board)
    st["done"] = True
    st["doneAt"] = st.get("doneAt") or _stamp(now)
    if reason:
        st["reason"] = reason


def _state(board: dict) -> dict:
    st = board.get("backfill")
    if not isinstance(st, dict):
        st = {}
        board["backfill"] = st
    boxes = st.get("boxes")
    if not isinstance(boxes, dict):
        boxes = {}
        st["boxes"] = boxes
    for box in BOXES:
        rec = boxes.get(box)
        if not isinstance(rec, dict):
            rec = {}
            boxes[box] = rec
        rec.setdefault("page", 1)
        rec.setdefault("done", False)
        rec.setdefault("added", 0)
        rec.setdefault("misses", 0)
    st.setdefault("added", 0)
    return st


def progress(board: dict) -> dict:
    """화면·상태 API 에 내려줄 백필 요약(작게)."""
    st = board.get("backfill") if isinstance(board.get("backfill"), dict) else {}
    return {
        "done": bool(st.get("done")),
        "added": int(st.get("added") or 0),
        "archived": archive_count(board),
        "rangeFrom": str(board.get("rangeFrom") or ""),
        "updatedAt": str(st.get("updatedAt") or st.get("doneAt") or ""),
        "reason": str(st.get("reason") or ""),
    }


# ────────────────────────────────────────────────────────────── 항목 만들기
def clean_preview(text: str, cap: int | None = None) -> str:
    """목록 미리보기 정리 — 공백 정리 + 서명 구분선/머리표 제거."""
    body = sm.strip_quoted(str(text or ""))
    body = re.sub(r"[－￣_=~-]{6,}", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    return body[:cap] if cap else body


def guess_category(title: str, preview: str = "") -> str:
    blob = (str(title or "") + " " + str(preview or "")).lower()
    for key, cat in _CATEGORY_HINTS:
        if key in blob:
            return cat
    return "기타"


def build_item(row: dict, *, box: str, contacts: dict[str, dict] | None = None,
               now: dt.datetime | None = None) -> dict:
    """목록 한 행 → 보드 항목(archived). 본문은 목록 미리보기를 쓴다."""
    contacts = contacts or {}
    mf = int(row["mf_no"])
    title = _clean(row.get("subject"))
    preview = clean_preview(row.get("preview") or "", PREVIEW_CAP)
    sender_raw = str(row.get("sender_raw") or "")
    email = str(row.get("sender_email") or "").lower()
    try:
        label = sm.sender_label(sender_raw, email, contacts)
        name, rank, _org = sm._sender_parts(sender_raw, email, contacts)
    except Exception:                                        # noqa: BLE001 - 형식이 달라도 계속
        label, name, rank = sender_raw, "", ""
    sender_name = f"{name} {rank}".strip() if rank and rank not in name else name
    link = row.get("mailLink") or (sm.MAIL_LINK % (box, mf))
    item: dict[str, Any] = {
        "id": (ARCHIVE_OUT_ID if box == "sent" else ARCHIVE_IN_ID) % mf,
        "title": title,
        "sender": label or sender_raw,
        "senderEmail": email,
        "receivedAt": row.get("receivedAt") or "",
        "deadline": "",
        "urgency": "normal",
        "category": guess_category(title, preview),
        "recipientRole": "CC",              # 목록에는 To/CC 구분이 없다 — 단정하지 않는다
        "mentionedUser": False,
        "dueDate": None,
        "replyStatus": "no_reply_needed",   # 과거 기록은 미회신 추적 대상이 아니다
        "summary": preview[:SUMMARY_CAP],
        "actionItems": [],
        "attachmentsUnverified": False,
        "replyDraft": None,
        "mailLink": link,
        "to": [],
        "cc": [],
        "thread": [{"id": (ARCHIVE_OUT_ID if box == "sent" else ARCHIVE_IN_ID) % mf,
                    "date": row.get("receivedAt") or "",
                    "from": label or sender_raw,
                    "gist": preview[:80],
                    "link": link}],
        "senderName": sender_name,
        "priority": 3,
        "priorityReason": "",
        "mailbox": box,
        "archived": True,
        "backfilledAt": _stamp(now),
    }
    if preview:
        item["bodyText"] = preview
    if row.get("attach"):
        item["hasAttachment"] = True
    try:
        sm.classify(item)                                    # VIP·지목 판정은 규칙을 그대로 따른다
    except Exception:                                        # noqa: BLE001
        pass
    reason = _clean(item.get("priorityReason"), 120)
    item["priorityReason"] = (reason + " · 과거 기록") if reason else "⚪ 과거 기록(색인)"
    return item


def _refresh_meta(board: dict) -> None:
    """개수·기간 메타를 실제 항목에서 다시 센다(화면·상태 API 용)."""
    items = board.get("items") or []
    archived = sum(1 for it in items if it.get("archived"))
    board["archivedCount"] = archived
    board["archiveIndexCount"] = archived
    oldest = ""
    for it in items:
        day = str(it.get("receivedAt") or "")[:10]
        if day and (not oldest or day < oldest):
            oldest = day
    if oldest:
        board["rangeFrom"] = oldest


# ────────────────────────────────────────────────────────────── 한 배치
def run_batch(board: dict, nm: Any, *, limit: int = 100, page_size: int = 100,
              max_pages: int = 30, s_date: int = 0, contacts: dict[str, dict] | None = None,
              now: dt.datetime | None = None, pause: float = 0.3) -> dict:
    """과거 메일을 한 배치 채워 board 를 직접 고친다(저장은 호출하는 쪽).

    nm: 로그인해 둔 sync_mail.Nmail (list_page 만 쓴다 — 편지읽기는 열지 않는다)
    반환: {"added", "scanned", "pages", "done", "warnings", ...}
    """
    now = now or sm.kst_now()
    st = _state(board)
    report: dict[str, Any] = {"added": 0, "scanned": 0, "pages": 0,
                              "done": bool(st.get("done")), "warnings": [],
                              "s_date": s_date}
    if limit <= 0:
        return report
    if not st.get("startedAt"):
        st["startedAt"] = _stamp(now)
    items = board.setdefault("items", [])
    known = existing_keys(board)
    pages_left = max(1, int(max_pages))

    for box in BOXES:
        box_st = st["boxes"][box]
        while (not box_st.get("done")) and report["added"] < limit and pages_left > 0:
            page = max(1, int(box_st.get("page") or 1))
            rows = nm.list_page(box, page=page, page_size=page_size, s_date=s_date)
            pages_left -= 1
            report["pages"] += 1
            if not rows and page == 1 and int(box_st.get("misses") or 0) < 1 and s_date == 0:
                # 웹메일이 '0=전체' 를 받아 주지 않는 경우가 있다 — 넓은 기간으로 한 번만 다시 읽어 본다
                rows = nm.list_page(box, page=1, page_size=page_size, s_date=3650)
                if rows:
                    report["warnings"].append(f"{box}: 전체기간(s_date=0) 목록이 비어 3650일로 다시 읽었습니다")
            if not rows:
                if page == 1 and int(box_st.get("misses") or 0) < 1:
                    # 첫 페이지가 비었다고 '끝'으로 단정하지 않는다(세션·s_date 문제일 수 있다)
                    box_st["misses"] = int(box_st.get("misses") or 0) + 1
                    report["warnings"].append(
                        f"{box}: 목록 1페이지가 비어 있습니다(s_date={s_date}) — 다음 실행에서 다시 확인")
                    break
                box_st["done"] = True
                break
            box_st["misses"] = 0
            full_page = True
            for row in rows:
                report["scanned"] += 1
                try:
                    mf = int(row["mf_no"])
                except (KeyError, TypeError, ValueError):
                    continue
                key = (box, mf)
                if key in known:
                    continue
                if report["added"] >= limit:
                    full_page = False
                    break
                items.append(build_item(row, box=box, contacts=contacts, now=now))
                known.add(key)
                report["added"] += 1
                box_st["added"] = int(box_st.get("added") or 0) + 1
            if full_page:
                if len(rows) < page_size:
                    box_st["done"] = True
                else:
                    box_st["page"] = page + 1
                    if pause:
                        time.sleep(pause)
            else:
                break          # 배치 한도에 걸림 — 같은 페이지에서 다음 실행이 이어 간다
        if report["added"] >= limit:
            break

    st["added"] = int(st.get("added") or 0) + report["added"]
    st["updatedAt"] = _stamp(now)
    if all(st["boxes"][b].get("done") for b in BOXES):
        st["done"] = True
        st["doneAt"] = _stamp(now)
    report["done"] = bool(st.get("done"))
    if report["added"]:
        items.sort(key=lambda it: (str(it.get("lastAt") or it.get("receivedAt") or ""), str(it.get("id"))),
                   reverse=True)
        _refresh_meta(board)
    return report
