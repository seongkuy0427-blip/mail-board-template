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
import struct
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
                 *, profile: str | None = PROFILE_HTML, header: str = HEADER_HTML,
                 book: "FakeAddressBook | None" = None):
        self.inbox, self.sent, self.pages = inbox, sent, pages
        self.profile, self.header = profile, header
        self.book = book
        self.reads: list[tuple[str, int]] = []
        self.seeded: list[tuple[str, int, int]] = []

    # ── 사내 주소록 — 실제 Nmail.fetch_contacts 가 쓰는 메소드만 진짜 파서로 시원한다 ──
    fetch_contacts = REAL_NMAIL.fetch_contacts      # 수집 루틴(그룹 순회·페이지 처리)은 실제 코드 그대로
    def contact_group_tree(self):
        if self.book is None:
            raise sm.NmailError("주소록 화면이 로그인 페이지로 돌아갔습니다 (테스트: 주소록 없음)")
        if self.book.fail:
            raise sm.NmailError("주소록 그룹 트리를 읽지 못했습니다 (테스트: 실패 주입)")
        self.book.tree_calls += 1
        groups = sm.parse_contact_tree(self.book.tree_html)
        if not groups:
            raise sm.NmailError("주소록 그룹 트리를 읽지 못했습니다")
        return groups

    def contact_rows(self, path, param, no, *, kind, max_pages=20, pause=0.25):
        assert self.book is not None
        pages = (self.book.org_pages if kind == "org" else self.book.personal_pages).get(str(no))
        if pages is None:
            raise sm.NmailError(f"테스트: 없는 그룹 {param}={no}")
        self.book.row_calls.append((kind, str(no)))
        rows: list[dict] = []
        total = 0
        for page in range(1, max_pages + 1):
            html = pages[page - 1] if page - 1 < len(pages) else ""
            if not html:
                break
            if page == 1:
                total = sm.contact_total(html)
            batch = sm.parse_org_contacts(html) if kind == "org" else sm.parse_personal_contacts(html)
            if not batch:
                break
            rows.extend(batch)
            if not total or len(rows) >= total:
                break
        return rows

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


# ─────────────────────────────────────────────── 가짜 사내 주소록 화면(실제 Nmail 마크업)
#  주소록 그룹 트리 — 실제 address_group.php 의 JS(TreeNode)와 같은 모양. 조직도 뒤에 개인주소록이
#  이어지면서 변수 이름(newNode, newNode_1 …)을 다시 쓰는 것까지 그대로 흉내낸다.
TREE_HTML = """<script language="JavaScript" type="text/javascript">
//<![CDATA[
	objTreeMenu_1 = new TreeMenu("/addon/HTML_TreeMenu/img", "objTreeMenu_1", "_self", "treeMenuDefault", true, false);
	 newNode = objTreeMenu_1.addItem(new TreeNode('조직도', 'folder_root.gif', null, true, true, 'treeMenuBold', '', 'folder_root.gif'));
	 newNode.setEvent('onClick', 'parent.addr_list_update(\\'G\\', \\'0\\', \\'\\'); treeMenuClick(this, true);');
	 newNode_1 = newNode.addItem(new TreeNode('자동차물류사업부', 'folder.gif', null, false, true, '', '', 'folder-expanded.gif'));
	 newNode_1.setEvent('onClick', 'parent.addr_list_update(\\'G\\', \\'5\\', \\'\\'); treeMenuClick(this, false);');
	 newNode_1_1 = newNode_1.addItem(new TreeNode('납입1팀', 'folder.gif', null, false, true, '', '', 'folder-expanded.gif'));
	 newNode_1_1.setEvent('onClick', 'parent.addr_list_update(\\'G\\', \\'177\\', \\'\\'); treeMenuClick(this, false);');
	 newNode_1_2 = newNode_1.addItem(new TreeNode('한화큐셀팀', 'folder.gif', null, false, true, '', '', 'folder-expanded.gif'));
	 newNode_1_2.setEvent('onClick', 'parent.addr_list_update(\\'G\\', \\'23\\', \\'\\'); treeMenuClick(this, false);');
	 newNode = objTreeMenu_1.addItem(new TreeNode('개인주소록', 'folder_root.gif', null, true, true, 'treeMenuBold', '', 'folder_root.gif'));
	 newNode.setEvent('onClick', 'parent.addr_list_update(\\'P\\', \\'0\\', \\'\\'); treeMenuClick(this, true);');
	 newNode_1 = newNode.addItem(new TreeNode('기본', 'folder.gif', null, false, true, '', '', 'folder-expanded.gif'));
	 newNode_1.setEvent('onClick', 'parent.addr_list_update(\\'P\\', \\'337\\', \\'\\'); treeMenuClick(this, false);');
	 newNode_2 = objTreeMenu_1.addItem(new TreeNode('공용주소록', 'folder_root.gif', null, true, true, 'treeMenuBold', '', 'folder_root.gif'));
	 newNode_2.setEvent('onClick', 'parent.addr_list_update(\\'S\\', \\'0\\', \\'\\'); treeMenuClick(this, true);');
//]]>
</script>"""


def org_row(n: int, name: str, email: str, title: str = "") -> str:
    """member_list.php 의 실제 행 마크업."""
    return f'''<tr id="tr{n}" class="main_table_body_Y{n % 2}" height="28">
\t\t<td align="center" nowrap=""><input type="checkbox" name="chk_m_no[]" value="{n}" onclick="JavaScript:list_click(document.all.tr{n}, 'main_table_body_click_Y', 'main_table_body_Y{n % 2}');"></td>
\t\t\t\t<td nowrap=""><a href="JavaScript:window_open('/member_info.php?m_no={n}', '', 750, 550);">{name}</a></td>
\t\t\t\t<td nowrap="">{title}</td>
\t\t\t\t<td nowrap=""><a href="/mail_write.php?mf_to=x" target="_parent">{email}</a></td>
\t\t\t</tr>'''


def personal_row(n: int, group: str, name: str, email: str) -> str:
    """addr_list.php 의 실제 행 마크업(그룹·이름·이메일·휴대폰·회사/학교·수정)."""
    return f'''<tr id="tr{n}" class="main_table_body_Y{n % 2}" height="28">
\t\t<td align="center" nowrap=""><input type="checkbox" name="chk_ad_no[]" value="337|{n}" onclick="JavaScript:list_click(document.all.tr{n}, 'main_table_body_click_Y', 'main_table_body_Y{n % 2}');"></td>
\t\t<td align="center" nowrap="">{group}</td>
\t\t<td nowrap=""><a href="/addr_mod.php?s_adg_no=337&amp;is_iframe=1&amp;adg_no=337&amp;ad_no={n}">{name}</a></td>
\t\t<td nowrap=""><a href="/mail_write.php?mf_to=x" target="_parent">{email}</a></td>
\t\t<td nowrap=""></td>
\t\t<td nowrap=""></td>
\t\t<td align="center" nowrap=""><a class="btn btn-default btn-xs" href="/addr_mod.php?s_adg_no=337&amp;is_iframe=1&amp;adg_no=337&amp;ad_no={n}">수정</a></td>
\t</tr>'''


def contact_page(rows: list[str], total: int, *, page: int = 1, pages: int = 1, base: str = "member_list.php",
                 param: str = "s_mg_no=177") -> str:
    """목록 화면 한 페이지 — `※ 전체 N명` 과 페이저가 함께 있는 실제 구조."""
    link = lambda p: (f'/../{base}?{param}&amp;is_iframe=1&amp;s_key2=ASC&amp;pageID={p}')
    nums = "".join(f'<font color="#999999">|</font><a href="{link(p)}" class="nm_page_no">{p}</a>'
                   for p in range(1, pages + 1) if p != page)
    return f'''<html><body><form name="list" method="get">
<table width="100%" cellspacing="0" cellpadding="0"><tbody><tr>
\t<td>&nbsp;※ 전체 {total}명</td><td align="right"><input type="text" name="s_word"></td>
</tr></tbody></table>
<div class="nm_pager"><b><span class="nm_page_no_curr">{page}</span></b>{nums}</div>
<table width="100%" cellspacing="0" cellpadding="3" class="table table-condensed">
\t<thead><tr class="main_table_head2"><th>&nbsp;</th><th>이름</th><th>직위</th><th>이메일</th></tr></thead>
\t<tbody>{''.join(rows)}</tbody></table>
</body></html>'''


NAPIP = [("강한", "kanghan@chunil.co.kr", ""), ("김기경", "sh624@chunil.co.kr", ""),
         ("김성규", "kimtest@chunil.co.kr", ""), ("김세유", "seyou@chunil.co.kr", ""),
         ("김수현 과장보", "ksh364@chunil.co.kr", ""), ("최수철", "csc7797@chunil.co.kr", "")]
QCELL_NAMES = [(f"큐셀직원{i:02d}", f"qcell{i:02d}@chunil.co.kr", "") for i in range(1, 22)]  # 21명 → 2페이지


class FakeAddressBook:
    """주소록 응답 묶음 — fetch_contacts 가 몇 번 호출됐는지도 센다."""

    def __init__(self, tree_html: str = TREE_HTML, *, fail: bool = False,
                 org_pages: dict | None = None, personal_pages: dict | None = None):
        self.tree_html = tree_html
        self.fail = fail
        self.tree_calls = 0
        self.row_calls: list[tuple[str, str]] = []
        if org_pages is None:
            org_pages = {
                "5": [contact_page([org_row(i + 1, *n) for i, n in enumerate(NAPIP[:2])], 2)],
                "23": [contact_page([org_row(i + 1, *n) for i, n in enumerate(QCELL_NAMES[:20])], 21,
                                    page=1, pages=2, param="s_mg_no=23"),
                       contact_page([org_row(21, *QCELL_NAMES[20])], 21, page=2, pages=2, param="s_mg_no=23")],
                "177": [contact_page([org_row(i + 1, *n) for i, n in enumerate(NAPIP)], len(NAPIP))],
            }
        self.org_pages = org_pages
        if personal_pages is None:
            personal_pages = {"337": [contact_page(
                [personal_row(365, "기본", "Sungwoo Jung", "sungwoo.jung@gm.com"),
                 personal_row(4611, "기본", "강지민", "jimin.kang@chunil.co.kr"),
                 personal_row(4612, "기본", "최수철", "csc7797@chunil.co.kr")], 3,
                base="addr_list.php", param="s_adg_no=337")]}
        self.personal_pages = personal_pages


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
                              gemini_key="fake-key", gemini_model=sm.DEFAULT_MODEL,
                              seed_days=14, seed_max=30, board_config=None, refresh_contacts=False)


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
    #  (저장소 루트의 board_config.json 이 vips: [] 라 테스트 설정을 지우지 않도록 파일 읽기를 막는다)
    sm.load_board_config = lambda *a, **kw: {}
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
    nm = FakeNmail(rows, [], pages, book=FakeAddressBook())
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
    book_saved = data.get("contacts") or []
    check("첫 실행에서 사내 주소록도 받아 저장",
          bool(book_saved) and any(c.get("dept") == "납입1팀" for c in book_saved),
          f"{len(book_saved)}명 예: {json.dumps((book_saved[:2] or [{}]), ensure_ascii=False)[:120]}")

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

    # ── F. 사내 주소록 파서 ──────────────────────────────────────────────
    print("\n[F] 사내 주소록(조직도·개인주소록) 파서")
    groups = sm.parse_contact_tree(TREE_HTML)
    triples = [(g["kind"], g["no"], g["name"], g["depth"]) for g in groups]
    check("그룹 트리: 조직도·개인주소록 구분",
          ("org", "0", "조직도", 0) in triples and ("personal", "337", "기본", 1) in triples,
          str(triples))
    check("그룹 깊이(부서 계층)와 변수 이름 재사용 처리",
          ("org", "177", "납입1팀", 2) in triples and ("org", "5", "자동차물류사업부", 1) in triples,
          str(triples))
    check("`※ 전체 N명` 파싱", sm.contact_total(contact_page([], 392)) == 392,
          str(sm.contact_total(contact_page([], 392))))
    org_only = sm.parse_org_contacts(contact_page([org_row(i + 1, *n) for i, n in enumerate(NAPIP)], len(NAPIP)))
    check("조직도 행 파싱(이름·이메일·직위 칸)",
          len(org_only) == len(NAPIP) and org_only[5]["email"] == "csc7797@chunil.co.kr"
          and org_only[4]["name"] == "김수현 과장보", json.dumps(org_only[:2] + org_only[-1:], ensure_ascii=False))
    per_only = sm.parse_personal_contacts(contact_page(
        [personal_row(365, "기본", "Sungwoo Jung", "sungwoo.jung@gm.com"),
         personal_row(4611, "기본", "강지민", "jimin.kang@chunil.co.kr")], 2,
        base="addr_list.php", param="s_adg_no=337"))
    check("개인주소록 행 파싱(그룹·이름·메일)",
          len(per_only) == 2 and per_only[0]["email"] == "sungwoo.jung@gm.com"
          and per_only[0]["group"] == "기본", json.dumps(per_only, ensure_ascii=False))
    dedup = sm.merge_contacts(
        [{"name": "최수철", "email": "csc7797@chunil.co.kr", "dept": "납입1팀", "kind": "org", "title": ""}],
        [{"name": "최수철", "email": "CSC7797@chunil.co.kr", "dept": "기본", "kind": "personal", "title": ""}])
    check("같은 메일은 한 줄(조직도 우선)",
          len(dedup) == 1 and dedup[0]["kind"] == "org" and dedup[0]["dept"] == "납입1팀",
          json.dumps(dedup, ensure_ascii=False))

    # ── G. owner 보완 — 사내 주소록 + @chunil.co.kr 우선 ────────────────
    print("\n[G] owner 보완 (외부 복구 메일 → 사내 메일 · 부서/직급)")
    hanmail_profile = """<html><body><form name="fmember"><table class="table">
<tr><th>이름</th><td><input type="text" name="m_name" value="최수철"></td></tr>
<tr><th>부서</th><td><input type="text" name="m_dept" value=""></td></tr>
<tr><th>직급</th><td><select name="m_jikwi"><option selected></option></select></td></tr>
<tr><th>메일</th><td>x@hanmail.net</td></tr>
</table></form></body></html>"""
    nm_outer = FakeNmail([], [], {}, profile=hanmail_profile)
    have_book = [{"name": "최수철", "email": "csc7797@chunil.co.kr", "dept": "납입1팀",
                  "kind": "org", "title": ""}]
    ow_ext = sm.detect_owner(nm_outer, "csc7797", contacts=have_book)
    check("외부 복구 메일 대신 사내 메일 저장",
          ow_ext["email"] == "csc7797@chunil.co.kr", json.dumps(ow_ext, ensure_ascii=False))
    check("주소록에서 부서·이름 보완",
          ow_ext["dept"] == "납입1팀" and ow_ext["name"] == "최수철", json.dumps(ow_ext, ensure_ascii=False))
    ow_name = sm.detect_owner(nm_outer, "csc7797", contacts=[
        {"name": "최수철", "email": "choi.s@chunil.co.kr", "dept": "납입1팀", "kind": "org", "title": "부장"}])
    check("주소록에 본인 메일이 없으면 이름으로 보완",
          ow_name["dept"] == "납입1팀" and ow_name["email"] == "csc7797@chunil.co.kr",
          json.dumps(ow_name, ensure_ascii=False))

    # ── H. 주소록 수집 흐름 (비었을 때 · 있을 때 · --refresh-contacts · 실패) ──
    print("\n[H] 주소록 수집 흐름")
    book_path = os.path.join(tmp, "book.enc.json")
    seed_area = sm.decrypt_payload(json.load(open(work, encoding="utf-8")), pin)
    seed_area["contacts"] = []
    json.dump(sm.encrypt_payload(seed_area, pin), open(book_path, "w", encoding="utf-8"))
    ab1 = FakeAddressBook()
    sm.Nmail = lambda *a, **kw: FakeNmail([], [], {}, book=ab1)
    rep_h1 = sm.sync(args_for(book_path))
    saved = sm.decrypt_payload(json.load(open(book_path, encoding="utf-8")), pin).get("contacts") or []
    check("주소록이 비어 있으면 자동 수집", ab1.tree_calls == 1 and len(saved) > 0, f"{len(saved)}명")
    check("메일 신규가 없어도 주소록 때문에 저장(changed)", rep_h1.get("changed") is True, str(rep_h1.get("changed")))
    check("21명 그룹은 2페이지까지 읽음", ("org", "23") in ab1.row_calls and
          sum(1 for c in saved if c.get("dept") == "한화큐셀팀") == 21,
          f"row_calls={ab1.row_calls} 큐셀={sum(1 for c in saved if c.get('dept') == '한화큐셀팀')}")
    check("조직도에 이미 있는 메일은 개인주소록에서 제외",
          sum(1 for c in saved if c.get("email") == "csc7797@chunil.co.kr") == 1,
          str([c for c in saved if c.get("email") == "csc7797@chunil.co.kr"]))
    ab2 = FakeAddressBook()
    sm.Nmail = lambda *a, **kw: FakeNmail([], [], {}, book=ab2)
    rep_h2 = sm.sync(args_for(book_path))
    check("주소록이 있으면 다시 받지 않음", ab2.tree_calls == 0 and rep_h2.get("changed") is False,
          f"tree_calls={ab2.tree_calls} changed={rep_h2.get('changed')}")
    ab3 = FakeAddressBook()
    sm.Nmail = lambda *a, **kw: FakeNmail([], [], {}, book=ab3)
    args3 = args_for(book_path)
    args3.refresh_contacts = True
    rep_h3 = sm.sync(args3)
    check("--refresh-contacts 면 새로 받음",
          ab3.tree_calls == 1 and rep_h3.get("changed") is True,
          f"tree_calls={ab3.tree_calls} changed={rep_h3.get('changed')}")
    # 수집 실패해도 메일 동기화는 계속된다
    n9 = base + 9
    pages[n9] = read_page(date="2026-11-23 12:10", frm=f'"{ME_NAME}" <{ME_ADDR}>',
                          to=[f"{ME_NAME} <{ME_ADDR}>"], cc=[], subject="주소록 실패 처리 확인",
                          body="<div>확인 부탁드립니다.</div>")
    seed_area["contacts"] = []
    json.dump(sm.encrypt_payload(seed_area, pin), open(book_path, "w", encoding="utf-8"))
    sm.Nmail = lambda *a, **kw: FakeNmail(
        [row(n9, "주소록 실패 처리 확인", f'"{ME_NAME}" <{ME_ADDR}>', "2026-11-23 12:10")],
        [], pages, book=FakeAddressBook(fail=True))
    rep_h4 = sm.sync(args_for(book_path))
    after = sm.decrypt_payload(json.load(open(book_path, encoding="utf-8")), pin)
    check("주소록 실패는 경고로만 남김",
          any("사내 주소록 수집 실패" in w for w in rep_h4.get("warnings") or []),
          str(rep_h4.get("warnings"))[:160])
    check("실패해도 메일은 정상 동기화",
          any(it["id"] == f"m-{n9}" for it in after.get("items") or []) and after.get("contacts") == [],
          f"items={len(after.get('items') or [])}")

    # 로고·슬로건·서명 제외 (v1.2.4) — 인라인 이미지와 본문 문구
    logo_html = read_page(date="2026-11-23 12:00", frm='"김수현" <suhyun@chunil.co.kr>',
                          to=["김성규 <kimpro@chunil.co.kr>"], cc=[], subject="로고 제외 확인",
                          body='<div>현장 사진 첨부드립니다.</div>'
                               '<div><img src="/include/download.php?name=image001.png&amp;key=1" width="400" alt=""></div>'
                               '<div id="signature"><img src="/include/download.php?name=photo.png" width="800" height="600" alt=""></div>'
                               '<div><img src="/include/download.php?name=slogan_banner.png" width="600" height="100" alt=""></div>'
                               '<div><img src="/include/download.php?name=work_table.png" width="1500" height="273" alt=""></div>')
    imgs = [x["src"].split("name=")[1].split("&")[0] for x in sm.parse_read(logo_html)["images"]]
    check("로고·슬로건·서명 이미지 제외", imgs == ["image001.png", "work_table.png"], str(imgs))
    sign_body = ("하기 11/24(화) 07:00 기준 EOP자재 재고실사 결과와 재고 수량 변동 내역을 송부드립니다.\n"
                 "첨부 자료 확인 부탁드립니다.\n\n"
                 "천일정기화물자동차 (주)\nSCM사업본부 자동차물류사업부 납입1팀 과장 김성규\n"
                 "주소 : 인천 부평구 부평대로 233 · TEL 032-330-2233 · Fax 032-330-3443\n"
                 "본 메일은 지정된 수신인에게만 수신이 허용됩니다.")
    cleaned = sm.clean_body(sign_body)
    check("본문 서명·면책 문구 제거",
          "재고실사 결과" in cleaned and not any(k in cleaned for k in ("TEL", "Fax", "지정된 수신인", "천일정기화물자동차 (주)")),
          repr(cleaned[-80:]))

    class _ImgSource:
        def fetch_bytes(self, url, *, cap=400_000):
            return b"\x89PNG\r\n\x1a\n" + (b"0" * (6_000 if "logo" in url else 90_000))

    got_imgs = sm.collect_images(_ImgSource(), [{"src": "/logo.png"}, {"src": "/photo.png"}], {})
    check("12KB 미만 이미지 제외",
          sm.IMG_MIN_BYTES == 12_000 and len(got_imgs) == 1
          and got_imgs[0]["dataUrl"].startswith("data:image/png;base64,"),
          f"min={sm.IMG_MIN_BYTES} kept={len(got_imgs)}")


    def _png(w, h, pad=9_000):
        return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
                + struct.pack(">II", w, h) + b"\x08\x02\x00\x00\x00" + b"\x00\x00\x00\x00" + b"0" * pad)

    class _BannerSource:
        def __init__(self, blobs):
            self.blobs = blobs

        def fetch_bytes(self, url, *, cap=400_000):
            return self.blobs.get(url)

    banner, photo = _png(500, 80), _png(900, 674, pad=90_000)      # 500x80 = 'Hi CHUNIL, High green'
    kept = sm.collect_images(_BannerSource({"b1": banner, "b2": banner, "b3": banner,
                                            "p1": photo, "p2": photo}),
                             [{"src": s} for s in ("b1", "b2", "b3", "p1", "p2")], {})
    check("슬로건 배너·중복 이미지 제외", len(kept) == 1, f"kept={len(kept)}")
    greet = sm.heuristic_analysis({"subject": "통합배차", "bodyText": (
        "안녕하십니까 ?\n천일정기화물자동차㈜ 조달물류팀 최재훈 입니다 .\n"
        "수신자 제위\n2026년 11월 24일 통합배차 송부드립니다 .")}, False)
    check("휴리스틱 요약에서 인사말 제외",
          greet["summary"].startswith("2026년") and "안녕하십니까" not in greet["summary"],
          greet["summary"][:60])

    check("기본 Gemini 모델", sm.DEFAULT_MODEL == "gemini-3.8-flash", sm.DEFAULT_MODEL)
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
