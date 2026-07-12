import { FileText } from 'lucide-react';

function uniqueSources(replay) {
  const seen = new Set();
  const sources = [];
  const push = (source) => {
    if (!source?.source_url) return;
    const key = `${source.source_name}-${source.source_url}`;
    if (seen.has(key)) return;
    seen.add(key);
    sources.push(source);
  };
  push(replay?.scenario?.source);
  replay?.official_events?.forEach((event) => push(event.source));
  replay?.assets?.forEach((asset) => push(asset));
  return sources;
}

export default function DataSourcePanel({ replay }) {
  const dataGaps = replay?.decision_result?.decision?.data_gaps || [];
  const sources = uniqueSources(replay);
  return (
    <section className="source-panel">
      <div className="panel-header">
        <div>
          <p className="eyebrow">Sources</p>
          <h2>데이터 출처</h2>
        </div>
        <FileText size={20} />
      </div>
      <div className="source-list">
        {sources.map((source) => (
          <a key={source.source_url} href={source.source_url} target="_blank" rel="noreferrer">
            <strong>{source.source_name}</strong>
            <span>{source.source_type}</span>
          </a>
        ))}
      </div>
      <div className="gap-list">
        <h3>Data gaps</h3>
        {dataGaps.map((gap) => <span key={gap}>{gap}</span>)}
        {dataGaps.length === 0 && <span>not_available</span>}
      </div>
    </section>
  );
}
