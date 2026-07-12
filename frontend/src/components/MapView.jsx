import { MapPin, Navigation } from 'lucide-react';

const colors = {
  green: '#2f7d52',
  yellow: '#c29b1d',
  orange: '#c75f1a',
  red: '#b9342c',
  gray: '#75817f',
};

function updateFor(asset, mapUpdates) {
  return mapUpdates.find((item) => item.target === asset.name);
}

function markerStyle(asset, assets) {
  const verified = assets.filter((item) => Array.isArray(item.coordinates) && item.coordinates.length === 2);
  const lats = verified.map((item) => Number(item.coordinates[0]));
  const lons = verified.map((item) => Number(item.coordinates[1]));
  const lat = Number(asset.coordinates[0]);
  const lon = Number(asset.coordinates[1]);
  if (verified.length === 1) {
    return { left: '50%', top: '50%' };
  }
  const minLat = Math.min(...lats);
  const maxLat = Math.max(...lats);
  const minLon = Math.min(...lons);
  const maxLon = Math.max(...lons);
  const left = maxLon === minLon ? 50 : ((lon - minLon) / (maxLon - minLon)) * 80 + 10;
  const top = maxLat === minLat ? 50 : (1 - (lat - minLat) / (maxLat - minLat)) * 80 + 10;
  return { left: `${left}%`, top: `${top}%` };
}

export default function MapView({ assets, mapUpdates }) {
  const verified = assets.filter((asset) => Array.isArray(asset.coordinates) && asset.coordinates.length === 2);
  const missing = assets.filter((asset) => !Array.isArray(asset.coordinates));

  return (
    <section className="map-panel">
      <div className="panel-header">
        <div>
          <p className="eyebrow">Map</p>
          <h2>검증 좌표 레이어</h2>
        </div>
        <Navigation size={20} />
      </div>
      <div className="map-canvas">
        {verified.length === 0 && (
          <div className="empty-map">
            <MapPin size={28} />
            <strong>공식 좌표 없음</strong>
            <span>지도 표시는 보류</span>
          </div>
        )}
        {verified.map((asset) => {
          const update = updateFor(asset, mapUpdates);
          return (
            <div
              key={asset.asset_id}
              className="map-marker"
              style={{ ...markerStyle(asset, verified), backgroundColor: colors[update?.color || 'gray'] }}
              title={`${asset.name}: ${update?.status || 'unknown'}`}
            >
              <MapPin size={16} />
            </div>
          );
        })}
      </div>
      <div className="asset-list">
        {assets.map((asset) => {
          const update = updateFor(asset, mapUpdates);
          return (
            <div className="asset-row" key={asset.asset_id}>
              <span className={`asset-dot ${update?.color || 'gray'}`} />
              <div>
                <strong>{asset.name}</strong>
                <span>{update?.status || asset.coordinate_status || 'unknown'}</span>
              </div>
              <small>{asset.coordinate_status === 'verified' ? '지도 표시' : '좌표 없음'}</small>
            </div>
          );
        })}
        {missing.length === 0 && <p className="muted">not_available</p>}
      </div>
    </section>
  );
}
