import { Clock } from 'lucide-react';

export default function TimelineSlider({ events, officialEvents, value, onChange }) {
  const current = events[value];
  const confirmedTypes = new Set(officialEvents.map((event) => event.event_type));

  return (
    <section className="timeline-panel">
      <div className="timeline-header">
        <div>
          <p className="eyebrow">Replay time</p>
          <h2>{current?.time || 'not_available'}</h2>
        </div>
        <Clock size={20} />
      </div>
      <input
        className="timeline-range"
        type="range"
        min="0"
        max={Math.max(events.length - 1, 0)}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
      />
      <div className="timeline-events">
        {events.map((event, index) => (
          <button
            key={`${event.time}-${event.event_type}`}
            type="button"
            className={`timeline-node ${index === value ? 'active' : ''} ${confirmedTypes.has(event.event_type) ? 'confirmed' : ''}`}
            onClick={() => onChange(index)}
          >
            <span>{event.time.slice(5, 16).replace('T', ' ')}</span>
            <strong>{event.event_type}</strong>
          </button>
        ))}
      </div>
    </section>
  );
}
