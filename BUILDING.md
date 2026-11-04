# 소스 실행

## 준비 도구

- Windows x64와 Python 3.14
- PowerShell 7
- Visual Studio C++ x64/x86 빌드 도구와 Windows SDK

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r dev/requirements-abmsynth.txt
```

## 외부 라이브러리와 자산

[DEPENDENCIES.json](DEPENDENCIES.json)의 `runtime_files`에는 필요한 파일의
버전·배치 경로·SHA-256이 있습니다. 같은 버전 포터블 프로그램을 풀면
`source` 경로의 파일을 `target` 경로로 복사할 수 있습니다.
ABMSynth 자체 DLL은 아래 명령으로 빌드할 수도 있습니다.
외부 라이브러리를 별도로 준비할 때에는 각 제공자의 사용 조건을 확인하세요.

BASS와 BASSMIDI SDK는 [Un4seen](https://www.un4seen.com/bass.html)에서 받습니다.
`c/bass.h`와 `c/bassmidi.h`를 저장소 루트에 둡니다.
헤더의 기준 해시는 `sdk_headers`에 있습니다.

기본 음원은 [FluidR3 GM 3.1 원본 배포](https://deb.debian.org/debian/pool/main/f/fluid-soundfont/fluid-soundfont_3.1.orig.tar.gz)의
`fluid-soundfont-3.1/FluidR3_GM.sf2`입니다.
`dev/assets/soundfonts/FluidR3.sf2`에 둡니다.
SHA-256은 `74594e8f4250680adf590507a306655a299935343583256f3b722c48a1bc1cb0`입니다.
음원의 원래 라이선스는 `dev/licenses/fluidr3`에 있습니다.

## 자체 DLL 빌드

```powershell
pwsh -NoProfile -File dev/native_dsp/build.ps1
pwsh -NoProfile -File dev/native_dsp/build_tempo_capture.ps1
pwsh -NoProfile -File driver/winmm/build.ps1 -Configuration Release -Architecture x64 -Mapper
pwsh -NoProfile -File driver/winmm/build.ps1 -Configuration Release -Architecture x86 -Mapper
```

DSP 빌드는 결과 DLL을 `dev`에 배치합니다. MIDI 드라이버의 결과는
`driver/winmm/out`에 생성됩니다. 드라이버 등록은 앱의 환경설정에서 진행하며
Windows 관리자 승인이 필요합니다. 외부 MIDI 포트를 이용할 때에는 자체
드라이버를 설치하지 않아도 됩니다.

## 실행

```powershell
cd dev
python app.py
```

소스 실행의 설정은 `local/profile/ABMSynth.ini`에 저장됩니다.
이 안내는 소스 실행과 자체 DLL 빌드를 다룹니다. 외부 라이브러리의 소스와
빌드 자료는 [THIRD_PARTY_SOURCES.md](THIRD_PARTY_SOURCES.md)를 참고하세요.
