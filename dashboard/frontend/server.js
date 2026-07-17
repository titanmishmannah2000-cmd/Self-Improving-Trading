const http = require('http');
const fs = require('fs');
const path = require('path');

const PORT = process.env.PORT || 4173;
const DIST = path.join(__dirname, 'dist');

const types = {
  '.html': 'text/html',
  '.js': 'application/javascript',
  '.css': 'text/css',
  '.json': 'application/json',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
};

function resolveApiBase() {
  if (process.env.VITE_API_BASE) {
    return process.env.VITE_API_BASE.replace(/\/$/, '');
  }
  const railwayApi = process.env.RAILWAY_SERVICE_HERMES_DASHBOARD_API_URL;
  if (railwayApi) {
    return railwayApi.startsWith('http') ? railwayApi : `https://${railwayApi}`;
  }
  return 'http://localhost:8000';
}

const API_BASE = resolveApiBase();

function sendJson(res, status, body) {
  res.writeHead(status, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(body));
}

function sendFile(res, filePath) {
  fs.readFile(filePath, (err, data) => {
    if (err) {
      res.writeHead(404);
      res.end('Not found');
      return;
    }
    res.writeHead(200, {
      'Content-Type': types[path.extname(filePath)] || 'application/octet-stream',
      'Cache-Control': path.extname(filePath) === '.html' ? 'no-cache' : 'public, max-age=31536000, immutable',
    });
    res.end(data);
  });
}

http.createServer((req, res) => {
  const urlPath = req.url.split('?')[0];

  if (urlPath === '/config.json') {
    return sendJson(res, 200, { apiBase: API_BASE });
  }

  if (urlPath === '/health' || urlPath === '/api/health') {
    return sendJson(res, 200, { status: 'ok', apiBase: API_BASE });
  }

  // Proxy /api/* to the FastAPI backend — forward method, headers, and body
  if (urlPath.startsWith('/api/')) {
    const query = req.url.includes('?') ? '?' + req.url.split('?')[1] : '';
    const proxyUrl = `${API_BASE}${urlPath}${query}`;
    const lib = API_BASE.startsWith('https') ? require('https') : require('http');
    // CRITICAL: do NOT forward Accept-Encoding. If we request gzip/br from the
    // backend, the response body is compressed binary and buffering it as a JS
    // string (body += chunk) corrupts the bytes — the browser then fails to
    // parse the JSON (garbage gzip magic). Request uncompressed so we can proxy
    // the response as clean UTF-8 text. (Fixed 2026-07-14 — root cause of the
    // "connection error" shown on every dashboard tab.)
    const fwdHeaders = { ...req.headers };
    delete fwdHeaders['accept-encoding'];
    delete fwdHeaders['Accept-Encoding'];
    delete fwdHeaders['host'];
    const opts = {
      method: req.method,
      headers: fwdHeaders,
    };
    const proxyReq = lib.request(proxyUrl, opts, (proxyRes) => {
      const chunks = [];
      proxyRes.on('data', (c) => chunks.push(Buffer.isBuffer(c) ? c : Buffer.from(c)));
      proxyRes.on('end', () => {
        const body = Buffer.concat(chunks);
        // Content-Encoding must be dropped — the response we forward is the
        // decoded bytes (backend sent uncompressed because we stripped
        // Accept-Encoding above). Otherwise the browser tries to gunzip plain
        // JSON and fails.
        const outHeaders = { ...proxyRes.headers };
        delete outHeaders['content-encoding'];
        delete outHeaders['Content-Encoding'];
        outHeaders['Content-Type'] = 'application/json; charset=utf-8';
        outHeaders['Cache-Control'] = 'no-store';
        res.writeHead(proxyRes.statusCode, outHeaders);
        res.end(body);
      });
    });
    proxyReq.on('error', (e) => sendJson(res, 502, { error: 'Backend unavailable', detail: e.message }));
    req.pipe(proxyReq);
    return;
  }

  let filePath = path.join(DIST, urlPath === '/' ? 'index.html' : urlPath);

  // SPA fallback: serve index.html for non-asset routes
  if (!urlPath.includes('.') && urlPath !== '/') {
    filePath = path.join(DIST, 'index.html');
  }

  sendFile(res, filePath);
}).listen(PORT, '0.0.0.0', () => {
  console.log(`Dashboard serving on port ${PORT} (API: ${API_BASE})`);
});
