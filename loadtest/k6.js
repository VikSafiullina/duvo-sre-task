import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE = __ENV.BASE_URL || 'http://localhost:8000';
const JSON_HEADERS = { headers: { 'Content-Type': 'application/json' } };

export const options = {
  stages: [
    { duration: '10s', target: 10 },
    { duration: '30s', target: 20 },
    { duration: '10s', target: 0 },
  ],
  thresholds: {
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(95)<300'],
  },
};

export default function () {
  const created = http.post(`${BASE}/items`, JSON.stringify({ name: `load-${__VU}-${__ITER}` }), JSON_HEADERS);
  check(created, { 'create 201': (r) => r.status === 201 });
  const id = created.json('id');
  check(http.get(`${BASE}/items/${id}`), { 'get 200': (r) => r.status === 200 });
  check(http.post(`${BASE}/items/${id}/process`), { 'process 202': (r) => r.status === 202 });
  check(http.get(`${BASE}/items?limit=20`), { 'list 200': (r) => r.status === 200 });
  sleep(0.5);
}
