# 기여 안내

ABMSynth의 오류 수정과 개선 제안을 환영합니다.

## 오류 보고

[Issues](https://github.com/ruain-dev/ABMSynth/issues)에 재현 순서, 기대한 결과,
실제 결과와 Windows 버전을 적어 주세요. 오디오 문제에는 출력 방식과
장치 이름을 함께 적으면 도움이 됩니다. 로그나 샘플을 첨부하기 전에는
개인정보와 해당 파일의 공유 권한을 확인하세요.

## 소스 변경

개발 환경은 [빌드 안내](BUILDING.md)를 참고하세요. 한 변경에는 하나의
목적을 담고, 동작이 달라지는 부분과 확인한 방법을 설명해 주세요.
효과 이름, MIDI 단위와 화면 용어는 기존 조절부와 일관되게 사용합니다.
외부 구성요소를 추가하면 출처와 원래 라이선스를 함께 제공하세요.

커밋 제목은 `fix(player): Resume playback without repeated notes`처럼
[Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) 형식을
사용합니다. `feat`, `fix`, `docs`, `test`, `build`, `refactor`, `perf`,
`chore` 중 목적에 맞는 종류를 고릅니다.

기여하는 코드에는 [LICENSE](LICENSE)와
[ADDITIONAL_PERMISSIONS.md](ADDITIONAL_PERMISSIONS.md)가 적용됩니다.
외부 구성요소의 권리는 각 원래 라이선스를 따릅니다.

## 버전

ABMSynth는 `X.Y.Z` 형식을 사용합니다. 일반 수정·개선과 작은 편의 기능은
세 번째 자리를 올립니다. 주요 기능군·핵심 작업 흐름의 유의미한 확장은
두 번째 자리, 대규모 전환이나 중요한 호환성 파괴는 첫 번째 자리 변경을
검토합니다. 개별 기능이나 커밋 수로 번호를 자동 결정하지 않습니다.
개발 중에는 `-dev`를 붙이고, 배포 시 제거합니다.
