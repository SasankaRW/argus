"""A tiny random Whisper built offline (tests of the voice training code; run with exec)."""

from pathlib import Path


def tiny_whisper(d: Path) -> None:
    """A tiny random Whisper with a byte-level tokenizer, built offline (only to run the real training code)."""
    import json

    from transformers import (
        WhisperConfig,
        WhisperFeatureExtractor,
        WhisperForConditionalGeneration,
        WhisperTokenizer,
        WhisperTokenizerFast,
    )

    bs = list(range(ord("!"), ord("~") + 1)) + list(range(ord("¡"), ord("¬") + 1)) + list(range(ord("®"), ord("ÿ") + 1))
    cs, n = bs[:], 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    d.mkdir(parents=True)
    (d / "vocab.json").write_text(json.dumps({chr(c): i for i, c in enumerate(cs)}))
    (d / "merges.txt").write_text("#version: 0.2\n")
    sp = ["<|startoftranscript|>", "<|en|>", "<|translate|>", "<|transcribe|>", "<|startoflm|>", "<|startofprev|>",
          "<|nocaptions|>", "<|notimestamps|>"]
    tok = WhisperTokenizer(str(d / "vocab.json"), str(d / "merges.txt"), unk_token="<|endoftext|>",
                           bos_token="<|endoftext|>", eos_token="<|endoftext|>", pad_token="<|endoftext|>")
    tok.add_special_tokens({"additional_special_tokens": sp})
    tok.save_pretrained(d)
    fast = WhisperTokenizerFast.from_pretrained(d)
    fast.save_pretrained(d)
    WhisperFeatureExtractor(feature_size=80).save_pretrained(d)
    ids = {t: fast.convert_tokens_to_ids(t) for t in ["<|endoftext|>", *sp]}
    cfg = WhisperConfig(vocab_size=len(fast), d_model=64, encoder_layers=1, decoder_layers=1, encoder_attention_heads=2,
                        decoder_attention_heads=2, encoder_ffn_dim=128, decoder_ffn_dim=128, num_mel_bins=80,
                        max_target_positions=448, decoder_start_token_id=ids["<|startoftranscript|>"],
                        eos_token_id=ids["<|endoftext|>"], pad_token_id=ids["<|endoftext|>"],
                        bos_token_id=ids["<|endoftext|>"])
    m = WhisperForConditionalGeneration(cfg)
    m.generation_config.decoder_start_token_id = cfg.decoder_start_token_id
    m.generation_config.no_timestamps_token_id = ids["<|notimestamps|>"]
    m.save_pretrained(d)
