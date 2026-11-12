# Guideline chunk boundaries

Generation uses 20 guideline chunks. Fifteen come from Chapter Ⅲ of the medical-sector guideline for preliminary feasibility studies (예비타당성조사 수행을 위한 세부지침: 의료부문; Korea Development Institute, Public and Private Infrastructure Investment Management Center, December 2022), and five come from the gravity model part of Chapter Ⅶ of the culture and tourism sector guideline (May 2021). The chunk texts are not included in this repository.

Each chunk is the span of the guideline from the opening phrase to the closing phrase below. The phrases are given in Korean as they appear in the stored text. Character counts refer to the stored chunk text, which includes some page headers and footnotes. Because the text was extracted from PDF, spacing differs from the original in places. Which chunks are retrieved for each subsection is given in Supplementary Material B of the paper and in `pipeline/agents/retrieval.py`.

`_run.chunk_store.chunk_text_sha256_12` in each run record holds a hash of every chunk text (first 12 characters of the sha256), which can be used to check that rebuilt chunks are identical.

| Chunk identifier | Opening phrase | Closing phrase | Characters | Note |
|---|---|---|---:|---|
| `202212-III-1` | 제1절 수요 추정 및 진료권 설정의료서비스의 … | …업의 규모 타당성 및 경제성 분석의 기본 개요 | 2,352 | |
| `202212-III-2-1` | 제2절 해당 의료시설의 진료권에 대한 분석1.… | …적용될 수 있는 방법을 논의하고자 한다. | 1,675 | Trailing page header excluded |
| `202212-III-2-2가` | 2.진료권 설정의 방법일반적으로 의료기관의 진… | …국을 진료권으로 설정하는 것이 타당할 것이다. | 2,840 | |
| `202212-III-2-2나` | 나.행정구역 중심의 진료권 설정의 시사점(최근… | …득력을 더욱 확보할 수 있을 것으로 기대한다. | 9,825 | |
| `202212-III-2-2다` | 다.진료권 결정 요소에 대한 분석다음으로, 비… | …값을 적용하는 것이 일반적인 방안이라 하겠다. | 5,602 | |
| `202212-III-2-2라` | 라.진료권 설정을 위하여 적용하는 자료다음으로… | … 지역친화도 자료를 분석할 필요성이 대두된다. | 5,631 | |
| `202212-III-2-특수목적` | 이상의 논의에서는 일반적인 의료시설에 대하여 … | …등의 전문치료)이 확보되는 경우를 말함. | 1,759 | Trailing page header excluded |
| `202105-VII-1-개요수식` | 1. 중력모형의 개요 중력모형은 물리학으로… | …예측 시나리오를 적용하여 최종수요를 산정한다. | 1,725 | Text transcribed from the culture and tourism sector guideline |
| `202105-VII-1-절차` | 그림 VII-1] 중력모형을 이용한 수요 추정… | …수요예측(최종 수요예측 및 연차별 수요예측). | 375 | Text transcribed from the culture and tourism sector guideline |
| `202105-VII-2-매력도` | 라. 관광시설 매력도 적용방법에 대한 검토. … | …력도를 산정하는 것이 적절할 것으로 판단된다. | 2,253 | Text transcribed from the culture and tourism sector guideline |
| `202105-VII-2-참여율` | 3) 참여비율(이용비율: α) 정형화에 대한 … | …수요를 산정하는 것이 적절할 것으로 여겨진다. | 766 | Text transcribed from the culture and tourism sector guideline |
| `202105-VII-2-쟁점원칙` | 제2절 중력모형을 이용한 수요 추정의 쟁점사항… | …정도의 방문객 수 평균을 적용할 필요가 있다. | 1,538 | Text transcribed from the culture and tourism sector guideline |
| `202212-III-3-1` | 제3절 병상 규모의 적정성 검토분석에 앞서 전… | …는 경우 심각한 과소 추정의 결과를 낳게 됨. | 3,508 | Trailing page header excluded |
| `202212-III-3-2` | 2.지역별 인구 대비 병상 공급량에 기반 한 … | …료시설 종별 등급별 현황, 응급병상 수) | 3,819 | Trailing page header excluded |
| `202212-III-4-1` | 1. 연간 입원환자 수의 추정 앞 절에서 논의… | …원 건립의 경우에도 동일하게 고려되어야 한다. | 735 | The leading "제4절 …" heading was added when the chunk was split |
| `202212-III-4-2` | 2. 연간 외래환자 수의 추정 외래환자 수는 … | …으며, 이를 반영할 수 없다는 단점을 가진다. | 718 | The leading "제4절 …" heading was added when the chunk was split |
| `202212-III-4-3` | 3. 신규 유입 환자규모 추정 진료권 내에 특… | …론을 적용함에 있어서는 많은 주의가 요구된다. | 904 | The leading "제4절 …" heading was added when the chunk was split |
| `202212-III-5-1` | 제5절 예방 가능 응급사망자 수 추정대규모 의… | …2019년도 응급의료통계연보｣,2020.12. | 5,043 | Trailing page header excluded |
| `202212-III-5-2` | 2.응급사망자 수 추정아래 소절에서 논의할 예… | …이다.응급사망자수인구추계×응급사망률 | 657 | |
| `202212-III-5-3` | 3.예방 가능 사망자 수 추정다음으로 응급의료… | … 전문병원의 사례를 참고하여 제시하고자 한다. | 4,374 | |
