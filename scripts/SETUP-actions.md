# GitHub Actions 로 무인 자동 갱신하기

`scripts/sync_mail.py` 는 웹메일(`mail.chunil.co.kr`)을 직접 읽어 Gemini API 로 정리하고,
`data.enc.json`(암호문)만 다시 만들어 커밋하는 스크립트입니다. 화면(`index.html`)과 데이터 형식은
그대로라서 대시보드 사용법은 바뀌지 않습니다.

```
웹메일 로그인(requests) → 로그인 사용자 인식 → 새 메일만 수집 → Gemini 분석
   → 기존 data.enc.json 복호화·병합 → v2 gzip + PBKDF2-SHA256(10만 회) + AES-256-GCM 재암호화
   → main·gh-pages 커밋 → GitHub Pages 배포
```

처음 실행할 때는 `data.enc.json` 이 없어도 됩니다. 스크립트가 **최근 14일 메일을 스스로 모아**
새 보드를 만들고(기본 30건, `--seed-days`/`--seed-max` 로 조정), 로그인 사용자의 이름·부서를 읽어
`data.owner` 에 넣습니다. 그 다음 실행부터는 새 메일만 이어 붙입니다.

## 1. 저장소 Secrets 4개 등록

GitHub → 내 저장소 → **Settings → Secrets and variables → Actions → New repository secret**

| 이름 | 값 |
| --- | --- |
| `CHUNIL_ID` | 웹메일 아이디 (`@` 앞부분만 — 예: `honggildong`) |
| `CHUNIL_PW` | 웹메일 비밀번호 |
| `GEMINI_API_KEY` | 공유받은 Gemini API 키 (또는 Google AI Studio 에서 발급) |
| `MAILBOARD_PIN` | 대시보드 로그인 비밀번호 (내가 정하는 숫자·문자, 잊으면 암호문을 열 수 없습니다) |

선택 사항(**Settings → Variables**): `GEMINI_MODEL`(기본 `gemini-2.5-flash`), `NMAIL_IP_CHECK`(기본 `1`).

## 2. 워크플로우 파일 만들기 (필수)

자동 실행(30분 간격)은 `.github/workflows/mail-sync.yml` 파일이 있어야 등록됩니다.
이 템플릿에는 그 파일이 빠져 있으므로 한 번 직접 만들어야 합니다(GitHub 앱 권한 문제로 함께 올리지 못했습니다).

1. 저장소 위쪽 **Add file → Create new file**
2. 파일 이름 칸에 `.github/workflows/mail-sync.yml` 을 그대로 입력
3. 새 탭에서 `scripts/mail-sync.yml.example` 을 열고 오른쪽 위 **Copy raw file** 로 내용을 복사 → 붙여넣기
4. 아래 **Commit changes**

## 3. GitHub Pages 켜기

저장소 → **Settings → Pages** → Source: **Deploy from a branch** → Branch: **`gh-pages`** / **`/ (root)`** → Save.
(`gh-pages` 브랜치는 첫 실행이 끝나면 자동으로 만들어집니다. 그래서 **순서는 4 → 3** 이어도 됩니다.)

## 4. 동작 확인

- **Actions → mail-board sync → Run workflow** 로 한 번 실행합니다(첫 실행은 1~3분).
- 로그에 아래처럼 찍히면 성공입니다.

```
[동기화] 기준 mf_no=0 · 신규 받은 30건 · 신규 보낸 10건 · 회신 반영 0건 · 변경=True · Gemini 30회 · 최초 구축
  · 로그인 사용자: 홍길동 과장 (도입2팀) honggildong@chunil.co.kr
```

- **Checkout main / Commit main / Deploy gh-pages** 세 단계가 초록불이면, 주소
  `https://<내아이디>.github.io/mail-board/` 를 열어 `MAILBOARD_PIN` 으로 들어갑니다.
- 저장소가 공개라 **Actions 로그도 공개**됩니다. 그래서 메일 제목·발신자·본문은 기본으로 찍지 않습니다
  (내 PC에서 직접 돌릴 때만 `SYNC_SHOW_SUBJECTS=1`).

## 5. 로컬에서 직접 돌려보기 (선택)

```bash
pip install -r scripts/requirements.txt
CHUNIL_ID=honggildong CHUNIL_PW=**** GEMINI_API_KEY=**** MAILBOARD_PIN=**** \
  python3 scripts/sync_mail.py --data-enc data.enc.json --dry-run     # 파일은 안 쓰고 결과만
python3 scripts/sync_mail.py --owner-probe                            # 사용자 인식 결과만 확인
MAILBOARD_PIN=**** python3 scripts/test_sync_offline.py                # 네트워크 없이 전체 점검
```

## 6. 문제가 생기면

| 증상 | 확인할 것 |
| --- | --- |
| `웹메일 로그인 실패` | `CHUNIL_ID` 가 메일 주소가 아닌 아이디인지, 비밀번호 오타. 회사 PC에서는 되는데 Actions 에서만 실패하면 `NMAIL_IP_CHECK` 값을 `0` 으로 바꿔 Variables 에 넣어 보세요 |
| `PIN 으로 열지 못했습니다` | `MAILBOARD_PIN` 이 이 저장소의 암호문과 다릅니다. 다른 PIN 이면 기존 기록을 덮어쓰지 않도록 일부러 멈춥니다 |
| 사이트에 `아직 데이터가 없습니다` | 첫 실행(Actions → Run workflow)이 아직 끝나지 않았습니다 |
| 화면이 예전 모습 | Pages 캐시(10분) 때문입니다. `Ctrl+F5` 또는 주소 뒤에 `?v=2` |
| 실행이 30분 넘게 걸림 | 처음 채우는 건수가 많습니다. `--seed-max` 를 줄이거나 다음 실행에서 이어집니다 |
