import {
  ButtonItem,
  ConfirmModal,
  Field,
  PanelSection,
  PanelSectionRow,
  showModal,
  staticClasses,
  ToggleField,
} from "@decky/ui";
import { callable, definePlugin } from "@decky/api";
import { useEffect, useState } from "react";

// Gaming On Steam colours, as in the web page and the SteamVR tab.
const BRAND = "#189dff";
const ACCENT = "#03bfff";
const MUTED = "#9fb3c8";

interface Status {
  active: boolean;
  urls: string[];
  pin: string;
  files: number;
  devices: number;
  idle_minutes: number;
  auto_stop: boolean;
  error?: string;
}

const getStatus = callable<[], Status>("status");
const startSharing = callable<[], Status>("start");
const stopSharing = callable<[], Status>("stop");
const forgetAll = callable<[], Status>("forget_all");
const setAutoStop = callable<[on: boolean], Status>("set_auto_stop");

const POLL_MS = 2000;

// The newest build from main, the same zip INSTALL.md gives for "Install plugin from URL".
const LATEST_ZIP = "https://github.com/MWGIH/SCopy/releases/download/decky-latest/SCopy.zip";
const INSTALL_TYPE_UPDATE = 2;

declare global {
  interface Window {
    DeckyBackend: { call<A extends any[], R>(route: string, ...args: A): Promise<R> };
  }
}

// Decky's own installer: it asks to confirm, then replaces this plugin with the downloaded zip.
// Settings and remembered computers live in ~/.config/scopy, so they stay.
const updatePlugin = () =>
  window.DeckyBackend.call("utilities/install_plugin", LATEST_ZIP, "SCopy", "latest", "", INSTALL_TYPE_UPDATE);

// Same shapes as assets/icon.svg, drawn in the menu's own colour. The outline sheet stops short of the
// front sheet, so only the background shows through the S.
function SCopyIcon() {
  return (
    <svg viewBox="0 0 256 256" width="1em" height="1em" fill="currentColor">
      <mask id="scopy-gap">
        <rect width="256" height="256" fill="white" />
        <rect x="78" y="66" width="144" height="172" rx="30" fill="black" />
      </mask>
      <mask id="scopy-s">
        <rect width="256" height="256" fill="white" />
        <text x="150" y="154" dominantBaseline="central" textAnchor="middle"
              fontFamily="sans-serif" fontWeight="bold" fontSize="120" fill="black">S</text>
      </mask>
      <g transform="translate(128 128) scale(1.15) translate(-128 -128)">
        <rect x="51" y="35" width="110" height="138" rx="15" fill="none" stroke="currentColor" strokeWidth="14"
              mask="url(#scopy-gap)" />
        <rect x="88" y="76" width="124" height="152" rx="22" mask="url(#scopy-s)" />
      </g>
    </svg>
  );
}

function Content() {
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  // Sharing can stop by itself after a while without use, so keep asking while the panel is open.
  useEffect(() => {
    let alive = true;
    const refresh = () => getStatus().then((s) => alive && setStatus(s)).catch(() => {});
    refresh();
    const timer = setInterval(refresh, POLL_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  const run = async (action: () => Promise<Status>) => {
    setBusy(true);
    try {
      const s = await action();
      setError(s.error ?? "");
      if (!s.error) setStatus(s);
    } finally {
      setBusy(false);
    }
  };

  const confirmForget = () =>
    showModal(
      <ConfirmModal
        strTitle="Forget every computer?"
        strDescription="Every PC and phone will need the PIN again next time."
        strOKButtonText="Forget"
        onOK={() => run(forgetAll)}
      />
    );

  if (!status) {
    return (
      <PanelSection>
        <PanelSectionRow>Loading...</PanelSectionRow>
      </PanelSection>
    );
  }

  return (
    <>
      <PanelSection>
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={busy} onClick={() => run(status.active ? stopSharing : startSharing)}>
            {status.active ? "Stop sharing" : "Start sharing"}
          </ButtonItem>
        </PanelSectionRow>
        {error && (
          <PanelSectionRow>
            <div style={{ color: "#ff4d5e" }}>{error}</div>
          </PanelSectionRow>
        )}
        {status.active ? (
          <>
            <PanelSectionRow>
              <Field label="In a browser on your PC or phone, open" bottomSeparator="none" />
            </PanelSectionRow>
            <PanelSectionRow>
              <div style={{ color: "white", fontSize: "18px", fontWeight: "bold", wordBreak: "break-all" }}>
                {status.urls[0]}
              </div>
              {status.urls.slice(1).map((url) => (
                <div key={url} style={{ color: MUTED, wordBreak: "break-all" }}>
                  or {url}
                </div>
              ))}
            </PanelSectionRow>
            <PanelSectionRow>
              <div style={{ textAlign: "center", margin: "8px 0" }}>
                <span style={{ color: MUTED, fontWeight: "bold", marginRight: "12px" }}>PIN</span>
                <span style={{ color: ACCENT, fontSize: "40px", fontWeight: "bold", letterSpacing: "4px" }}>
                  {status.pin}
                </span>
              </div>
            </PanelSectionRow>
            <PanelSectionRow>
              <div style={{ color: MUTED, fontSize: "12px" }}>
                {status.files} files: recordings, screenshots, videos, pictures.{" "}
                {status.auto_stop
                  ? `Stops by itself after ${status.idle_minutes} minutes without use.`
                  : "Keeps sharing until you press Stop sharing."}
              </div>
            </PanelSectionRow>
          </>
        ) : (
          <PanelSectionRow>
            <div style={{ color: MUTED, fontSize: "12px" }}>
              Share your recordings, screenshots and videos with any browser on the same network.
            </div>
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <ToggleField
            label={`Auto-stop after ${status.idle_minutes} min`}
            checked={status.auto_stop}
            disabled={busy}
            onChange={(on) => run(() => setAutoStop(on))}
          />
        </PanelSectionRow>
      </PanelSection>
      <PanelSection>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={busy}
            onClick={() => updatePlugin().catch((e) => setError(`Can't update: ${e?.message ?? e}`))}
          >
            Update plugin
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <div style={{ color: MUTED, fontSize: "12px" }}>Installs the newest SCopy from GitHub.</div>
        </PanelSectionRow>
      </PanelSection>
      <PanelSection title="Remembered computers">
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={busy || status.devices === 0} onClick={confirmForget}>
            Forget PCs ({status.devices})
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}

export default definePlugin(() => ({
  name: "SCopy",
  titleView: <div className={staticClasses.Title} style={{ color: BRAND }}>SCopy</div>,
  content: <Content />,
  icon: <SCopyIcon />,
  onDismount() {},
}));
