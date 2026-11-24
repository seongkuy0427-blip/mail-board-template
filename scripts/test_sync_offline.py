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

    def analyze(self, mail, *, kind="받은편지함", brief=False, contacts=None, cache=None):
        self.calls += 1
        to_me = sm._is_to(mail)
        title = mail.get("subject", "")
        _dept = sm.our_dept_label()
        _tl = list((cache or {}).get("timeline") or [])
        _tl.append(f"[{sm.short_when(mail.get('date') or mail.get('receivedAt'))}] 테스트 발신자 → {_dept} : "
                   + (mail.get("subject") or "")[:40])
        return {
            "timeline": _tl,
            "mainDept": _dept,
            "deptBasis": "우리 부서가 수신(To)" if to_me else "참조(CC)",
            "ourDeptAction": "[직접 회신 필요] 확인 후 회신" if to_me else "[없음 (타 부서 주담당 · 단순 참조)]",
            "myAction": "[직접 회신 필요] 확인" if to_me else "[할 일 없음(확인 처리 권장)]",
            "headline": f"[테스트] {mail.get('subject', '')[:40]}",
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
                              seed_days=14, seed_max=30, board_config=None, refresh_contacts=False,
                              attach_refresh=0)


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
        rd = a.get("replyDrafts") or {}
        att_a = a.get("attachments") or []
        check("첨부 메타(이름·크기·주소)·답장 초안",
              [{"name": x.get("name"), "size": x.get("size")} for x in att_a] == [{"name": "실적.xlsx", "size": "31.11 KB"}]
              and att_a[0].get("url", "").startswith("https://mail.chunil.co.kr/mail_attach_down.php")
              and {"attach", "result", "forward"} <= set(rd) and rd.get("attach"),
              str(att_a))
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

    # v1.2.17 — 첨부 빈 칩(아이콘 전용 <a>) 제외 · 같은 파일 중복 제거 · 내려받기 주소 저장
    icon_li = ('<li><a href="/mail_attach_down.php?mf_no=5418517&amp;mf_an_struct=9.9" class="btn-dl">'
               '<i class="bi bi-download"></i></a></li>')
    dup_page = read_page(date="2026-11-24 13:05", frm='"김수현" <suhyun@chunil.co.kr>',
                         to=["김성규 <kimpro@chunil.co.kr>"], cc=[], subject="현장 사진 송부",
                         body="<div>사진 첨부드립니다.</div>",
                         attach=[("사진1.jpg", "543.14K"), ("사진2.jpg", "535.76K")])
    dup_page = dup_page.replace("</ul></td></tr>", icon_li + icon_li + "</ul></td></tr>")
    dup_atts = sm.parse_read(dup_page)["attachments"]
    check("빈 첨부 칩(아이콘 전용)은 첨부로 세지 않는다",
          len(dup_atts) == 2 and [x["name"] for x in dup_atts] == ["사진1.jpg", "사진2.jpg"], str(dup_atts))
    check("첨부 내려받기 주소(url) 저장",
          all(str(x.get("url", "")).startswith("https://mail.chunil.co.kr/mail_attach_down.php") for x in dup_atts),
          str(dup_atts))

    # 예전 수집분 첨부 다시 읽기 — 사진 첨부(또는 빈 이름)만 대상, 일반 첨부는 건드리지 않는다
    fixed_atts = [{"name": "사진1.jpg", "size": "543.14 KB",
                   "url": "https://mail.chunil.co.kr/mail_attach_down.php?mf_no=7001&mf_an_struct=1.2"}]
    old_items = [{"id": "m-7001", "receivedAt": "2026-11-24 09:00", "mailbox": "inbox",
                  "attachments": [{"name": "사진1.jpg", "size": "543.14 KB"}, {"name": "", "size": ""}]},
                 {"id": "m-7002", "receivedAt": "2026-11-23 09:00", "mailbox": "inbox",
                  "attachments": [{"name": "문서.pdf", "size": "12 KB"}]}]

    class _AttNm:
        def __init__(self):
            self.calls = []

        def read_mail(self, box, mf_no):
            self.calls.append((box, mf_no))
            return {"attachments": [dict(x) for x in fixed_atts]}

    nmx = _AttNm()
    fixed_n = sm.refresh_attachment_meta(nmx, old_items, limit=4)
    check("예전 첨부 다시 읽기 — 사진 첨부만 고친다",
          fixed_n == 1 and nmx.calls == [("inbox", 7001)]
          and old_items[0]["attachments"] == fixed_atts
          and old_items[1]["attachments"] == [{"name": "문서.pdf", "size": "12 KB"}],
          f"calls={nmx.calls} fixed={fixed_n}")
    check("예전 첨부 다시 읽기 — 남은 대상이 없으면 0건",
          sm.refresh_attachment_meta(nmx, old_items, limit=4) == 0 and nmx.calls == [("inbox", 7001)], str(nmx.calls))
    sign_body = ("하기 11/24(화) 07:00 기준 EOP자재 재고실사 결과와 재고 수량 변동 내역을 송부드립니다.\n"
                 "첨부 자료 확인 부탁드립니다.\n\n"
                 "— 서명 —\n천일정기화물자동차 (주)\nSCM사업본부 자동차물류사업부 납입1팀 과장 김성규\n"
                 "주소 : 인천 부평구 부평대로 233 · TEL 032-330-2233 · Fax 032-330-3443\n\n"
                 "Hi CHUNIL, High green\nThe Next 100\n"
                 "본 메일은 지정된 수신인에게만 수신이 허용됩니다.")
    cleaned = sm.clean_body(sign_body)
    # 2026-11-24 사용자 정정 — 직원 서명(이름·직급·연락처)은 보여야 하고, 회사 슬로건·기밀 면책만 뺀다
    check("본문 서명 유지·슬로건/면책 제거",
          "재고실사 결과" in cleaned
          and all(k in cleaned for k in ("— 서명 —", "천일정기화물자동차 (주)", "김성규", "TEL 032-330-2233", "Fax 032-330-3443"))
          and not any(k in cleaned for k in ("Hi CHUNIL", "High green", "The Next 100", "지정된 수신인")),
          repr(cleaned[-120:]))

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
          "2026년 11월 24일 통합배차" in greet["summary"] and "안녕하십니까" not in greet["summary"],
          greet["summary"][:60])
    # v1.2.7 — 📝 메일 분석은 본문 복붙이 아니라 "누가 → 누구에게 → 무엇을" 3인칭 요약
    eop_body = sm.el_text(sm.BeautifulSoup(
        "<div id='w'><div>피세영 대리님,</div><div>납입 담당자님</div><div>하기 품번 명일</div>"
        "<div>07:00</div><div>기준 재고실사 요청드립니다</div><div>(MY26 EOP</div>"
        "<div>자재로 입항지연 예상 )</div><div>42711726</div><div>HOUSING AS</div></div>",
        "html.parser").find(id="w"))
    check("본문 줄바꿈 정리(문장 이어 붙이기)",
          "하기 품번 명일 07:00 기준 재고실사 요청드립니다" in eop_body
          and "(MY26 EOP 자재로 입항지연 예상 )" in eop_body,
          eop_body.replace("\n", " / ")[:80])
    eop = sm.heuristic_analysis({
        "subject": "MY26 EOP자재 재고실사 요청_11/24(화) 07시", "bodyText": eop_body,
        "date": "2026-11-23 16:30", "from_raw": "염주희 <johhee.yeom@gm.com>",
        "from_email": "johhee.yeom@gm.com", "to": ["Seeyoung Pi (C) <seeyoung@chunil.co.kr>"]},
        False, {"seeyoung@chunil.co.kr": {"name": "피세영", "title": "대리", "dept": "납입1팀"},
                "johhee.yeom@gm.com": {"name": "염주희", "title": "차장", "dept": "GM"}})
    check("메일 분석 3단(진행 경과·주담당·결론) — 누가→누구에게→무엇을",
          eop["summary"].startswith("1) 🔄") and "2) 🎯" in eop["summary"] and "3) ✅" in eop["summary"]
          and "GM 염주희 차장" in eop["summary"] and "피세영 대리" in eop["summary"]
          and "11/24 07:00 기준 재고실사" in eop["summary"]
          and "피세영 대리님," not in eop["summary"],
          eop["summary"][:90])
    check("요약 프롬프트에 본문 복붙 금지",
          "그대로 옮기지 않는다" in sm.SYSTEM_PROMPT and "제3자 보고체" in sm.SYSTEM_PROMPT,
          sm.SYSTEM_PROMPT.count("제3자 보고체"))

    # v1.2.8 — 답장 문구는 말투 3종이 아니라 실무 상황 3안(자료 송부·조치 결과·담당자 전달)
    draft_item = {"id": "m-x", "title": "RE: 26년 11월 KPI & BPD 자료 회신 요청",
                  "senderName": "김태영 과장", "senderEmail": "tykim@chunil.co.kr",
                  "actionItems": ["납입관리부 3개 항목 정리"], "dueDate": "2026-12-03",
                  "attachments": [{"name": "KPI.xlsx", "size": "12KB"}]}
    d3 = sm.build_reply_drafts(draft_item)
    check("답장 3안 — 자료 송부·조치 결과·담당자 전달",
          set(d3) == {"attach", "result", "forward"}
          and "첨부 송부드립니다" in d3["attach"] and "KPI.xlsx" in d3["attach"]
          and "확인한 결과 공유드립니다" in d3["result"]
          and "재회신드리겠습니다" in d3["forward"] and "12/3(목)" in d3["forward"],
          d3["forward"].splitlines()[0][:50])
    ext3 = sm.build_reply_drafts(dict(draft_item, senderEmail="johhee.yeom@gm.com",
                                      senderName="염주희 차장"))["attach"]
    check("답장 말투 자동 — 외부는 격식체",
          ext3.startswith("염주희 차장님, 안녕하십니까.") and "입니다." in ext3 and "안녕하세요" not in ext3,
          ext3.splitlines()[0][:40])
    shot = ("김태영 과장 님, 안녕하세요.\n안녕하세요.\n천일정기화물자동차(주) 납입1팀 김성규입니다.\n\n"
            "2026년 11월 KPI 자료 회신 요청 확인했습니다.\n12/3(목)까지 회신드리겠습니다.\n\n감사합니다.\n\n김성규 드림")
    fixed = sm.normalize_reply_drafts({"polite": shot}, draft_item)
    check("답장 인사말 중복 제거 · '과장 님' → '과장님'",
          all(t.count("안녕하세요") == 1 and "과장 님" not in t for t in fixed.values())
          and fixed["attach"].startswith("김태영 과장님, 안녕하세요."),
          fixed["attach"].splitlines()[0][:40])
    legacy_items = [{"id": "m-y", "title": "PTR 막차 납입 일정 회신 요청", "replyStatus": "needs_reply",
                     "senderName": "이방형 부장", "senderEmail": "bhlee@gm.com",
                     "replyDrafts": {"polite": "확인했습니다. 회신드리겠습니다.",
                                     "factual": "확인했습니다. 회신드리겠습니다.",
                                     "concise": "확인했습니다. 회신드리겠습니다."}}]
    check("저장된 옛 답장 초안 자동 정리",
          sm.refresh_reply_drafts(legacy_items) == 1
          and set(legacy_items[0]["replyDrafts"]) == {"attach", "result", "forward"}
          and sm.refresh_reply_drafts(legacy_items) == 0
          and "확인했습니다. 회신드리겠습니다." not in legacy_items[0]["replyDrafts"]["attach"],
          legacy_items[0]["replyDrafts"]["forward"].splitlines()[0][:40])

    check("기본 Gemini 모델", sm.DEFAULT_MODEL == "gemini-3.8-flash", sm.DEFAULT_MODEL)

    # ─────────────────────────────────────────────────────────── v1.2.11
    #  ⚪ 단순 공유 Zero-Token · 전체 스레드 보존 · 서명 잡음 제거 · 3단 분석 · 스레드 증분 캐시
    if hasattr(sm, "configure"):
        sm.configure({"name": "김성규", "title": "과장", "dept": "납입1팀",
                      "email": "kimpro@chunil.co.kr", "company": "천일정기화물자동차"}, {})
    dept = sm.our_dept_label()
    contacts = {"kimpro@chunil.co.kr": {"name": "김성규", "title": "과장", "dept": "납입1팀"},
                "seeyoung@chunil.co.kr": {"name": "피세영", "title": "대리", "dept": "납입1팀"},
                "ryu@chunil.co.kr": {"name": "류광종", "title": "과장", "dept": "해외물류부"}}

    thread_body = ("김성규 과장님,\n\n하기 품번 재고실사 요청드립니다.\n42711726 HOUSING AS 11EA\n\n"
                   "—\nSCM사업본부 자동차물류사업부 납입1팀 과장 김성규\n"
                   "주소 : 인천 부평구 부평대로 233\nTEL 032-330-2233 · Fax 032-330-3443\n"
                   "Hi CHUNIL, High green\n본 메일은 지정된 수신인에게만 수신이 허용됩니다.\n\n"
                   "-----Original Message-----\n보낸 사람: 염주희 <johhee.yeom@gm.com>\n"
                   "보낸 날짜: 2026-11-20 09:12\n제목: MY26 EOP 재고실사\n\n"
                   "명일 07시 기준 재고파악 부탁드립니다.\n")
    thread_mail = {"mf_no": 990001, "subject": "MY26 EOP자재 재고실사 요청", "bodyText": thread_body,
                   "from_raw": "염주희 <johhee.yeom@gm.com>", "from_email": "johhee.yeom@gm.com",
                   "date": "2026-11-23 16:30", "to": ["피세영 <seeyoung@chunil.co.kr>"],
                   "cc": ["김성규 <kimpro@chunil.co.kr>"]}
    text = sm.analysis_body(thread_mail)
    check("이전 회신 스레드 미절단(최초→최신 보존)",
          "명일 07시 기준 재고파악" in text and "하기 품번 재고실사" in text,
          text.replace("\n", " / ")[:90])
    check("서명 부서·직급 1줄 보존 + 주소/TEL/슬로건/면책 제거",
          "납입1팀 과장 김성규" in text
          and not any(k in text for k in ("TEL 032-330-2233", "Hi CHUNIL", "지정된 수신인", "부평대로")),
          repr(text[-90:]))

    long_body = "\n\n".join(
        f"보낸 사람: 사람{i} <p{i}@chunil.co.kr>\n보낸 날짜: 2026-11-{10 + i % 10:02d} 09:00\n"
        f"{i}번째 회신 — 품번 1234{i} 확인 부탁드립니다." for i in range(12))
    squeezed = sm.analysis_body({"bodyText": long_body})
    check("10통 이상 스레드 — 최초 2 + 최근 4 원문 · 중간 1줄 압축",
          "중간 6건 — 1줄 요약" in squeezed and "… 9번째 회신" in squeezed
          and squeezed.count("보낸 사람:") == 6,
          f"원문 {squeezed.count('보낸 사람:')}통")

    gem_texts = []

    class _Gem:
        def analyze(self, mail, *, kind="", brief=False, contacts=None, cache=None):
            gem_texts.append(sm.analysis_input_text(mail, kind, contacts=contacts, cache=cache, brief=brief))
            tl = list((cache or {}).get("timeline") or [])
            tl.append(f"[{sm.short_when(mail.get('date'))}] 염주희 차장 → {dept} : 재고실사 요청")
            return {"timeline": tl, "mainDept": dept, "deptBasis": "우리 부서가 수신(To)",
                    "ourDeptAction": "[직접 회신 필요] 결과 회신", "myAction": "[직접 회신 필요] 확인",
                    "summary": "", "actionItems": [], "dueDate": None, "deadline": "별도 회신 기한 없음",
                    "replyStatus": "needs_reply", "category": "재고/실사", "urgency": "normal",
                    "threadGist": "재고실사", "tags": [], "replyDrafts": {}}

    share = {"mf_no": 990002, "subject": "생산 1공장 6차 출하리스트 입니다.", "bodyText": "하기 첨부 참조.",
             "from_raw": "조승환 <jsh3117@chunil.co.kr>", "from_email": "jsh3117@chunil.co.kr",
             "date": "2026-11-23 10:09", "to": ["강한 <kanghan@chunil.co.kr>"],
             "cc": ["김성규 <kimpro@chunil.co.kr>"]}
    res = sm.analyze_mail(_Gem(), share, "받은편지함", contacts, thread_cache={})
    check("⚪ 단순 공유 — AI 호출 0 · aiSkipped · 답장 초안 없음",
          res["aiSkipped"] and res["aiSkipReason"] == "simple_share"
          and res["summary"] == sm.SKIP_SIMPLE_SUMMARY and res["replyDrafts"] == {}
          and len(gem_texts) == 0,
          res["summary"])
    named = dict(share, bodyText="출하리스트 입니다. 김성규 과장님 확인 부탁드립니다.")
    res2 = sm.analyze_mail(_Gem(), named, "받은편지함", contacts, thread_cache={})
    check("⚪ 단순 공유라도 나를 지목하면 정상 AI 분석",
          not res2.get("aiSkipped") and len(gem_texts) == 1
          and sm.is_detailed_summary(res2["summary"]) and "3) ✅" in res2["summary"],
          res2["summary"].splitlines()[0])
    other = dict(thread_mail, subject="12월 선복 부킹 확정 공유", bodyText="선복 부킹 확정되었습니다.",
                 from_raw="류광종 <ryu@chunil.co.kr>", from_email="ryu@chunil.co.kr",
                 to=["해외물류부 <overseas@chunil.co.kr>"], cc=["김성규 <kimpro@chunil.co.kr>"])
    res3 = sm.analyze_mail(_Gem(), other, "받은편지함", contacts, thread_cache={})
    check("타 부서 참조(CC) — 경량 요약 · AI 호출 없음",
          res3["aiSkipped"] and res3["aiSkipReason"] == "other_dept"
          and res3["summary"].startswith(sm.LIGHT_TAG) and len(gem_texts) == 1
          and "없음" in res3["ourDeptAction"],
          res3["summary"][:70])

    store: dict = {}
    first = dict(thread_mail, mf_no=990003, subject="RE: 스레드 증분 확인",
                 bodyText="FIRSTBODY 첫 회신입니다. 11/24 07시 기준 재고실사 부탁드립니다.")
    sm.analyze_mail(_Gem(), first, "받은편지함", contacts, thread_cache=store)
    check("첫 분석은 전체 본문 전달",
          "[기존 누적 타임라인]" not in gem_texts[-1] and "FIRSTBODY" in gem_texts[-1])
    second = dict(first, mf_no=990004, date="2026-11-24 08:10",
                  bodyText="SECONDBODY 결과 11EA/5EA 확인했습니다.")
    res4 = sm.analyze_mail(_Gem(), second, "받은편지함", contacts, thread_cache=store)
    check("2통째는 누적 타임라인 + 새 메일 1통만 전달",
          "[기존 누적 타임라인]" in gem_texts[-1] and "[새로 추가된 메일 1통]" in gem_texts[-1]
          and "SECONDBODY" in gem_texts[-1] and "FIRSTBODY" not in gem_texts[-1])
    check("타임라인이 이어 붙는다", len(res4["timeline"]) == 2 and len(store) >= 1,
          str(res4["timeline"]))

    # ─────────────────────────────────────────────────────────── v1.2.12
    #  수신(To)/참조(CC) 기본 분류 · 내 규칙(발신자→키워드) · 단순 공유 예외
    print("\n[I] 수신(To)/참조(CC) 기본 분류 · 내 규칙 · 단순 공유 예외")
    sm.configure({"name": ME_NAME, "title": ME_TITLE, "dept": ME_DEPT,
                  "email": ME_ADDR, "company": "천일정기화물자동차"},
                 {"rules": {"senders": [{"key": "etd@chunil.co.kr", "name": "엄태동 상무",
                                          "email": "etd@chunil.co.kr", "priority": 3}],
                            "keywords": [{"text": "PTR", "priority": 2}]}})

    def _mk(**kw):
        it = {"title": "생산 1공장 출하리스트", "bodyText": "하기 참조.",
              "sender": "엄태동 상무", "senderName": "엄태동 상무",
              "senderEmail": "etd@chunil.co.kr", "recipientRole": "CC"}
        it.update(kw)
        sm.classify(it)
        return it

    to_it = _mk(recipientRole="To")
    check("수신(To) → 🔴 즉시 (단순 공유 발신자 규칙 무시)",
          to_it["priority"] == 1 and "내 규칙" not in to_it["priorityReason"], to_it["priorityReason"])
    cc_it = _mk()
    check("참조(CC)만 → ⚪ 단순 공유 규칙 적용",
          cc_it["priority"] == 3 and "내 규칙" in cc_it["priorityReason"], cc_it["priorityReason"])
    named_it = _mk(title=f"{ME_NAME} {ME_TITLE}님 확인 — 생산 1공장 출하리스트")
    check("참조(CC)라도 이름 지목 → 🔴 즉시(규칙 무시)",
          named_it["priority"] == 1, named_it["priorityReason"])
    kw_it = _mk(title="PTR 막차 납입 확인", sender="김동료 대리", senderName="김동료 대리",
                senderEmail="dongryo@chunil.co.kr")
    check("제목 키워드 규칙(PTR) → 🟡 흐름 참고", kw_it["priority"] == 2, kw_it["priorityReason"])
    plain_it = _mk(title="공정이관 검토 요청", sender="김동료 대리", senderName="김동료 대리",
                   senderEmail="dongryo@chunil.co.kr")
    check("규칙 없는 참조(CC) → 🟡 흐름 참고", plain_it["priority"] == 2, plain_it["priorityReason"])

    gem_calls2 = []

    class _Gem2:
        def analyze(self, mail, **kw):
            gem_calls2.append(1)
            return {"summary": "x", "timeline": [], "actionItems": [], "dueDate": None, "deadline": "",
                    "replyStatus": "no_reply_needed", "category": "기타", "urgency": "normal",
                    "threadGist": "", "tags": [], "replyDrafts": {}}

    share_mail = {"mf_no": 991001, "subject": "생산 1공장 출하리스트", "bodyText": "하기 참조.",
                  "from_raw": "엄태동 상무 <etd@chunil.co.kr>", "from_email": "etd@chunil.co.kr",
                  "date": "2026-11-24 09:00", "to": ["강한 <kanghan@chunil.co.kr>"],
                  "cc": [f"{ME_NAME} <{ME_ADDR}>"]}
    res_share = sm.analyze_mail(_Gem2(), share_mail, "받은편지함", {}, thread_cache={})
    check("⚪ 단순 공유 규칙 + 참조(CC) → AI 0회",
          res_share["aiSkipped"] and res_share["aiSkipReason"] == "simple_share" and not gem_calls2,
          res_share.get("summary", "")[:30])
    to_share = dict(share_mail, to=[f"{ME_NAME} <{ME_ADDR}>"], cc=[])
    res_to = sm.analyze_mail(_Gem2(), to_share, "받은편지함", {}, thread_cache={})
    check("수신(To)이면 단순 공유 규칙이 있어도 정상 분석",
          not res_to.get("aiSkipped") and len(gem_calls2) == 1, str(res_to.get("aiSkipReason")))
    sm.set_custom_rules({})       # 뒤 테스트로 규칙이 새지 않게

    # v1.2.13 — 결론은 구체적으로 · 일이 없다는 마무리 금지 · 주담당 교차 판별
    ct = {ME_ADDR.lower(): {"name": ME_NAME, "title": ME_TITLE, "dept": ME_DEPT},
          "ryu@chunil.co.kr": {"name": "류광종", "title": "과장", "dept": "해외물류부"},
          "johhee.yeom@gm.com": {"name": "염주희", "title": "차장", "dept": "GM"}}
    eop2 = {"subject": "EOP자재 재고실사 요청_11/24(화) 07시",
            "bodyText": "하기 품번 명일 07:00 기준 재고실사 요청드립니다.\n42711726 HOUSING AS",
            "date": "2026-11-23 16:30", "from_raw": "염주희 <johhee.yeom@gm.com>",
            "from_email": "johhee.yeom@gm.com", "to": [f"{ME_NAME} <{ME_ADDR}>"], "cc": []}
    h2 = sm.heuristic_analysis(eop2, True, ct)
    check("결론 — 품번·기한·회신 대상이 들어간 구체적 행동",
          h2["ourDeptAction"].startswith("[직접 회신 필요]") and "42711726" in h2["ourDeptAction"]
          and "염주희" in h2["ourDeptAction"] and "11/24(화) 07시" in h2["ourDeptAction"]
          and "본문 확인" not in h2["ourDeptAction"], h2["ourDeptAction"])
    check("일이 없다는 식의 짧은 마무리 미사용",
          not sm.ACTION_BANNED_RE.search(h2["myAction"]), h2["myAction"])
    ov2 = {"subject": "12월 선복 부킹 확정 공유",
           "bodyText": "선복 부킹과 통관 일정 확인 부탁드립니다.",
           "date": "2026-11-24 08:10", "from_raw": "류광종 <ryu@chunil.co.kr>",
           "from_email": "ryu@chunil.co.kr",
           "to": ["해외물류부 <overseas@chunil.co.kr>"], "cc": [f"{ME_NAME} <{ME_ADDR}>"]}
    lt2 = sm.light_analysis(ov2, ct)
    check("타 부서 참조 — 직접 조치 없음으로 결론",
          lt2["ourDeptAction"].startswith("[직접 조치 없음") and "해외물류부" in lt2["ourDeptAction"]
          and lt2["replyStatus"] == "no_reply_needed", lt2["ourDeptAction"])
    mix2 = dict(ov2, subject="12월 선적 스케줄 협의",
                to=[f"{ME_NAME} <{ME_ADDR}>", "해외물류부 <overseas@chunil.co.kr>"], cc=[])
    mx = sm.heuristic_analysis(mix2, True, ct)
    check("To에 우리 팀이 있어도 본문 요청이 타 부서면 넘긴다",
          "해외물류부" in mx["mainDept"] and mx["ourDeptAction"].startswith("[직접 조치 없음")
          and mx["replyStatus"] == "no_reply_needed", f"{mx['mainDept']} / {mx['ourDeptAction']}")
    check("프롬프트에 구체적 결론 규칙·금지 문구", "금지 문구" in sm.SYSTEM_PROMPT
          and "구체적 행위 + 대상/품번·기준" in sm.SYSTEM_PROMPT
          and not sm.ACTION_BANNED_RE.search(sm.SYSTEM_PROMPT))
    # v1.2.14 — 수신(To) 판정 보강 · 데이터상 To 등급 재분류 (사용자 신고: ‘수신인(To)’ + 🟡 흐름 참고)
    if not sm.OWNER.get("email"):
        sm.OWNER["email"] = 'kimtest@chunil.co.kr'
    sm.ME = sm.OWNER.get("email") or sm.ME
    _me_addr = sm.OWNER.get("email")
    quoted = {"id": "m-eop", "title": "RE: [EXTERNAL] RE: EOP자재 재고실사 요청_11/25일(수) 07시",
               "sender": "류광종 과장 (해외물류부)", "senderEmail": "rgj@chunil.co.kr",
               "recipientRole": "CC", "priority": 2,
               "to": ["김정호 <kjh@chunil.co.kr>", f"'김성규' <{_me_addr}>"]}
    check("수신 명단에 내 주소가 있으면 수신으로 판정", sm._item_to_me(quoted) is True)
    sm.classify(quoted)
    check("‘수신인(To)’인데 🟡 로 남지 않는다", quoted["priority"] == 1, str(quoted.get("priority")))
    check("판정 근거에 수신(To) 표시", "수신인(To)" in str(quoted.get("priorityReason")),
          str(quoted.get("priorityReason")))
    cc_only = {"id": "m-ccx", "title": "RE: EOP자재 재고실사 요청", "sender": "류광종 과장 (해외물류부)",
                "senderEmail": "rgj@chunil.co.kr", "recipientRole": "CC", "priority": 2,
                "to": ["김정호 <kjh@chunil.co.kr>"], "cc": [f"김성규 <{_me_addr}>"]}
    sm.classify(cc_only)
    check("참조(CC)만이면 🟡 흐름 참고 유지", cc_only["priority"] == 2, str(cc_only.get("priority")))
    stale = dict(quoted, id="m-stale", recipientRole="CC", priority=2, priorityReason="")
    fixed_n = sm.repair_to_priority([stale, cc_only])
    check("repair_to_priority — 수신(To)인데 🟡인 항목을 🔴로 되살린다",
          stale["recipientRole"] == "To" and stale["priority"] == 1 and fixed_n >= 1,
          f"{stale['recipientRole']}/{stale['priority']}/{fixed_n}")
    check("repair_to_priority — 참조(CC)는 건드리지 않는다", cc_only["priority"] == 2, str(cc_only.get("priority")))
    merged = {"id": "m-merge", "title": "RE: EOP자재 재고실사 요청", "recipientRole": "CC", "priority": 2,
               "receivedAt": "2026-11-20 09:00",
               "thread": [{"id": "m-old", "date": "2026-11-20 09:00"}]}
    sm.merge_into_thread(merged, {"thread": [{"id": "m-new", "date": "2026-11-24 10:40"}],
                                  "priority": 1, "priorityReason": "🔴 즉시 · 나를 수신인(To)으로 지정",
                                  "recipientRole": "To", "replyStatus": "needs_reply"})
    check("스레드 병합 — 수신 구분도 최신 메일(To)을 따라간다",
          merged.get("recipientRole") == "To" and merged.get("priority") == 1,
          f"{merged.get('recipientRole')}/{merged.get('priority')}")

    # ─────────────────────────────────────────────────────────── v1.2.18
    #  [object Object] · 웹메일 메뉴 첨부 · [Begin] 마커 · 히스토리 중복 · To 타인 + 우리팀 CC 오판정
    menu = ["메일", "일정관리", "주소록", "환경설정", "개인정보", "김성규", "로그보기", "로그아웃", "편지쓰기",
            "내게쓰기", "안읽음 1", "중요", "수신확인", "대용량 첨부", "전체메일 1", "관리", "받은편지함 4",
            "임시보관함 4", "지운편지함", "비움", "스팸편지함", "스팸메일 설정", "POP3 이용안내",
            "SMTP/POP3 문제해결", "리턴메일 분석", "로그인"]
    menu_html = ("<html><body><div class='side'><ul>" +
                 "".join(f'<li><a href="/mail_list.php?x={i}">{n}</a></li>' for i, n in enumerate(menu)) +
                 '</ul></div><table><tr><td id="mf_body_view">본문</td></tr></table></body></html>')
    menu_atts = sm.parse_read(menu_html)["attachments"]
    check("웹메일 메뉴 26종은 첨부가 아니다(v1.2.18)", menu_atts == [], str(menu_atts)[:60])
    att_page = read_page(date="2026-11-24 12:27", frm="조승환 <jsh3117@chunil.co.kr>",
                         to=["김성규 <kimpro@chunil.co.kr>"], cc=[], subject="첨부 확인 부탁",
                         body="첨부 확인 부탁드립니다.",
                         attach=[("1123 1340.xlsx", "57.58K"), ("도면.pdf", "120.1K")])
    att_names = [a["name"] for a in sm.parse_read(att_page)["attachments"]]
    check("실제 첨부는 이름·용량 그대로 읽는다(v1.2.18)",
          att_names == ["1123 1340.xlsx", "도면.pdf"], str(att_names))
    dirty = [{"id": "m-dirty", "attachments": [{"name": n, "size": ""} for n in menu] + [{"name": "a.xlsx", "size": "1 KB"}]}]
    removed = sm.clean_attachments(dirty)
    check("이미 저장된 웹메일 메뉴 첨부를 정리한다(v1.2.18)",
          removed == len(menu) and [a["name"] for a in dirty[0].get("attachments") or []] == ["a.xlsx"],
          f"{removed} / {dirty[0].get('attachments')}")

    begin_page = read_page(date="2026-11-24 12:27", frm="김세유 <seyu@chunil.co.kr>",
                           to=["김성규 <kimpro@chunil.co.kr>"], cc=[], subject="[Begin] 제목 확인",
                           body="<!-- [Begin]상단 디자인 -->[Begin] 메일 본문 하고 <!-- [End] -->참조 바랍니다")
    begin_read = sm.parse_read(begin_page)
    check("[Begin]/[End] 파싱 마커가 본문·제목에 남지 않는다(v1.2.18)",
          "[Begin]" not in begin_read["bodyText"] and "[End]" not in begin_read["bodyText"]
          and "[Begin]" not in begin_read["subject"],
          repr(begin_read["bodyText"][:60]))

    dup = {"id": "m-dup", "receivedAt": "2026-11-24 12:27",
           "thread": [{"id": "m-5418517", "date": "2026-11-24 12:27", "from": "김세유 과장",
                       "gist": "FW: 13G 하치장 로컬 휴게실 상단 미사용 실외기 철거 요청 관련_260904"}] * 12}
    check("히스토리 12줄 중복을 1줄로 병합(v1.2.18)",
          sm.dedup_all_threads([dup]) == 11 and len(dup["thread"]) == 1, str(len(dup["thread"])))
    check("스레드에 병합된 번호도 기준선에 들어간다(v1.2.18)",
          sm.max_mf_no({"items": [{"id": "m-5418000", "thread": [{"id": "m-5418517"}]}]}) == 5418517)
    same = {"id": "m-1", "thread": [{"id": "m-5418517", "date": "2026-11-24 12:27", "from": "김세유 과장", "gist": "x"}]}
    sm.merge_into_thread(same, {"thread": [{"id": "m-5418517", "date": "2026-11-24 12:27", "from": "김세유 과장", "gist": "x"}],
                                "priority": 2, "priorityReason": "", "replyStatus": "no_reply_needed"})
    check("같은 메일을 다시 병합해도 한 줄(v1.2.18)", len(same["thread"]) == 1, str(len(same["thread"])))

    fw = {"mf_no": 990918, "subject": "FW: 13G 하치장 로컬 휴게실 상단 미사용 실외기 철거 요청 관련_260904",
          "bodyText": ("[Begin] 메일 본문 하고 참조 바랍니다.\n\n------Original Message------\n"
                       "From: Jaeyeong Bae (배재영 - 롯데온 ) <jaeyeong.bae@lotteon.com>\n"
                       "13G 하치장 로컬 휴게실 상단 미사용 실외기 철거 요청드립니다.\n\n"
                       "납입1팀 과장 김성규\n"),
          "from_raw": "김세유 <seyu@chunil.co.kr>", "from_email": "seyu@chunil.co.kr",
          "date": "2026-11-24 12:27", "to": ["서유태 <seota@chunil.co.kr>"],
          "cc": ["김성규 <kimpro@chunil.co.kr>", "김기경 <kkg@chunil.co.kr>"]}
    fw_ruling = sm.dept_ruling(fw, contacts)
    check("To 가 타인·우리 팀은 CC — 인용·서명에 이름이 보여도 우리 부서 직접 회신으로 단정하지 않는다(v1.2.18)",
          fw_ruling["ours"] is False and "납입1팀" not in fw_ruling["main"].split("(주담당)")[0],
          f"{fw_ruling['main']} / ours={fw_ruling['ours']}")
    fw_actions = sm.concrete_actions(fw, contacts, ruling=fw_ruling)
    check("To 가 타인인 참조 건의 결론은 직접 조치 없음(v1.2.18)",
          fw_actions[0].startswith("[직접 조치 없음"), fw_actions[0])
    fw_polished = sm.polish_actions({"mainDept": "납입관리부(납입1팀)", "ourDeptAction": "[직접 회신 필요] 결과 회신",
                                     "myAction": "[참조 · 업무 흐름 파악]", "replyStatus": "needs_reply",
                                     "actionItems": ["결과 회신"]}, fw, contacts)
    check("AI 가 우리 팀 직접 회신으로 써도 결론·회신 필요를 바로잡는다(v1.2.18)",
          fw_polished["ourDeptAction"].startswith("[직접 조치 없음")
          and fw_polished["replyStatus"] == "no_reply_needed" and not fw_polished["actionItems"],
          f"{fw_polished['ourDeptAction']} / {fw_polished['replyStatus']}")

    # ─────────────────────────────────────────────────────────── v1.2.19
    #  유사 사례 카드마다 ‘웹메일 원본 ↗’ — 카드가 가리키는 과거 메일의 원문 주소가 항상 남아야 한다
    no_link = [it.get("id") for it in data.get("items", [])
               if not str(it.get("mailLink") or "").startswith(sm.BASE + "/mail_read.php?")]
    check("카드 원본 링크 — 저장된 모든 메일(이전 기록 포함)에 원문 주소가 있다(v1.2.19)",
          no_link == [], str(no_link[:3]))
    if a:
        check("카드 원본 링크 — 편지함(mb_id)·메일번호(mf_no)가 주소에 그대로 들어간다(v1.2.19)",
              a.get("mailLink") == sm.MAIL_LINK % ("inbox", base + 1), str(a.get("mailLink")))
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
