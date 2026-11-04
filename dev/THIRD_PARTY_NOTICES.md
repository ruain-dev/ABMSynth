# ABMSynth third-party notices

External components retain their own licences and copyrights. ABMSynth's
LICENSE and ADDITIONAL_PERMISSIONS.md do not replace or restrict those terms.
The offline licence reader uses licenses/catalog.json. Each document is
verified against its reviewed SHA-256 before display.

## Runtime components

- FluidR3 GM 3.1 by Frank Wen is included as the default SoundFont under MIT.
  The original COPYING and README are in licenses/fluidr3. The distributed
  FluidR3.sf2 has the same bytes as the original FluidR3_GM.sf2.

- BASS, BASSMIDI, BASSASIO, BASSWASAPI, BASSloud, BASSenc and encoders:
  complete original notices are in licenses/audio. The free BASS/BASSASIO
  route applies to a non-commercial developer whose product does not earn
  money through sales, advertising or similar product revenue. Other uses
  require the corresponding Un4seen licence.
- BASS FX: the original author permits credited commercial use. Its notice
  also identifies SoundTouch 2.0.0 under LGPL. Original SoundTouch source and
  licence and the intact official BASS FX package are supplied separately.
  BASS FX remains the unmodified official binary. Library rights remain
  governed by the original notices,not ABMSynth's own-code licence.
- Qt, PySide6 and Shiboken6: the deployed dynamic libraries use the LGPL route.
  licenses/qt includes LGPLv3, the incorporated GPLv3 text and source-module
  third-party notice collections. The collections preserve original file
  contents; they do not claim that every platform-specific source component
  is compiled into the Windows libraries. The source archive preserves full
  module sources and per-file copyright/SPDX records.
- FFmpeg 7.1.5: LGPL-2.1-or-later build, without enable-gpl or enable-nonfree.
  The licence, build configuration and zlib 1.3.1 notice are included.
- Python, NumPy (including OpenBLAS), PyChord and the adapted librosa analysis
  subsets retain their original notices in the corresponding licence folders.
- libffi, bzip2, Expat, liblzma, OpenSSL, zlib, Zstandard and libmpdec have
  individual original notices in licenses/runtime. The separately deployed
  zlib DLL is version 1.3.2; it is distinct from FFmpeg's zlib 1.3.1 build input.
  liblzma uses the 0BSD route; unrelated XZ command-line script licences do
  not establish the licence of this DLL. Zstandard uses its BSD option.
- Microsoft Visual C++ and Universal C Runtime files retain Microsoft terms.
  These are unmodified redistributable runtime files, not Microsoft source
  covered by ABMSynth's licence. Their licence texts are included.
- Mesa llvmpipe / software OpenGL notices and related Qt LLVM attribution
  are retained under licenses/runtime/mesa. PyInstaller's original bootloader
  exception is included under licenses/runtime/pyinstaller.
- Noto Sans KR retains the SIL Open Font License. MIDI-mode artwork and
  marks retain their separate attribution and rights notices.

## Sources and replacement rights

The matching release must publish ABMSynth-1.0.0-ThirdPartySources.zip beside
its binary assets and SHA256SUMS.txt. It contains version-matched upstream
Qt/PySide, FFmpeg, zlib, SoundTouch sources, available build recipes and the
BASSenc_MP3 source archive. See THIRD_PARTY_SOURCES.md in the release root.
Upstream release sources are not a claim of byte-identical binary reproduction
or proof that undocumented vendor modifications have been supplied.

Users retain the external libraries' modification, replacement, relinking and
debugging rights. The application does not block replacing its dynamic DLLs.
User-supplied SoundFonts, samples and impulse responses retain separate rights.

Official references:
- https://www.un4seen.com/bass.html#license
- https://www.un4seen.com/bassasio.html#license
- https://www.qt.io/development/open-source-lgpl-obligations
- https://ffmpeg.org/legal.html
- https://www.surina.net/soundtouch/license.html
- https://learn.microsoft.com/en-us/visualstudio/releases/2022/redistribution
- https://pyinstaller.org/en/stable/license.html
