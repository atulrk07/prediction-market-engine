const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'dashboard.html'), 'utf8');
// Load the page's real functions without starting its DOM event handlers or timer.
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1].split('$("refresh").addEventListener')[0];

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function dashboard() {
  const nodes = new Map();
  const errors = [];
  const document = {
    getElementById(id) {
      if (!nodes.has(id)) {
        const classes = new Set();
        nodes.set(id, {
          textContent: '', hidden: false, disabled: false,
          classList: { add: value => classes.add(value), remove: value => classes.delete(value), contains: value => classes.has(value) },
          querySelectorAll: () => [],
        });
      }
      return nodes.get(id);
    },
  };
  const context = vm.createContext({ document, console, Intl, URLSearchParams, onError: error => errors.push(error.message) });
  const run = code => vm.runInContext(code, context);
  run(script);
  // Rendering is exercised separately in a real browser; these tests control HTTP timing.
  run(`
    renderStats = renderMarkets = renderChart = renderHistoryTable = () => {};
    renderBanner = error => { if (error) onError(error); };
    state.markets = [
      { id: 1, title: 'Synthetic A', external_id: 'A' },
      { id: 2, title: 'Synthetic B', external_id: 'B' }
    ];
    state.selected = 1;
  `);
  return { context, run, nodes, errors };
}

function historyRequests(page) {
  const requests = [];
  page.context.fetchHistory = (id, hours) => {
    const request = { id, hours, ...deferred() };
    requests.push(request);
    return request.promise;
  };
  page.run('loadHistory = (id, hours) => fetchHistory(id, hours)');
  return requests;
}

function select(page, id, hours = 24) {
  return page.run(`state.selected = ${id}; state.rangeHours = ${hours}; refreshDetail()`);
}

const rows = ticker => [{ snapshot_id: ticker === 'A' ? 1 : 2, quote: { ticker } }];

function loadedTicker(page) {
  return page.run('state.history[0]?.quote.ticker');
}

test('switching clears old history and disables CSV until new history loads', async () => {
  const page = dashboard();
  const requests = historyRequests(page);
  page.run('state.history = [{quote: {ticker: "A"}}]; state.historyStatus = "ready"');
  const loading = select(page, 2);
  assert.equal(page.run('state.history.length'), 0);
  assert.equal(page.nodes.get('download').disabled, true);
  assert.equal(page.run('state.historyStatus'), 'loading');
  requests[0].resolve(rows('B'));
  await loading;
  assert.equal(loadedTicker(page), 'B');
  assert.equal(page.nodes.get('download').disabled, false);
});

test('a failed selection leaves no old chart or downloadable history', async () => {
  const page = dashboard();
  const requests = historyRequests(page);
  page.run('state.history = [{quote: {ticker: "A"}}]; state.historyStatus = "ready"');
  const loading = select(page, 2);
  requests[0].reject(new Error('Synthetic history outage'));
  await loading;
  assert.equal(page.nodes.get('detail-title').textContent, 'Synthetic B');
  assert.equal(page.run('state.history.length'), 0);
  assert.equal(page.run('state.historyStatus'), 'error');
  assert.equal(page.nodes.get('download').disabled, true);
  assert.deepEqual(page.errors, ['Synthetic history outage']);
  page.run('downloadCsv()'); // Must return before trying to create a download.
});

test('an older market response cannot replace the newly selected market', async () => {
  const page = dashboard();
  const requests = historyRequests(page);
  const older = select(page, 1);
  const newer = select(page, 2);
  requests[1].resolve(rows('B'));
  await newer;
  requests[0].resolve(rows('A'));
  await older;
  assert.equal(page.run('state.selected'), 2);
  assert.equal(page.nodes.get('detail-title').textContent, 'Synthetic B');
  assert.equal(loadedTicker(page), 'B');
});

test('an older failure cannot replace a successful current view with an error', async () => {
  const page = dashboard();
  const requests = historyRequests(page);
  const older = select(page, 1);
  const newer = select(page, 2);
  requests[1].resolve(rows('B'));
  await newer;
  requests[0].reject(new Error('Synthetic obsolete failure'));
  await older;
  assert.deepEqual(page.errors, []);
  assert.equal(loadedTicker(page), 'B');
  assert.equal(page.run('state.historyStatus'), 'ready');
  assert.equal(page.nodes.get('download').disabled, false);
});

test('changing ranges ignores history from an older range of the same market', async () => {
  const page = dashboard();
  const requests = historyRequests(page);
  const older = select(page, 1, 24);
  const newer = select(page, 1, 6);
  assert.deepEqual(requests.map(request => request.hours), [24, 6]);
  requests[1].resolve([{ snapshot_id: 6 }]);
  await newer;
  requests[0].resolve([{ snapshot_id: 24 }]);
  await older;
  assert.equal(page.run('state.history[0].snapshot_id'), 6);
});

test('an obsolete completion does not end the current loading state', async () => {
  const page = dashboard();
  const requests = historyRequests(page);
  const older = select(page, 1);
  const newer = select(page, 2);
  requests[0].resolve(rows('A'));
  await older;
  assert.equal(page.nodes.get('chart-box').classList.contains('loading'), true);
  assert.equal(page.nodes.get('download').disabled, true);
  assert.equal(page.run('state.history.length'), 0);
  requests[1].resolve(rows('B'));
  await newer;
  assert.equal(page.nodes.get('chart-box').classList.contains('loading'), false);
});

test('overlapping overview requests retain the newest market list and quotes', async () => {
  const page = dashboard();
  const first = deferred(), second = deferred();
  let calls = 0;
  page.context.fetchMarkets = () => (++calls === 1 ? first.promise : second.promise);
  page.context.fetchQuote = async url => ({ market_id: Number(url.split('/')[2]) });
  page.run('loadAllMarkets = () => fetchMarkets(); getJson = url => fetchQuote(url)');
  const older = page.run('refreshOverview()');
  const newer = page.run('refreshOverview()');
  second.resolve([{ id: 2, title: 'Synthetic B' }]);
  assert.equal(await newer, true);
  first.resolve([{ id: 1, title: 'Synthetic A' }]);
  assert.equal(await older, false);
  assert.equal(page.run('state.markets[0].id'), 2);
  assert.equal(page.run('state.quotes.has(2)'), true);
  assert.equal(page.run('state.quotes.has(1)'), false);
});

test('failed overview refresh retains the previous matching markets and quotes', async () => {
  const page = dashboard();
  page.run('state.quotes = new Map([[1, {market_id: 1}]])');
  page.context.fetchMarkets = async () => [{ id: 3, title: 'Synthetic C' }];
  page.context.fetchQuote = async () => { throw new Error('Synthetic quote outage'); };
  page.run('loadAllMarkets = () => fetchMarkets(); getJson = () => fetchQuote()');
  assert.equal(await page.run('refreshOverview()'), false);
  assert.equal(page.run('state.markets[0].id'), 1);
  assert.equal(page.run('state.quotes.has(1)'), true);
  assert.deepEqual(page.errors, ['Synthetic quote outage']);
});
