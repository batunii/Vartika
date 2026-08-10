// Load the page against a running server, execute its JavaScript, and report
// what rendered and what threw.
//
// This exists because a control can be silently emptied by an unrelated render
// and the page still looks fine: reading the source will not catch it, and
// neither will testing the API. It has already caught two such bugs.
//
//   npm install jsdom
//   node check_page.js <token> [base-url]
//
// Exits non-zero if any check fails.

const { JSDOM, VirtualConsole } = require('jsdom');

const TOKEN = process.argv[2];
const BASE = process.argv[3] || 'http://127.0.0.1:8000';
if (!TOKEN) {
  console.log('usage: node check_page.js <token> [base-url]');
  process.exit(2);
}

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', e => errors.push('jsdomError: ' + (e.stack || e.message)));
vc.on('error', (...a) => errors.push('console.error: ' + a.join(' ')));

const wait = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const dom = await JSDOM.fromURL(`${BASE}/?token=${TOKEN}`, {
    runScripts: 'dangerously',
    resources: 'usable',
    pretendToBeVisual: true,
    virtualConsole: vc,
    beforeParse(window) {
      // jsdom has no fetch. Give the page one carrying the token, so this
      // exercises the same code path a real browser would.
      window.fetch = (input, init = {}) => {
        const url = String(input).startsWith('http') ? String(input) : BASE + String(input);
        const headers = Object.assign({}, init.headers, { 'x-rewrite-token': TOKEN });
        return fetch(url, Object.assign({}, init, { headers })).then(r => ({
          ok: r.ok, status: r.status, json: () => r.json(), text: () => r.text(),
        }));
      };
      window.alert = m => console.log('  [alert]', String(m).slice(0, 120));
      window.confirm = () => true;
    },
  });

  const { window } = dom;
  await wait(4000);
  const $ = id => window.document.getElementById(id);
  let failures = 0;
  const check = (label, actual, ok) => {
    if (!ok) failures++;
    console.log(`  ${ok ? 'ok  ' : 'FAIL'} ${label}: ${JSON.stringify(actual)}`);
  };

  console.log('=== errors while loading ===');
  console.log(errors.length ? errors.join('\n---\n').slice(0, 2000) : '  none');
  if (errors.length) failures++;

  console.log('\n=== the page after loading ===');
  check('no fatal box', $('fatal').hidden ? 'hidden' : $('fatal-message').textContent, $('fatal').hidden);
  check('manuscript button', $('open-name').textContent, $('open-name').textContent.length > 1);
  check('mode button', $('mode-label').textContent, $('mode-label').textContent !== '—');
  check('style button', $('style-name').textContent, $('style-name').textContent.length > 0);
  check('style shows a name not a path', $('style-name').textContent,
        !/[\\/]/.test($('style-name').textContent));
  check('files listed', $('files').children.length, $('files').children.length > 0);
  check('paragraphs listed', $('paras').children.length, $('paras').children.length > 0);
  check('model options', $('model-picker').options.length, $('model-picker').options.length === 3);

  console.log('\n=== opening another file must not disturb the header ===');
  const files = [...$('files').children].filter(n => n.dataset.file);
  if (files[1]) { files[1].click(); await wait(2500); }
  check('mode button survived', $('mode-label').textContent, $('mode-label').textContent !== '—');
  check('style button survived', $('style-name').textContent, $('style-name').textContent.length > 0);
  check('model options survived', $('model-picker').options.length, $('model-picker').options.length === 3);

  console.log('\n=== the sheets open with content in them ===');
  $('btn-modes').click(); await wait(400);
  check('mode cards', $('mode-cards').children.length, $('mode-cards').children.length === 3);
  $('modes-close').click();

  $('btn-style').click(); await wait(600);
  check('style sheet opens', !$('style-backdrop').hidden, !$('style-backdrop').hidden);
  check('style sheet explains state', $('style-current').textContent.slice(0, 40),
        $('style-current').textContent.length > 10);
  $('style-close').click();

  $('btn-changes').click(); await wait(800);
  check('changes sheet opens', !$('changes-backdrop').hidden, !$('changes-backdrop').hidden);
  $('changes-close').click();

  console.log(`\n${failures ? failures + ' CHECK(S) FAILED' : 'all checks passed'}`);
  window.close();
  process.exit(failures ? 1 : 0);
})().catch(e => { console.log('HARNESS FAILED:', e.stack || e.message); process.exit(1); });
