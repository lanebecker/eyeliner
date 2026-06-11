// Abstract album-cover placeholders. Original geometric/typographic
// compositions tinted to each record's palette — NOT recreations of the
// real artwork.

function CoverSister({ size = 320 }) {
  const s = size;
  return (
    <svg width={s} height={s} viewBox="0 0 320 320" style={{ display: 'block' }}>
      <rect width="320" height="320" fill="#3a4228" />
      <rect width="320" height="180" fill="#2e3520" />
      <rect y="180" width="320" height="60" fill="#4a5030" />
      {/* faint religious-painting halo wash */}
      <radialGradient id="cs-halo" cx="0.5" cy="0.55" r="0.55">
        <stop offset="0" stopColor="#a08f5a" stopOpacity="0.35" />
        <stop offset="1" stopColor="#000" stopOpacity="0" />
      </radialGradient>
      <rect width="320" height="320" fill="url(#cs-halo)" />
      <ellipse cx="160" cy="160" rx="78" ry="98" fill="#1e2416" opacity="0.55" />
      <ellipse cx="160" cy="140" rx="50" ry="56" fill="#0e120b" opacity="0.85" />
      <ellipse cx="160" cy="128" rx="26" ry="30" fill="#5a5e3a" opacity="0.55" />
      <rect y="246" width="320" height="74" fill="#141810" opacity="0.85" />
      <text x="160" y="282" textAnchor="middle" fontFamily="Newsreader, Georgia, serif"
        fontStyle="italic" fontSize="22" letterSpacing="6" fill="#c8c4a0" opacity="0.92">Sister</text>
      <text x="160" y="304" textAnchor="middle" fontFamily="ui-sans-serif, system-ui"
        fontSize="9" letterSpacing="3" fill="#9a9a7a" opacity="0.75">SONIC YOUTH</text>
    </svg>
  );
}

function CoverBushOfGhosts({ size = 320 }) {
  const s = size;
  return (
    <svg width={s} height={s} viewBox="0 0 320 320" style={{ display: 'block' }}>
      <rect width="320" height="320" fill="#3a1f15" />
      {/* warm vignette */}
      <radialGradient id="cb-r" cx="0.5" cy="0.4" r="0.7">
        <stop offset="0" stopColor="#e07a3b" stopOpacity="0.55" />
        <stop offset="0.6" stopColor="#3a1f15" stopOpacity="0" />
      </radialGradient>
      <rect width="320" height="320" fill="url(#cb-r)" />
      {/* concentric ritual circles */}
      {[112, 92, 70, 50, 32].map((r, i) => (
        <circle key={i} cx="160" cy="148" r={r}
          fill="none" stroke="#e07a3b" strokeWidth={i === 2 ? 1.5 : 0.6} opacity={0.18 + i * 0.08} />
      ))}
      <circle cx="160" cy="148" r="14" fill="#e07a3b" opacity="0.85" />
      {/* horizontal type bars */}
      <rect x="32" y="262" width="120" height="2" fill="#e07a3b" opacity="0.8" />
      <rect x="32" y="284" width="180" height="1" fill="#e07a3b" opacity="0.5" />
      <text x="32" y="258" fontFamily="ui-monospace, Menlo, monospace"
        fontSize="9" letterSpacing="2" fill="#f0d9b6" opacity="0.8">B. ENO  /  D. BYRNE</text>
      <text x="32" y="300" fontFamily="ui-monospace, Menlo, monospace"
        fontSize="14" letterSpacing="1" fill="#f0d9b6">MY LIFE IN THE</text>
      <text x="32" y="316" fontFamily="ui-monospace, Menlo, monospace"
        fontSize="14" letterSpacing="1" fill="#f0d9b6">BUSH OF GHOSTS</text>
    </svg>
  );
}

function CoverScissors({ size = 320 }) {
  const s = size;
  return (
    <svg width={s} height={s} viewBox="0 0 320 320" style={{ display: 'block' }}>
      <defs>
        <linearGradient id="cv-g" x1="0" x2="0" y1="0" y2="1">
          <stop offset="0" stopColor="#a6dad5" />
          <stop offset="1" stopColor="#2d4d54" />
        </linearGradient>
      </defs>
      <rect width="320" height="320" fill="url(#cv-g)" />
      {/* pastel sun */}
      <circle cx="240" cy="86" r="46" fill="#f6c9b4" opacity="0.9" />
      <circle cx="240" cy="86" r="46" fill="none" stroke="#fff" strokeOpacity="0.4" strokeWidth="1" />
      {/* hand-doodle horizon line */}
      <path d="M0 210 Q 80 196 160 210 T 320 208" fill="none" stroke="#fff" strokeOpacity="0.55" strokeWidth="1.5" />
      <path d="M0 232 Q 100 220 200 232 T 320 230" fill="none" stroke="#fff" strokeOpacity="0.25" strokeWidth="1" />
      {/* scribbled title */}
      <text x="22" y="56" fontFamily="Caveat, Bradley Hand, cursive"
        fontSize="38" fill="#fff" opacity="0.95">running with</text>
      <text x="22" y="100" fontFamily="Caveat, Bradley Hand, cursive"
        fontSize="38" fill="#fff" opacity="0.95">scissors</text>
      <text x="22" y="298" fontFamily="ui-sans-serif, system-ui"
        fontSize="10" letterSpacing="3" fill="#fff" opacity="0.7">CAVETOWN · 2024</text>
    </svg>
  );
}

function CoverRepeater({ size = 320 }) {
  const s = size;
  return (
    <svg width={s} height={s} viewBox="0 0 320 320" style={{ display: 'block' }}>
      <rect width="320" height="320" fill="#0e0e10" />
      {/* xerox grain band */}
      <rect y="58" width="320" height="204" fill="#1a1a1a" />
      {/* red mark */}
      <rect x="24" y="80" width="84" height="84" fill="#d24234" />
      <rect x="24" y="80" width="84" height="84" fill="none" stroke="#0e0e10" strokeWidth="2" strokeDasharray="2 3" opacity="0.4" />
      <text x="116" y="118" fontFamily="ui-monospace, Menlo, monospace"
        fontWeight="700" fontSize="34" fill="#ebe6dc" letterSpacing="-1">FUGAZI</text>
      <text x="116" y="148" fontFamily="ui-monospace, Menlo, monospace"
        fontSize="14" fill="#ebe6dc" opacity="0.7" letterSpacing="2">REPEATER</text>
      {/* stamped catalog */}
      <text x="24" y="220" fontFamily="ui-monospace, Menlo, monospace"
        fontSize="10" fill="#d24234" letterSpacing="3">DISCHORD 45 · 1990</text>
      <line x1="24" y1="232" x2="296" y2="232" stroke="#ebe6dc" strokeOpacity="0.2" strokeWidth="0.5" />
      <text x="24" y="252" fontFamily="ui-monospace, Menlo, monospace"
        fontSize="9" fill="#ebe6dc" opacity="0.55" letterSpacing="2">SIDE A · SIDE B</text>
    </svg>
  );
}

function CoverBachelor({ size = 320 }) {
  const s = size;
  return (
    <svg width={s} height={s} viewBox="0 0 320 320" style={{ display: 'block' }}>
      <defs>
        <linearGradient id="cb2-g" x1="0" x2="1" y1="0" y2="1">
          <stop offset="0" stopColor="#2a3142" />
          <stop offset="1" stopColor="#1a1f28" />
        </linearGradient>
      </defs>
      <rect width="320" height="320" fill="url(#cb2-g)" />
      {/* moody soft circle / portrait blur */}
      <circle cx="160" cy="150" r="78" fill="#9cb3d6" opacity="0.15" />
      <circle cx="160" cy="150" r="50" fill="#9cb3d6" opacity="0.18" />
      <circle cx="160" cy="150" r="24" fill="#9cb3d6" opacity="0.3" />
      {/* serif title */}
      <text x="160" y="252" textAnchor="middle" fontFamily="Newsreader, Georgia, serif"
        fontStyle="italic" fontSize="26" fill="#dde3ed">Aimee Mann</text>
      <line x1="100" y1="266" x2="220" y2="266" stroke="#9cb3d6" strokeOpacity="0.4" strokeWidth="0.5" />
      <text x="160" y="290" textAnchor="middle" fontFamily="Newsreader, Georgia, serif"
        fontSize="14" fill="#9cb3d6" opacity="0.85" letterSpacing="3">BACHELOR No. 2</text>
    </svg>
  );
}

const COVERS = {
  sister: CoverSister,
  bushofghosts: CoverBushOfGhosts,
  scissors: CoverScissors,
  repeater: CoverRepeater,
  bachelor: CoverBachelor,
};

// Unified album-cover renderer. If album.cover is set, render the real image;
// otherwise fall back to the abstract SVG placeholder for that album id. Used
// by all directions so dropping in real artwork is a one-line change per album.
function AlbumCover({ album, size = 320 }) {
  if (album && album.cover) {
    return (
      <img src={album.cover} alt={`${album.artist} — ${album.album}`} width={size} height={size}
        style={{ display: 'block', width: size, height: size, objectFit: 'cover' }} />
    );
  }
  const C = COVERS[album && album.id];
  return C ? <C size={size} /> : (
    <div style={{ width: size, height: size, background: '#222' }} />
  );
}

Object.assign(window, { COVERS, AlbumCover });
