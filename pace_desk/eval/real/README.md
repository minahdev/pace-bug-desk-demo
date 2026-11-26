# 실제 리뷰 시험지

진짜 사용자 글에서도 분류가 잘 되는지 확인하는 실전용 시험지다. 본시험은 PACE 시험지(`../pace_eval.jsonl`)이고, 이 시험지는 그 결과가 실제 글에서도 통하는지 확인하는 데 쓴다.

## 출처
- 데이터: Jialiang Wei, Anne-Lise Courbis, Thomas Lambolais, Binbin Xu, Pierre Louis Bernard, Gérard Dray. *Zero-shot Bilingual App Reviews Mining with Large Language Models.* ICTAI 2023. doi:10.1109/ICTAI59109.2023.00135
- 받은 곳: https://zenodo.org/records/11066414 (2026-11-24에 받음), MIT 라이선스
- 원본 파일: `raw/dataset.zip`, 압축을 푼 `raw/dataset/`, 기록 정보 `raw/record.json`

## 원본에 든 것
- 분류용 12,000개
  - 앱 3개: Garmin Connect, Huawei Health, Samsung Health
  - 언어별 6,000개씩: 영어(en), 프랑스어(fr)
  - 정답 칸: problem_report(문제 보고), feature_request(기능 요청), irrelevant(무관). 한 리뷰에 정답이 둘 붙기도 한다.
- 묶음용 1,200개: 같은 문제·요청을 말하는 리뷰끼리 사람이 묶은 번호(ground_truth)

## 고른 방법 (`make_real_set.py`)
- 먼저 중복 1,104개, 20자 미만 1,509개, 라벨 없는 글 2개를 뺐다. 남은 것은 9,385개다.
- 앱·언어마다 80개씩, 모두 480개를 골랐다.

  | 종류 | 앱·언어마다 | 합계 |
  |---|---|---|
  | 문제 보고만 | 30 | 180 |
  | 기능 요청만 | 25 | 150 |
  | 둘 다 | 5 | 30 |
  | 무관 | 20 | 120 |

- 무관은 40자 이상만 골랐다. 그 절반은 별점 3 이하로, 불만은 있지만 구체적인 문제나 요청은 아닌 헷갈리는 글이다.
- 연습용(dev) 288개, 시험용(test) 192개로 나눴다.

## 우리 서비스 답과 맞춰 보는 법

| 우리 서비스의 분류 | 이 데이터의 정답 |
|---|---|
| 버그 | 문제 보고 |
| 개선 요청 | 기능 요청 |
| 문의, 판단 불가 | 무관 |

정답이 둘 다인 글은 우리 답이 둘 중 하나와 맞으면 맞힌 것으로 본다. 자세한 채점 방법은 분류 실험(4단계)에서 정한다.
