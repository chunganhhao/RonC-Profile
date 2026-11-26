import { test } from '@e2e-dev/web';
import { expect } from 'e2e';

test('Account Warming studio: scenario cards, visual step builder, and task persistence', async ({ app, browser }) => {
  await browser.setViewport({ width: 1440, height: 900 });
  await app.open('/');
  await browser.locator('#token').fill('fixture-token-' + 't'.repeat(32));
  await browser.locator('#login-form button').click();
  await expect(browser.locator('#app')).toBeVisible();

  await browser.locator('#warming-btn').click();
  await expect(browser.locator('[role=dialog]')).toBeVisible();

  // 1. All 6 separated scenario cards render visually; built-in cards advertise interactive step count
  for (const id of ['youtube_trust', 'social_news', 'gmail_trust', 'ecommerce_cookie', 'custom', 'script']) {
    await expect(browser.locator(`[data-scenario="${id}"]`)).toBeVisible();
  }
  await expect(browser.locator('[data-scenario="social_news"]')).toContainText('steps');
  await expect(browser.locator('[data-scenario="gmail_trust"]')).toContainText('steps');

  // 2. Built-in Social & News shows truthful interactive playbook (Google hot news + Wikipedia + HN, no Reddit)
  await browser.locator('[data-scenario="social_news"]').click();
  await expect(browser.locator('[data-scenario="social_news"]')).toHaveAttribute('class', /active/);
  await expect(browser.locator('#warm-steps')).toContainText('www.google.com');
  await expect(browser.locator('#warm-steps')).toContainText('www.wikipedia.org');
  await expect(browser.locator('#warm-steps')).toContainText('#searchInput');
  await expect(browser.locator('#warm-steps')).not.toContainText('reddit.com');

  // Gmail Trust card displays Gmail inbox scroll & email click playbook
  await browser.locator('[data-scenario="gmail_trust"]').click();
  await expect(browser.locator('#warm-steps')).toContainText('mail.google.com');
  await expect(browser.locator('#warm-steps')).toContainText('tr.zA');

  await browser.locator('[data-scenario="social_news"]').click();
  await browser.locator('#warm-payload').fill('https://example.test/override');
  await expect(browser.locator('#warm-steps')).toContainText('example.test/override');
  await expect(browser.locator('#warm-steps')).not.toContainText('www.wikipedia.org');
  await expect(browser.locator('#warm-steps')).not.toContainText('#searchInput');
  await browser.locator('#warm-payload').fill('');

  // 3. Customize as Script (empty draft -> no prompt) copies full playbook into editable visual builder
  await browser.locator('#warm-customize-btn').click();
  await expect(browser.locator('[data-scenario="script"]')).toHaveAttribute('class', /active/);
  await expect(browser.locator('#warm-payload')).toHaveValue(/wikipedia\.org/);
  await expect(browser.locator('#warm-steps')).toContainText('#searchInput');

  const initialCount = await browser.locator('.warm-step-row').count();
  expect(initialCount).toBeGreaterThan(5);

  // Add a step visually and verify step list & JSON stay synced
  await browser.locator('#warm-add-action').selectOption('click');
  await browser.locator('#warm-add-val').fill('#main-banner');
  await browser.locator('#warm-add-btn').click();
  await expect(browser.locator('.warm-step-row')).toHaveCount(initialCount + 1);
  await expect(browser.locator('#warm-payload')).toHaveValue(/#main-banner/);

  // Reorder last step up, then remove the last step visually
  await browser.locator('[data-step-up]').last().click();
  await browser.locator('[data-step-del]').last().click();
  await expect(browser.locator('.warm-step-row')).toHaveCount(initialCount);

  // 4. [null] JSON item and non-numeric wait input are rejected cleanly without uncaught errors
  await browser.locator('#warm-payload').fill('[null]');
  await expect(browser.locator('#warm-steps .msg.err')).toContainText('Step 1');
  await browser.locator('[data-warm-tpl="social_news"]').click();
  // Confirm replacing non-empty unsaved JSON draft
  await browser.locator('.modal:has-text("Replace your unsaved") .btn-act').click();
  await browser.locator('#warm-add-action').selectOption('wait');
  await browser.locator('#warm-add-val').fill('abc');
  await browser.locator('#warm-add-btn').click();
  await expect(browser.locator('#warm-msg')).toHaveAttribute('class', /err/);

  // 5. Invalid script shows clean REST 400 validation error without saving
  await browser.locator('#warm-payload').fill('[{"action":"evaluate","script":"alert(1)"}]');
  await browser.locator('#warm-save-btn').click();
  await expect(browser.locator('#warm-msg')).toHaveAttribute('class', /err/);

  // 6. Load Social template (confirm overwrite), toggle Hide Browser, save schedule, verify persistence across reload, then delete
  await browser.locator('[data-warm-tpl="social_news"]').click();
  await browser.locator('.modal:has-text("Replace your unsaved") .btn-act').click();
  await browser.locator('#warm-name').fill('E2E Social Flow');
  await expect(browser.locator('#warm-headless')).not.toBeChecked();
  await browser.locator('#warm-headless').check();
  await expect(browser.locator('#warm-headless')).toBeChecked();
  await browser.locator('#warm-save-btn').click();
  await expect(browser.locator('#warm-tasks')).toContainText('E2E Social Flow');
  await expect(browser.locator('#warm-tasks')).toContainText('Hidden');

  await browser.reload();
  await browser.locator('#warming-btn').click();
  await expect(browser.locator('#warm-tasks')).toContainText('E2E Social Flow');
  await expect(browser.locator('#warm-tasks')).toContainText('Hidden');

  await browser.locator('[data-warm-del]').first().click();
  await expect(browser.locator('#warm-tasks')).not.toContainText('E2E Social Flow');
});

test('Account Warming studio: redacts malformed URLs, gates delayed catalog, guards unsaved drafts, and polls queue live', async ({ app, browser }) => {
  await browser.setViewport({ width: 1440, height: 900 });
  await app.open('/');
  await browser.locator('#token').fill('fixture-token-' + 't'.repeat(32));
  await browser.locator('#login-form button').click();
  await expect(browser.locator('#app')).toBeVisible();

  await browser.evaluate(() => {
    const w = window as unknown as Record<string, unknown>;
    w.__warmPageErrors = [];
    window.addEventListener('error', (e) => (w.__warmPageErrors as string[]).push(e.message));
    const origFetch = window.fetch.bind(window);
    const fullSteps = Array.from({ length: 15 }, (_, i) => ({
      action: i === 0 ? 'navigate' : 'wait',
      ...(i === 0 ? { url: 'https://www.wikipedia.org' } : { seconds: 1 }),
    }));
    const catalogPayload = [
      { id: 'youtube_trust', recommended_minutes: 4, default_urls: ['https://www.youtube.com'], sample_steps: fullSteps },
      { id: 'social_news', recommended_minutes: 5, default_urls: ['https://www.google.com', 'https://www.wikipedia.org'], sample_steps: fullSteps },
      { id: 'gmail_trust', recommended_minutes: 4, default_urls: ['https://mail.google.com/mail/u/0/#inbox'], sample_steps: fullSteps },
      { id: 'ecommerce_cookie', recommended_minutes: 4, default_urls: ['https://www.amazon.com'], sample_steps: fullSteps },
      { id: 'custom', recommended_minutes: 3, default_urls: [], sample_steps: [] },
      { id: 'script', recommended_minutes: 3, default_urls: [], sample_steps: [{ action: 'navigate', url: 'https://news.google.com' }] },
    ];
    let phase = 0; // 0: fail GET, 1: idle, 2: running, 3+: completed
    w.__warmGetCount = 0;
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input instanceof Request ? input.url : String(input);
      if (url.endsWith('/api/v1/warming/tasks')) {
        w.__warmGetCount = (w.__warmGetCount as number) + 1;
        phase += 1;
        if (phase === 1) {
          return new Response(JSON.stringify({ detail: 'Catalog offline' }), {
            status: 503,
            headers: { 'content-type': 'application/json' },
          });
        }
        const status = phase === 2 ? 'idle' : phase === 3 ? 'running' : 'completed';
        const lastResult = status === 'completed' ? 'OK (1/1 profiles)' : '';
        return new Response(JSON.stringify({
          scenarios: catalogPayload,
          tasks: [{
            id: 't-live',
            name: 'Live Poll Task',
            profile_names: ['Studio Gmail'],
            preset: 'youtube_trust',
            duration_minutes: 3,
            schedule_time: '',
            status,
            last_result: lastResult,
          }],
        }), { status: 200, headers: { 'content-type': 'application/json' } });
      }
      if (url.endsWith('/api/v1/warming/tasks/t-live/run')) {
        return new Response(JSON.stringify({ started: false, task_id: 't-live' }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        });
      }
      return origFetch(input, init);
    };
  });

  await browser.locator('#warming-btn').click();
  await expect(browser.locator('[role=dialog]')).toBeVisible();

  // #5 Failed GET disables Customize and surfaces Retry, while allowing custom Script authoring
  await expect(browser.locator('#warm-customize-btn')).toBeDisabled();
  await expect(browser.locator('#warm-retry-btn')).toBeVisible();

  await browser.locator('[data-scenario="script"]').click();
  const unsavedDraft = '[{"action":"navigate","url":"https://user.example/unsaved"}]';
  await browser.locator('#warm-payload').fill(unsavedDraft);

  // Retry succeeds without overwriting user's /unsaved script draft
  await browser.locator('#warm-retry-btn').click();
  await expect(browser.locator('#warm-payload')).toHaveValue(unsavedDraft);
  await expect(browser.locator('[data-warm-tpl="social_news"]')).toBeEnabled();

  // #6 Customize over unsaved draft prompts; Cancel keeps JSON, preset, and duration intact
  await browser.locator('[data-scenario="social_news"]').click();
  await browser.locator('#warm-duration').fill('12');
  await expect(browser.locator('#warm-customize-btn')).toBeEnabled();
  await browser.locator('#warm-customize-btn').click();
  await expect(browser.locator('.modal:has-text("Replace your unsaved")')).toBeVisible();
  await browser.locator('.modal:has-text("Replace your unsaved") button:has-text("Cancel")').click();
  await expect(browser.locator('[data-scenario="social_news"]')).toHaveAttribute('class', /active/);
  await expect(browser.locator('#warm-duration')).toHaveValue('12');
  await browser.locator('[data-scenario="script"]').click();
  await expect(browser.locator('#warm-payload')).toHaveValue(unsavedDraft);

  // #3 Malformed URL & {"toString":null} render safely as "URL" with zero raw secret leak or uncaught error
  await browser.locator('#warm-payload').fill('[{"action":"navigate","url":"https://u:DUMMY@x:bad/?q=FAKE#z"}]');
  await expect(browser.locator('#warm-steps .warm-step-det')).toHaveText('URL');
  await expect(browser.locator('#warm-steps .warm-step-det')).toHaveAttribute('title', 'URL');
  await expect(browser.locator('#warm-steps')).not.toContainText('DUMMY');
  await expect(browser.locator('#warm-steps')).not.toContainText('FAKE');

  await browser.locator('#warm-payload').fill('[{"action":"navigate","url":{"toString":null}}]');
  await expect(browser.locator('#warm-steps .warm-step-det')).toHaveText('URL');
  const pageErrors = await browser.evaluate(() => (window as unknown as { __warmPageErrors: string[] }).__warmPageErrors);
  expect(pageErrors).toEqual([]);

  // #4 Live queue polling transitions idle -> running (Stop) -> completed (Launch) while modal stays open
  await expect(browser.locator('#warm-tasks button:has-text("Stop")')).toBeVisible({ timeout: 5000 });
  await expect(browser.locator('#warm-tasks button:has-text("Launch")')).toBeVisible({ timeout: 5000 });
  await expect(browser.locator('#warm-tasks')).toContainText('OK (1/1 profiles)');

  // started:false on Launch shows explicit error instead of silent success
  await browser.locator('#warm-tasks button:has-text("Launch")').click();
  await expect(browser.locator('#warm-msg')).toHaveAttribute('class', /err/);

  // Closing modal stops background queue polling
  await browser.locator('.modal-foot button:has-text("Close")').click();
  const countAtClose = await browser.evaluate(() => (window as unknown as { __warmGetCount: number }).__warmGetCount);
  await new Promise((r) => setTimeout(r, 2300));
  const countLater = await browser.evaluate(() => (window as unknown as { __warmGetCount: number }).__warmGetCount);
  expect(countLater).toBe(countAtClose);
});

test('Account Warming studio: slow >2s GETs are not self-aborted and unsupported overrides are rejected cleanly', async ({ app, browser }) => {
  await browser.setViewport({ width: 1440, height: 900 });
  await app.open('/');
  await browser.locator('#token').fill('fixture-token-' + 't'.repeat(32));
  await browser.locator('#login-form button').click();
  await expect(browser.locator('#app')).toBeVisible();

  await browser.evaluate(() => {
    const w = window as unknown as Record<string, unknown>;
    const origFetch = window.fetch.bind(window);
    const fullSteps = Array.from({ length: 15 }, (_, i) => ({
      action: i === 0 ? 'navigate' : 'wait',
      ...(i === 0 ? { url: 'https://www.wikipedia.org' } : { seconds: 1 }),
    }));
    const catalogPayload = [
      { id: 'youtube_trust', recommended_minutes: 4, default_urls: ['https://www.youtube.com'], sample_steps: fullSteps },
      { id: 'social_news', recommended_minutes: 5, default_urls: ['https://www.google.com', 'https://www.wikipedia.org'], sample_steps: fullSteps },
      { id: 'gmail_trust', recommended_minutes: 4, default_urls: ['https://mail.google.com/mail/u/0/#inbox'], sample_steps: fullSteps },
      { id: 'ecommerce_cookie', recommended_minutes: 4, default_urls: ['https://www.amazon.com'], sample_steps: fullSteps },
      { id: 'custom', recommended_minutes: 3, default_urls: [], sample_steps: [] },
      { id: 'script', recommended_minutes: 3, default_urls: [], sample_steps: [{ action: 'navigate', url: 'https://news.google.com' }] },
    ];
    let getIdx = 0;
    w.__warmAbortedGets = 0;
    w.__warmPostBodies = [];
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input instanceof Request ? input.url : String(input);
      const method = (init?.method || 'GET').toUpperCase();
      if (url.endsWith('/api/v1/warming/tasks') && method === 'GET') {
        getIdx += 1;
        const status = getIdx === 1 ? 'idle' : 'running';
        await new Promise<void>((resolve, reject) => {
          const timer = setTimeout(resolve, 2350);
          init?.signal?.addEventListener('abort', () => {
            clearTimeout(timer);
            w.__warmAbortedGets = (w.__warmAbortedGets as number) + 1;
            reject(new DOMException('Aborted', 'AbortError'));
          });
        });
        return new Response(JSON.stringify({
          scenarios: catalogPayload,
          tasks: [{
            id: 't-slow',
            name: 'Slow Backend Task',
            profile_names: ['Studio Gmail'],
            preset: 'social_news',
            duration_minutes: 4,
            schedule_time: '',
            status,
            last_result: '',
          }],
        }), { status: 200, headers: { 'content-type': 'application/json' } });
      }
      if (url.endsWith('/api/v1/warming/tasks') && method === 'POST') {
        const parsed = JSON.parse(String(init?.body || '{}'));
        (w.__warmPostBodies as unknown[]).push(parsed);
        return new Response(JSON.stringify({ ...parsed, id: 't-created', status: 'idle', last_result: '' }), {
          status: 201,
          headers: { 'content-type': 'application/json' },
        });
      }
      return origFetch(input, init);
    };
  });

  await browser.locator('#warming-btn').click();
  await expect(browser.locator('[role=dialog]')).toBeVisible();

  // §9.2 Slow 2.35s GET #1 completes without self-abort, enables Customize, and slow GET #2 updates idle -> running
  await expect(browser.locator('#warm-customize-btn')).toBeEnabled({ timeout: 5000 });
  await expect(browser.locator('#warm-tasks button:has-text("Launch")')).toBeVisible();
  await expect(browser.locator('#warm-tasks button:has-text("Stop")')).toBeVisible({ timeout: 6000 });
  const aborted = await browser.evaluate(() => (window as unknown as { __warmAbortedGets: number }).__warmAbortedGets);
  expect(aborted).toBe(0);

  // §9.4 Unsupported scheme override is flagged in preview and blocked on Save (zero silent Wikipedia default save)
  await browser.locator('[data-scenario="social_news"]').click();
  await browser.locator('#warm-payload').fill('ftp://example.test/override');
  await expect(browser.locator('#warm-steps .msg.err')).toBeVisible();
  await expect(browser.locator('#warm-steps')).not.toContainText('example.test/override');
  await browser.locator('#warm-save-btn').click();
  await expect(browser.locator('#warm-msg')).toHaveAttribute('class', /err/);
  const postsBeforeValid = await browser.evaluate(() => (window as unknown as { __warmPostBodies: unknown[] }).__warmPostBodies.length);

  // Mixed override displays only valid HTTP(S) target, warns about ignored token, and submits filtered list
  await browser.locator('#warm-payload').fill('ftp://bad.test https://good.example/override');
  await expect(browser.locator('#warm-steps')).toContainText('good.example/override');
  await expect(browser.locator('#warm-mode-note')).toContainText('1 unsupported');
  await browser.locator('#warm-save-btn').click();
  await expect(browser.locator('#warm-msg')).toHaveAttribute('class', /ok/);
  const postBodies = await browser.evaluate(() => (window as unknown as { __warmPostBodies: Array<{ custom_urls: string[] }> }).__warmPostBodies);
  expect(postsBeforeValid).toBe(postBodies.length - 1);
  expect(postBodies.at(-1)?.custom_urls).toEqual(['https://good.example/override']);
});

test('Account Warming studio: Web rejects extra-slash URLs like backend and decouples Save from hung GET timeout', async ({ app, browser }) => {
  await browser.setViewport({ width: 1440, height: 900 });
  await app.open('/');
  await browser.locator('#token').fill('fixture-token-' + 't'.repeat(32));
  await browser.locator('#login-form button').click();
  await expect(browser.locator('#app')).toBeVisible();

  // §11.4 Real REST loopback: extra-slash URL is rejected in preview & filtered/blocked consistently
  await browser.locator('#warming-btn').click();
  await expect(browser.locator('[role=dialog]')).toBeVisible();
  await expect(browser.locator('#warm-customize-btn')).toBeEnabled();

  await browser.locator('[data-scenario="social_news"]').click();
  await browser.locator('#warm-payload').fill('https:///bad.example/old https://good.example/only');
  await expect(browser.locator('#warm-steps')).toContainText('good.example/only');
  await expect(browser.locator('#warm-steps')).not.toContainText('bad.example');
  await expect(browser.locator('#warm-mode-note')).toContainText('1 unsupported');
  await browser.locator('#warm-save-btn').click();
  await expect(browser.locator('#warm-msg')).toHaveAttribute('class', /ok/);

  const savedCheck = await browser.evaluate(async () => {
    const r = await fetch('/api/v1/warming/tasks', {
      headers: { Authorization: 'Bearer ' + localStorage.getItem('foxprofile.token') },
    });
    return r.json();
  });
  expect(savedCheck.tasks.at(-1).custom_urls).toEqual(['https://good.example/only']);

  // Pure extra-slash override shows preview error (never active) and fails client-side without POST
  await browser.locator('#warm-payload').fill('https:///bad.example/old');
  await expect(browser.locator('#warm-steps .msg.err')).toBeVisible();
  await expect(browser.locator('#warm-steps')).not.toContainText('bad.example');
  await browser.locator('#warm-save-btn').click();
  await expect(browser.locator('#warm-msg')).toHaveAttribute('class', /err/);

  // Clean up test task and close modal
  await browser.locator('[data-warm-del]').first().click();
  await expect(browser.locator('#warm-tasks')).not.toContainText('Warm 1 profile(s)');
  await browser.locator('.modal-foot button:has-text("Close")').click();

  // §11.2 Hung GET #1 (>5s timeout) does NOT freeze Save/Run buttons and recovers on Retry
  await browser.evaluate(() => {
    const w = window as unknown as Record<string, unknown>;
    const origFetch = window.fetch.bind(window);
    let getIdx = 0;
    w.__warmHangUnblock = false;
    w.__warmHungPosts = 0;
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input instanceof Request ? input.url : String(input);
      const method = (init?.method || 'GET').toUpperCase();
      if (url.endsWith('/api/v1/warming/tasks') && method === 'GET') {
        getIdx += 1;
        if (getIdx === 1 && !w.__warmHangUnblock) {
          return await new Promise<Response>((_, reject) => {
            init?.signal?.addEventListener('abort', () => {
              reject(new DOMException('Aborted', 'AbortError'));
            });
          });
        }
      }
      if (url.endsWith('/api/v1/warming/tasks') && method === 'POST') {
        w.__warmHungPosts = (w.__warmHungPosts as number) + 1;
      }
      return origFetch(input, init);
    };
  });

  await browser.locator('#warming-btn').click();
  await expect(browser.locator('[role=dialog]')).toBeVisible();
  await browser.locator('[data-scenario="custom"]').click();
  await browser.locator('#warm-payload').fill('https://good.example/hung-save');
  await browser.locator('#warm-save-btn').click();

  // Save completes & re-enables Save/Run immediately despite hung GET #1
  await expect(browser.locator('#warm-msg')).toHaveAttribute('class', /ok/);
  await expect(browser.locator('#warm-save-btn')).toBeEnabled();
  await expect(browser.locator('#warm-run-btn')).toBeEnabled();
  await expect(browser.locator('#warm-tasks')).toContainText('Warm 1 profile(s)');

  // After independent GET timeout (5s), Retry becomes visible and recovers queue when unblocked
  await expect(browser.locator('#warm-retry-btn')).toBeVisible({ timeout: 7500 });
  await browser.evaluate(() => {
    (window as unknown as Record<string, unknown>).__warmHangUnblock = true;
  });
  await browser.locator('#warm-retry-btn').click();
  await expect(browser.locator('#warm-customize-btn')).toBeEnabled({ timeout: 5000 });
  const postsMade = await browser.evaluate(() => (window as unknown as { __warmHungPosts: number }).__warmHungPosts);
  expect(postsMade).toBe(1);

  await browser.locator('[data-warm-del]').first().click();
});

test('Account Warming studio: recovers empty queue after 503, blocks stale GET overwrites on Save/Delete, and matches backend port validation', async ({ app, browser }) => {
  await browser.setViewport({ width: 1440, height: 900 });
  await app.open('/');
  await browser.locator('#token').fill('fixture-token-' + 't'.repeat(32));
  await browser.locator('#login-form button').click();
  await expect(browser.locator('#app')).toBeVisible();

  await browser.evaluate(() => {
    const w = window as unknown as Record<string, unknown>;
    const origFetch = window.fetch.bind(window);
    const held: Array<() => void> = [];
    w.__s13Mode = 'pass';
    w.__s13HeldCount = 0;
    w.__s13ReleaseOne = () => {
      const fn = held.shift();
      if (fn) {
        w.__s13HeldCount = held.length;
        fn();
        return true;
      }
      return false;
    };
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input instanceof Request ? input.url : String(input);
      const method = (init?.method || 'GET').toUpperCase();
      if (url.endsWith('/api/v1/warming/tasks') && method === 'GET') {
        const mode = String(w.__s13Mode || 'pass');
        if (mode === 'fail503') {
          return new Response(JSON.stringify({ detail: 'Simulated 503 outage' }), {
            status: 503,
            headers: { 'content-type': 'application/json' },
          });
        }
        const realResp = await origFetch(input, init);
        if (w.__s13Mode === 'hold') {
          await new Promise<void>((resolve, reject) => {
            held.push(resolve);
            w.__s13HeldCount = held.length;
            init?.signal?.addEventListener('abort', () => {
              const idx = held.indexOf(resolve);
              if (idx !== -1) held.splice(idx, 1);
              w.__s13HeldCount = held.length;
              reject(new DOMException('Aborted', 'AbortError'));
            });
          });
        }
        return realResp;
      }
      return origFetch(input, init);
    };
  });

  const waitForHeldCount = async (target: number) => {
    const deadline = Date.now() + 6000;
    while (Date.now() < deadline) {
      const cnt = await browser.evaluate(() => (window as unknown as { __s13HeldCount: number }).__s13HeldCount);
      if (cnt >= target) return;
      await new Promise((r) => setTimeout(r, 100));
    }
    throw new Error(`Timed out waiting for __s13HeldCount >= ${target}`);
  };

  // §13.1: 200 [] -> 503 -> 200 [] poll recovery & Retry recovery without clearing client validation msg
  await browser.locator('#warming-btn').click();
  await expect(browser.locator('[role=dialog]')).toBeVisible();
  await expect(browser.locator('#warm-customize-btn')).toBeEnabled();
  await expect(browser.locator('#warm-tasks')).toContainText('No warming tasks yet');

  await browser.evaluate(() => { (window as unknown as Record<string, unknown>).__s13Mode = 'fail503'; });
  await expect(browser.locator('#warm-tasks .msg.err')).toContainText('Simulated 503 outage', { timeout: 5000 });

  await browser.locator('[data-scenario="custom"]').click();
  await browser.locator('#warm-payload').fill('');
  await browser.locator('#warm-save-btn').click();
  await expect(browser.locator('#warm-msg')).toHaveAttribute('class', /err/);

  await browser.evaluate(() => { (window as unknown as Record<string, unknown>).__s13Mode = 'pass'; });
  await expect(browser.locator('#warm-tasks .msg.err')).toHaveCount(0, { timeout: 5000 });
  await expect(browser.locator('#warm-tasks')).toContainText('No warming tasks yet');
  await expect(browser.locator('#warm-msg')).toHaveAttribute('class', /err/);

  await browser.evaluate(() => { (window as unknown as Record<string, unknown>).__s13Mode = 'fail503'; });
  await expect(browser.locator('#warm-tasks .msg.err')).toContainText('Simulated 503 outage', { timeout: 5000 });
  await browser.evaluate(() => { (window as unknown as Record<string, unknown>).__s13Mode = 'pass'; });
  await browser.locator('#warm-tasks button:has-text("Retry")').click();
  await expect(browser.locator('#warm-tasks .msg.err')).toHaveCount(0, { timeout: 5000 });
  await expect(browser.locator('#warm-tasks')).toContainText('No warming tasks yet');

  // §13.2: In-flight stale GET started before Save or Delete must never overwrite optimistic queue
  await browser.evaluate(() => { (window as unknown as Record<string, unknown>).__s13Mode = 'hold'; });
  await waitForHeldCount(1);

  await browser.locator('#warm-name').fill('Stale GET Guard');
  await browser.locator('[data-scenario="custom"]').click();
  await browser.locator('#warm-payload').fill('https://example.test:8443/ok');
  await browser.locator('#warm-save-btn').click();
  await expect(browser.locator('#warm-msg')).toHaveAttribute('class', /ok/);
  await expect(browser.locator('#warm-tasks')).toContainText('Stale GET Guard');
  await expect(browser.locator('#warm-save-btn')).toBeEnabled();
  await expect(browser.locator('#warm-run-btn')).toBeEnabled();

  // Release stale empty GET #1 while still holding follow-up GET #2 -> saved row must remain visible
  const rel1 = await browser.evaluate(() => (window as unknown as { __s13ReleaseOne: () => boolean }).__s13ReleaseOne());
  expect(rel1).toBe(true);
  await waitForHeldCount(1);
  await expect(browser.locator('#warm-tasks')).toContainText('Stale GET Guard');
  await expect(browser.locator('#warm-tasks')).not.toContainText('No warming tasks yet');

  // Release GET #2 ([Stale GET Guard]) -> wait for next held poll GET #3 (reading [Stale GET Guard])
  await browser.evaluate(() => (window as unknown as { __s13ReleaseOne: () => boolean }).__s13ReleaseOne());
  await waitForHeldCount(1);

  // Delete while GET #3 (containing Stale GET Guard) is in flight, then release GET #3 while holding GET #4
  await browser.locator('[data-warm-del]').first().click();
  await expect(browser.locator('#warm-tasks')).toContainText('No warming tasks yet');
  await expect(browser.locator('#warm-tasks')).not.toContainText('Stale GET Guard');

  const rel3 = await browser.evaluate(() => (window as unknown as { __s13ReleaseOne: () => boolean }).__s13ReleaseOne());
  expect(rel3).toBe(true);
  await waitForHeldCount(1);
  await expect(browser.locator('#warm-tasks')).not.toContainText('Stale GET Guard');
  await expect(browser.locator('#warm-tasks')).toContainText('No warming tasks yet');

  await browser.evaluate(() => {
    const w = window as unknown as Record<string, unknown>;
    w.__s13Mode = 'pass';
    (w.__s13ReleaseOne as () => boolean)();
  });
  await expect(browser.locator('#warm-tasks')).toContainText('No warming tasks yet');

  // §13.3: Web and real REST loopback match on out-of-range port rejection and mixed filtering
  await browser.locator('[data-scenario="social_news"]').click();
  await browser.locator('#warm-payload').fill('https://example.test:70000/');
  await expect(browser.locator('#warm-steps .msg.err')).toBeVisible();
  await expect(browser.locator('#warm-steps')).not.toContainText('70000');
  await browser.locator('#warm-save-btn').click();
  await expect(browser.locator('#warm-msg')).toHaveAttribute('class', /err/);

  await browser.locator('#warm-payload').fill('https://example.test:70000/ https://example.test:8443/ok');
  await expect(browser.locator('#warm-steps')).toContainText('example.test:8443/ok');
  await expect(browser.locator('#warm-steps')).not.toContainText('70000');
  await expect(browser.locator('#warm-mode-note')).toContainText('1 unsupported');
  await browser.locator('#warm-save-btn').click();
  await expect(browser.locator('#warm-msg')).toHaveAttribute('class', /ok/);

  const savedPortCheck = await browser.evaluate(async () => {
    const r = await fetch('/api/v1/warming/tasks', {
      headers: { Authorization: 'Bearer ' + localStorage.getItem('foxprofile.token') },
    });
    return r.json();
  });
  expect(savedPortCheck.tasks.at(-1).custom_urls).toEqual(['https://example.test:8443/ok']);

  await browser.locator('[data-warm-del]').first().click();
  await expect(browser.locator('#warm-tasks')).toContainText('No warming tasks yet');
});

test('Warming keeps a custom campaign name when targets change', async ({ app, browser }) => {
  await app.open('/');
  await browser.locator('#token').fill('fixture-token-' + 't'.repeat(32));
  await browser.locator('#login-form button').click();
  await expect(browser.locator('#app')).toBeVisible();
  await browser.locator('#warming-btn').click();

  const name = browser.locator('#warm-name');
  const secondTarget = browser.locator('.warm-prof-chip input:not(:checked)').first();
  await expect(name).toHaveValue('Warm 1 profile(s)');
  await secondTarget.click();
  await expect(name).toHaveValue('Warm 2 profile(s)');
  await name.fill('Weekend campaign');
  await browser.locator('.warm-prof-chip input:checked').last().click();
  await expect(name).toHaveValue('Weekend campaign');
  await name.fill('Warm 1 profile(s)');
  await secondTarget.click();
  await expect(name).toHaveValue('Warm 2 profile(s)');
  await name.fill('');
  await browser.locator('.warm-prof-chip input:checked').last().click();
  await expect(name).toHaveValue('');
});

test('Warming shows active status instead of the previous result during a rerun', async ({ app, browser }) => {
  await app.open('/');
  await browser.locator('#token').fill('fixture-token-' + 't'.repeat(32));
  await browser.locator('#login-form button').click();
  await expect(browser.locator('#app')).toBeVisible();

  await browser.evaluate(() => {
    const originalFetch = window.fetch.bind(window);
    let running = false;
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input instanceof Request ? input.url : String(input);
      if (url.endsWith('/warming/tasks/repeat-task/run')) {
        running = true;
        return Response.json({ started: true, task_id: 'repeat-task' });
      }
      const response = await originalFetch(input, init);
      if (!url.endsWith('/warming/tasks') || !response.ok) return response;
      const catalog = await response.json();
      return Response.json({
        ...catalog,
        tasks: [{
          id: 'repeat-task', name: 'Repeat campaign', profile_names: ['Studio Gmail'],
          preset: 'custom', duration_minutes: 3, schedule_time: '',
          status: running ? 'running' : 'completed', last_result: 'OK (1/1 profiles)',
        }],
      });
    };
  });
  await browser.locator('#warming-btn').click();
  const task = '.warm-task-row:has-text("Repeat campaign")';
  const stamp = browser.locator(`${task} .stamp`);
  await expect(stamp).toContainText('OK (1/1 profiles)');
  await browser.locator(`${task} button:has-text("Launch")`).click();
  await expect(browser.locator(`${task} button:has-text("Stop")`)).toBeVisible();
  await expect(stamp).toContainText('Running');
});

test('Warming keeps keyboard focus after a scenario is activated', async ({ app, browser }) => {
  await app.open('/');
  await browser.locator('#token').fill('fixture-token-' + 't'.repeat(32));
  await browser.locator('#login-form button').click();
  await browser.evaluate(() => {
    const originalFetch = window.fetch.bind(window);
    const resume = new Promise<void>((resolve) => {
      window.addEventListener('test-warming-catalog-ready', () => resolve(), { once: true });
    });
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const response = await originalFetch(input, init);
      const url = typeof input === 'string' ? input : input instanceof Request ? input.url : String(input);
      if (!url.endsWith('/warming/tasks') || !response.ok) return response;
      await resume;
      const catalog = await response.json();
      return Response.json({
        ...catalog,
        scenarios: catalog.scenarios.filter((scenario: { id: string }) => scenario.id !== 'custom'),
      });
    };
  });
  await browser.locator('#warming-btn').click();
  await browser.evaluate(() => {
    document.querySelector<HTMLElement>('[data-scenario="custom"]')?.focus();
    window.dispatchEvent(new Event('test-warming-catalog-ready'));
  });
  await expect(browser.locator('#warm-customize-btn')).toBeEnabled();
  expect(await browser.evaluate(() => (document.activeElement as HTMLElement).dataset.scenario)).toBe('youtube_trust');

  await browser.evaluate(() => document.querySelector<HTMLElement>('[data-scenario="social_news"]')?.focus());
  await browser.keyboard.press('Enter');
  await expect(browser.locator('[data-scenario="social_news"]')).toHaveAttribute('class', /active/);
  const nextScenario = await browser.evaluate(() =>
    document.querySelector('[data-scenario="social_news"]')?.nextElementSibling?.getAttribute('data-scenario')
  );
  expect(nextScenario).toBeTruthy();
  await browser.keyboard.press('Tab');
  expect(await browser.evaluate(() => (document.activeElement as HTMLElement).dataset.scenario)).toBe(nextScenario);
});

test('Warming keeps long campaign names inside the queue at mobile and desktop widths', async ({ app, browser }) => {
  await app.open('/');
  await browser.locator('#token').fill('fixture-token-' + 't'.repeat(32));
  await browser.locator('#login-form button').click();
  await browser.locator('#warming-btn').click();
  const longName = 'LongCampaignName'.repeat(5);
  await browser.locator('#warm-name').fill(longName);
  await browser.locator('#warm-save-btn').click();
  await expect(browser.locator('#warm-tasks')).toContainText(longName);

  for (const width of [375, 1000]) {
    await browser.setViewport({ width, height: 700 });
    const fits = await browser.evaluate(() => {
      const queue = document.querySelector('#warm-tasks')!;
      const name = queue.querySelector('.warm-task-name')!;
      return queue.scrollWidth === queue.clientWidth && name.scrollWidth === name.clientWidth;
    });
    expect(fits).toBe(true);
    await expect(browser.locator(`[aria-label="Delete: ${longName}"]`)).toBeVisible();
  }
  await browser.locator(`[aria-label="Delete: ${longName}"]`).click();
  await expect(browser.locator('#warm-tasks')).not.toContainText(longName);
});

test('Account Warming studio: background GET timeout and recovery preserve failed DELETE error and task row', async ({ app, browser }) => {
  await browser.setViewport({ width: 1440, height: 900 });
  await app.open('/');
  await browser.locator('#token').fill('fixture-token-' + 't'.repeat(32));
  await browser.locator('#login-form button').click();
  await expect(browser.locator('#app')).toBeVisible();

  await browser.evaluate(async () => {
    const token = localStorage.getItem('foxprofile.token') || '';
    const r = await fetch('/api/v1/warming/tasks', {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        Authorization: 'Bearer ' + token,
      },
      body: JSON.stringify({
        name: 'Keep On Delete Error',
        profile_names: ['Studio Profile'],
        preset: 'custom',
        custom_urls: ['https://example.test/keep'],
        duration_minutes: 3,
      }),
    });
    if (!r.ok) throw new Error('Seed POST failed: ' + r.status);
  });

  await browser.evaluate(() => {
    const w = window as unknown as Record<string, unknown>;
    const origFetch = window.fetch.bind(window);
    let getIdx = 0;
    w.__s15Get2Started = false;
    w.__s15Get2Aborted = false;
    w.__s15RecoveredGets = 0;
    w.__s15FailDelete = true;
    w.__s15DeleteCalls = 0;
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input instanceof Request ? input.url : String(input);
      const method = (init?.method || 'GET').toUpperCase();
      if (url.endsWith('/api/v1/warming/tasks') && method === 'GET') {
        getIdx += 1;
        if (getIdx === 2) {
          w.__s15Get2Started = true;
          return await new Promise<Response>((_, reject) => {
            init?.signal?.addEventListener('abort', () => {
              w.__s15Get2Aborted = true;
              reject(new DOMException('Aborted', 'AbortError'));
            });
          });
        }
        if (getIdx >= 3) {
          w.__s15RecoveredGets = (w.__s15RecoveredGets as number) + 1;
        }
      }
      if (/\/api\/v1\/warming\/tasks\/[^/]+$/.test(url) && method === 'DELETE' && w.__s15FailDelete) {
        w.__s15DeleteCalls = (w.__s15DeleteCalls as number) + 1;
        return new Response(JSON.stringify({ detail: 'Fixture: could not delete' }), {
          status: 500,
          headers: { 'content-type': 'application/json' },
        });
      }
      return origFetch(input, init);
    };
  });

  await browser.locator('#warming-btn').click();
  await expect(browser.locator('[role=dialog]')).toBeVisible();
  await expect(browser.locator('#warm-customize-btn')).toBeEnabled();
  await expect(browser.locator('#warm-tasks')).toContainText('Keep On Delete Error');

  const deadline = Date.now() + 5000;
  let get2Started = false;
  while (Date.now() < deadline) {
    get2Started = await browser.evaluate(() => Boolean((window as unknown as { __s15Get2Started: boolean }).__s15Get2Started));
    if (get2Started) break;
    await new Promise((r) => setTimeout(r, 100));
  }
  expect(get2Started).toBe(true);

  // Trigger failing DELETE 500 while background poll GET #2 is in-flight
  await browser.locator('[data-warm-del]').first().click();
  await expect(browser.locator('#warm-msg')).toHaveAttribute('class', /err/);
  await expect(browser.locator('#warm-msg')).toContainText('Fixture: could not delete');
  await expect(browser.locator('#warm-tasks')).toContainText('Keep On Delete Error');

  // Background GET #2 hits 5s timeout: queue banner shows timeout error, action feedback keeps DELETE error
  await expect(browser.locator('#warm-tasks .msg.err')).toContainText('Request timed out', { timeout: 7500 });
  await expect(browser.locator('#warm-msg')).toHaveAttribute('class', /err/);
  await expect(browser.locator('#warm-msg')).toContainText('Fixture: could not delete');
  await expect(browser.locator('#warm-tasks')).toContainText('Keep On Delete Error');

  // Subsequent poll / Retry recovers queue banner (GET #3 returns 200) without wiping DELETE error or task row
  await browser.locator('#warm-tasks button:has-text("Retry")').click();
  await expect(browser.locator('#warm-tasks .msg.err')).toHaveCount(0, { timeout: 5000 });
  await expect(browser.locator('#warm-msg')).toHaveAttribute('class', /err/);
  await expect(browser.locator('#warm-msg')).toContainText('Fixture: could not delete');
  await expect(browser.locator('#warm-tasks')).toContainText('Keep On Delete Error');

  const state = await browser.evaluate(() => {
    const w = window as unknown as { __s15Get2Aborted: boolean; __s15RecoveredGets: number; __s15DeleteCalls: number };
    return { aborted: w.__s15Get2Aborted, recovered: w.__s15RecoveredGets, delCalls: w.__s15DeleteCalls };
  });
  expect(state.aborted).toBe(true);
  expect(state.recovered).toBeGreaterThanOrEqual(1);
  expect(state.delCalls).toBe(1);

  // Clean up seeded task from local store
  await browser.evaluate(() => { (window as unknown as Record<string, unknown>).__s15FailDelete = false; });
  await browser.locator('[data-warm-del]').first().click();
  await expect(browser.locator('#warm-tasks')).toContainText('No warming tasks yet');
});

test('Account Warming studio: gates blank built-ins while catalog is loading/503 and reflects server display capability', async ({ app, browser }) => {
  await browser.setViewport({ width: 1440, height: 900 });
  await app.open('/');
  await browser.locator('#token').fill('fixture-token-' + 't'.repeat(32));
  await browser.locator('#login-form button').click();
  await expect(browser.locator('#app')).toBeVisible();

  await browser.evaluate(() => {
    const w = window as unknown as Record<string, unknown>;
    const origFetch = window.fetch.bind(window);
    w.__s7Mode = 'pending';
    w.__s7Posts = [];
    let releasePending: (() => void) | null = null;
    w.__s7ReleasePending = () => { if (releasePending) { const fn = releasePending; releasePending = null; fn(); } };
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input instanceof Request ? input.url : String(input);
      const method = (init?.method || 'GET').toUpperCase();
      if (url.endsWith('/api/v1/warming/tasks') && method === 'GET') {
        if (w.__s7Mode === 'pending') {
          await new Promise<void>((resolve, reject) => {
            releasePending = resolve;
            init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
          });
          return new Response(JSON.stringify({ detail: 'Catalog 503' }), {
            status: 503,
            headers: { 'content-type': 'application/json' },
          });
        }
        if (w.__s7Mode === 'fail503') {
          return new Response(JSON.stringify({ detail: 'Catalog 503' }), {
            status: 503,
            headers: { 'content-type': 'application/json' },
          });
        }
        const realRes = await origFetch(input, init);
        const data = await realRes.json();
        if (w.__s7Mode === 'no_display') {
          data.host_supports_headed = false;
          data.tasks = [
            {
              id: 't-api-false',
              name: 'API Headless False',
              profile_names: ['Studio Profile'],
              preset: 'custom',
              custom_urls: ['https://good.example/a'],
              duration_minutes: 3,
              schedule_time: '',
              headless: false,
              status: 'idle',
              last_result: '',
            },
            {
              id: 't-api-null',
              name: 'Legacy Inherit Task',
              profile_names: ['Studio Profile'],
              preset: 'custom',
              custom_urls: ['https://good.example/b'],
              duration_minutes: 3,
              schedule_time: '',
              headless: null,
              status: 'idle',
              last_result: '',
            },
          ];
        }
        return new Response(JSON.stringify(data), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        });
      }
      if (url.includes('/api/v1/warming/tasks') && method === 'POST') {
        const parsedBody = init?.body ? JSON.parse(String(init.body)) : null;
        (w.__s7Posts as unknown[]).push({ url, body: parsedBody });
        return new Response(JSON.stringify({ id: 't-s7', name: 'Override Task', profile_names: ['Studio Profile'], preset: 'gmail_trust', custom_urls: ['https://good.example/ovr'], duration_minutes: 4, headless: parsedBody ? parsedBody.headless : false, status: 'idle', last_result: '' }), {
          status: 201,
          headers: { 'content-type': 'application/json' },
        });
      }
      return origFetch(input, init);
    };
  });

  await browser.locator('#warming-btn').click();
  await expect(browser.locator('[role=dialog]')).toBeVisible();

  // While catalog GET is pending on blank Gmail built-in: no URL fallback rows, Save & Run disabled
  await browser.locator('[data-scenario="gmail_trust"]').click();
  await expect(browser.locator('#warm-steps .warm-step-row')).toHaveCount(0);
  await expect(browser.locator('#warm-steps')).not.toContainText('navigate');
  await expect(browser.locator('#warm-save-btn')).toBeDisabled();
  await expect(browser.locator('#warm-run-btn')).toBeDisabled();

  // Release pending GET as 503: still no URL fallback rows, Save & Run disabled, zero POSTs
  await browser.evaluate(() => {
    const w = window as unknown as Record<string, unknown>;
    w.__s7Mode = 'fail503';
    (w.__s7ReleasePending as () => void)();
  });
  await expect(browser.locator('#warm-retry-btn')).toBeVisible();
  await expect(browser.locator('#warm-steps .warm-step-row')).toHaveCount(0);
  await expect(browser.locator('#warm-steps')).not.toContainText('navigate');
  await expect(browser.locator('#warm-save-btn')).toBeDisabled();
  await expect(browser.locator('#warm-run-btn')).toBeDisabled();

  // Typing an explicit valid URL override while catalog is 503 unblocks Save cleanly without promising Visible
  await browser.locator('#warm-payload').fill('https://good.example/ovr');
  await expect(browser.locator('#warm-steps')).toContainText('good.example/ovr');
  await expect(browser.locator('#warm-save-btn')).toBeEnabled();
  await expect(browser.locator('#warm-headless')).not.toBeDisabled();
  await browser.locator('#warm-save-btn').click();
  await expect(browser.locator('#warm-msg')).toHaveAttribute('class', /ok/);
  await expect(browser.locator('#warm-tasks')).toContainText('Visible requested (new)');
  const postsAfterOvr = await browser.evaluate(() => (window as unknown as { __s7Posts: Array<{ body: { custom_urls: string[]; headless: boolean } }> }).__s7Posts);
  expect(postsAfterOvr.length).toBe(1);
  expect(postsAfterOvr[0].body.custom_urls).toEqual(['https://good.example/ovr']);
  expect(postsAfterOvr[0].body.headless).toBe(false);

  // Clearing override re-blocks blank built-in until Retry succeeds; host_supports_headed=false forces checked & disabled #warm-headless and labels saved headless=false row as Hidden (no display), never Visible
  await browser.locator('#warm-payload').fill('');
  await expect(browser.locator('#warm-save-btn')).toBeDisabled();
  await browser.evaluate(() => { (window as unknown as Record<string, unknown>).__s7Mode = 'no_display'; });
  await browser.locator('#warm-retry-btn').click();
  await expect(browser.locator('#warm-steps')).toContainText('mail.google.com');
  await expect(browser.locator('#warm-steps')).toContainText('tr.zA');
  await expect(browser.locator('#warm-save-btn')).toBeEnabled();
  await expect(browser.locator('#warm-headless')).toBeChecked();
  await expect(browser.locator('#warm-headless')).toBeDisabled();
  const apiFalseRow = browser.locator('.warm-task-row:has-text("API Headless False")');
  await expect(apiFalseRow).toContainText('Hidden (no display)');
  await expect(apiFalseRow).not.toContainText('Visible');
  const legacyNullRow = browser.locator('.warm-task-row:has-text("Legacy Inherit Task")');
  await expect(legacyNullRow).not.toContainText('Hidden');
  await expect(legacyNullRow).not.toContainText('Visible');
});
