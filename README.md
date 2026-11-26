# PACE 버그 제보 데스크

건강 앱 PACE에 들어온 버그 제보 글을 Claude Haiku 5.5가 읽고 정리해서, 이미 알려진 이슈와 연결하고 우선순위를 매겨 주는 서비스입니다. LLM 서비스 수업의 과제(개발팀 업무)로 만들었습니다.

**데모 사이트: https://minahdev.github.io/pace-bug-desk-demo/**
진짜 서비스가 예시 제보 8개를 분석한 결과를 볼 수 있습니다. 새 글을 직접 분석하려면 아래 실행 방법대로 내 컴퓨터에서 실행합니다.

## 하는 일

```
제보 글
 → ① 분류 (LLM): 버그 · 문의 · 개선 요청 · 판단 불가
 → ② 추출 (LLM): 플랫폼 · 기기 · 앱 버전 · 재현 절차         ┐ ①의 결과를 받아 동시에 처리
 → ③ 이슈 연결·심각도 (LLM + 기록부 검색 도구)               ┘
 → ④ 계산 (코드): 우선순위 · 정보 완성도 · 부족한 정보 질문
 → 웹 화면
```

- 글의 뜻을 판단하는 일은 LLM이, 같은 입력에 늘 같은 점수가 나와야 하는 계산은 코드가 맡습니다.
- 우선순위 = 심각도(LLM) + 영향 범위(코드) + 해결 상태(코드), 0~6점
  - 5~6점 긴급, 4점 높음, 2~3점 보통, 0~1점 낮음

## 평가 결과

시험지 400문제(연습용 240, 시험용 160)로 채점했습니다. 프롬프트는 연습용으로만 고쳤고, 시험용은 마지막에 한 번만 풀었습니다.

| 태스크 | 지표 | 목표 | 시험용 결과 |
|---|---|---|---|
| 분류 | 정확도 (3번 평균) | 95% 이상 | 98.1% |
| 추출 | 플랫폼 · 앱 버전 | 95% 이상 | 100% · 100% |
| 추출 | 기기 · 재현 절차 있음/없음 | 90% 이상 | 99.4% · 98.1% |
| 추출 | 지어낸 값 | 1% 이하 | 0% |
| 연결·점수 | 이슈 연결 (첫 번째로 고른 이슈가 정답과 같은 비율) | 90% 이상 | 96.3% |
| 연결·점수 | 우선순위 일치 | 85% 이상 | 98.5% |

- 제보 1건 처리: 약 8초, 약 0.0013달러(API 정가 환산)
- 회차별 개선 히스토리: `pace_desk/eval/results/final/improvement_history.csv`
- 최종 결과 표: `pace_desk/eval/results/final/final_test_results.csv`

## 실행 방법

필요한 것:
- Python 3.12
- Claude Code (설치하고 로그인). Haiku는 Claude Code 헤드리스 모드(`claude -p --model claude-haiku-5-5`)로 부릅니다. API 키는 쓰지 않습니다.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app:app --app-dir pace_desk/service --port 8010
```

브라우저에서 http://127.0.0.1:8010 을 엽니다. 분석할 때마다 Claude 요금제 사용량이 조금 쓰입니다.

## 폴더 안내

| 위치 | 내용 |
|---|---|
| `pace_desk/service/` | 웹 서비스(`app.py`)와 처리 흐름(`pipeline.py`) |
| `pace_desk/llm.py` | Haiku를 부르는 코드. 모든 호출이 여기를 거칩니다 |
| `pace_desk/prompts/` | 회차별 프롬프트. 최종은 `cls_c7.md`, `ext_e2.md`, `link_s3.md` |
| `pace_desk/tools.py`, `pace_desk/mcp_server.py` | 기록부 검색 도구와, 그 도구를 Haiku에 연결하는 MCP 서버 |
| `pace_desk/data/pace_db.json` | 기록부: 알려진 이슈 37개(21개는 가상), 버전 기록 14개 |
| `pace_desk/eval/` | 시험지(`pace_eval.jsonl`), 기준표(`labeling_guide.md`), 채점 코드(`eval_*.py`), 결과(`results/`) |

## 데이터

- 시험지의 제보 글 400개는 과제를 위해 만든 가상 제보입니다.
- 실제 리뷰 확인에 쓴 리뷰 데이터는 이 저장소에 넣지 않았습니다.
  - 출처: Wei et al., *Zero-shot Bilingual App Reviews Mining with Large Language Models*, ICTAI 2023, https://zenodo.org/records/11066414 (MIT 라이선스)
  - 받은 파일을 `pace_desk/eval/real/raw/`에 두고 `pace_desk/eval/real/make_real_set.py`를 실행하면 같은 480개를 만듭니다.
