#!/usr/bin/env python3
"""sync_mail.py 오프라인 종단 테스트 — 네트워크·Gemini 없이 파이프라인 전체를 검증한다.

두 가지를 확인한다.

 A. **첫 실행(빈 보드)** — data.enc.json 이 없어도
    ① 로그인 사용자를 웹메일 화면에서 읽어 data.owner 에 넣는가
       (개인정보 페이지가 막혀 있으면 상단 프로필 이름으로 넘어가는가)
    ② 최근 N일 메일을 자동으로 모아 항목을 만드는가
    ③ 분류 규칙(VIP · 본문 지목 · To · 출하리스트 · 고객사 참조)이 설정대로 붙는가
 B. **두 번째 실행(증분)** — 새 메일만 추가되고, 회신 반영·스레드 병합이 되는가,
    PIN 이 틀리면 기존 암호문을 **덮어쓰지 않고** 멈추는가.

사용
  MAILBOARD_PIN=**** python3 scripts/test_sync_offline.py
  MAILBOARD_PIN=**** python3 scripts/test_sync_offline.py --keep   # 임시 폴더를 남긴다
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import sync_mail as sm  # noqa: E402

REAL_NMAIL = sm.Nmail      # 테스트에서 sm.Nmail 을 가짜로 바꾸므로 실제 파서를 따로 붙잡아 둔다

ME_NAME, ME_TITLE, ME_DEPT, ME_ADDR = "이수민", "과장", "도입2팀", "kimtest@chunil.co.kr"
VIP1 = {"name": "박상무", "email": "sangmu@chunil.co.kr", "label": "1-1 최우선", "rank": 1}
VIP2 = {"name": "정부장", "email": "jungbj@chunil.co.kr", "label": "1-2 주요", "rank": 2, "toOnly": True}


# ─────────────────────────────────────────────── 가짜 웹메일 화면
def read_page(*, date: str, frm: str, to: list[str], cc: list[str], subject: str,
              body: str, attach: list[tuple[str, str]] | None = None) -> str:
    """웹메일 편지읽기 페이지와 같은 구조의 HTML(실제 마크업 기준)."""
    def people(xs):
        return ", ".join(f'<a href="/mail_write.php?mf_to=x" title="{x.replace(chr(34), "&quot;")}">'
                         f'{x}</a>' for x in xs)
    att_html = ""
    if attach:
        items = "".join(
            f'<li><a href="/mail_attach_down.php?mf_no=1" class="mf_attach_name_link">'
            f'<span class="glyphicon glyphicon-paperclip"></span> {n} '
            f'<span class="mf_attach_name_size">{s}</span></a></li>' for n, s in attach)
        att_html = (f'<tr><th><b>첨부파일</b></th><td class="mf_attach_names_wrap">'
                    f'<ul class="list-unstyled list-inline">{items}</ul></td></tr>')
    return f"""<html><body>
<table class="table table-bordered"><tbody>
<tr><th width="120"><b>보낸날짜</b></th><td>{date}</td></tr>
<tr><th><b>보낸사람</b></th><td><a href="/x" title="{frm.replace(chr(34), "&quot;")}">{frm}</a></td></tr>
<tr><th><b>받는사람</b></th><td>{people(to)}</td></tr>
<tr><th><b>참조</b></th><td>{people(cc)}</td></tr>
<tr><th><b>제 목</b></th><td><b>{subject}</b></td></tr>
{att_html}
<tr><td colspan="2"><table><tr><td class="main_table_body2 mail-body-view" id="mf_body_view">{body}</td></tr></table></td></tr>
</tbody></table></body></html>"""


PROFILE_HTML = f"""<html><body><form name="fmember">
<table class="table">
<tr><th>이름</th><td><input type="text" name="m_name" value="{ME_NAME}"></td></tr>
<tr><th>부서</th><td><input type="text" name="m_dept" value="{ME_DEPT}"></td></tr>
<tr><th>직급</th><td><select name="m_jikwi"><option>사원</option><option selected>{ME_TITLE}</option></select></td></tr>
<tr><th>메일</th><td>{ME_ADDR}</td></tr>
<tr><th>비밀번호</th><td><input type="password" name="m_pwd" value=""></td></tr>
</table></form></body></html>"""

# 개인정보 페이지가 막힌 경우의 마지막 보험 — 상단 프로필 줄(실제 마크업)
HEADER_HTML = """<html><body><nav><ul class="nav navbar-nav navbar-right">
<li class="dropdown login-bi">
<a class="dropdown-toggle" href="#" data-toggle="dropdown"><span class="glyphicon glyphicon-user" aria-hidden="true"></span>
<!--b>납입1팀</b-->
<font color=#376BBF><b>이수민</b></font>
<b class="caret"></b>&nbsp;&nbsp;&nbsp;&nbsp;</a>
</li></ul></nav></body></html>"""


class FakeNmail:
    """웹메일 서버 대신 정해진 응답을 돌려준다."""

    def __init__(self, inbox: list[dict], sent: list[dict], pages: dict[int, str],
                 *, profile: str | None = PROFILE_HTML, header: str = HEADER_HTML):
        self.inbox, self.sent, self.pages = inbox, sent, pages
        self.profile, self.header = profile, header
        self.reads: list[tuple[str, int]] = []
        self.seeded: list[tuple[str, int, int]] = []

    def login(self):
        return None

    def profile_page(self):
        return self.profile or ""

    def header_identity(self):
        if not self.header:
            return {"name": "", "dept": ""}
        return REAL_NMAIL.header_identity.__get__(self)()  # 실제 파서를 그대로 쓴다

    # header_identity 가 self._get / self._dump 을 쓰므로 최소 흉내를 낸다
    def _dump(self, name, text):
        return None

    def _get(self, url, **kw):
        html = self.header

        class R:
            text = html
            url = ""

        return R()

    def list_mailbox(self, box, *, pages=3, page_size=100, stop_below=0):
        rows = self.inbox if box == "inbox" else self.sent
        rows = sorted(rows, key=lambda r: r["mf_no"], reverse=True)
        return [r for r in rows if not stop_below or r["mf_no"] > stop_below]

    def seed_mailbox(self, box, *, days=14, limit=30):
        self.seeded.append((box, days, limit))
        rows = sorted(self.inbox if box == "inbox" else self.sent,
                      key=lambda r: r["mf_no"], reverse=True)
        return rows[:limit]

    def read_mail(self, box, mf_no):
        self.reads.append((box, mf_no))
        html = self.pages[mf_no]
        d = sm.parse_read(html)
        d.update({"mf_no": mf_no, "box": box, "mailLink": sm.MAIL_LINK % (box, mf_no), "images": []})
        return d

    def fetch_bytes(self, url, cap=400_000):
        return None


class FakeGemini:
    def __init__(self, *a, **kw):
        self.calls = 0

    def analyze(self, mail, *, kind="받은편지함", brief=False):
        self.calls += 1
        to_me = sm._is_to(mail)
        title = mail.get("subject", "")
        return {
            "summary": f"[테스트] {title[:40]} / 이슈→진행→남은 일 요약",
            "actionItems": ["테스트 할 일 1"] if to_me else [],
            "dueDate": "2026-12-04" if "출하리스트" not in title else None,
            "deadline": "12/4(금)까지",
            "replyStatus": "needs_reply" if to_me else "no_reply_needed",
            "category": "출하/보급" if "출하리스트" in title else "승인/검토",
            "urgency": "normal",
            "threadGist": "테스트 요지",
            "tags": ["테스트"],
            "replyDrafts": {"polite": "정중", "factual": "사실", "concise": "간결"} if to_me else None,
        }


def row(mf_no: int, subject: str, sender: str, when: str, box="inbox") -> dict:
    return {"mf_no": mf_no, "box": box, "sender_raw": sender, "sender_email": sm._addr_email(sender),
            "subject": subject, "receivedAt": when, "size": "12.3K", "unread": True, "attach": False,
            "important": False, "mailLink": sm.MAIL_LINK % (box, mf_no), "listMfMismatch": False}


def args_for(data_enc: str) -> argparse.Namespace:
    return argparse.Namespace(data_enc=data_enc, out=None, dry_run=False, max_pages=2, max_new=40,
                              thread_merge=True, track_replies=True, images=False, dump_dir=None,
                              chunil_id="kimtest", chunil_pw="x", ip_check="1",
                              gemini_key="fake-key", gemini_model="gemini-2.5-flash",
                              seed_days=14, seed_max=30, board_config=None)


def run(pin: str, keep: bool) -> int:
    tmp = tempfile.mkdtemp(prefix="mailboard-test-")
    work = os.path.join(tmp, "data.enc.json")
    shutil.copy2(os.path.join(ROOT, "index.html"), os.path.join(tmp, "index.html"))
    os.environ["MAILBOARD_PIN"] = pin
    fails: list[str] = []

    def check(name, cond, detail=""):
        print(("  ✔ " if cond else "  ✘ ") + name + (f" — {detail}" if detail else ""))
        if not cond:
            fails.append(name)

    # 설정(VIP 두 명) — 실제 배포에서는 board_config.json
    sm.configure({"name": ME_NAME, "title": ME_TITLE, "dept": ME_DEPT, "email": ME_ADDR},
                 {"vips": [VIP1, VIP2], "to_is_urgent": True})

    # ── A. 첫 실행 ────────────────────────────────────────────────────────
    print("\n[A] 빈 보드에서 첫 실행")
    base = 5500000
    rows = [
        row(base + 1, "생산실적 보고 요청", '"박상무" <sangmu@chunil.co.kr>', "2026-11-23 08:10"),
        row(base + 2, "공정이관 검토 요청", '"정부장" <jungbj@chunil.co.kr>', "2026-11-23 08:20"),
        row(base + 3, "생산 1공장 11월 23일 6차 출하리스트 입니다.",
            '"조승환" <jsh3117@chunil.co.kr>', "2026-11-23 08:25"),
        row(base + 4, "입고 일정 확인 부탁", f'"{ME_NAME} 과장" <{ME_ADDR}>', "2026-11-23 08:30"),
        row(base + 5, "PTR 협조 요청", '"정성우" <sungwoo.jung@gm.com>', "2026-11-23 08:35"),
        row(base + 6, "승인 검토 건", '"김동료" <dongryo@chunil.co.kr>', "2026-11-23 08:40"),
    ]
    pages = {
        base + 1: read_page(date="2026-11-23 08:10", frm='"박상무" <sangmu@chunil.co.kr>',
                            to=[f"{ME_NAME} <{ME_ADDR}>"], cc=[], subject="생산실적 보고 요청",
                            body="<div>정리 부탁드립니다.</div>", attach=[("실적.xlsx", "31.11K")]),
        base + 2: read_page(date="2026-11-23 08:20", frm='"정부장" <jungbj@chunil.co.kr>',
                            to=[f"{ME_NAME} <{ME_ADDR}>"], cc=["김기경 <sh624@chunil.co.kr>"],
                            subject="공정이관 검토 요청", body="<div>검토 후 의견 주세요.</div>"),
        base + 3: read_page(date="2026-11-23 08:25", frm='"조승환" <jsh3117@chunil.co.kr>',
                            to=["강한 <kanghan@chunil.co.kr>"], cc=[f"{ME_NAME} <{ME_ADDR}>"],
                            subject="생산 1공장 11월 23일 6차 출하리스트 입니다.",
                            body="<div>하기 첨부 참조.</div>", attach=[("출하.xlsx", "142.44K")]),
        base + 4: read_page(date="2026-11-23 08:30", frm='"김동료" <dongryo@chunil.co.kr>',
                            to=[f"{ME_NAME} <{ME_ADDR}>"], cc=[],
                            subject="입고 일정 확인 부탁",
                            body=f"<div>{ME_NAME} 과장님, {ME_TITLE}님 확인 부탁드립니다.</div>"),
        base + 5: read_page(date="2026-11-23 08:35", frm='"정성우" <sungwoo.jung@gm.com>',
                            to=["강한 <kanghan@chunil.co.kr>"],
                            cc=[f"{ME_NAME} <{ME_ADDR}>", "김기경 <sh624@chunil.co.kr>"],
                            subject="PTR 협조 요청", body="<div>참고 부탁드립니다.</div>"),
        base + 6: read_page(date="2026-11-23 08:40", frm='"김동료" <dongryo@chunil.co.kr>',
                            to=[f"{ME_NAME} <{ME_ADDR}>"], cc=[], subject="승인 검토 건",
                            body="<div>검토 부탁드립니다.</div>"),
    }
    nm = FakeNmail(rows, [], pages)
    sm.Nmail = lambda *a, **kw: nm
    sm.Gemini = FakeGemini
    rep = sm.sync(args_for(work))
    check("첫 실행을 최초 구축으로 판단", rep.get("init") is True and rep.get("mode") == "init", str(rep.get("mode")))
    check("data.enc.json 생성", os.path.exists(work), work)
    enc = json.load(open(work, encoding="utf-8"))
    data = sm.decrypt_payload(enc, pin)
    check("복호화 가능 (PIN 일치)", bool(data.get("items")), f"{len(data.get('items', []))}건")
    ow = data.get("owner") or {}
    check("owner 이름·직급·부서·메일 인식",
          (ow.get("name"), ow.get("title"), ow.get("dept"), ow.get("email")) == (ME_NAME, ME_TITLE, ME_DEPT, ME_ADDR),
          json.dumps(ow, ensure_ascii=False))
    check("계정 주소도 owner 메일로", data.get("account") == ME_ADDR, str(data.get("account")))
    check("rangeFrom 기록", str(data.get("rangeFrom", "")).count("-") == 2, str(data.get("rangeFrom")))
    items = {it["id"]: it for it in data.get("items", [])}
    check("최근 N일 메일 6건 등록", len(items) == 6, f"{len(items)}건")
    check("seed_mailbox 사용(최근 14일·상한 30)", nm.seeded[:1] == [("inbox", 14, 30)], str(nm.seeded))

    a = items.get(f"m-{base + 1}")
    check("VIP 1-1 → 🔴 최상위", bool(a) and a["priority"] == 1 and a.get("vipRank") == 1,
          f"priority={a and a['priority']} label={a and a.get('vipLabel')}")
    if a:
        check("첨부 메타·답장 초안", a.get("attachments") == [{"name": "실적.xlsx", "size": "31.11 KB"}]
              and "polite" in (a.get("replyDrafts") or {}), str(a.get("attachments")))
    b = items.get(f"m-{base + 2}")
    check("VIP 1-2 + To → 🔴", bool(b) and b["priority"] == 1 and b.get("vipLabel") == "1-2 주요",
          f"priority={b and b['priority']}")
    c = items.get(f"m-{base + 3}")
    check("출하리스트는 To 가 아니어도 ⚪", bool(c) and c["priority"] == 3, f"priority={c and c['priority']}")
    d = items.get(f"m-{base + 4}")
    check("내 이름 직접 지목 → 🔴 1-3",
          bool(d) and d["priority"] == 1 and d.get("vipLabel") == "1-3 지목"
          and ME_NAME in str(d.get("priorityReason", "")), f"reason={d and d.get('priorityReason')}")
    e = items.get(f"m-{base + 5}")
    check("고객사 참조(CC) → 🟡", bool(e) and e["priority"] == 2, f"priority={e and e['priority']}")
    f = items.get(f"m-{base + 6}")
    check("나를 To 로 지정 → 🔴 (기본 설정)", bool(f) and f["priority"] == 1, f"priority={f and f['priority']}")

    # ── B. 두 번째 실행 ──────────────────────────────────────────────────
    print("\n[B] 두 번째 실행(증분) · 회신 반영")
    n7, s1 = base + 7, base + 8
    pages[base + 2]  # 기존 페이지 유지
    pages[n7] = read_page(date="2026-11-23 11:05", frm='"정부장" <jungbj@chunil.co.kr>',
                          to=[f"{ME_NAME} <{ME_ADDR}>"], cc=[], subject="RE: 공정이관 검토 요청",
                          body="<div>추가 검토 부탁드립니다.</div>")
    pages[s1] = read_page(date="2026-11-23 11:30", frm=f'"{ME_NAME}" <{ME_ADDR}>',
                          to=["정부장 <jungbj@chunil.co.kr>"], cc=[],
                          subject="RE: 공정이관 검토 요청", body="<div>의견 드립니다.</div>")
    nm2 = FakeNmail([row(n7, "RE: 공정이관 검토 요청", '"정부장" <jungbj@chunil.co.kr>', "2026-11-23 11:05")],
                    [row(s1, "RE: 공정이관 검토 요청", f'"{ME_NAME}" <{ME_ADDR}>', "2026-11-23 11:30", box="sent")],
                    pages)
    sm.Nmail = lambda *a, **kw: nm2
    rep2 = sm.sync(args_for(work))
    data2 = sm.decrypt_payload(json.load(open(work, encoding="utf-8")), pin)
    items2 = {it["id"]: it for it in data2["items"]}
    check("증분 실행은 최초 구축이 아님", rep2.get("init") is False, str(rep2.get("init")))
    check("같은 제목은 스레드로 병합", f"m-{n7}" not in items2 and len(items2) == 6, f"{len(items2)}건")
    tgt = items2.get(f"m-{base + 2}")
    check("보낸편지함 대조 → 회신 완료", bool(tgt) and tgt["replyStatus"] == "replied",
          str(tgt and tgt.get("replyStatus")))
    check("스레드에 발신 추가", bool(tgt) and any(str(t.get("id", "")).startswith("s-") for t in tgt.get("thread", [])),
          str(tgt and [t.get("id") for t in tgt.get("thread", [])]))
    check("updatedAt 갱신", "(KST)" in str(data2.get("updatedAt")), str(data2.get("updatedAt")))
    check("owner 가 매 실행에서도 유지", (data2.get("owner") or {}).get("email") == ME_ADDR,
          json.dumps(data2.get("owner"), ensure_ascii=False))

    # ── C. 개인정보 페이지가 막힌 경우 ───────────────────────────────────
    print("\n[C] 개인정보 페이지가 막혀도 상단 프로필로 인식")
    nm3 = FakeNmail([], [], {}, profile=None)
    check("상단 프로필에서 이름 인식", sm.detect_owner(nm3, "kimtest").get("name") == ME_NAME,
          json.dumps(sm.detect_owner(nm3, "kimtest"), ensure_ascii=False))

    # ── D. PIN 이 틀리면 덮어쓰지 않는다 ────────────────────────────────
    print("\n[D] 잘못된 PIN 보호")
    before = open(work, encoding="utf-8").read()
    try:
        sm.load_or_init(work, "9999")
        ok = False
    except SystemExit:
        ok = True
    check("PIN 불일치 → 중단(SystemExit)", ok)
    check("기존 암호문 그대로", open(work, encoding="utf-8").read() == before)

    # ── E. items: [] 인 빈 파일도 첫 실행으로 ────────────────────────────
    print("\n[E] 빈 보드(items: []) 감지")
    empty = sm.encrypt_payload(sm.new_payload(seed_days=7), pin)
    empty_path = os.path.join(tmp, "empty.enc.json")
    json.dump(empty, open(empty_path, "w", encoding="utf-8"))
    _, is_new = sm.load_or_init(empty_path, pin, seed_days=7)
    check("items 가 비면 최초 구축으로 판단", is_new is True)

    print("\n결과:", "모두 통과" if not fails else f"실패 {len(fails)}건 — {fails}")
    if keep:
        print("임시 폴더:", tmp)
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if not fails else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    pin = os.environ.get("MAILBOARD_PIN") or "2026"
    print(f"· 테스트 PIN={pin}")
    sys.exit(run(pin, a.keep))
