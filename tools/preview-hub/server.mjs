#!/usr/bin/env node
/**
 * Preview Hub — lightweight static server + browser chrome for static sites / SPAs.
 * Companion for editors without Simple Browser (e.g. Grok TUI).
 * Node stdlib only. Windows / macOS / Linux.
 */

import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import os from 'node:os';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const HUB_DIR = __dirname;

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
  '.ico': 'image/x-icon',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.ttf': 'font/ttf',
  '.txt': 'text/plain; charset=utf-8',
  '.map': 'application/json',
  '.webmanifest': 'application/manifest+json',
};

function parseArgs(argv) {
  const out = {
    port: process.env.PREVIEW_PORT || '4567',
    host: process.env.PREVIEW_HOST || '127.0.0.1',
    token: process.env.PREVIEW_TOKEN || '',
    open: process.env.PREVIEW_OPEN === '1' || process.env.PREVIEW_OPEN === 'true',
    root: process.env.PREVIEW_ROOT || '',
    spa: process.env.PREVIEW_SPA === '1' || process.env.PREVIEW_SPA === 'true',
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--port' && argv[i + 1]) out.port = argv[++i];
    else if (a === '--host' && argv[i + 1]) out.host = argv[++i];
    else if (a === '--token' && argv[i + 1]) out.token = argv[++i];
    else if (a === '--root' && argv[i + 1]) out.root = argv[++i];
    else if (a === '--open') out.open = true;
    else if (a === '--spa') out.spa = true;
    else if (a.startsWith('--port=')) out.port = a.slice(7);
    else if (a.startsWith('--host=')) out.host = a.slice(7);
    else if (a.startsWith('--token=')) out.token = a.slice(8);
    else if (a.startsWith('--root=')) out.root = a.slice(7);
  }
  out.port = Number(out.port) || 4567;
  out.root = path.resolve(out.root || process.cwd());
  return out;
}

function isAuthorized(req, url, token) {
  if (!token) return true;
  const header = req.headers['x-preview-token'];
  if (header && header === token) return true;
  const q = url.searchParams.get('token');
  if (q && q === token) return true;
  return false;
}

function send(res, status, body, headers = {}) {
  const payload = typeof body === 'string' || Buffer.isBuffer(body) ? body : JSON.stringify(body);
  res.writeHead(status, {
    'Cache-Control': 'no-store',
    ...headers,
  });
  res.end(payload);
}

function safeJoin(root, requestPath) {
  // Decode and normalize; reject null bytes and absolute escapes
  let decoded;
  try {
    decoded = decodeURIComponent(requestPath);
  } catch {
    return null;
  }
  if (decoded.includes('\0')) return null;

  const cleaned = decoded.replace(/^\/+/, '');
  const resolved = path.resolve(root, cleaned);
  const rootWithSep = root.endsWith(path.sep) ? root : root + path.sep;
  if (resolved !== root && !resolved.startsWith(rootWithSep)) {
    return null;
  }
  return resolved;
}

function looksLikeFile(p) {
  const base = path.basename(p);
  return base.includes('.');
}

function openBrowser(url) {
  const platform = process.platform;
  try {
    if (platform === 'win32') {
      spawn('cmd', ['/c', 'start', '', url], { detached: true, stdio: 'ignore' }).unref();
    } else if (platform === 'darwin') {
      spawn('open', [url], { detached: true, stdio: 'ignore' }).unref();
    } else {
      spawn('xdg-open', [url], { detached: true, stdio: 'ignore' }).unref();
    }
  } catch (err) {
    console.error('Could not open browser:', err.message);
  }
}

function lanAddresses() {
  const nets = os.networkInterfaces();
  const addrs = [];
  for (const name of Object.keys(nets)) {
    for (const net of nets[name] || []) {
      if (net.family === 'IPv4' && !net.internal) addrs.push(net.address);
    }
  }
  return addrs;
}

function serveFile(filePath, res) {
  fs.stat(filePath, (err, st) => {
    if (err || !st.isFile()) {
      send(res, 404, 'Not found', { 'Content-Type': 'text/plain; charset=utf-8' });
      return;
    }
    const ext = path.extname(filePath).toLowerCase();
    const type = MIME[ext] || 'application/octet-stream';
    res.writeHead(200, {
      'Content-Type': type,
      'Content-Length': st.size,
      'Cache-Control': 'no-store',
    });
    fs.createReadStream(filePath).pipe(res);
  });
}

const config = parseArgs(process.argv.slice(2));
const ROOT = config.root;

const server = http.createServer((req, res) => {
  const host = req.headers.host || `${config.host}:${config.port}`;
  let url;
  try {
    url = new URL(req.url || '/', `http://${host}`);
  } catch {
    send(res, 400, 'Bad request', { 'Content-Type': 'text/plain; charset=utf-8' });
    return;
  }

  // Health is always open (useful for probes)
  if (url.pathname === '/__hub/health') {
    send(res, 200, { ok: true }, { 'Content-Type': 'application/json; charset=utf-8' });
    return;
  }

  if (!isAuthorized(req, url, config.token)) {
    send(res, 401, 'Unauthorized. Provide ?token=... or X-Preview-Token header.\n', {
      'Content-Type': 'text/plain; charset=utf-8',
      'WWW-Authenticate': 'Preview-Token',
    });
    return;
  }

  // Hub chrome
  if (url.pathname === '/__hub' || url.pathname === '/__hub/') {
    const hubPath = path.join(HUB_DIR, 'hub.html');
    serveFile(hubPath, res);
    return;
  }

  // Hub-relative assets (optional)
  if (url.pathname.startsWith('/__hub/')) {
    const rel = url.pathname.slice('/__hub/'.length);
    const filePath = safeJoin(HUB_DIR, rel);
    if (!filePath) {
      send(res, 403, 'Forbidden', { 'Content-Type': 'text/plain; charset=utf-8' });
      return;
    }
    serveFile(filePath, res);
    return;
  }

  // Site root
  if (url.pathname === '/' || url.pathname === '') {
    serveFile(path.join(ROOT, 'index.html'), res);
    return;
  }

  const filePath = safeJoin(ROOT, url.pathname);
  if (!filePath) {
    send(res, 403, 'Forbidden', { 'Content-Type': 'text/plain; charset=utf-8' });
    return;
  }

  fs.stat(filePath, (err, st) => {
    if (!err && st.isFile()) {
      serveFile(filePath, res);
      return;
    }
    if (!err && st.isDirectory()) {
      const indexInDir = path.join(filePath, 'index.html');
      fs.stat(indexInDir, (e2, st2) => {
        if (!e2 && st2.isFile()) {
          serveFile(indexInDir, res);
          return;
        }
        send(res, 404, 'Not found', { 'Content-Type': 'text/plain; charset=utf-8' });
      });
      return;
    }
    // Optional SPA fallback: unknown non-file paths → root index.html
    if (config.spa && !looksLikeFile(url.pathname)) {
      serveFile(path.join(ROOT, 'index.html'), res);
      return;
    }
    send(res, 404, 'Not found', { 'Content-Type': 'text/plain; charset=utf-8' });
  });
});

server.listen(config.port, config.host, () => {
  const localHost = config.host === '0.0.0.0' || config.host === '::' ? '127.0.0.1' : config.host;
  const tokenQ = config.token ? `?token=${encodeURIComponent(config.token)}` : '';
  const hubUrl = `http://${localHost}:${config.port}/__hub${tokenQ}`;
  const siteUrl = `http://${localHost}:${config.port}/${tokenQ ? tokenQ : ''}`;

  console.log('');
  console.log('  Preview Hub');
  console.log('  ─────────────────────────────────────────');
  console.log(`  Root:        ${ROOT}`);
  console.log(`  SPA mode:    ${config.spa ? 'on (unknown paths → index.html)' : 'off'}`);
  console.log(`  Listening:   ${config.host}:${config.port}`);
  console.log(`  Desktop hub: ${hubUrl}`);
  console.log(`  Site direct: ${siteUrl}`);
  if (config.token) {
    console.log(`  Token:       enabled (required on all routes except /__hub/health)`);
  } else {
    console.log(`  Token:       off`);
  }
  if (config.host === '0.0.0.0' || config.host === '::') {
    const lans = lanAddresses();
    console.log('');
    console.log('  LAN / remote access (host is 0.0.0.0):');
    if (lans.length) {
      for (const ip of lans) {
        const q = config.token ? `?token=${encodeURIComponent(config.token)}` : '';
        console.log(`    http://${ip}:${config.port}/__hub${q}`);
      }
    } else {
      console.log('    (no non-loopback IPv4 addresses found)');
    }
    console.log('');
    console.log('  ⚠  SECURITY: 0.0.0.0 exposes this server on all interfaces.');
    if (!config.token) {
      console.log('  ⚠  No PREVIEW_TOKEN set. Anyone on the network can browse the site.');
      console.log('     Prefer: --token YOUR_SECRET  or  PREVIEW_TOKEN=...');
    }
  }
  console.log('  ─────────────────────────────────────────');
  console.log('  Ctrl+C to stop');
  console.log('');

  if (config.open) {
    openBrowser(hubUrl);
  }
});

server.on('error', (err) => {
  if (err.code === 'EADDRINUSE') {
    console.error(`Port ${config.port} is already in use. Try --port <other> or free the port.`);
  } else {
    console.error(err);
  }
  process.exit(1);
});
