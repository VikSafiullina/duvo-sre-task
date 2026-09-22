import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE = __ENV.BASE_URL || 'http://localhost:8000';
const JSON_HEADERS = { headers: { 'Content-Type': 'application/json' } };

// Every create launches a real container, so admission control (429 at the cap) is an
// expected, healthy answer under load — not an error.
http.setResponseCallback(http.expectedStatuses(200, 202, 429));

export const options = {
  stages: [
    { duration: '10s', target: 5 },
    { duration: '30s', target: 10 },
    { duration: '10s', target: 0 },
  ],
  thresholds: {
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(95)<300'],
  },
};

export default function () {
  const created = http.post(`${BASE}/sandboxes`, JSON.stringify({ type: 'http', ttl_s: 60 }), JSON_HEADERS);
  check(created, { 'create 202/429': (r) => r.status === 202 || r.status === 429 });
  if (created.status === 202) {
    const id = created.json('sandbox_id');
    check(http.get(`${BASE}/sandboxes/${id}`), { 'get 200': (r) => r.status === 200 });
    sleep(1);
    check(http.del(`${BASE}/sandboxes/${id}`), { 'delete 202': (r) => r.status === 202 });
  }
  check(http.get(`${BASE}/sandboxes?limit=20`), { 'list 200': (r) => r.status === 200 });
  sleep(0.5);
}
