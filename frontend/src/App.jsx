import { useEffect, useMemo, useState } from 'react';
import { AlertTriangle, Database, RefreshCw } from 'lucide-react';
import MapView from './components/MapView.jsx';
import TimelineSlider from './components/TimelineSlider.jsx';
import DecisionCards from './components/DecisionCards.jsx';
import DataSourcePanel from './components/DataSourcePanel.jsx';

const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8001';

function eventTimeAt(events, index) {
  if (!events.length) return null;
  return events[Math.max(0, Math.min(index, events.length - 1))]?.time || null;
}

export default function App() {
  const [scenario, setScenario] = useState(null);
  const [replay, setReplay] = useState(null);
  const [index, setIndex] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    fetch(`${API_BASE}/api/scenarios/osong_2023_07_15`)
      .then((res) => res.json())
      .then((json) => {
        setScenario(json.data);
        const last = Math.max((json.data?.timeline?.length || 1) - 1, 0);
        setIndex(last);
      })
      .catch((err) => setError(String(err)));
  }, []);

  const selectedTime = useMemo(() => eventTimeAt(scenario?.timeline || [], index), [scenario, index]);

  useEffect(() => {
    if (!selectedTime) return;
    setLoading(true);
    setError('');
    fetch(`${API_BASE}/api/replay/osong_2023_07_15?at=${encodeURIComponent(selectedTime)}`)
      .then((res) => res.json())
      .then((json) => setReplay(json.data))
      .catch((err) => setError(String(err)))
      .finally(() => setLoading(false));
  }, [selectedTime]);

  const decision = replay?.decision_result?.decision;
  const officialEvents = replay?.official_events || [];

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">Official data replay</p>
          <h1>충북 집중호우 재난 대응 디지털 트윈</h1>
        </div>
        <div className="status-strip">
          <span className="status-pill"><Database size={16} /> STRICT_DATA_MODE</span>
          <span className={`risk-chip ${decision?.risk_level || 'unknown'}`}>{decision?.risk_level || 'unknown'}</span>
        </div>
      </header>

      {error && (
        <section className="notice error">
          <AlertTriangle size={18} />
          <span>{error}</span>
        </section>
      )}

      <section className="scenario-band">
        <div>
          <p className="eyebrow">Scenario</p>
          <h2>{scenario?.event_name || '오송 2023 리플레이'}</h2>
        </div>
        <button className="icon-button" type="button" onClick={() => selectedTime && setIndex(index)}>
          <RefreshCw size={18} />
          <span>{loading ? 'loading' : 'refresh'}</span>
        </button>
      </section>

      <TimelineSlider
        events={scenario?.timeline || []}
        officialEvents={officialEvents}
        value={index}
        onChange={setIndex}
      />

      <section className="workspace-grid">
        <MapView assets={replay?.assets || []} mapUpdates={decision?.map_updates || []} />
        <DecisionCards decision={decision} llmStatus={replay?.decision_result} />
        <DataSourcePanel replay={replay} />
      </section>
    </main>
  );
}
