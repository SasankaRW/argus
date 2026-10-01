// Ari's replies carry a mood and sounds for its voice ("[cheerful] Oh nice! [laugh]"; argus/expressive.py).
// On screen, and for voices that can't laugh, only the words.
const TAGS = /\s*\[(?:neutral|cheerful|excited|playful|calm|sympathetic|serious|laugh|laughs|chuckle|chuckles|sigh|sighs|gasp|groan|clear throat)\]\s*/gi;
export function plain(text: string): string {
  return text.replace(TAGS, " ").replace(/\s+([.,!?])/g, "$1").replace(/\s{2,}/g, " ").trim();
}
