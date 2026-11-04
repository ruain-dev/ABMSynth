# librosa에서 가져온 분석 코드

원본: https://github.com/librosa/librosa

기준 커밋: `08e2c0a20d650fe00f10aaf3a1eaa9698ac19641`

ISC 라이선스 원문은 `LICENSE.md`, 원본 파일의 URL과 해시는
`UPSTREAM.json`에 있습니다.

`vendor/librosa_analysis.py`는 YIN 음높이 분석, FFT 자기상관과 Slaney mel
필터를 NumPy로 실행하도록 수정한 코드입니다. `vendor/librosa_tempo.py`는
onset strength, tempogram과 tempo 계산을 NumPy로 실행하도록 수정했습니다.
전체 librosa API를 제공하지 않으며 SciPy·Numba를 요구하지 않습니다.

`tempo-mel.npy`는 22,050 Hz, FFT 1024, 64-band Slaney mel 계수입니다.
실시간 분석의 입력 관리와 샘플레이트 변환은 `tempo_analysis.py`가 담당합니다.
템포는 반 박자·두 배 박자로 해석될 수 있습니다.
