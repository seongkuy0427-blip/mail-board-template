# CHUNIL Smart Mail Portal — 사내 배포용 템플릿

천일정기화물자동차(주) **회사 웹메일**(`mail.chunil.co.kr`)을 30분마다 자동으로 읽어
**오늘 내가 처리할 일** 중심으로 정리해 보여주는 **개인용 암호화 대시보드**입니다.

- 프로그램을 설치하거나 코딩할 필요가 없습니다. **GitHub 계정 + 이 저장소 복사 + 비밀 4개 입력 + 실행 1회** 로 끝납니다(약 5~10분).
- 메일 내용은 **내 PIN 으로만 열리는 암호문**(`data.enc.json`, AES-256-GCM)으로 저장됩니다.
  저장소가 공개여도 제목·본문·품번·고객사 정보는 평문으로 남지 않습니다.
- PC·휴대폰 어디서든 같은 주소로 열리고, 휴대폰 바탕화면에 앱처럼 고정할 수 있습니다.

```
회사 웹메일 ──(GitHub Actions, 30분마다)──▶ Gemini 로 요약·분류 ──▶ data.enc.json (암호문)
                                                                        │
                                              GitHub Pages (정적 호스팅) ──▶ 내 브라우저에서 PIN 으로 복호화
```

## 쓰는 법 (요약)

1. 이 저장소에서 **Use this template → Create a new repository** → 이름 `mail-board` → **Create**.
   (버튼이 안 보이면 **Import** 로도 됩니다: `github.com/new/import` → 위 저장소 주소 붙여넣기 → 이름 `mail-board` → Public)
2. **Settings → Secrets and variables → Actions** 에 4개 등록:
   `CHUNIL_ID`(웹메일 아이디, `@` 앞부분) · `CHUNIL_PW` · `GEMINI_API_KEY` · `MAILBOARD_PIN`(내 대시보드 비밀번호).
3. **자동 실행 파일 만들기** — 저장소 **Add file → Create new file** → 이름에 `.github/workflows/mail-sync.yml` →
   [`scripts/mail-sync.yml.example`](scripts/mail-sync.yml.example) 내용을 복사해 붙여넣고 **Commit changes**.
   (아직 저장소에 없습니다 — GitHub 앱 권한 문제로 템플릿에 함께 넣지 못했습니다.)
4. **Settings → Pages** → Source: `Deploy from a branch` → Branch **`gh-pages`** / `/ (root)` → Save.
   (처음에는 `gh-pages` 가 없으므로 5번 실행 후에 다시 오면 됩니다.)
5. **Actions → mail-board sync → Run workflow** 한 번 실행 (첫 실행은 1~3분).
6. `https://<내깃허브아이디>.github.io/mail-board/` 접속 → 아이디와 `MAILBOARD_PIN` 입력.

자세한 절차·문제 해결은 **[`scripts/SETUP-actions.md`](scripts/SETUP-actions.md)** 를 보세요.

## 무엇을 해 주나

| 영역 | 내용 |
| --- | --- |
| 분류 | 🔴 즉시 확인 / 🟡 흐름 참고 / ⚪ 단순 공유 3단계. **내가 To 로 받은 메일**, **본문에 내 이름이 있는 메일**, 지정한 발신자(VIP) 메일이 위로 올라옵니다 |
| 상단 요약 | 즉시 확인 · 오늘 마감 · 회신 필요 · 미회신 대기 · 완료 5개 카드(KPI) — 눌러서 바로 필터 |
| 상세 | 메일 원문 → 핵심 분석 → 할 일 → 이전 메일 히스토리 → **답장 문구 실무 3안**(📎 자료 송부형 · ✅ 조치·결과 회신형 · 👉 담당자 전달·확인중) 복사 |
| 캘린더 | 마감일이나 직접 고른 날짜로 일정 등록·수정·옮기기·삭제, 관련 메일 자동 연결 |
| 과거 사례 | 품번·ASN/BL·키워드가 겹치는 과거 메일을 최대 6건 자동 연결 |
| 알림 | 새 🔴 메일은 화면 상단 알림 카드 + 탭 제목 깜빡임(사내 PC 는 Windows 팝업이 막혀 있어도 동작) |
| 분류 규칙 ⚙️ | 발신자·키워드별 등급을 브라우저에서 직접 바꿔 즉시 반영(내 PC·휴대폰에만 저장) |

## 내 이름·부서는 자동으로

로그인 사용자의 이름·메일·부서·직급은 `sync_mail.py` 가 웹메일 화면(개인정보 페이지 → 상단 프로필 →
**사내 주소록의 본인 항목**)에서 읽어 `data.owner` 에 넣습니다. 화면 상단(`○○○ 님 ▾`), 답장 서명,
본문 지목 판정이 모두 이 값을 씁니다. **코드에 사람 이름을 고칠 필요가 없습니다.**

`data.owner` 의 메일은 언제나 **사내 메일(`아이디@chunil.co.kr`)** 입니다. 개인정보 페이지에 적힌
외부 복구 메일(hanmail·naver 등)은 쓰지 않습니다 — To 판정이 그 주소로 이뤄지기 때문입니다.

## 사내 주소록은 자동으로

`sync_mail.py` 는 웹메일의 **조직도 + 개인주소록**을 읽어 `data.contacts` 로 함께 암호화해 넣습니다
(이름·메일·부서·구분). 메일 발신자의 이름·부서 표기와 대시보드 **⚙️ 분류 규칙 설정** 목록이 이 값을 씁니다.

- 수집 시점: `data.contacts` 가 비어 있을 때(첫 실행), 또는 `--refresh-contacts` 를 줬을 때
- 수집에 실패해도 메일 동기화는 그대로 진행하고 경고만 남깁니다.
- 직접 확인: `python3 scripts/sync_mail.py --contacts-probe` (몇 명 읽혔는지 출력)

## 분류 기준 바꾸기

`board_config.json`:

```json
{
  "company": "천일정기화물자동차",
  "to_is_urgent": true,
  "p3_title_keywords": ["출하리스트", "납입요청", "미상차현황"],
  "vips": []
}
```

- `to_is_urgent` — 내가 **To** 로 직접 받은 메일을 🔴 로 올릴지(기본 `true`).
- `p3_title_keywords` — 제목에 이 말이 있으면 **To 여도 ⚪ 단순 공유**로 내립니다.
- `vips` — 항상 위에 두고 싶은 발신자. 예:

```json
"vips": [{ "name": "홍길동", "email": "hong@chunil.co.kr", "label": "1-1 최우선", "rank": 1, "toOnly": false }]
```

> **주의** — 이 파일은 공개 저장소에 올라갑니다. 사람 이름·메일 주소를 넣는 `vips` 는
> **저장소를 Private 으로 두거나**, 대시보드의 **⚙️ 분류 규칙**(브라우저에만 저장)을 쓰는 편이 안전합니다.

## 저장소 구성

| 경로 | 역할 |
| --- | --- |
| `index.html` | 대시보드 화면 전체(한 파일). 데이터는 브라우저에서 PIN 으로 복호화 |
| `data.enc.json` | 암호문. 첫 실행 때 자동 생성 → 이후 Actions 가 갱신 |
| `assets/left-banner.jpg` | 좌측 여백 배너 이미지 |
| `board_config.json` | 분류 기준(위 설명) |
| `scripts/sync_mail.py` | 웹메일 로그인 → 사내 주소록·사용자 인식 → 새 메일 수집 → Gemini 분석 → 재암호화 |
| `scripts/backfill_archive.py` | (선택) 과거 메일 본문을 조금씩 채우기 — 실행 건수를 넣었을 때만 |
| `scripts/test_sync_offline.py` | 네트워크 없이 파이프라인 전체를 검사하는 테스트 |
| `.github/workflows/mail-sync.yml` | (3단계에서 직접 만드는 파일) 30분 스케줄 · 수동 실행 · main/gh-pages 커밋·배포. 원본은 `scripts/mail-sync.yml.example` |

## 안전하게 쓰기

- `CHUNIL_PW`·`GEMINI_API_KEY`·`MAILBOARD_PIN` 은 **Secrets** 에만 넣습니다. 코드·파일에는 절대 쓰지 않습니다.
- `MAILBOARD_PIN` 을 잊으면 암호문을 열 수 없습니다(복구 불가). 안전한 곳에 적어 두세요.
- 공용 PC 에서는 로그인 화면의 **이 기기에서 로그인 유지**를 끄세요.
- 저장소를 공개로 두면 `index.html`·`data.enc.json`·Actions 로그도 공개됩니다. 메일 내용은 암호문이라 읽히지 않지만,
  로그에 제목이 찍히지 않도록 기본으로 가려 둡니다(`SYNC_SHOW_SUBJECTS` 를 켜지 않는 한).
