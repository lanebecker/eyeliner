// Direction A — "Museum Card" + variants.
// prefers-reduced-motion: checked once at module load; inline styles reference this.
const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
// All variants share the same type hierarchy and palette logic — the
// changes are layout (cover position, sleeve frame, accent block,
// chrome density). Pass `variant` to switch.
//
// Variants:
//   default — cover left, meta right (the canonical version)
//   right   — mirror: cover right, meta left
//   block   — accent-tinted column behind meta panel
//   sleeve  — cover offset inside a record-sleeve-style inner frame
//   compact — no top status strip; eyebrow lives inline next to song

function DirectionA({
  album, state = 'playing', showAdjacent = false, themed = true,
  variant = 'default',
}) {
  const p = themed ? album.palette : window.FALLBACK_PALETTE;
  const isEmpty = state === 'idle' || state === 'boot' || state === 'error';
  const mirror = variant === 'right';
  const showTopStrip = variant !== 'compact';
  const hasBlock = variant === 'block';
  const hasSleeve = variant === 'sleeve';

  // Cover element (real image or placeholder)
  const coverNode = isEmpty
    ? <DirAEmptyCover state={state} p={p} />
    : <window.AlbumCover album={album} size={440} />;

  return (
    <div style={{
      width: 1024, height: 600, position: 'relative',
      background: p.bg, color: p.text, overflow: 'hidden',
      fontFamily: '"Inter Tight", "DejaVu Sans", Arial, sans-serif',
    }}>
      {/* subtle radial light */}
      <div style={{
        position: 'absolute', inset: 0,
        background: `radial-gradient(60% 70% at ${mirror ? 75 : 25}% 35%, ${p.surface} 0%, ${p.bg} 65%)`,
      }} />

      {/* accent block behind meta column (block variant only) */}
      {hasBlock && (
        <div style={{
          position: 'absolute',
          top: 0, bottom: 0,
          left: mirror ? 0 : 534, right: mirror ? 534 : 0,
          background: `linear-gradient(${mirror ? 90 : 270}deg, ${window.hexA(p.accent, 0.0)} 0%, ${window.hexA(p.accent, 0.10)} 60%, ${window.hexA(p.accent, 0.16)} 100%)`,
        }} />
      )}

      {/* status strip */}
      {showTopStrip && (
        <div aria-live="polite" style={{
          position: 'absolute', top: 0, left: 0, right: 0, height: 30,
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          padding: '0 26px', gap: 16,
          fontFamily: '"JetBrains Mono", ui-monospace, monospace',
          fontSize: 11, letterSpacing: '0.16em', textTransform: 'uppercase',
          color: p.muted, whiteSpace: 'nowrap',
        }}>
          <DirAStatus state={state} accent={p.accent} muted={p.muted} />
          {!isEmpty && (
            <span style={{ whiteSpace: 'nowrap' }}>SIDE {album.side} · {String(album.position).padStart(2, '0')} OF {String(album.sideTracks).padStart(2, '0')}</span>
          )}
        </div>
      )}

      <div style={{
        position: 'absolute',
        inset: showTopStrip ? '60px 50px 40px 50px' : '40px 50px 40px 50px',
        display: 'grid',
        gridTemplateColumns: mirror ? '1fr 440px' : '440px 1fr',
        gap: 44,
      }}>
        {/* COVER */}
        <div style={{
          gridColumn: mirror ? 2 : 1,
          width: 440, height: 440, position: 'relative',
          padding: hasSleeve ? 14 : 0,
          background: hasSleeve ? p.surface : 'transparent',
          boxShadow: hasSleeve
            ? '0 30px 60px rgba(0,0,0,0.55)'
            : '0 30px 60px rgba(0,0,0,0.55), 0 0 0 1px rgba(255,255,255,0.04)',
        }}>
          <div style={{
            width: hasSleeve ? 412 : 440,
            height: hasSleeve ? 412 : 440,
            boxShadow: hasSleeve ? '0 0 0 1px rgba(0,0,0,0.4) inset' : 'none',
          }}>
            {React.cloneElement(coverNode, { size: hasSleeve ? 412 : 440 })}
          </div>
          {hasSleeve && (
            <div style={{
              position: 'absolute', bottom: 0, left: 14, right: 14, height: 16,
              fontFamily: '"JetBrains Mono", monospace',
              fontSize: 8, letterSpacing: '0.24em',
              color: p.muted,
              display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            }}>
              <span>{album.catalog}</span>
              <span>SIDE {album.side}</span>
            </div>
          )}
        </div>

        {/* META */}
        <div style={{
          gridColumn: mirror ? 1 : 2,
          gridRow: 1,
          display: 'flex', flexDirection: 'column',
          paddingTop: 6, minWidth: 0,
          textAlign: 'left',
        }}>
          {/* eyebrow — only shown in compact variant (where the top
              status strip is suppressed). In other variants the top strip
              already shows state, so repeating it here is redundant. */}
          {variant === 'compact' && (
            <div style={{
              display: 'flex', alignItems: 'center', gap: 10,
              fontFamily: '"JetBrains Mono", monospace',
              fontSize: 12, letterSpacing: '0.22em', textTransform: 'uppercase',
              color: p.muted, marginBottom: 18,
            }}>
              <span style={{
                width: 8, height: 8, borderRadius: 8, background: p.accent,
                boxShadow: state === 'playing' ? `0 0 8px ${p.accent}` : 'none',
                animation: state === 'playing' && !prefersReducedMotion ? 'pulse 1.6s ease-in-out infinite' : 'none',
              }} />
              <span>{stateLabel(state)}</span>
              {!isEmpty && (
                <span style={{ marginLeft: 'auto' }}>
                  SIDE {album.side}{album.position}/{album.sideTracks}
                </span>
              )}
            </div>
          )}

          {/* SONG — hero */}
          <div style={{
            fontSize: isEmpty ? 48 : 72,
            lineHeight: 0.98,
            fontWeight: 600,
            letterSpacing: '-0.03em',
            color: p.text,
            textWrap: 'balance',
            marginBottom: 22,
            overflow: 'hidden',
            display: '-webkit-box',
            WebkitLineClamp: 3,
            WebkitBoxOrient: 'vertical',
          }}>
            {trackText(album, state)}
          </div>

          {/* divider */}
          <div style={{
            height: 2, background: p.accent, marginBottom: 20,
            width: 64,
          }} />

          {/* artist */}
          <div style={{
            fontSize: 48, fontWeight: 500, color: p.text,
            lineHeight: 1.04,
            letterSpacing: '-0.022em',
            textWrap: 'balance',
            marginBottom: 12,
          }}>{album.artist}</div>

          {/* album */}
          <div style={{
            fontSize: 32, color: p.accent,
            fontStyle: 'italic',
            fontFamily: '"Newsreader", Georgia, serif',
            lineHeight: 1.12,
            marginBottom: 24,
            overflow: 'hidden',
            display: '-webkit-box',
            WebkitLineClamp: 2,
            WebkitBoxOrient: 'vertical',
          }}>{album.album}</div>

          {/* genre chips — max 3 displayed; "+N" overflow indicator if more */}
          {/* album.genres guarded: Discogs can return null/undefined for some pressings */}
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 'auto', minHeight: 28 }}>
            {(album.genres ?? []).slice(0, 3).map(g => (
              <span key={g} style={{
                padding: '5px 12px',
                fontFamily: '"JetBrains Mono", monospace',
                fontSize: 12, letterSpacing: '0.1em',
                color: p.muted, border: `1px solid ${p.muted}55`,
                borderRadius: 2,
              }}>{g}</span>
            ))}
            {(album.genres ?? []).length > 3 && (
              <span style={{
                padding: '5px 12px',
                fontFamily: '"JetBrains Mono", monospace',
                fontSize: 12, letterSpacing: '0.1em',
                color: p.muted, border: `1px solid ${p.muted}55`,
                borderRadius: 2,
              }}>+{(album.genres ?? []).length - 3}</span>
            )}
          </div>

          {/* footer: catalog + adjacent */}
          <div style={{
            marginTop: 'auto',
            paddingTop: 12,
            fontFamily: '"JetBrains Mono", monospace',
            fontSize: 13, letterSpacing: '0.08em',
            color: p.muted,
          }}>
            {album.year} · {album.label} · {album.catalog}
          </div>

          {showAdjacent && state === 'playing' && (album.prev || album.next) && (
            <div style={{
              marginTop: 12, display: 'flex', gap: 32,
              fontFamily: '"JetBrains Mono", monospace',
              fontSize: 11, letterSpacing: '0.12em',
            }}>
              {/* PREV — hidden for first track on a side (no predecessor) */}
              {album.prev && (
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div style={{ color: p.muted }}>← PREV</div>
                  <div style={{
                    color: p.text, marginTop: 4,
                    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                    letterSpacing: 0, fontFamily: '"Inter Tight", "DejaVu Sans", Arial, sans-serif', fontSize: 14,
                  }}>{album.prev.track}</div>
                </div>
              )}
              {/* NEXT — hidden for last track on a side (no successor) */}
              {album.next && (
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div style={{ color: p.muted }}>NEXT →</div>
                  <div style={{
                    color: p.text, marginTop: 4,
                    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                    letterSpacing: 0, fontFamily: '"Inter Tight", "DejaVu Sans", Arial, sans-serif', fontSize: 14,
                    fontWeight: 500,
                  }}>{album.next.track}</div>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function DirAStatus({ state, accent, muted }) {
  const dot = {
    playing: accent, between: '#e0a040', paused: muted, idle: muted, boot: accent, error: '#c85050',
  }[state];
  const pulse = state === 'playing' || state === 'boot';
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, whiteSpace: 'nowrap' }}>
      <span style={{
        width: 8, height: 8, borderRadius: 8, background: dot, flex: '0 0 auto',
        boxShadow: pulse ? `0 0 8px ${dot}` : 'none',
        animation: pulse && !prefersReducedMotion ? 'pulse 1.6s ease-in-out infinite' : 'none',
      }} />
      <span style={{ whiteSpace: 'nowrap' }}>{stateLabel(state)}</span>
    </span>
  );
}

// Boot state elapsed-time hook — counts seconds since the cover mounted in boot state.
// Lets the listener distinguish "still identifying" from "hung" after a long wait.
function useBootElapsed(state) {
  const [elapsed, setElapsed] = React.useState(0);
  React.useEffect(() => {
    if (state !== 'boot') { setElapsed(0); return; }
    setElapsed(0);
    const t = setInterval(() => setElapsed(s => s + 1), 1000);
    return () => clearInterval(t);
  }, [state]);
  return elapsed;
}

function DirAEmptyCover({ state, p }) {
  const elapsed = useBootElapsed(state);
  // Label transitions: first 20s → "WARMING UP", 20-59s → "STILL LISTENING…", 60s+ → "IDENTIFYING… 1:mm"
  const bootLabel = React.useMemo(() => {
    if (elapsed < 20) return 'WARMING UP';
    if (elapsed < 60) return 'STILL LISTENING…';
    const m = Math.floor(elapsed / 60);
    const s = String(elapsed % 60).padStart(2, '0');
    return `IDENTIFYING… ${m}:${s}`;
  }, [elapsed]);

  if (state === 'error') {
    return (
      <div style={{
        width: '100%', height: '100%',
        background: p.surface,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        flexDirection: 'column', gap: 18,
      }}>
        <svg width="72" height="72" viewBox="0 0 72 72">
          <circle cx="36" cy="36" r="32" stroke={p.muted} strokeOpacity="0.4" strokeWidth="1" fill="none" />
          <circle cx="36" cy="36" r="32" stroke="#c85050" strokeWidth="1.5" fill="none"
            strokeDasharray="50 200" strokeLinecap="round" style={{
              transformOrigin: '36px 36px',
            }} />
        </svg>
        <div style={{
          fontFamily: '"JetBrains Mono", monospace',
          fontSize: 11, letterSpacing: '0.2em', color: p.muted,
        }}>NO MATCH FOUND</div>
      </div>
    );
  }
  if (state === 'boot') {
    return (
      <div style={{
        width: '100%', height: '100%',
        background: p.surface,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        flexDirection: 'column', gap: 18,
      }}>
        <svg width="72" height="72" viewBox="0 0 72 72">
          <circle cx="36" cy="36" r="32" stroke={p.muted} strokeOpacity="0.4" strokeWidth="1" fill="none" />
          <circle cx="36" cy="36" r="32" stroke={p.accent} strokeWidth="1.5" fill="none"
            strokeDasharray="50 200" strokeLinecap="round" style={{
              transformOrigin: '36px 36px',
              animation: prefersReducedMotion ? 'none' : 'rotate 1.4s linear infinite',
            }} />
        </svg>
        <div style={{
          fontFamily: '"JetBrains Mono", monospace',
          fontSize: 11, letterSpacing: '0.2em', color: p.muted,
        }}>{bootLabel}</div>
      </div>
    );
  }
  // idle
  return (
    <div style={{
      width: '100%', height: '100%',
      background: `repeating-linear-gradient(135deg, ${p.surface} 0 12px, ${p.bg} 12px 24px)`,
      display: 'flex', alignItems: 'center', justifyContent: 'center',
    }}>
      <div style={{
        fontFamily: '"JetBrains Mono", monospace',
        fontSize: 13, letterSpacing: '0.24em',
        color: p.muted,
      }}>NO RECORD ON PLATTER</div>
    </div>
  );
}

function stateLabel(s) {
  return {
    playing: 'NOW PLAYING',
    between: 'BETWEEN TRACKS',
    paused: 'PAUSED · TONEARM UP',
    idle: 'IDLE',
    boot: 'IDENTIFYING…',
    error: 'NO MATCH FOUND',
  }[s] || 'NOW PLAYING';
}

function trackText(album, state) {
  if (state === 'idle') return 'Waiting for a record';
  if (state === 'boot') return 'Listening…';
  if (state === 'error') return 'Couldn\'t identify';
  if (state === 'between') return `Up next — ${album.next.track}`;
  return album.track;
}

Object.assign(window, { DirectionA, stateLabel });
