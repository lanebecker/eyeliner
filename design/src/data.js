// Album metadata + extracted palettes for the now-playing display.
// Covers are original abstract placeholders, not the real artwork.
//
// palette = [bg, surface, accent, text, muted]
//   bg      — main background tint (dark, ~oklch L 0.15-0.22)
//   surface — slightly lighter card tone
//   accent  — bright extracted color (track name, badges)
//   text    — primary text color (near-white, tinted)
//   muted   — secondary text color
//
// NOTE (PR-4): these accents were HAND-CORRECTED for ≥60° OKLCH separation
// across albums, and a few muted values nudged to clear 4.5:1 vs bg (see the
// inline comments below).  They are REFERENCE TARGETS for the look, not an
// implementation.  The guarantees must come from production code, not this
// hand-tuned data: muted-contrast is enforced by `src/display/palette.py`
// (extract_palette / ensure_contrast); cross-album Hue-Diversity is the
// ASPIRATIONAL A-1 (no cross-album OKLCH registry exists in code yet — see
// CLAUDE.md / DESIGN.md §2).

const ALBUMS = [
  {
    id: 'sister',
    artist: 'Sonic Youth',
    album: 'Sister',
    cover: 'covers/sister.png',
    track: 'Catholic Block',
    side: 'A',
    position: 4,
    sideTracks: 6,
    year: 1987,
    label: 'SST Records',
    catalog: 'SST-134',
    genres: ['Noise Rock', 'Alt Rock', 'Post-Punk'],
    duration: 178, // seconds — for visual progress only
    elapsed: 92,
    prev: { track: 'Stereo Sanctity', side: 'A', position: 3 },
    next: { track: 'Beauty Lies in the Eye', side: 'A', position: 5 },
    palette: {
      bg: '#1d1822',
      surface: '#2c2530',
      accent: '#e58a52',
      text: '#f0e4cf',
      muted: '#8c7e88',
    },
  },
  {
    id: 'bushofghosts',
    artist: 'Brian Eno & David Byrne',
    album: 'My Life in the Bush of Ghosts',
    cover: 'covers/bushofghosts.jpg',
    track: 'Regiment',
    side: 'A',
    position: 2,
    sideTracks: 5,
    year: 1981,
    label: 'Sire / E.G.',
    catalog: 'SRK 6093',
    genres: ['Art Rock', 'Worldbeat', 'Experimental'],
    duration: 220,
    elapsed: 47,
    prev: { track: 'America Is Waiting', side: 'A', position: 1 },
    next: { track: 'Help Me Somebody', side: 'A', position: 3 },
    palette: {
      bg: '#1d1830',
      surface: '#2a2240',
      accent: '#b88adc',   // OKLCH ~290° (violet) — was orange; corrected per Hue Diversity Rule
      text: '#ece4f0',
      muted: '#8a8296',
    },
  },
  {
    id: 'scissors',
    artist: 'Cavetown',
    album: 'Running with Scissors',
    cover: 'covers/scissors.png',
    track: 'Frog',
    side: 'B',
    position: 2,
    sideTracks: 6,
    year: 2024,
    label: 'Sire',
    catalog: '093624852575',
    genres: ['Bedroom Pop', 'Indie Folk'],
    duration: 196,
    elapsed: 124,
    prev: { track: 'A Kind Thing to Do', side: 'B', position: 1 },
    next: { track: 'Worm Food', side: 'B', position: 3 },
    palette: {
      bg: '#0e1a2a',
      surface: '#162a3c',
      accent: '#e080b0',   // OKLCH ~345° (dusty rose) — was orange; corrected per Hue Diversity Rule
      text: '#e4ecf4',
      muted: '#718499',   // lightened from #6e8195 — was 4.36:1, now 4.55:1 on bg (#0e1a2a)
    },
  },
  {
    id: 'repeater',
    artist: 'Fugazi',
    album: 'Repeater',
    cover: 'covers/repeater.jpg',
    track: 'Merchandise',
    side: 'A',
    position: 4,
    sideTracks: 6,
    year: 1990,
    label: 'Dischord Records',
    catalog: 'Dischord 45',
    genres: ['Post-Hardcore', 'Punk'],
    duration: 175,
    elapsed: 30,
    prev: { track: 'Brendan #1', side: 'A', position: 3 },
    next: { track: 'Blueprint', side: 'A', position: 5 },
    palette: {
      bg: '#0f1a24',
      surface: '#1a2632',
      accent: '#b5cee2',
      text: '#e6edf4',
      muted: '#7a8a98',
    },
  },
  {
    id: 'bachelor',
    artist: 'Aimee Mann',
    album: 'Bachelor No. 2 or, the Last Remains of the Dodo',
    cover: 'covers/bachelor.jpg',
    track: 'Deathly',
    side: 'A',
    position: 2,
    sideTracks: 6,
    year: 2000,
    label: 'SuperEgo Records',
    catalog: 'SE-002',
    genres: ['Singer-Songwriter', 'Chamber Pop'],
    duration: 247,
    elapsed: 164,
    prev: { track: 'How Am I Different', side: 'A', position: 1 },
    next: { track: 'Save Me', side: 'A', position: 3 },
    palette: {
      bg: '#26211a',
      surface: '#332c22',
      accent: '#a3b25a',
      text: '#ebe2cb',
      muted: '#918876',   // lightened from #8c8472 — was 4.30:1, now 4.55:1 on bg (#26211a)
    },
  },
];

const FALLBACK_PALETTE = {
  bg: '#0a0a0a',
  surface: '#161616',
  accent: '#c8c8c8',
  text: '#ebe6dc',
  muted: '#8a857c',
};

// Hex+alpha helper, lifted to global so any direction can use it without
// depending on another direction file being loaded.
function hexA(hex, a) {
  const h = hex.replace('#', '');
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${a})`;
}

Object.assign(window, { ALBUMS, FALLBACK_PALETTE, hexA });
