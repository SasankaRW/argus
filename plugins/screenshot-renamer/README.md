# screenshot-renamer

Renames new screenshots in `Pictures\Screenshots` (and `Screenshot*.png` on the Desktop; OneDrive copies too) from
"Screenshot 2026-09-28 214501.png" to "2026-09-28 cashly login bug.png", in the same folder.

- **Text route:** Tesseract reads the text on screen, T1 turns it into 3-6 words (T2 if T1's name fails the check).
- **Picture route:** little or no text, or no Tesseract: the vision model (V1, `qwen2.5vl:7b`) looks at the image,
  shrunk to 1280 px.
- The name is checked in code (3-6 words, not generic, no dates); the date comes from the original name.
- Only default names (Screenshot..., Capture..., image...) are touched. Renames never overwrite; Undo in Helios.
- Starts in dry-run; add `screenshot-renamer` under `plugins.live` in argus.yaml.

Setup on the PC:

    ollama pull qwen2.5vl:7b
    # argus.yaml, under models.tiers:
    #   V1: {provider: ollama, model: "qwen2.5vl:7b", group: pc}
    winget install UB-Mannheim.TesseractOCR     # optional: faster text route

Pillow and pytesseract come with `dev.ps1 setup` (the `plugins` extra).
