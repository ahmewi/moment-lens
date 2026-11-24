import { Tab, TabList, makeStyles, shorthands, tokens, Caption1 } from "@fluentui/react-components";
import { Routes, Route, useNavigate, useLocation, Navigate } from "react-router-dom";
import PipelinePage from "./pages/PipelinePage";
import LibraryPage from "./pages/LibraryPage";
import UploadPane from "./components/UploadPane";

const useStyles = makeStyles({
  shell: {
    display: "flex",
    flexDirection: "column",
    height: "100%",
    minHeight: "100vh",
  },
  header: {
    padding: "16px 32px 0",
    ...shorthands.borderBottom("1px", "solid", tokens.colorNeutralStroke2),
    background: `linear-gradient(180deg, ${tokens.colorNeutralBackground3} 0%, ${tokens.colorNeutralBackground2} 100%)`,
    backdropFilter: "saturate(140%) blur(8px)",
    "@media (max-width: 600px)": { padding: "12px 16px 0" },
  },
  topRow: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    gap: "24px",
    flexWrap: "wrap",
  },
  brand: {
    display: "flex",
    alignItems: "center",
    gap: "14px",
  },
  logo: {
    height: "84px",
    width: "auto",
    objectFit: "contain",
    "@media (max-width: 600px)": { height: "56px" },
  },
  brandText: {
    display: "flex",
    flexDirection: "column",
    lineHeight: 1.1,
  },
  brandName: {
    fontSize: "32px",
    fontWeight: 700,
    letterSpacing: "0",
    lineHeight: 1.05,
    "@media (max-width: 600px)": { fontSize: "26px" },
  },
  brandMoment: { color: "#0F2A6B" },
  brandLens: { color: "#F2811D" },
  tagline: {
    color: tokens.colorNeutralForeground3,
    marginTop: "2px",
  },
  tabs: {
    marginTop: "12px",
    maxWidth: "100%",
    overflowX: "auto",
  },
});

export default function App() {
  const cls = useStyles();
  const nav = useNavigate();
  const loc = useLocation();
  const tab = loc.pathname.startsWith("/library")
    ? "library"
    : loc.pathname.startsWith("/index-video")
    ? "index-video"
    : "video-indexer";

  return (
    <div className={cls.shell}>
      <header className={cls.header}>
        <div className={cls.topRow}>
          <div className={cls.brand}>
            <img src="/momentlens-logo.png" alt="MomentLens" className={cls.logo} />
            <div className={cls.brandText}>
              <div className={cls.brandName}>
                <span className={cls.brandMoment}>Moment</span><span className={cls.brandLens}>Lens</span>
              </div>
              <Caption1 className={cls.tagline}>
                Find the moments that matter
              </Caption1>
            </div>
          </div>
        </div>
        <TabList
          className={cls.tabs}
          selectedValue={tab}
          onTabSelect={(_, d) => nav("/" + d.value)}
          size="large"
        >
          <Tab value="video-indexer">Find moments</Tab>
          <Tab value="index-video">Index a video</Tab>
          <Tab value="library">Library</Tab>
        </TabList>
      </header>
      <main style={{ flex: 1, overflow: "auto" }}>
        <Routes>
          <Route path="/" element={<Navigate to="/video-indexer" replace />} />
          <Route path="/video-indexer" element={
            <PipelinePage title="Find moments"
              blurb="Advanced video insights, frame evidence, and hybrid search to find and validate match moments." />
          } />
          <Route path="/index-video" element={<div style={{ padding: 24, maxWidth: 900, margin: "0 auto" }}>
            <UploadPane onIndexed={videoId => nav(`/video-indexer?video=${encodeURIComponent(videoId)}`)} />
          </div>} />
          <Route path="/library" element={<LibraryPage />} />
          <Route path="*" element={<Navigate to="/video-indexer" replace />} />
        </Routes>
      </main>
    </div>
  );
}
