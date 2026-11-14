# Third-party code and models in Argus

## Smart Turn v3 (Daily / pipecat-ai)

- core/argus/whisper_features.py is vendored from pipecat-ai 1.12.0.
- The Smart Turn v3.2 CPU model (smart-turn-v3.2-cpu.onnx) is downloaded on first use from the pipecat-ai 1.12.0 wheel on PyPI (checked by SHA-256).

```
BSD 2-Clause License

Copyright (c) 2024–2026, Daily

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

## Inter (Rasmus Andersson / The Inter Project Authors)

- Helios no longer uses Inter (it uses Roboto Flex, below). The official `InterVariable.woff2` from the rsms/inter release stays in
  helios/src/fonts only as the source `scripts/island_fonts.py` cuts the island's fonts from; it is not built into core/argus/helios_dist.
  Licence text: helios/src/fonts/LICENSE-Inter.txt.
- Ari's island on the PC (core/argus/ari_popup.py) draws with four cut-down static instances of the same font
  (core/argus/island_fonts/Inter-*.ttf, made by scripts/island_fonts.py: weights 400, 500, 600 and a 640 display cut, with
  `ss01` and `cv11` baked in). The font is renamed per file (Inter, Inter Medium, Inter SemiBold, Inter Display);
  the licence text above travels with it (the OFL allows modified versions without a Reserved Font Name; Inter has none).
- Licence: SIL Open Font License 1.1 (https://openfontlicense.org). Copyright 2016 The Inter Project Authors (https://github.com/rsms/inter).

## Roboto Flex (The Roboto Flex Project Authors)

- Helios (phone and desktop) uses the variable Roboto Flex with its width axis (wght 100-1000, wdth 25-151), the latin subset
  `roboto-flex-latin-wdth-normal.woff2` from the npm package `@fontsource-variable/roboto-flex` 5.3.0, copied unchanged to
  helios/src/fonts/RobotoFlex-wdth.woff2 and built into core/argus/helios_dist. Licence text: helios/src/fonts/LICENSE-RobotoFlex.txt.
- Licence: SIL Open Font License 1.1 (https://openfontlicense.org). Copyright 2017 The Roboto Flex Project Authors (https://github.com/googlefonts/roboto-flex).

## Roboto Mono (The Roboto Mono Project Authors)

- Helios uses the variable Roboto Mono (wght 100-700) for rates, durations, timeline ticks and the Logs page: the latin subset
  `roboto-mono-latin-wght-normal.woff2` from `@fontsource-variable/roboto-mono` 5.3.0, copied unchanged to
  helios/src/fonts/RobotoMono-wght.woff2. Licence text: helios/src/fonts/LICENSE-RobotoMono.txt.
- Licence: SIL Open Font License 1.1 (https://openfontlicense.org). Copyright 2015 The Roboto Mono Project Authors (https://github.com/googlefonts/robotomono).

## Material Symbols Rounded (Google)

- Helios (phone and desktop) uses a cut-down copy of Material Symbols Rounded (helios/src/fonts/symbols-rounded.woff2, made by
  helios/scripts/icons.py from the npm package `material-symbols`). The island on the PC uses two static cuts of it
  (core/argus/island_fonts/Symbols-Rounded.ttf, outlined, and Symbols-Rounded-Filled.ttf, made by scripts/island_fonts.py).
- Licence: Apache License 2.0 (https://www.apache.org/licenses/LICENSE-2.0). Copyright Google LLC.
