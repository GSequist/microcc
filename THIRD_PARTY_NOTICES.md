# Third-party notices

Portions of micro-cc derive from the following projects.

## markitdown
`src/micro_cc/browser/_md_convert.py` — Copyright (c) Microsoft Corporation. https://github.com/microsoft/markitdown

## smolagents and AutoGen
`src/micro_cc/browser/simpletextbrowser.py` is adapted from `text_web_browser.py` in Hugging Face's smolagents (Apache License 2.0, https://github.com/huggingface/smolagents; license text at https://www.apache.org/licenses/LICENSE-2.0), which was itself adapted from AutoGen's `browser_utils.py` — Copyright (c) Microsoft Corporation, MIT, https://github.com/microsoft/autogen. Modified for micro-cc; the sample cookie list shipped with the upstream browser was removed.

## Anthropic skills
`src/micro_cc/skills/design`, `docx`, `xlsx` and `pptx` are derived from skills published by Anthropic (https://github.com/anthropics/skills), adapted for micro-cc. Anthropic retains rights in the original material; refer to that repository for its terms.

## painted-video renderer
`src/micro_cc/skills/painted-video/` — see `LICENSE-lib` in that folder.

## hairline-create
`src/micro_cc/skills/hairline-create/` is adapted from the `hairline-create` skill and the `@lucasmarkes/hairline` engine by Lucas Marques (https://github.com/lucasmarkes/hairline, MIT; see `LICENSE-lib` in that folder). `kernel.js`, `bench.html`, `look.mjs`, the ten rules and the `terrain` and `riffle` examples are his. micro-cc adds `free.js` (free-form drawing layer), `stamp.mjs`, the `contour` example, and the changes to `build.mjs`, `validate.mjs` and the skill prose that let figures leave the isometric style.

## Fonts
`src/micro_cc/skills/design/canvas-fonts/` — each family ships with its SIL Open Font License text (`*-OFL.txt`).

---

MIT License terms for the Microsoft projects above:

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
