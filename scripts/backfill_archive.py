#!/usr/bin/env python3
"""과거 메일 본문·요약 점진 보강(백필) — 전체 기간을 최신순으로 거슬러 올라가며 채운다.

`sync_mail.py` 가 '새 메일'을 처리한다면, 이 스크립트는 이미 저장된 **과거 항목** 중
`bodyText` 가 비어 있는 건을 웹메일에서 다시 읽어 채운다. 대시보드의 '과거 유사 사례'가
제목만이 아니라 실제 본문 근거로 연결되도록 하는 것이 목적이다.

사용하는 두 가지 수집 방식
 - `--source preview`(기본): 목록 페이지의 본문 미리보기(data-preview)를 쓴다.
   한 페이지에 100건이 들어 있어 9,100건도 약 90번의 요청이면 되고, **편지읽기 페이지를 열지 않으므로
   메일이 '읽음'으로 바뀌지 않는다**(사용자 받은편지함 상태를 건드리지 않음).
 - `--source read`: 편지읽기 페이지에서 전문을 가져온다(정확하지만 메일이 읽음 처리될 수 있음).
   미리보기가 너무 짧은 건만 `--fallback-read` 로 골라 읽을 수도 있다.

지키는 선
 - 2024-07 이후 전체(약 9,100건) 대상, **최신 날짜부터 역순**.
 - 1회 실행당 기본 100건(`--limit`) — 스케줄로 반복하면 비어 있는 항목이 없어질 때까지 이어진다.
 - 본문은 인용 꼬리·회사 서명을 걷어내고 **최대 1,100자**(`--chars`)로 저장.
 - Gemini 는 1~2줄 `summary` 만 갱신(`--no-summary` 면 호출 0회).
 - 암호문 용량 상한(기본 5.0MB)에 닿으면 멈추고 사유를 보고한다.

사용
  MAILBOARD_PIN=**** python3 scripts/backfill_archive.py --limit 100
  MAILBOARD_PIN=**** python3 scripts/backfill_archive.py --limit 300 --chars 1200
  MAILBOARD_PIN=**** python3 scripts/backfill_archive.py --dry-run
  MAILBOARD_PIN=**** python3 scripts/backfill_archive.py --limit 100 --source read   # 전문으로
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sync_mail as sm  # noqa: E402

# 주요 건 가중치 (제목·요약·카테고리) — 정렬은 날짜 우선, 같을 때만 점수
KEYWORDS = (("GM", 3), ("PTR", 3), ("EOP", 3), ("실사", 3), ("용기", 3), ("결품", 3),
            ("승인", 2), ("불량", 2), ("안전", 2), ("납입", 1), ("출하", 1), ("재고", 1))

# 서명·법인 정보 꼬리 (본문 뒤쪽에서 잘라낸다)
SIGNATURE_PATTERNS = ("***********************************************************", "천일정기화물자동차 (주)",
                      "천일정기화물자동차(주)", "SCM사업본부 자동차물류사업부", "주소 : 인천광역시")

WINDOWS = (30, 90, 182, 365, 730, 1095, 1825, 0)   # 0 = 전체 (웹메일 's_date' 선택값)


def score_item(it: dict) -> int:
    blob = " ".join(str(it.get(k) or "") for k in ("title", "summary", "category", "deadline"))
    score = sum(w for k, w in KEYWORDS if k in blob)
    if (it.get("senderEmail") or "").lower().endswith("@gm.com"):
        score += 2
    if it.get("priority") == 1:
        score += 3
    elif it.get("priority") == 2:
        score += 1
    return score


def target_of(it: dict) -> tuple[int, str] | None:
    """항목에서 (mf_no, 편지함)을 꺼낸다. 색인 항목(a-…)은 mailLink 가 유일한 근거다."""
    link = str(it.get("mailLink") or "")
    m = re.search(r"mf_no=(\d+)", link)
    box_m = re.search(r"mb_id=(inbox|sent)", link)
    if m:
        return int(m.group(1)), (box_m.group(1) if box_m else "inbox")
    m = re.fullmatch(r"([ms])-(\d+)", str(it.get("id") or ""))
    if m:
        return int(m.group(2)), ("sent" if m.group(1) == "s" else "inbox")
    return None


def needs_body(it: dict) -> bool:
    return len(str(it.get("bodyText") or "").strip()) < 40


def select_candidates(items: list[dict], *, limit: int, months: int = 0,
                      keywords_only: bool = False, keywords_first: bool = False) -> tuple[list[dict], int]:
    """(이번에 채울 대상, 남은 후보 수). 기본은 전체 기간·최신순."""
    cutoff = ""
    if months > 0:
        cutoff = (sm.kst_now().replace(tzinfo=None) - dt.timedelta(days=months * 30)).strftime("%Y-%m-%d")
    rows = []
    for it in items:
        if not needs_body(it):
            continue
        tgt = target_of(it)
        if tgt is None:
            continue
        mf, _box = tgt
        when = str(it.get("receivedAt") or "")
        if cutoff and when[:10] < cutoff:
            continue
        score = score_item(it)
        if keywords_only and score <= 0:
            continue
        rows.append({"item": it, "mf": mf, "when": when, "score": score})
    if keywords_first:
        rows.sort(key=lambda r: (r["score"], r["when"], r["mf"]), reverse=True)
    else:
        rows.sort(key=lambda r: (r["when"], r["mf"]), reverse=True)
    return [r["item"] for r in rows[:limit]], len(rows) - min(len(rows), limit)


def clean_body(text: str, cap: int) -> str:
    """인용 꼬리·서명을 걷어내고 cap 자로 자른다."""
    body = sm.strip_quoted(text or "")
    cut = len(body)
    for pat in SIGNATURE_PATTERNS:
        idx = body.rfind(pat)
        if idx > len(body) * 0.5 and idx < cut:      # 뒤쪽 절반에서만 (본문 중간의 우연한 일치 보호)
            cut = idx
    body = body[:cut]
    body = re.sub(r"[－￣_=~-]{6,}", "", body)          # 서명 구분선
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body[:cap]


def estimate_growth(text: str) -> int:
    """gzip 압축 후 대략 증가량(한국어 혼합 0.6배 보수 추정)."""
    return int(len(text.encode("utf-8")) * 0.6)


def window_days(items: list[dict]) -> int:
    """목록 요청의 '최근 N일' 값. 0 = 전체기간.

    9,100건을 최신순으로 끝까지 훑어야 하므로 2025년 이전까지 닿지 않는
    좁은 창(6개월 등)은 쓰지 않는다 — 가장 오래된 대상이 창 밖이면 0(전체).
    """
    oldest = min((str(it.get("receivedAt") or "")[:10] for it in items), default="")
    try:
        days = (sm.kst_now().date() - dt.date.fromisoformat(oldest)).days + 7
    except (TypeError, ValueError):
        return 0
    for step in WINDOWS:
        if step == 0 or days <= step:
            return step
    return 0


def start_page(items: list[dict], box: str) -> int:
    """이미 본문이 찬 항목 수로 '훑기 시작할 페이지'를 추정한다(1페이지=100건, 여유 2페이지).
    앞선 실행이 최신순으로 채워 두므로, 매번 1페이지부터 훑지 않아도 된다."""
    have = 0
    for it in items:
        tgt = target_of(it)
        if tgt and tgt[1] == box and not needs_body(it):
            have += 1
    return max(1, have // 100 - 2)


def run(args: argparse.Namespace) -> dict:
    pin = os.environ.get("MAILBOARD_PIN", "").strip()
    if not pin:
        raise SystemExit("MAILBOARD_PIN 환경변수가 필요합니다.")
    enc_size = os.path.getsize(args.data_enc)
    with open(args.data_enc, encoding="utf-8") as f:
        enc = json.load(f)
    data = sm.decrypt_payload(enc, pin)
    items = data.get("items") or []
    targets, remaining_after = select_candidates(
        items, limit=args.limit, months=args.months,
        keywords_only=args.keywords_only, keywords_first=args.keywords_first)
    report: dict = {"candidates": len(targets), "remaining_after": remaining_after,
                    "filled": 0, "with_summary": 0, "skipped": 0, "gemini_calls": 0,
                    "warnings": [], "enc_size_before": enc_size, "enc_size_after": None,
                    "stopped": None, "changed": False, "source": args.source,
                    "mode": "gemini" if (args.gemini_key and not args.no_summary) else "body-only"}

    if args.dry_run:
        report["plan"] = [{"id": it["id"], "receivedAt": it.get("receivedAt"), "score": score_item(it),
                           "title": (it.get("title") or "")[:60]} for it in targets]
        return report
    if not targets:
        report["stopped"] = "채울 항목 없음(모두 본문 보유)"
        return report

    nm = sm.Nmail(args.chunil_id, args.chunil_pw, ip_check=args.ip_check, debug_dir=args.dump_dir)
    nm.login()
    gem = sm.Gemini(args.gemini_key, args.gemini_model) if (args.gemini_key and not args.no_summary) else None

    cap_bytes = int(float(args.max_enc_mb) * 1024 * 1024)
    budget = cap_bytes - enc_size

    def apply_body(it: dict, body: str, meta: dict) -> bool:
        nonlocal budget
        it["bodyText"] = body
        it["backfilledAt"] = sm.kst_now().strftime("%Y-%m-%d %H:%M")
        report["filled"] += 1
        budget -= estimate_growth(body)
        if gem is not None:
            try:
                report["gemini_calls"] += 1
                analysis = gem.analyze(meta, kind="과거 기록", brief=True)
                if analysis.get("summary"):
                    it["summary"] = str(analysis["summary"])[:600]
                    report["with_summary"] += 1
            except Exception as e:  # noqa: BLE001
                report["warnings"].append(f"{it['id']} 요약 실패: {type(e).__name__}: {str(e)[:160]}")
        return True

    filled: list[dict] = []

    if args.source == "preview":
        by_box: dict[str, dict[int, dict]] = {"inbox": {}, "sent": {}}
        for it in targets:
            mf, box = target_of(it)
            by_box.setdefault(box, {})[mf] = it
        for box, wanted in by_box.items():
            if not wanted or report["filled"] >= args.limit:
                continue
            s_date = window_days(list(wanted.values()))
            first = start_page(items, box)
            report.setdefault("start_page", {})[box] = first
            for page in range(first, first + args.max_pages):
                if report["filled"] >= args.limit or budget <= 0:
                    break
                rows = nm.list_page(box, page=page, page_size=100, s_date=s_date)
                if not rows:
                    break
                for row in rows:
                    if report["filled"] >= args.limit or budget <= 0:
                        break
                    it = wanted.get(row["mf_no"])
                    if it is None:
                        continue
                    body = clean_body(row.get("preview") or "", args.chars)
                    if len(body) < 20:
                        if args.fallback_read:
                            try:
                                detail = nm.read_mail(box, row["mf_no"])
                                body = clean_body(detail.get("bodyText") or "", args.chars)
                            except Exception as e:  # noqa: BLE001
                                report["warnings"].append(f"{it['id']} 본문 읽기 실패: {type(e).__name__}")
                                report["skipped"] += 1
                                continue
                        if len(body) < 20:
                            report["skipped"] += 1
                            continue
                    meta = {"subject": row.get("subject") or it.get("title", ""),
                            "date": row.get("receivedAt") or it.get("receivedAt", ""),
                            "from_raw": row.get("sender_raw", ""), "to": [], "cc": [],
                            "bodyText": body}
                    apply_body(it, body, meta)
                    filled.append(it)
    else:
        for it in targets:
            if report["filled"] >= args.limit or budget <= 0:
                break
            mf, box = target_of(it)
            try:
                detail = nm.read_mail(box, mf)
            except Exception as e:  # noqa: BLE001
                report["warnings"].append(f"{it['id']} 본문 읽기 실패: {type(e).__name__}")
                report["skipped"] += 1
                continue
            body = clean_body(detail.get("bodyText") or "", args.chars)
            if len(body) < 20:
                report["skipped"] += 1
                continue
            apply_body(it, body, {**detail, "subject": detail.get("subject") or it.get("title", "")})
            filled.append(it)

    if budget <= 0:
        report["stopped"] = f"암호문 용량 상한({args.max_enc_mb}MB) 도달 — 상한을 올리거나 --chars 를 줄이세요"
    elif report["filled"] < len(targets):
        report["stopped"] = report["stopped"] or (
            f"목록 페이지 한도({args.max_pages})에 닿음 — --max-pages 를 올리거나 --source read 로 실행하세요")

    report["filled_ids"] = [it["id"] for it in filled]
    total_candidates = remaining_after + len(targets)          # 이번 실행 전 비어 있던 전체
    report["remaining_after"] = max(total_candidates - report["filled"], 0)
    if not filled:
        return report

    data["updatedAt"] = sm.kst_now().strftime("%Y-%m-%d %H:%M") + " (KST)"
    out = sm.encrypt_payload(data, pin, compress=sm.page_supports_gzip(args.data_enc))
    path = args.out or args.data_enc
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)
    report["changed"] = True
    report["written"] = path
    report["enc_size_after"] = os.path.getsize(path)
    if report["enc_size_after"] > cap_bytes:
        report["warnings"].append(
            f"암호문이 상한을 넘었습니다: {report['enc_size_after'] / 1048576:.2f}MB > {args.max_enc_mb}MB")
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="과거 메일 본문·요약 점진 보강(전체 기간·최신순)")
    ap.add_argument("--data-enc", default="data.enc.json")
    ap.add_argument("--out", default=None, help="출력 경로(기본: --data-enc 덮어쓰기)")
    ap.add_argument("--limit", type=int, default=100, help="이번 실행에서 채울 최대 건수(기본 100)")
    ap.add_argument("--months", type=int, default=0, help="0=전체 기간(기본). N>0 이면 최근 N개월만")
    ap.add_argument("--chars", type=int, default=1100, help="저장할 본문 최대 길이(기본 1,100자)")
    ap.add_argument("--max-enc-mb", default="5.0", help="암호문 용량 상한 MB(기본 5.0)")
    ap.add_argument("--source", choices=("preview", "read"), default="preview",
                    help="preview=목록 미리보기(읽음 처리 없음, 기본) / read=편지읽기 전문")
    ap.add_argument("--fallback-read", action="store_true",
                    help="미리보기가 너무 짧은 건만 편지읽기로 보완(읽음 처리될 수 있음)")
    ap.add_argument("--max-pages", type=int, default=120, help="목록을 최대 몇 페이지까지 훑을지(기본 120=12,000건)")
    ap.add_argument("--keywords-only", action="store_true", help="GM·PTR·EOP·실사·용기 등 주요 건만")
    ap.add_argument("--keywords-first", action="store_true", help="날짜보다 주요 키워드 우선으로 정렬")
    ap.add_argument("--no-summary", action="store_true", help="Gemini 요약 없이 본문만(호출 0회)")
    ap.add_argument("--dry-run", action="store_true", help="대상 목록만 확인(웹메일 접속 안 함)")
    ap.add_argument("--dump-dir", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    args.chunil_id = os.environ.get("CHUNIL_ID", "").strip()
    args.chunil_pw = os.environ.get("CHUNIL_PW", "").strip()
    args.gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    args.gemini_model = (os.environ.get("GEMINI_MODEL") or sm.DEFAULT_MODEL).strip()
    args.ip_check = os.environ.get("NMAIL_IP_CHECK", "1").strip()
    if not os.environ.get("MAILBOARD_PIN", "").strip():
        print("MAILBOARD_PIN 환경변수가 필요합니다.", file=sys.stderr)
        return 2
    if not args.dry_run and not (args.chunil_id and args.chunil_pw):
        print("CHUNIL_ID / CHUNIL_PW 환경변수가 필요합니다.", file=sys.stderr)
        return 2
    try:
        report = run(args)
    except sm.NmailError as e:
        print(f"웹메일 오류: {e}", file=sys.stderr)
        return 3
    except (ValueError, OSError) as e:
        print(f"실행 오류: {e}", file=sys.stderr)
        return 4

    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"bf_changed={'true' if report.get('changed') else 'false'}\n")
            f.write(f"bf_filled={report.get('filled', 0)}\n")
            f.write(f"bf_remaining={report.get('remaining_after', 0)}\n")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"[백필] 이번 대상 {report['candidates']}건 · 채움 {report['filled']}건 · 요약 {report['with_summary']}건 · "
              f"Gemini {report['gemini_calls']}회 · 건너뜀 {report['skipped']}건 · 남은 후보 {report['remaining_after']}건 "
              f"· 수집={report['source']} · 모드={report['mode']}")
        if report.get("stopped"):
            print("  · 중단: " + report["stopped"])
        for w in report["warnings"][:10]:
            print("  ! " + w)
        if report.get("enc_size_after"):
            print(f"  → {report['written']} ({report['enc_size_before'] / 1048576:.2f}MB → "
                  f"{report['enc_size_after'] / 1048576:.2f}MB)")
        if args.dry_run:
            for p in report.get("plan", [])[:15]:
                print(f"  · {p['id']} {p['receivedAt']} (점수 {p['score']}) {p['title']}")
            if len(report.get("plan", [])) > 15:
                print(f"  · … 외 {len(report['plan']) - 15}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
