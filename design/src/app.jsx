// Main app — Vinyl Now Playing display
// Trimmed export build: shows only the "Every record, default layout" row.

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "themed": true,
  "showAdjacent": true,
  "primaryAlbumId": "sister"
}/*EDITMODE-END*/;

function App() {
  const [t, setTweak] = window.useTweaks(TWEAK_DEFAULTS);

  const { DesignCanvas, DCSection, DCArtboard, DirectionA } = window;

  return (
    <React.Fragment>
      <DesignCanvas
        title="Vinyl · Now Playing — Direction A"
        subtitle={`Cover-left museum card. Prev/next on. Theme extraction: ${t.themed ? 'on' : 'off'}.`}
      >
        <DCSection id="records"
          title="Every record, default layout"
          subtitle="Direction A across all five demo albums, with previous/next shown. Each row tints itself from its cover.">
          {window.ALBUMS.map(a => (
            <DCArtboard key={a.id} id={`rec-${a.id}`}
              label={`${a.artist} — ${a.album.split(' or')[0]}`}
              width={1024} height={600}>
              <DirectionA album={a} state="playing" themed={t.themed} showAdjacent={true} />
            </DCArtboard>
          ))}
        </DCSection>
      </DesignCanvas>

      <window.TweaksPanel title="Tweaks">
        <window.TweakSection label="Display" />
        <window.TweakToggle
          label="Theme from cover"
          value={t.themed}
          onChange={v => setTweak('themed', v)}
        />
        <window.TweakToggle
          label="Show prev / next"
          value={t.showAdjacent}
          onChange={v => setTweak('showAdjacent', v)}
        />
      </window.TweaksPanel>
    </React.Fragment>
  );
}

const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(<App />);
