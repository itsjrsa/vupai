# vupai brand assets

Accent: **teal `#14b8a6`** · Ink: `#0f1115`

The waveform/equalizer bars read as both a voice waveform and side-by-side panes.
Text in the lockups is converted to vector paths, so the SVGs render anywhere
without JetBrains Mono installed.

## Files

| File | Use |
|------|-----|
| `vupai-lockup.svg` / `.png` | Primary lockup, for light backgrounds (ink wordmark) |
| `vupai-lockup-dark.svg` / `.png` | Lockup for dark backgrounds (white wordmark) |
| `vupai-icon.svg` / `.png` | Standalone app icon / avatar (512px PNG) |
| `favicon.svg` | Scalable favicon (same as icon) |
| `favicon-180.png` | Apple touch icon |
| `favicon-32.png` / `favicon-16.png` | Classic favicons |

PNGs have transparent backgrounds. The lockup canvas is 196×60 (≈3.27:1).

## HTML

```html
<link rel="icon" href="/assets/brand/favicon.svg" type="image/svg+xml">
<link rel="icon" href="/assets/brand/favicon-32.png" sizes="32x32">
<link rel="apple-touch-icon" href="/assets/brand/favicon-180.png">
```

## Regenerating

Source generator: text is flattened from `JetBrainsMono-Bold.ttf` via `fonttools`
(`SVGPathPen`). Re-run the generator script if the wordmark or geometry changes,
then re-export PNGs with headless Chrome and downscale favicons with `sips`.
