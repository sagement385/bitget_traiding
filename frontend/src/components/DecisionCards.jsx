import { ShieldAlert } from 'lucide-react';

export default function DecisionCards({ decision, llmStatus }) {
  const actions = decision?.actions || [];
  return (
    <section className="decision-panel">
      <div className="panel-header">
        <div>
          <p className="eyebrow">Decision cards</p>
          <h2>대응 조치</h2>
        </div>
        <ShieldAlert size={20} />
      </div>
      <div className="llm-state">
        <span>{llmStatus?.status || 'unknown'}</span>
        <small>{llmStatus?.reason || llmStatus?.model || 'schema_guarded'}</small>
      </div>
      {actions.map((action) => (
        <article className="decision-card" key={`${action.action}-${action.priority}`}>
          <div className="priority">{action.priority}</div>
          <div>
            <h3>{action.action}</h3>
            <p>{action.target}</p>
            <span>{action.reason}</span>
          </div>
          <strong>{action.confidence}</strong>
        </article>
      ))}
      {actions.length === 0 && <p className="muted">source_basis 있는 조치 없음</p>}
    </section>
  );
}
