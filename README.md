# DB Managing Console
> Local LLM + NL2SQL


## 기능

0. Table 조회
1. PDF,CSV,EXEL 파싱 → 확인 및 수정 → Table 생성 및 적재
2. 자연어 → 쿼리 생성 → 확인 및 수정 → 실행

## 테스트 환경

앱 서버 (VM)
CPU : Intel Xeon E5-2630 v4 (Broadwell, AVX2 지원)
RAM : 4GB

LLM 호스트
CPU : Intel Core Ultra 7 265
RAM : 16GB
ETC : LM Studio, OpenAI 호환 API
AI : Qwen3-4B

나머지는 requirements 참고

## Comment

상용 서비스가 편의성과 성능이 더 좋지만 그래도 보안은 로컬 AI가 더 좋다